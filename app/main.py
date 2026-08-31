from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import auth, categories, health as health_api, operations, settings, subscriptions
from .api.health import _latest_rows
from .config import APP_VERSION, CLIENT_TYPES, RULE_PRESET, STATIC_DIR, SUBCONVERTER_URL
from .db import db, get_setting, init_db
from .repository import load_subscription, parse_iso
from .security import ip_allowed, redact, safe_filename, utcnow_iso
from .services.cache import read_output
from .services.fetcher import fetcher
from .services.refresh import refresh_subscription, runtime_status, scheduler_loop
from .services.renderer import get_internal_source
from .services.health import health_runtime, health_scheduler_loop, stop_health_task


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    await fetcher.start()
    scheduler = asyncio.create_task(scheduler_loop(), name="subscription-scheduler")
    health_scheduler = asyncio.create_task(health_scheduler_loop(), name="health-scheduler")
    try:
        yield
    finally:
        scheduler.cancel()
        health_scheduler.cancel()
        health_task = stop_health_task()
        await asyncio.gather(*[x for x in (scheduler, health_scheduler, health_task) if x], return_exceptions=True)
        await fetcher.close()


app = FastAPI(title="Sub Manager", version=APP_VERSION, lifespan=lifespan)
app.include_router(auth.router)
app.include_router(auth.legacy_router)
app.include_router(settings.router)
app.include_router(subscriptions.router)
app.include_router(operations.router)
app.include_router(health_api.router)
app.include_router(categories.router)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default response includes the rejected input verbatim. Invalid
    # subscription URLs can still contain provider credentials, so expose only
    # the field location and a redacted validation reason.
    errors = [{
        "type": item.get("type", "validation_error"),
        "loc": item.get("loc", ()),
        "msg": redact(item.get("msg", "请求数据格式不正确")),
    } for item in exc.errors()]
    return JSONResponse({"detail": "请求数据格式不正确", "errors": errors}, status_code=422)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies:
        origin = request.headers.get("origin")
        if origin:
            origin_host = urlsplit(origin).netloc.lower()
            request_host = request.headers.get("host", "").lower()
            configured = get_setting("public_base_url", "")
            configured_host = urlsplit(configured).netloc.lower() if configured else ""
            if origin_host not in {request_host, configured_host}:
                return Response("请求来源校验失败", status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.get("/internal/source/{token}/{index}.{extension}", include_in_schema=False)
async def internal_source(token: str, index: int, extension: str, request: Request) -> Response:
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(404)
    source = await get_internal_source(token, index)
    if source is None or source.extension != extension:
        raise HTTPException(404)
    media = "application/yaml" if extension in {"yaml", "yml"} else "text/plain"
    return Response(source.body, media_type=media, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def health() -> dict[str, object]:
    converter = False
    version = None
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{SUBCONVERTER_URL}/version")
            converter = response.status_code == 200
            version = response.text.strip()[:100] if converter else None
    except httpx.HTTPError:
        pass
    with db() as conn:
        database = conn.execute("SELECT 1").fetchone() is not None
        rows = [dict(row) for row in conn.execute("SELECT enabled,last_success_at,last_refresh_at,interval_minutes,cache_state FROM subscriptions")]
    now = datetime.now(timezone.utc)
    due_groups = 0
    for row in rows:
        base = parse_iso(row.get("last_success_at") or row.get("last_refresh_at"))
        if row["enabled"] and (row.get("cache_state") == "stale" or not base or now >= base + timedelta(minutes=int(row["interval_minutes"]))):
            due_groups += 1
    runtime = runtime_status()
    return {"ok": database, "database": database, "subconverter": converter,
            "subconverter_version": version, "app_version": APP_VERSION,
            "scheduler_enabled": get_setting("scheduler_enabled", "1") == "1", "due_groups": due_groups, **runtime, **health_runtime()}


@app.get("/s/{token}/{slug}")
async def public_subscription(token: str, slug: str, request: Request) -> Response:
    with db() as conn:
        row = conn.execute("SELECT id FROM subscriptions WHERE token=? AND enabled=1", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "订阅不存在")
    group = load_subscription(int(row["id"]))
    whitelist = str(group.get("ip_whitelist") or "")
    if whitelist.strip():
        forwarded = request.headers.get("x-forwarded-for", "")
        client_ip = forwarded.split(",")[0].strip() if forwarded.strip() else (
            request.client.host if request.client else ""
        )
        if not ip_allowed(whitelist, client_ip):
            # 404 与“订阅不存在”相同，不向外泄漏白名单存在性
            raise HTTPException(404, "订阅不存在")
    output = next((x for x in group["outputs"] if x["slug"] == slug and x["enabled"]), None)
    if not output:
        raise HTTPException(404, "输出不存在")
    cached = read_output(token, slug)
    expired = True
    if cached:
        _, meta = cached
        updated_at = parse_iso(meta.get("updated_at"))
        revision_matches = int(meta.get("config_revision", 0)) == int(group.get("config_revision", 1))
        expired = not revision_matches or not updated_at or datetime.now(timezone.utc) >= updated_at + timedelta(minutes=int(group["interval_minutes"]))
    refresh_result: dict[str, object] | None = None
    if expired:
        refresh_result = await refresh_subscription(int(group["id"]), "public")
        refreshed = read_output(token, slug)
        if refreshed:
            cached = refreshed
    stale_allowed = get_setting("stale_cache_fallback", "1") == "1"
    if not cached or (refresh_result and refresh_result.get("status") in {"stale", "error"} and not stale_allowed):
        raise HTTPException(502, "上游更新失败且没有可用缓存")
    body, meta = cached
    status = str(refresh_result.get("status")) if refresh_result else str(group.get("last_refresh_status") or "ok")
    stale = status in {"stale", "error"} or int(meta.get("config_revision", 0)) != int(group.get("config_revision", 1))
    filename = output["name"] + "." + str(CLIENT_TYPES.get(output["client_type"], {}).get("ext", "txt"))
    download = request.query_params.get("download") == "1"
    headers = {
        "Cache-Control": "no-store",
        "Profile-Update-Interval": str(max(1, (int(output["update_interval_minutes"]) + 59) // 60)),
        "Content-Disposition": f"{'attachment' if download else 'inline'}; filename=\"{safe_filename(filename)}\"; filename*=UTF-8''{quote(filename)}",
        "Profile-Title": str(output["name"]),
        "X-SubManager-Updated-At": str(meta.get("updated_at", "")),
        "X-SubManager-Rule": str(RULE_PRESET["id"]),
        "X-SubManager-Cache": "stale" if stale else "fresh",
    }
    media = str(meta.get("content_type", "text/plain")) if download else "text/plain; charset=utf-8"
    return Response(body, media_type=media, headers=headers)


@app.get("/api/public/health/{token}", include_in_schema=False)
def public_node_health(token: str, request: Request) -> dict[str, object]:
    """公开节点状态页数据：只暴露节点名、状态与延迟，不含地址/来源等敏感信息。"""
    with db() as conn:
        row = conn.execute("SELECT id,name,ip_whitelist FROM subscriptions WHERE token=? AND enabled=1", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "订阅不存在")
    whitelist = str(row["ip_whitelist"] or "")
    if whitelist.strip():
        forwarded = request.headers.get("x-forwarded-for", "")
        client_ip = forwarded.split(",")[0].strip() if forwarded.strip() else (
            request.client.host if request.client else ""
        )
        if not ip_allowed(whitelist, client_ip):
            # 404 与“订阅不存在”相同，不向外泄漏白名单存在性
            raise HTTPException(404, "订阅不存在")
    sub_id = int(row["id"])
    rows = _latest_rows("WHERE n.subscription_id=?", (sub_id,))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    history: dict[str, list[dict[str, object]]] = {}
    with db() as conn:
        for item in conn.execute(
                "SELECT node_key,status,connectivity_latency_ms,tested_at FROM node_health_results "
                "WHERE subscription_id=? AND tested_at>=? ORDER BY tested_at", (sub_id, cutoff)):
            history.setdefault(str(item["node_key"]), []).append(
                {"status": item["status"], "connectivity_latency_ms": item["connectivity_latency_ms"],
                 "tested_at": item["tested_at"]})
    nodes = [{
        "node_key": r["node_key"], "final_name": r["final_name"], "protocol": r["protocol"],
        "status": r["status"], "connectivity_latency_ms": r["connectivity_latency_ms"],
        "google_ok": r["google_ok"], "google_latency_ms": r["google_latency_ms"],
        "tested_at": r["tested_at"], "history": history.get(str(r["node_key"]), []),
    } for r in rows]
    counts = {name: sum(1 for x in nodes if x["status"] == name) for name in (
        "healthy", "google_blocked", "connectivity_target_failed", "unavailable", "untested")}
    return {"group_name": str(row["name"]), "total": len(nodes), "counts": counts,
            "generated_at": utcnow_iso(), "nodes": nodes}


BRAND_ASSETS = {
    "brand-logo.png", "brand-logo-white.png", "favicon.ico", "favicon-32.png",
    "apple-touch-icon.png", "icon-192.png", "icon-512.png", "site.webmanifest",
}


@app.get("/{asset_name}", include_in_schema=False)
def brand_asset(asset_name: str) -> FileResponse:
    if asset_name not in BRAND_ASSETS:
        raise HTTPException(404)
    return FileResponse(STATIC_DIR / asset_name, headers={"Cache-Control": "public, max-age=86400"})


if (STATIC_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
