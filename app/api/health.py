from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..config import HEALTH_CONNECTIVITY_URL, HEALTH_GOOGLE_URL
from ..db import db, get_setting
from ..schemas import HealthTestRequest
from ..services.health import health_runtime, start_health_test
from .deps import require_auth


router = APIRouter(prefix="/api/node-health", dependencies=[Depends(require_auth)], tags=["node-health"])


def _bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _latest_rows(where: str = "", values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db() as conn:
        rows = [dict(x) for x in conn.execute(
            f"""SELECT n.subscription_id,n.position,n.node_key,n.final_name,n.source_name,n.protocol,
                s.name AS subscription_name,l.status,l.connectivity_ok,l.connectivity_latency_ms,
                l.google_ok,l.google_latency_ms,l.consecutive_failures,l.error_code,l.tested_at
                FROM node_snapshots n JOIN subscriptions s ON s.id=n.subscription_id
                LEFT JOIN node_health_latest l ON l.subscription_id=n.subscription_id AND l.node_key=n.node_key
                {where} ORDER BY CASE WHEN l.status='unavailable' THEN 0 WHEN l.status IS NULL THEN 1 ELSE 2 END,
                s.name,n.position""", values)]
    for row in rows:
        row["connectivity_ok"], row["google_ok"] = _bool(row["connectivity_ok"]), _bool(row["google_ok"])
        row["status"] = row["status"] or "untested"
    return rows


@router.get("/overview")
def overview() -> dict[str, Any]:
    rows = _latest_rows()
    counts = {name: sum(1 for x in rows if x["status"] == name) for name in (
        "healthy", "google_blocked", "connectivity_target_failed", "unavailable", "untested")}
    return {"total": len(rows), "available": sum(counts[x] for x in ("healthy", "google_blocked", "connectivity_target_failed")),
            "google_available": sum(1 for x in rows if x.get("google_ok") is True), "counts": counts,
            "targets": {"connectivity": HEALTH_CONNECTIVITY_URL, "google": HEALTH_GOOGLE_URL}, **health_runtime()}


@router.get("/nodes")
def nodes(subscription_id: int | None = None, status: str | None = None, protocol: str | None = None,
          search: str | None = None, hours: int = 24, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    conditions, values = [], []
    if subscription_id:
        conditions.append("n.subscription_id=?"); values.append(subscription_id)
    if status:
        if status == "untested": conditions.append("l.status IS NULL")
        else: conditions.append("l.status=?"); values.append(status)
    if protocol:
        conditions.append("n.protocol=?"); values.append(protocol)
    if search:
        conditions.append("(n.final_name LIKE ? OR n.source_name LIKE ?)")
        values.extend([f"%{search}%", f"%{search}%"])
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = _latest_rows(where, tuple(values))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 720)))).isoformat()
    keys = [(int(x["subscription_id"]), str(x["node_key"])) for x in rows]
    history: dict[tuple[int, str], list[dict[str, Any]]] = {key: [] for key in keys}
    if keys:
        with db() as conn:
            for item in conn.execute("SELECT subscription_id,node_key,status,tested_at FROM node_health_results WHERE tested_at>=? ORDER BY tested_at", (cutoff,)):
                key = (int(item["subscription_id"]), str(item["node_key"]))
                if key in history: history[key].append({"status": item["status"], "tested_at": item["tested_at"]})
    for row in rows:
        row["history"] = history[(int(row["subscription_id"]), str(row["node_key"]))]
    total, page_size, page = len(rows), min(max(page_size, 1), 100), max(page, 1)
    return {"items": rows[(page-1)*page_size:page*page_size], "total": total, "page": page, "page_size": page_size}


@router.get("/nodes/{subscription_id}/{node_key}/history")
def node_history(subscription_id: int, node_key: str, hours: int = 720) -> dict[str, Any]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 720)))).isoformat()
    with db() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT status,connectivity_ok,connectivity_latency_ms,google_ok,google_latency_ms,error_code,tested_at "
            "FROM node_health_results WHERE subscription_id=? AND node_key=? AND tested_at>=? ORDER BY tested_at DESC",
            (subscription_id, node_key, cutoff))]
    for row in rows:
        row["connectivity_ok"], row["google_ok"] = _bool(row["connectivity_ok"]), _bool(row["google_ok"])
    return {"subscription_id": subscription_id, "node_key": node_key, "items": rows}


@router.get("/runs")
def runs(page: int = 1, page_size: int = 30) -> dict[str, Any]:
    page, page_size = max(page, 1), min(max(page_size, 1), 100)
    with db() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM health_check_runs").fetchone()[0])
        rows = [dict(x) for x in conn.execute("SELECT * FROM health_check_runs ORDER BY id DESC LIMIT ? OFFSET ?",
                                              (page_size, (page-1)*page_size))]
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/runs/{run_id}")
def run(run_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM health_check_runs WHERE id=?", (run_id,)).fetchone()
    if not row: raise HTTPException(404, "测活任务不存在")
    return dict(row)


@router.post("/tests")
async def test(payload: HealthTestRequest) -> dict[str, Any]:
    return await start_health_test(payload.subscription_id, payload.node_key, "manual")
