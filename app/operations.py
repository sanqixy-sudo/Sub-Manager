from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from typing import Any

from fastapi import HTTPException

from .db import db, get_setting
from .security import redact, utcnow_iso
from .services.cache import read_output
from .services.parser import NormalizedNode
from .services.renamer import apply_alias, comparison_name, ensure_unique_names


def _node_key(subscription_id: int, fingerprint: str) -> str:
    secret = get_setting("node_key_secret")
    return hmac.new(secret.encode(), f"{subscription_id}:{fingerprint}".encode(), hashlib.sha256).hexdigest()


def prepare_node_state(subscription_id: int, nodes: list[NormalizedNode]) -> list[dict[str, Any]]:
    with db() as conn:
        initialized = bool(conn.execute(
            "SELECT node_tracking_initialized FROM subscriptions WHERE id=?", (subscription_id,)
        ).fetchone()[0])
        existing = {str(row["node_key"]): dict(row) for row in conn.execute(
            "SELECT * FROM node_preferences WHERE subscription_id=?", (subscription_id,)
        )}
    prepared: list[dict[str, Any]] = []
    for node in nodes:
        key = _node_key(subscription_id, node.fingerprint)
        current = redact(comparison_name(node.original_name), 160)
        old = existing.get(key)
        if not node.rename_managed:
            baseline = current
            status = "confirmed"
            previous = None
            alias = old.get("alias") if old else None
            first_seen = str(old["first_seen_at"]) if old else utcnow_iso()
        elif old:
            baseline = str(old["baseline_name"])
            status = str(old["status"])
            previous = old.get("previous_name")
            if status == "confirmed" and current != baseline:
                status, previous = "pending_changed", baseline
            alias = old.get("alias")
            first_seen = str(old["first_seen_at"])
        else:
            baseline = current
            status = "pending_new" if initialized else "confirmed"
            previous = None
            alias = None
            first_seen = utcnow_iso()
        node.node_key = key
        node.confirmation_status = status
        node.previous_name = previous
        apply_alias(node, str(alias) if alias else None, status != "confirmed")
        prepared.append({
            "node_key": key, "alias": alias, "baseline_name": baseline, "current_name": current,
            "status": status, "previous_name": previous, "first_seen_at": first_seen,
        })
    ensure_unique_names(nodes)
    return prepared


def store_node_snapshot(subscription_id: int, nodes: list[NormalizedNode],
                        preferences: list[dict[str, Any]] | None = None) -> None:
    now = utcnow_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM node_snapshots WHERE subscription_id=?", (subscription_id,))
        if preferences is not None:
            conn.executemany(
                """INSERT INTO node_preferences(subscription_id,node_key,alias,baseline_name,current_name,status,
                   previous_name,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(subscription_id,node_key) DO UPDATE SET alias=excluded.alias,
                   baseline_name=excluded.baseline_name,current_name=excluded.current_name,status=excluded.status,
                   previous_name=excluded.previous_name,last_seen_at=excluded.last_seen_at""",
                [(subscription_id, item["node_key"], item["alias"], item["baseline_name"], item["current_name"],
                  item["status"], item["previous_name"], item["first_seen_at"], now) for item in preferences],
            )
        conn.executemany(
            """INSERT INTO node_snapshots(subscription_id,position,original_name,final_name,source_name,protocol,
               updated_at,node_key,rule_name,alias,confirmation_status,previous_name,traffic,reset)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (subscription_id, index, redact(node.original_name, 160), redact(node.name, 160),
                 redact(node.source_name, 100), node.protocol, now, node.node_key, redact(node.rule_name, 160),
                 redact(node.alias, 80) if node.alias else None, node.confirmation_status,
                 redact(node.previous_name, 160) if node.previous_name else None, node.traffic, node.reset)
                for index, node in enumerate(nodes, 1)
            ],
        )
        conn.execute("UPDATE subscriptions SET node_tracking_initialized=1 WHERE id=?", (subscription_id,))
        conn.commit()


def list_node_snapshots(subscription_id: int) -> dict[str, Any]:
    with db() as conn:
        if not conn.execute("SELECT 1 FROM subscriptions WHERE id=?", (subscription_id,)).fetchone():
            raise HTTPException(404, "订阅组不存在")
        rows = [dict(row) for row in conn.execute(
            """SELECT n.position,n.node_key,n.original_name,n.rule_name,n.alias,n.final_name,n.source_name,n.protocol,
               n.confirmation_status,n.previous_name,n.traffic,n.reset,n.updated_at,
               h.status AS health_status,h.connectivity_latency_ms,h.google_ok,h.google_latency_ms,
               h.consecutive_failures,h.tested_at AS health_tested_at
               FROM node_snapshots n LEFT JOIN node_health_latest h
               ON h.subscription_id=n.subscription_id AND h.node_key=n.node_key
               WHERE n.subscription_id=? ORDER BY n.position""",
            (subscription_id,),
        )]
    for row in rows:
        row["health_status"] = row["health_status"] or "untested"
    return {"subscription_id": subscription_id, "total": len(rows), "nodes": rows}


def update_node_preference(subscription_id: int, node_key: str, alias: str | None, confirm: bool) -> None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM node_preferences WHERE subscription_id=? AND node_key=?", (subscription_id, node_key)
        ).fetchone()
        if not row:
            raise HTTPException(404, "节点不存在或尚未完成首次刷新")
        baseline = str(row["current_name"]) if confirm or alias is not None else str(row["baseline_name"])
        status = "confirmed" if confirm or alias is not None else str(row["status"])
        conn.execute(
            """UPDATE node_preferences SET alias=?,baseline_name=?,status=?,previous_name=NULL,last_seen_at=?
               WHERE subscription_id=? AND node_key=?""",
            (alias, baseline, status, utcnow_iso(), subscription_id, node_key),
        )
        conn.commit()


def confirm_nodes(subscription_id: int, node_keys: list[str] | None = None) -> int:
    with db() as conn:
        values: list[Any] = [utcnow_iso(), subscription_id]
        where = "subscription_id=? AND status!='confirmed'"
        if node_keys is not None:
            if not node_keys:
                return 0
            marks = ",".join("?" for _ in node_keys)
            where += f" AND node_key IN ({marks})"
            values.extend(node_keys)
        cur = conn.execute(
            f"""UPDATE node_preferences SET baseline_name=current_name,status='confirmed',previous_name=NULL,
                last_seen_at=? WHERE {where}""", values
        )
        conn.commit()
        return int(cur.rowcount)


def cleanup_runs(conn: Any) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute("DELETE FROM refresh_runs WHERE finished_at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM refresh_runs WHERE id NOT IN (SELECT id FROM refresh_runs ORDER BY finished_at DESC,id DESC LIMIT 1000)"
    )


def store_refresh_run(
    subscription_id: int,
    trigger: str,
    started_at: str,
    *,
    status: str,
    duration_ms: int,
    upstream_total: int = 0,
    upstream_success: int = 0,
    output_total: int = 0,
    output_success: int = 0,
    node_count: int = 0,
    filtered_count: int = 0,
    error: str | None = None,
) -> None:
    finished_at = utcnow_iso()
    safe_error = redact(error) if error else None
    with db() as conn:
        conn.execute(
            """INSERT INTO refresh_runs(subscription_id,trigger,status,started_at,finished_at,duration_ms,
               upstream_total,upstream_success,output_total,output_success,node_count,filtered_count,error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (subscription_id, trigger, status, started_at, finished_at, duration_ms, upstream_total,
             upstream_success, output_total, output_success, node_count, filtered_count, safe_error),
        )
        cleanup_runs(conn)
        conn.commit()


def list_runs(*, subscription_id: int | None = None, status: str | None = None,
              trigger: str | None = None, started_after: str | None = None,
              started_before: str | None = None, page: int = 1, page_size: int = 30) -> dict[str, Any]:
    conditions: list[str] = []
    values: list[Any] = []
    if subscription_id is not None:
        conditions.append("r.subscription_id=?")
        values.append(subscription_id)
    if status:
        conditions.append("r.status=?")
        values.append(status)
    if trigger:
        conditions.append("r.trigger=?")
        values.append(trigger)
    if started_after:
        conditions.append("r.started_at>=?")
        values.append(started_after)
    if started_before:
        conditions.append("r.started_at<=?")
        values.append(started_before)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    page_size = min(max(page_size, 1), 100)
    page = max(page, 1)
    with db() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM refresh_runs r{where}", values).fetchone()[0])
        rows = [dict(row) for row in conn.execute(
            f"""SELECT r.*,s.name AS subscription_name FROM refresh_runs r
                 JOIN subscriptions s ON s.id=r.subscription_id{where}
                 ORDER BY r.finished_at DESC,r.id DESC LIMIT ? OFFSET ?""",
            (*values, page_size, (page - 1) * page_size),
        )]
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


def output_status(subscription: dict[str, Any], public_urls: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {item["id"]: item for item in public_urls}
    items = []
    for output in subscription["outputs"]:
        cached = read_output(subscription["token"], output["slug"])
        meta = cached[1] if cached else {}
        items.append({
            "id": output["id"], "name": output["name"], "slug": output["slug"],
            "client_type": output["client_type"], "enabled": bool(output["enabled"]),
            "status": "ready" if cached else "empty", "updated_at": meta.get("updated_at"),
            "bytes": int(meta.get("bytes", 0)), "renderer": meta.get("renderer"),
            "skipped_nodes": int(meta.get("skipped_nodes", 0)),
            "node_count": int(meta.get("node_count", 0)), "url": by_id.get(output["id"], {}).get("url"),
        })
    return {"subscription_id": subscription["id"], "outputs": items}
