from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .db import db
from .schemas import ProxyCategoryIn
from .security import utcnow_iso
from .services.coordination import group_mutation


def list_categories(subscription_id: int) -> list[dict[str, Any]]:
    with db() as conn:
        categories = [dict(x) for x in conn.execute(
            "SELECT * FROM proxy_categories WHERE subscription_id=? ORDER BY sort_order,id", (subscription_id,))]
        for item in categories:
            item["source_ids"] = [int(x[0]) for x in conn.execute(
                "SELECT upstream_id FROM proxy_category_sources WHERE category_id=? ORDER BY upstream_id", (item["id"],))]
            item["node_keys"] = [str(x[0]) for x in conn.execute(
                "SELECT node_key FROM proxy_category_nodes WHERE category_id=? ORDER BY rowid", (item["id"],))]
    return categories


@group_mutation
def save_category(subscription_id: int, payload: ProxyCategoryIn, category_id: int | None = None) -> dict[str, Any]:
    now = utcnow_iso()
    with db() as conn:
        if not conn.execute("SELECT 1 FROM subscriptions WHERE id=?", (subscription_id,)).fetchone():
            raise HTTPException(404, "订阅组不存在")
        valid_sources = {int(x[0]) for x in conn.execute("SELECT id FROM upstreams WHERE subscription_id=?", (subscription_id,))}
        label = (payload.icon.strip() + ' ' if payload.icon else '') + payload.name
        labels = {(str(r['icon']).strip() + ' ' if r['icon'] else '') + r['name'] for r in conn.execute('SELECT id,name,icon FROM proxy_categories WHERE subscription_id=? AND id<>?', (subscription_id, category_id or -1))}
        if label in labels or label in {'PROXY', 'DIRECT', 'REJECT', '♻️ 自动选择'}:
            raise HTTPException(409, '分类显示名称与其他代理组冲突')
        if any(x not in valid_sources for x in payload.source_ids):
            raise HTTPException(400, "分类包含不属于当前组的上游来源")
        valid_keys = {str(x[0]) for x in conn.execute("SELECT node_key FROM node_preferences WHERE subscription_id=?", (subscription_id,))}
        if any(x not in valid_keys for x in payload.node_keys):
            raise HTTPException(400, "分类包含不存在的节点")
        conn.execute("BEGIN IMMEDIATE")
        if category_id is None:
            order = int(conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM proxy_categories WHERE subscription_id=?",
                                     (subscription_id,)).fetchone()[0])
            try:
                cur = conn.execute("INSERT INTO proxy_categories(subscription_id,name,icon,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                   (subscription_id, payload.name, payload.icon, order, now, now))
            except Exception:
                raise HTTPException(409, "分类名称已存在") from None
            category_id = int(cur.lastrowid)
        else:
            cur = conn.execute("UPDATE proxy_categories SET name=?,icon=?,updated_at=? WHERE id=? AND subscription_id=?",
                               (payload.name, payload.icon, now, category_id, subscription_id))
            if not cur.rowcount: raise HTTPException(404, "分类不存在")
            conn.execute("DELETE FROM proxy_category_sources WHERE category_id=?", (category_id,))
            conn.execute("DELETE FROM proxy_category_nodes WHERE category_id=?", (category_id,))
        conn.executemany("INSERT INTO proxy_category_sources(category_id,upstream_id) VALUES(?,?)",
                         [(category_id, x) for x in dict.fromkeys(payload.source_ids)])
        conn.executemany("INSERT INTO proxy_category_nodes(category_id,node_key) VALUES(?,?)",
                         [(category_id, x) for x in dict.fromkeys(payload.node_keys)])
        conn.execute("""UPDATE subscriptions SET config_revision=config_revision+1,
                     cache_state=CASE WHEN cache_state='empty' THEN 'empty' ELSE 'stale' END,
                     updated_at=? WHERE id=?""", (now, subscription_id))
        conn.commit()
    return next(x for x in list_categories(subscription_id) if int(x["id"]) == category_id)


@group_mutation
def reorder_categories(subscription_id: int, category_ids: list[int]) -> list[dict[str, Any]]:
    now = utcnow_iso()
    with db() as conn:
        existing = [int(row[0]) for row in conn.execute(
            "SELECT id FROM proxy_categories WHERE subscription_id=? ORDER BY sort_order,id", (subscription_id,))]
        if len(category_ids) != len(existing) or set(category_ids) != set(existing):
            raise HTTPException(400, "排序列表必须包含当前订阅组的全部分类")
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("UPDATE proxy_categories SET sort_order=?,updated_at=? WHERE id=? AND subscription_id=?",
                         [(index, now, category_id, subscription_id)
                          for index, category_id in enumerate(category_ids)])
        conn.execute("""UPDATE subscriptions SET config_revision=config_revision+1,
                     cache_state=CASE WHEN cache_state='empty' THEN 'empty' ELSE 'stale' END,
                     updated_at=? WHERE id=?""", (now, subscription_id))
        conn.commit()
    return list_categories(subscription_id)


@group_mutation
def delete_category(subscription_id: int, category_id: int) -> None:
    with db() as conn:
        cur = conn.execute("DELETE FROM proxy_categories WHERE id=? AND subscription_id=?", (category_id, subscription_id))
        if cur.rowcount:
            conn.execute("""UPDATE subscriptions SET config_revision=config_revision+1,
                         cache_state=CASE WHEN cache_state='empty' THEN 'empty' ELSE 'stale' END,
                         updated_at=? WHERE id=?""", (utcnow_iso(), subscription_id))
        conn.commit()
    if not cur.rowcount: raise HTTPException(404, "分类不存在")


def category_groups(subscription_id: int, nodes: list[Any]) -> list[dict[str, Any]]:
    result = []
    for category in list_categories(subscription_id):
        sources, keys = set(category["source_ids"]), list(category["node_keys"])
        by_key = {node.node_key: node for node in nodes}
        # Whole-source membership follows the global node order. Explicit
        # assignments not already covered by a source follow their saved order.
        selected = [node for node in nodes if node.source_id is not None and node.source_id in sources]
        selected_keys = {node.node_key for node in selected}
        selected.extend(by_key[key] for key in keys if key in by_key and key not in selected_keys)
        names = list(dict.fromkeys(node.name for node in selected))
        if names:
            label = (str(category["icon"]).strip() + " " if category["icon"] else "") + str(category["name"])
            result.append({"name": label, "type": "select", "proxies": names})
    return result
