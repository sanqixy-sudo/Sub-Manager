from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from ..config import APP_VERSION, CLIENT_TYPES, DATA_DIR, RULE_PRESET
from ..db import db, get_setting, set_settings
from ..repository import normalize_public_base_url
from ..schemas import SettingsUpdate
from ..security import hash_password, verify_password
from .deps import require_auth


router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], tags=["settings"])


def settings_payload() -> dict[str, object]:
    username = get_setting("admin_username", "admin")
    default_valid, _ = verify_password("admin", get_setting("admin_password_hash"))
    return {
        "site_name": get_setting("site_name", "Sub Manager"),
        "public_base_url": get_setting("public_base_url", ""), "admin_username": username,
        "default_refresh_interval_minutes": int(get_setting("default_refresh_interval_minutes", "30")),
        "default_output_interval_minutes": int(get_setting("default_output_interval_minutes", "60")),
        "default_client_type": get_setting("default_client_type", "mihomo"),
        "upstream_timeout_seconds": int(get_setting("upstream_timeout_seconds", "30")),
        "converter_timeout_seconds": int(get_setting("converter_timeout_seconds", "90")),
        "pseudo_node_filter": get_setting("pseudo_node_filter"),
        "scheduler_enabled": get_setting("scheduler_enabled", "1") == "1",
        "stale_cache_fallback": get_setting("stale_cache_fallback", "1") == "1",
        "skip_failed_upstreams": get_setting("skip_failed_upstreams", "1") == "1",
        "upstream_user_agent": get_setting("upstream_user_agent", "ClashVergeRev/2.4 SubManager/3.0"),
        "scheduler_concurrency": int(get_setting("scheduler_concurrency", "3")),
        "health_check_enabled": get_setting("health_check_enabled", "1") == "1",
        "health_check_interval_hours": int(get_setting("health_check_interval_hours", "6")),
        "health_check_concurrency": int(get_setting("health_check_concurrency", "5")),
        "health_check_timeout_seconds": int(get_setting("health_check_timeout_seconds", "8")),
        "health_notify_enabled": get_setting("health_notify_enabled", "0") == "1",
        "health_notify_webhook": get_setting("health_notify_webhook"),
        "health_notify_threshold": int(get_setting("health_notify_threshold", "3")),
        "default_credentials": username == "admin" and default_valid, "listen_port": 7777, "data_dir": str(DATA_DIR),
    }


@router.get("/meta")
def meta() -> dict[str, object]:
    return {"version": APP_VERSION, "rule": RULE_PRESET, "site_name": get_setting("site_name", "Sub Manager"),
            "public_base_url": get_setting("public_base_url", "") or None}


@router.get("/settings")
def read_settings() -> dict[str, object]:
    return settings_payload()


@router.put("/settings")
def update_settings(payload: SettingsUpdate) -> dict[str, object]:
    if payload.default_client_type not in CLIENT_TYPES:
        raise HTTPException(400, "默认客户端类型不支持")
    try:
        re.compile(payload.pseudo_node_filter)
    except re.error as exc:
        raise HTTPException(400, f"伪节点过滤正则无效: {exc}") from exc
    if payload.health_notify_webhook.strip() and not re.match(r"^https?://", payload.health_notify_webhook.strip()):
        raise HTTPException(400, "Webhook 地址必须以 http:// 或 https:// 开头")
    current_user = get_setting("admin_username", "admin")
    identity_changed = payload.admin_username.strip() != current_user or bool(payload.new_password)
    if identity_changed:
        valid, _ = verify_password(payload.current_password or "", get_setting("admin_password_hash"))
        if not valid:
            raise HTTPException(400, "修改管理员账号或密码时，需要填写当前密码")
    values = {
        "site_name": payload.site_name.strip(), "public_base_url": normalize_public_base_url(payload.public_base_url),
        "admin_username": payload.admin_username.strip(),
        "default_refresh_interval_minutes": str(payload.default_refresh_interval_minutes),
        "default_output_interval_minutes": str(payload.default_output_interval_minutes),
        "default_client_type": payload.default_client_type, "upstream_timeout_seconds": str(payload.upstream_timeout_seconds),
        "converter_timeout_seconds": str(payload.converter_timeout_seconds), "pseudo_node_filter": payload.pseudo_node_filter,
        "scheduler_enabled": "1" if payload.scheduler_enabled else "0",
        "stale_cache_fallback": "1" if payload.stale_cache_fallback else "0",
        "skip_failed_upstreams": "1" if payload.skip_failed_upstreams else "0",
        "upstream_user_agent": payload.upstream_user_agent.strip() or "ClashVergeRev/2.4 SubManager/3.0",
        "scheduler_concurrency": str(payload.scheduler_concurrency),
        "health_check_enabled": "1" if payload.health_check_enabled else "0",
        "health_check_interval_hours": str(payload.health_check_interval_hours),
        "health_check_concurrency": str(payload.health_check_concurrency),
        "health_check_timeout_seconds": str(payload.health_check_timeout_seconds),
        "health_notify_enabled": "1" if payload.health_notify_enabled else "0",
        "health_notify_webhook": payload.health_notify_webhook.strip(),
        "health_notify_threshold": str(payload.health_notify_threshold),
    }
    if payload.new_password:
        values["admin_password_hash"] = hash_password(payload.new_password)
    set_settings(values)
    with db() as conn:
        conn.execute("UPDATE subscriptions SET config_revision=config_revision+1,cache_state=CASE WHEN cache_state='empty' THEN 'empty' ELSE 'stale' END")
        conn.commit()
    if identity_changed:
        with db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.commit()
    return {"ok": True, "reauth_required": identity_changed, "settings": settings_payload()}


@router.get("/client-types")
def client_types() -> dict[str, dict[str, object]]:
    return CLIENT_TYPES
