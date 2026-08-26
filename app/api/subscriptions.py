from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from ..db import db, get_setting
from ..repository import create_subscription, list_subscriptions, load_subscription, update_subscription
from ..schemas import ManualNodeImport, ManualNodeUpdate, NodeConfirmPayload, NodePreferenceUpdate, SubscriptionIn
from ..security import utcnow_iso
from ..services.cache import delete_group, delete_upstream, move_token
from ..services.fetcher import fetcher
from ..operations import confirm_nodes, list_node_snapshots, list_runs, output_status, update_node_preference
from ..services.refresh import refresh_subscription
from ..services.manual_nodes import delete_manual_node, import_manual_nodes, list_manual_nodes, update_manual_node
from .deps import require_auth


router = APIRouter(prefix="/api/subscriptions", dependencies=[Depends(require_auth)], tags=["subscriptions"])


@router.get("")
def list_groups() -> list[dict[str, object]]:
    return list_subscriptions()


@router.get("/{sub_id}")
def group_detail(sub_id: int) -> dict[str, object]:
    data = load_subscription(sub_id)
    data.pop("token", None)
    data["recent_runs"] = list_runs(subscription_id=sub_id, page=1, page_size=5)["items"]
    return data


@router.post("")
def create_group(payload: SubscriptionIn) -> dict[str, object]:
    data = create_subscription(payload)
    data.pop("token", None)
    return data


@router.put("/{sub_id}")
def update_group(sub_id: int, payload: SubscriptionIn) -> dict[str, object]:
    result, removed_urls = update_subscription(sub_id, payload)
    for url in removed_urls:
        delete_upstream(sub_id, url)
    result.pop("token", None)
    return result


@router.delete("/{sub_id}")
def delete_group_api(sub_id: int) -> dict[str, bool]:
    group = load_subscription(sub_id)
    with db() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
        conn.commit()
    delete_group(sub_id, group["token"], [x["slug"] for x in group["outputs"]], [x["url"] for x in group["upstreams"]])
    return {"ok": True}


@router.post("/{sub_id}/refresh")
async def refresh_group(sub_id: int) -> dict[str, object]:
    return await refresh_subscription(sub_id, "manual")


@router.post("/{sub_id}/diagnose")
async def diagnose_group(sub_id: int) -> dict[str, object]:
    group = load_subscription(sub_id)
    results = await fetcher.fetch_group(group)
    clean = [{k: x.get(k) for k in ("name", "ok", "fresh", "cached", "status", "status_code", "content_type", "bytes", "duration_ms", "error", "source_format", "node_count", "filtered_count", "success_at")} for x in results]
    return {"ok": any(x["ok"] for x in results), "results": clean}


def _base_url(request: Request) -> str:
    return get_setting("public_base_url", "") or str(request.base_url).rstrip("/")


@router.get("/{sub_id}/public-urls")
def public_urls(sub_id: int, request: Request) -> dict[str, object]:
    group = load_subscription(sub_id)
    base = _base_url(request)
    return {"group_id": sub_id, "enabled": group["enabled"], "urls": [{
        "id": x["id"], "client_type": x["client_type"], "name": x["name"], "slug": x["slug"],
        "update_interval_minutes": x["update_interval_minutes"], "url": f"{base}/s/{group['token']}/{x['slug']}"
    } for x in group["outputs"] if x["enabled"]]}


@router.get("/{sub_id}/runs")
def group_runs(sub_id: int, page: int = 1, page_size: int = 30) -> dict[str, object]:
    load_subscription(sub_id, include_secrets=False)
    return list_runs(subscription_id=sub_id, page=page, page_size=page_size)


@router.get("/{sub_id}/nodes")
def group_nodes(sub_id: int) -> dict[str, object]:
    return list_node_snapshots(sub_id)


@router.put("/{sub_id}/nodes/{node_key}")
async def change_node_preference(sub_id: int, node_key: str, payload: NodePreferenceUpdate) -> dict[str, object]:
    load_subscription(sub_id, include_secrets=False)
    if len(node_key) != 64 or any(char not in "0123456789abcdef" for char in node_key):
        raise HTTPException(404, "节点不存在")
    update_node_preference(sub_id, node_key, payload.alias, payload.confirm)
    result = await refresh_subscription(sub_id, "manual")
    return {"ok": result.get("status") in {"ok", "partial"}, "refresh": result,
            "nodes": list_node_snapshots(sub_id)}


@router.post("/{sub_id}/nodes/confirm")
async def confirm_group_nodes(sub_id: int, payload: NodeConfirmPayload) -> dict[str, object]:
    load_subscription(sub_id, include_secrets=False)
    count = confirm_nodes(sub_id, payload.node_keys)
    result = await refresh_subscription(sub_id, "manual") if count else {"status": "ok"}
    return {"ok": result.get("status") in {"ok", "partial"}, "confirmed": count,
            "refresh": result, "nodes": list_node_snapshots(sub_id)}


@router.get("/{sub_id}/outputs/status")
def group_output_status(sub_id: int, request: Request) -> dict[str, object]:
    group = load_subscription(sub_id)
    urls = public_urls(sub_id, request)["urls"]
    return output_status(group, urls)  # type: ignore[arg-type]


@router.get("/{sub_id}/manual-nodes")
def manual_nodes(sub_id: int) -> dict[str, object]:
    load_subscription(sub_id, include_secrets=False)
    items = list_manual_nodes(sub_id)
    return {"subscription_id": sub_id, "total": len(items), "nodes": items}


@router.post("/{sub_id}/manual-nodes/import")
def import_nodes(sub_id: int, payload: ManualNodeImport) -> dict[str, object]:
    items = import_manual_nodes(sub_id, payload.content)
    return {"ok": True, "imported": len(items), "nodes": items}


@router.put("/{sub_id}/manual-nodes/{manual_id}")
def update_manual(sub_id: int, manual_id: int, payload: ManualNodeUpdate) -> dict[str, object]:
    update_manual_node(sub_id, manual_id, enabled=payload.enabled, sort_order=payload.sort_order,
                       content=payload.content)
    return {"ok": True, "nodes": list_manual_nodes(sub_id)}


@router.delete("/{sub_id}/manual-nodes/{manual_id}")
def delete_manual(sub_id: int, manual_id: int) -> dict[str, object]:
    delete_manual_node(sub_id, manual_id)
    return {"ok": True, "nodes": list_manual_nodes(sub_id)}


@router.post("/{sub_id}/rotate-token")
def rotate_token(sub_id: int, request: Request) -> dict[str, object]:
    group = load_subscription(sub_id)
    new_token = secrets.token_hex(20)
    with db() as conn:
        conn.execute("UPDATE subscriptions SET token=?,updated_at=? WHERE id=?", (new_token, utcnow_iso(), sub_id))
        conn.commit()
    move_token(group["token"], new_token, [x["slug"] for x in group["outputs"]])
    return public_urls(sub_id, request)
