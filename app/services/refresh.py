from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..db import db, get_setting
from ..operations import apply_node_order, prepare_node_state, store_node_snapshot, store_refresh_run
from ..repository import load_subscription
from ..security import redact, utcnow_iso
from .cache import read_output
from .fetcher import fetcher
from .parser import merge_nodes, render_internal_sources
from .parser import ParseResult
from .renamer import DEFAULT_TEMPLATE, rename_nodes
from .renderer import register_sources, remove_sources, render_output
from .manual_nodes import load_manual_nodes


_flights: dict[int, asyncio.Task[dict[str, Any]]] = {}
_flight_lock = asyncio.Lock()
_scheduler_last_poll: str | None = None
_scheduler_next_poll: str | None = None


def _has_output_cache(subscription: dict[str, Any]) -> bool:
    return any(read_output(subscription["token"], x["slug"]) for x in subscription["outputs"] if x["enabled"])


def _store_group_result(sub_id: int, *, status: str, error: str | None, node_count: int,
                        filtered_count: int, duration_ms: int, success: bool) -> None:
    now = utcnow_iso()
    with db() as conn:
        conn.execute(
            """UPDATE subscriptions SET last_refresh_at=?,last_refresh_status=?,last_error=?,node_count=?,
               filtered_count=?,last_duration_ms=?,cache_state=?,refresh_count=COALESCE(refresh_count,0)+1,
               last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,updated_at=? WHERE id=?""",
            (now, status, error, node_count, filtered_count, duration_ms,
             "fresh" if status in {"ok", "partial"} and success else ("stale" if status == "stale" else "empty"),
             int(success), now, now, sub_id),
        )
        conn.commit()


def _record_run(sub_id: int, trigger: str, started_at: str, result: dict[str, Any],
                upstream_results: list[dict[str, Any]] | None = None) -> None:
    upstream_results = upstream_results or []
    outputs = result.get("outputs") or []
    store_refresh_run(
        sub_id, trigger, started_at, status=str(result["status"]), duration_ms=int(result.get("duration_ms", 0)),
        upstream_total=len(upstream_results), upstream_success=sum(1 for item in upstream_results if item.get("ok")),
        output_total=len(outputs), output_success=sum(1 for item in outputs if item.get("ok")),
        node_count=int(result.get("node_count", 0)), filtered_count=int(result.get("filtered_count", 0)),
        error="；".join(str(item) for item in result.get("errors", []) if item) or None,
    )


async def _run_refresh(sub_id: int, trigger: str = "manual") -> dict[str, Any]:
    started = time.monotonic()
    started_at = utcnow_iso()
    subscription = load_subscription(sub_id)
    attempt = started_at
    with db() as conn:
        conn.execute("UPDATE subscriptions SET last_attempt_at=?,updated_at=? WHERE id=?", (attempt, attempt, sub_id))
        conn.commit()
    try:
        enabled_remote = [x for x in subscription["upstreams"] if x["enabled"]]
        upstream_results = await fetcher.fetch_group(subscription) if enabled_remote else []
    except Exception as exc:
        error = redact(exc, known_tokens=(subscription["token"],))
        status = "stale" if _has_output_cache(subscription) else "error"
        duration = round((time.monotonic() - started) * 1000)
        _store_group_result(sub_id, status=status, error=error, node_count=0, filtered_count=0, duration_ms=duration, success=False)
        result = {"status": status, "success": 0, "errors": [error], "duration_ms": duration}
        _record_run(sub_id, trigger, started_at, result)
        return result

    enabled_upstreams = [item for item in subscription["upstreams"] if item["enabled"]]
    for upstream, upstream_result in zip(enabled_upstreams, upstream_results):
        parsed = upstream_result.get("parsed")
        if parsed and hasattr(parsed, "nodes"):
            for node in parsed.nodes:
                node.source_id = int(upstream["id"])

    good = [x for x in upstream_results if x["ok"]]
    fresh = [x for x in good if x["fresh"]]
    failed = [x for x in upstream_results if not x["ok"]]
    try:
        manual_nodes = load_manual_nodes(sub_id)
    except Exception:
        # Keeps legacy/unit-test databases readable before the V7 migration is invoked.
        manual_nodes = []
    if failed and get_setting("skip_failed_upstreams", "1") != "1":
        good = []
    if not good and not manual_nodes:
        status = "stale" if _has_output_cache(subscription) else "error"
        errors = [f"{x['name']}: {x.get('error') or '使用旧快照'}" for x in upstream_results if not x.get("fresh")]
        duration = round((time.monotonic() - started) * 1000)
        _store_group_result(sub_id, status=status, error="；".join(errors), node_count=0, filtered_count=0, duration_ms=duration, success=False)
        result = {"status": status, "success": 0, "errors": errors, "duration_ms": duration}
        _record_run(sub_id, trigger, started_at, result, upstream_results)
        return result

    parsed_sources = [x["parsed"] for x in good if x.get("parsed")]
    if manual_nodes:
        parsed_sources.append(ParseResult("manual", manual_nodes))
    nodes, filtered = merge_nodes(parsed_sources)
    if not nodes:
        error = "上游中没有可用真实节点"
        status = "stale" if _has_output_cache(subscription) else "error"
        duration = round((time.monotonic() - started) * 1000)
        _store_group_result(sub_id, status=status, error=error, node_count=0, filtered_count=filtered, duration_ms=duration, success=False)
        result = {"status": status, "success": 0, "errors": [error], "duration_ms": duration,
                  "node_count": 0, "filtered_count": filtered}
        _record_run(sub_id, trigger, started_at, result, upstream_results)
        return result

    rename_nodes(nodes, str(subscription.get("rename_mode", "passthrough")),
                 str(subscription.get("rename_ignore", "")),
                 str(subscription.get("rename_template") or DEFAULT_TEMPLATE),
                 subscription["upstreams"])
    node_preferences = prepare_node_state(sub_id, nodes)
    # Keep the effective order stable across refreshes; unseen nodes are appended.
    apply_node_order(sub_id, nodes)

    items = render_internal_sources(nodes)
    source_token, urls = await register_sources(items)
    try:
        enabled_outputs = [x for x in subscription["outputs"] if x["enabled"]]
        async with httpx.AsyncClient() as client:
            output_results = await asyncio.gather(*(
                render_output(client, subscription, output, urls, upstream_results, len(nodes), filtered, nodes)
                for output in enabled_outputs
            ))
    finally:
        await remove_sources(source_token)
    succeeded = sum(1 for x in output_results if x["ok"])
    output_errors = [f"{x['name']}: {x['error']}" for x in output_results if not x["ok"]]
    upstream_warnings = [f"{x['name']}: {x.get('error') or '使用旧快照'}" for x in upstream_results if not x["fresh"]]
    if succeeded:
        status = "stale" if upstream_results and not fresh else ("partial" if output_errors or upstream_warnings else "ok")
    else:
        status = "stale" if _has_output_cache(subscription) else "error"
    errors = upstream_warnings + output_errors
    duration = round((time.monotonic() - started) * 1000)
    _store_group_result(sub_id, status=status, error="；".join(errors) or None, node_count=len(nodes),
                        filtered_count=filtered, duration_ms=duration, success=bool(succeeded and fresh))
    result = {"status": status, "success": succeeded, "errors": errors, "duration_ms": duration,
              "node_count": len(nodes), "filtered_count": filtered, "outputs": output_results,
              "upstreams": [{k: x.get(k) for k in ("name", "status", "cached", "duration_ms", "node_count", "filtered_count", "error")} for x in upstream_results]}
    if succeeded and status in {"ok", "partial"}:
        store_node_snapshot(sub_id, nodes, node_preferences)
    _record_run(sub_id, trigger, started_at, result, upstream_results)
    return result


async def refresh_subscription(sub_id: int, trigger: str = "manual") -> dict[str, Any]:
    async with _flight_lock:
        task = _flights.get(sub_id)
        if task is None or task.done():
            coroutine = _run_refresh(sub_id) if trigger == "manual" else _run_refresh(sub_id, trigger)
            task = asyncio.create_task(coroutine, name=f"refresh-subscription-{sub_id}")
            _flights[sub_id] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done():
            async with _flight_lock:
                if _flights.get(sub_id) is task:
                    _flights.pop(sub_id, None)


async def scheduler_loop() -> None:
    global _scheduler_last_poll, _scheduler_next_poll
    while True:
        try:
            _scheduler_last_poll = utcnow_iso()
            _scheduler_next_poll = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
            if get_setting("scheduler_enabled", "1") == "1":
                with db() as conn:
                    ids = [int(x["id"]) for x in conn.execute("SELECT id FROM subscriptions WHERE enabled=1")]
                from ..repository import is_due
                due = [sub_id for sub_id in ids if is_due(load_subscription(sub_id, include_secrets=False))]
                semaphore = asyncio.Semaphore(int(get_setting("scheduler_concurrency", "3")))
                async def run_one(sub_id: int) -> None:
                    async with semaphore:
                        await refresh_subscription(sub_id, "scheduler")
                await asyncio.gather(*(run_one(x) for x in due), return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(60)


def runtime_status() -> dict[str, Any]:
    return {
        "last_poll_at": _scheduler_last_poll,
        "next_poll_at": _scheduler_next_poll,
        "active_refreshes": sum(1 for task in _flights.values() if not task.done()),
        "active_group_ids": [sub_id for sub_id, task in _flights.items() if not task.done()],
    }
