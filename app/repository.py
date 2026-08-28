from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from .config import CLIENT_TYPES
from .db import db, get_setting
from .schemas import SubscriptionIn
from .security import masked_url, utcnow_iso


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def validate_subscription(payload: SubscriptionIn) -> None:
    if not any(x.enabled for x in payload.outputs):
        raise HTTPException(400, "至少启用一个输出客户端")
    slugs: set[str] = set()
    for output in payload.outputs:
        if output.client_type not in CLIENT_TYPES:
            raise HTTPException(400, f"不支持客户端: {output.client_type}")
        key = output.slug.lower()
        if key in slugs:
            raise HTTPException(400, f"输出 URL 标识重复: {output.slug}")
        slugs.add(key)
    if payload.rename_mode == "smart":
        from .services.renamer import validate_template
        validate_template(payload.rename_template)
    for upstream in payload.upstreams:
        if upstream.rename_policy == "smart":
            from .services.renamer import validate_template
            validate_template(upstream.rename_template)


def _next_refresh(data: dict[str, Any]) -> str | None:
    base = parse_iso(data.get("last_success_at") or data.get("last_refresh_at"))
    if not base or not data.get("enabled"):
        return None
    return (base + timedelta(minutes=int(data["interval_minutes"]))).isoformat()


def load_subscription(sub_id: int, *, include_secrets: bool = True) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        if not row:
            raise HTTPException(404, "订阅组不存在")
        upstreams = [dict(x) for x in conn.execute(
            "SELECT * FROM upstreams WHERE subscription_id=? ORDER BY sort_order,id", (sub_id,)
        )]
        outputs = [dict(x) for x in conn.execute(
            "SELECT * FROM outputs WHERE subscription_id=? ORDER BY id", (sub_id,)
        )]
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    for upstream in upstreams:
        upstream["enabled"] = bool(upstream["enabled"])
        upstream["used_cache"] = bool(upstream.get("used_cache"))
        upstream["url_masked"] = masked_url(upstream["url"])
        if not include_secrets:
            upstream.pop("url", None)
    for output in outputs:
        output["enabled"] = bool(output["enabled"])
    data["upstreams"] = upstreams
    data["outputs"] = outputs
    from .services.manual_nodes import list_manual_nodes
    data["manual_nodes"] = list_manual_nodes(sub_id)
    if data.get("cache_state") == "empty":
        from .services.cache import read_output
        if any(read_output(data["token"], output["slug"]) for output in outputs if output["enabled"]):
            data["cache_state"] = "stale"
    data["next_refresh_at"] = _next_refresh(data)
    with db() as conn:
        data["pending_node_count"] = int(conn.execute(
            """SELECT COUNT(*) FROM node_preferences p
               JOIN node_snapshots n ON n.subscription_id=p.subscription_id AND n.node_key=p.node_key
               WHERE p.subscription_id=? AND p.status!='confirmed'""", (sub_id,)
        ).fetchone()[0])
    if not include_secrets:
        data.pop("token", None)
    return data


def list_subscriptions() -> list[dict[str, Any]]:
    with db() as conn:
        ids = [int(x["id"]) for x in conn.execute("SELECT id FROM subscriptions ORDER BY id DESC")]
    return [load_subscription(x, include_secrets=False) for x in ids]


def create_subscription(payload: SubscriptionIn) -> dict[str, Any]:
    validate_subscription(payload)
    ts = utcnow_iso()
    token = secrets.token_hex(20)  # Preserve V2 public URL shape and entropy.
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """INSERT INTO subscriptions(name,note,token,interval_minutes,enabled,rename_mode,rename_ignore,
               rename_template,ip_whitelist,created_at,updated_at,config_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
            (payload.name.strip(), payload.note.strip(), token, payload.interval_minutes, int(payload.enabled),
             payload.rename_mode, payload.rename_ignore.strip(), payload.rename_template.strip(),
             payload.ip_whitelist.strip(), ts, ts),
        )
        sub_id = int(cur.lastrowid)
        for idx, upstream in enumerate(payload.upstreams):
            conn.execute(
                """INSERT INTO upstreams(subscription_id,name,url,enabled,sort_order,rename_policy,rename_ignore,
                   rename_template,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (sub_id, upstream.name.strip(), str(upstream.url), int(upstream.enabled), idx,
                 upstream.rename_policy, upstream.rename_ignore.strip(), upstream.rename_template.strip(), ts, ts),
            )
        for output in payload.outputs:
            conn.execute(
                "INSERT INTO outputs(subscription_id,client_type,name,slug,update_interval_minutes,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (sub_id, output.client_type, output.name.strip(), output.slug, output.update_interval_minutes, int(output.enabled), ts, ts),
            )
        conn.commit()
    return load_subscription(sub_id)


def update_subscription(sub_id: int, payload: SubscriptionIn) -> tuple[dict[str, Any], list[str]]:
    validate_subscription(payload)
    ts = utcnow_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM subscriptions WHERE id=?", (sub_id,)).fetchone():
            raise HTTPException(404, "订阅组不存在")
        old_urls = {str(x["url"]) for x in conn.execute("SELECT url FROM upstreams WHERE subscription_id=?", (sub_id,))}
        keep_up_ids = {x.id for x in payload.upstreams if x.id is not None}
        keep_out_ids = {x.id for x in payload.outputs if x.id is not None}
        conn.execute(
            """UPDATE subscriptions SET name=?,note=?,interval_minutes=?,enabled=?,rename_mode=?,rename_ignore=?,
               rename_template=?,ip_whitelist=?,updated_at=?,config_revision=config_revision+1,
               cache_state=CASE WHEN cache_state='empty' THEN 'empty' ELSE 'stale' END WHERE id=?""",
            (payload.name.strip(), payload.note.strip(), payload.interval_minutes, int(payload.enabled),
             payload.rename_mode, payload.rename_ignore.strip(), payload.rename_template.strip(),
             payload.ip_whitelist.strip(), ts, sub_id),
        )
        for idx, upstream in enumerate(payload.upstreams):
            if upstream.id and conn.execute("SELECT 1 FROM upstreams WHERE id=? AND subscription_id=?", (upstream.id, sub_id)).fetchone():
                conn.execute(
                    """UPDATE upstreams SET name=?,url=?,enabled=?,sort_order=?,rename_policy=?,rename_ignore=?,
                       rename_template=?,updated_at=? WHERE id=?""",
                    (upstream.name.strip(), str(upstream.url), int(upstream.enabled), idx, upstream.rename_policy,
                     upstream.rename_ignore.strip(), upstream.rename_template.strip(), ts, upstream.id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO upstreams(subscription_id,name,url,enabled,sort_order,rename_policy,rename_ignore,
                       rename_template,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (sub_id, upstream.name.strip(), str(upstream.url), int(upstream.enabled), idx,
                     upstream.rename_policy, upstream.rename_ignore.strip(), upstream.rename_template.strip(), ts, ts),
                )
                keep_up_ids.add(int(cur.lastrowid))
        if keep_up_ids:
            marks = ",".join("?" for _ in keep_up_ids)
            conn.execute(f"DELETE FROM upstreams WHERE subscription_id=? AND id NOT IN ({marks})", (sub_id, *keep_up_ids))
        # Move current slugs out of the unique namespace first, so two outputs can
        # safely exchange slugs in one edit transaction.
        conn.execute("UPDATE outputs SET slug='__v3_edit_' || id WHERE subscription_id=?", (sub_id,))
        for output in payload.outputs:
            if output.id and conn.execute("SELECT 1 FROM outputs WHERE id=? AND subscription_id=?", (output.id, sub_id)).fetchone():
                conn.execute(
                    "UPDATE outputs SET client_type=?,name=?,slug=?,update_interval_minutes=?,enabled=?,updated_at=? WHERE id=?",
                    (output.client_type, output.name.strip(), output.slug, output.update_interval_minutes, int(output.enabled), ts, output.id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO outputs(subscription_id,client_type,name,slug,update_interval_minutes,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (sub_id, output.client_type, output.name.strip(), output.slug, output.update_interval_minutes, int(output.enabled), ts, ts),
                )
                keep_out_ids.add(int(cur.lastrowid))
        if keep_out_ids:
            marks = ",".join("?" for _ in keep_out_ids)
            conn.execute(f"DELETE FROM outputs WHERE subscription_id=? AND id NOT IN ({marks})", (sub_id, *keep_out_ids))
        conn.commit()
    new_urls = {str(x.url) for x in payload.upstreams}
    return load_subscription(sub_id), list(old_urls - new_urls)


def update_upstream_result(upstream_id: int, result: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """UPDATE upstreams SET last_status=?,last_http_status=?,last_content_type=?,last_bytes=?,
               last_duration_ms=?,last_success_at=COALESCE(?,last_success_at),last_error=?,source_format=?,
               node_count=?,filtered_count=?,used_cache=?,updated_at=? WHERE id=?""",
            (
                result.get("status"), result.get("status_code"), result.get("content_type"), result.get("bytes", 0),
                result.get("duration_ms"), result.get("success_at"), result.get("error"), result.get("source_format"),
                result.get("node_count", 0), result.get("filtered_count", 0), int(bool(result.get("cached"))),
                utcnow_iso(), upstream_id,
            ),
        )
        conn.commit()


def is_due(data: dict[str, Any]) -> bool:
    if data.get("cache_state") == "stale":
        return True
    base = parse_iso(data.get("last_success_at") or data.get("last_refresh_at"))
    return not base or datetime.now(timezone.utc) >= base + timedelta(minutes=int(data["interval_minutes"]))


def normalize_public_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise HTTPException(400, "公开访问地址格式不正确")
    return value
