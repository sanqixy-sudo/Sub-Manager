from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from ..db import db, get_setting
from ..operations import list_runs
from ..repository import list_subscriptions
from ..schemas import InspectPayload
from ..security import redact
from ..services.parser import merge_nodes, parse_subscription
from ..services.renamer import rename_nodes
from .deps import require_auth


router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], tags=["operations"])


@router.get("/overview")
def overview() -> dict[str, object]:
    groups = list_subscriptions()
    upstreams = [upstream for group in groups for upstream in group["upstreams"] if upstream["enabled"]]
    healthy_upstreams = sum(1 for item in upstreams if item.get("last_status") == "ok")
    statuses = {name: sum(1 for group in groups if (group.get("last_refresh_status") or "empty") == name)
                for name in ("ok", "partial", "stale", "error", "empty")}
    recent = list_runs(page=1, page_size=8)["items"]
    with db() as conn:
        health_rows = [dict(row) for row in conn.execute(
            """SELECT h.status,h.google_ok FROM node_health_latest h
               JOIN node_snapshots n ON n.subscription_id=h.subscription_id AND n.node_key=h.node_key"""
        )]
    health = {
        "tested": len(health_rows),
        "available": sum(1 for row in health_rows if row["status"] in {"healthy", "google_blocked", "connectivity_target_failed"}),
        "google_available": sum(1 for row in health_rows if row.get("google_ok") == 1),
        "unavailable": sum(1 for row in health_rows if row["status"] == "unavailable"),
    }
    return {
        "groups_total": len(groups), "groups_enabled": sum(1 for group in groups if group["enabled"]),
        "status_counts": statuses, "upstreams_total": len(upstreams), "upstreams_healthy": healthy_upstreams,
        "outputs_total": sum(len([item for item in group["outputs"] if item["enabled"]]) for group in groups),
        "nodes_total": sum(int(group.get("node_count", 0)) for group in groups),
        "pending_nodes": sum(int(group.get("pending_node_count", 0)) for group in groups),
        "stale_groups": sum(1 for group in groups if group.get("cache_state") == "stale"),
        "attention_groups": sorted(
            [group for group in groups if group.get("last_refresh_status") in {"partial", "stale", "error"}
             or int(group.get("pending_node_count", 0)) > 0],
            key=lambda group: (0 if group.get("last_refresh_status") in {"error", "stale", "partial"} else 1,
                               -int(group.get("pending_node_count", 0))),
        )[:8],
        "node_health": health,
        "recent_runs": recent,
    }


@router.get("/runs")
def runs(subscription_id: int | None = None, status: str | None = None, trigger: str | None = None,
         started_after: str | None = None, started_before: str | None = None,
         page: int = 1, page_size: int = 30) -> dict[str, object]:
    if status and status not in {"ok", "partial", "stale", "error"}:
        raise HTTPException(400, "运行状态筛选无效")
    if trigger and trigger not in {"manual", "scheduler", "public"}:
        raise HTTPException(400, "触发来源筛选无效")
    return list_runs(subscription_id=subscription_id, status=status, trigger=trigger,
                     started_after=started_after, started_before=started_before, page=page, page_size=page_size)


@router.post("/tools/inspect")
def inspect(payload: InspectPayload) -> dict[str, object]:
    try:
        pseudo_filter = re.compile(get_setting("pseudo_node_filter"), re.I)
        parsed = parse_subscription(payload.content.encode("utf-8"), payload.source_name, pseudo_filter)
        nodes, filtered = merge_nodes([parsed])
        rename_nodes(nodes, payload.rename_mode, payload.rename_ignore, payload.rename_template)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, redact(exc)) from None
    return {
        "source_format": parsed.source_format, "node_count": len(nodes), "filtered_count": filtered,
        "nodes": [{"position": index, "original_name": redact(node.original_name, 160), "final_name": redact(node.name, 160),
                   "source_name": redact(node.source_name, 100), "protocol": node.protocol}
                  for index, node in enumerate(nodes, 1)],
    }
