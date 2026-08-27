from __future__ import annotations

import json
import os
import re
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from ..config import MANUAL_NODE_KEY_PATH
from ..db import db, get_setting
from ..security import redact, utcnow_iso
from .parser import NormalizedNode, ParseResult, merge_nodes, parse_subscription


def node_key_for(subscription_id: int, fingerprint: str) -> str:
    import hashlib
    import hmac
    secret = get_setting("node_key_secret")
    return hmac.new(secret.encode(), f"{subscription_id}:{fingerprint}".encode(), hashlib.sha256).hexdigest()


def _key() -> bytes:
    MANUAL_NODE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not MANUAL_NODE_KEY_PATH.exists():
        value = os.urandom(32)
        fd = os.open(MANUAL_NODE_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
        return value
    value = MANUAL_NODE_KEY_PATH.read_bytes()
    if len(value) != 32:
        raise RuntimeError("手动节点加密密钥无效")
    return value


def _encode(node: NormalizedNode) -> tuple[bytes, bytes]:
    raw = json.dumps({"uri": node.uri, "proxy": node.proxy, "fingerprint": node.fingerprint}, ensure_ascii=False).encode()
    nonce = os.urandom(12)
    return AESGCM(_key()).encrypt(nonce, raw, b"sub-manager-manual-node-v1"), nonce


def _decode(ciphertext: bytes, nonce: bytes, row: dict[str, Any]) -> NormalizedNode:
    raw = AESGCM(_key()).decrypt(nonce, ciphertext, b"sub-manager-manual-node-v1")
    value = json.loads(raw)
    fingerprint = str(value.get("fingerprint") or row.get("fingerprint") or row["node_key"])
    return NormalizedNode(str(row["name"]), fingerprint, "手动节点",
                          uri=value.get("uri"), proxy=value.get("proxy"))


def parse_manual_content(content: str) -> list[NormalizedNode]:
    try:
        pattern = re.compile(get_setting("pseudo_node_filter"), re.I)
        parsed = parse_subscription(content.encode(), "手动节点", pattern)
        nodes, _ = merge_nodes([parsed])
        if not nodes:
            raise ValueError("没有发现有效节点")
        return nodes
    except Exception:
        raise HTTPException(400, "手动节点内容无法识别，请检查 URI 或 proxies YAML") from None


def import_manual_nodes(subscription_id: int, content: str) -> list[dict[str, Any]]:
    nodes = parse_manual_content(content)
    now = utcnow_iso()
    with db() as conn:
        if not conn.execute("SELECT 1 FROM subscriptions WHERE id=?", (subscription_id,)).fetchone():
            raise HTTPException(404, "订阅组不存在")
        start = int(conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM manual_nodes WHERE subscription_id=?",
                                 (subscription_id,)).fetchone()[0])
        conn.execute("BEGIN IMMEDIATE")
        for offset, node in enumerate(nodes):
            key = node_key_for(subscription_id, node.fingerprint)
            cipher, nonce = _encode(node)
            conn.execute(
                """INSERT INTO manual_nodes(subscription_id,name,protocol,node_key,fingerprint,ciphertext,nonce,
                   enabled,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(subscription_id,node_key) DO UPDATE SET name=excluded.name,protocol=excluded.protocol,
                   ciphertext=excluded.ciphertext,nonce=excluded.nonce,enabled=1,updated_at=excluded.updated_at""",
                (subscription_id, redact(node.original_name, 160), node.protocol, key, "",
                 cipher, nonce, 1, start + offset, now, now),
            )
        conn.execute("""UPDATE subscriptions SET config_revision=config_revision+1,
                     cache_state=CASE WHEN cache_state='empty' THEN 'empty' ELSE 'stale' END,
                     updated_at=? WHERE id=?""", (now, subscription_id))
        conn.commit()
    return list_manual_nodes(subscription_id)


def list_manual_nodes(subscription_id: int) -> list[dict[str, Any]]:
    with db() as conn:
        rows = [dict(x) for x in conn.execute(
            """SELECT m.id,m.name,m.protocol,m.node_key,m.enabled,m.sort_order,m.created_at,m.updated_at,
               (SELECT s.final_name FROM node_snapshots s
                WHERE s.subscription_id=m.subscription_id AND s.node_key=m.node_key
                ORDER BY s.position LIMIT 1) AS final_name
               FROM manual_nodes m WHERE m.subscription_id=? ORDER BY m.sort_order,m.id""",
            (subscription_id,))]
    for row in rows:
        row["enabled"] = bool(row["enabled"])
    return rows


def load_manual_nodes(subscription_id: int, enabled_only: bool = True) -> list[NormalizedNode]:
    where = " AND enabled=1" if enabled_only else ""
    with db() as conn:
        rows = [dict(x) for x in conn.execute(
            f"SELECT * FROM manual_nodes WHERE subscription_id=?{where} ORDER BY sort_order,id", (subscription_id,))]
    result: list[NormalizedNode] = []
    for row in rows:
        try:
            node = _decode(bytes(row["ciphertext"]), bytes(row["nonce"]), row)
            node.node_key = str(row["node_key"])
            result.append(node)
        except Exception:
            continue
    return result


def update_manual_node(subscription_id: int, manual_id: int, *, enabled: bool | None = None,
                       sort_order: int | None = None, content: str | None = None) -> None:
    with db() as conn:
        row = conn.execute("SELECT * FROM manual_nodes WHERE id=? AND subscription_id=?", (manual_id, subscription_id)).fetchone()
    if not row:
        raise HTTPException(404, "手动节点不存在")
    old_key = str(row["node_key"])
    values: dict[str, Any] = {"enabled": int(enabled) if enabled is not None else int(row["enabled"]),
                              "sort_order": sort_order if sort_order is not None else int(row["sort_order"]),
                              "name": row["name"], "protocol": row["protocol"], "node_key": old_key,
                              "fingerprint": "", "ciphertext": row["ciphertext"], "nonce": row["nonce"]}
    new_key = old_key
    if content is not None:
        nodes = parse_manual_content(content)
        if len(nodes) != 1:
            raise HTTPException(400, "替换单个节点时只能提交一条配置")
        node = nodes[0]
        new_key = node_key_for(subscription_id, node.fingerprint)
        values.update(name=redact(node.original_name, 160), protocol=node.protocol,
                      node_key=new_key, fingerprint="")
        values["ciphertext"], values["nonce"] = _encode(node)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if new_key != old_key and conn.execute(
            "SELECT 1 FROM manual_nodes WHERE subscription_id=? AND node_key=? AND id<>?",
            (subscription_id, new_key, manual_id)).fetchone():
            raise HTTPException(409, "替换后的节点已存在")
        # Move the opaque preference identity so an existing alias, confirmation
        # state, category membership and health history survive content edits.
        if new_key != old_key:
            pref = conn.execute("SELECT * FROM node_preferences WHERE subscription_id=? AND node_key=?",
                                (subscription_id, old_key)).fetchone()
            if pref and not conn.execute("SELECT 1 FROM node_preferences WHERE subscription_id=? AND node_key=?",
                                         (subscription_id, new_key)).fetchone():
                conn.execute("UPDATE node_preferences SET node_key=? WHERE subscription_id=? AND node_key=?",
                             (new_key, subscription_id, old_key))
            conn.execute("UPDATE proxy_category_nodes SET node_key=? WHERE node_key=? AND category_id IN "
                         "(SELECT id FROM proxy_categories WHERE subscription_id=?)", (new_key, old_key, subscription_id))
            conn.execute("UPDATE node_health_latest SET node_key=? WHERE subscription_id=? AND node_key=?",
                         (new_key, subscription_id, old_key))
        conn.execute("""UPDATE manual_nodes SET name=?,protocol=?,node_key=?,fingerprint=?,ciphertext=?,nonce=?,
                     enabled=?,sort_order=?,updated_at=? WHERE id=? AND subscription_id=?""",
                     (values["name"], values["protocol"], values["node_key"], values["fingerprint"],
                      values["ciphertext"], values["nonce"], values["enabled"], values["sort_order"],
                      utcnow_iso(), manual_id, subscription_id))
        conn.execute("""UPDATE subscriptions SET config_revision=config_revision+1,
                     cache_state=CASE WHEN cache_state='empty' THEN 'empty' ELSE 'stale' END,
                     updated_at=? WHERE id=?""", (utcnow_iso(), subscription_id))
        conn.commit()


def delete_manual_node(subscription_id: int, manual_id: int) -> None:
    with db() as conn:
        row = conn.execute("SELECT node_key FROM manual_nodes WHERE id=? AND subscription_id=?",
                           (manual_id, subscription_id)).fetchone()
        cur = conn.execute("DELETE FROM manual_nodes WHERE id=? AND subscription_id=?", (manual_id, subscription_id))
        if cur.rowcount:
            key = str(row[0]) if row else ""
            conn.execute("DELETE FROM proxy_category_nodes WHERE node_key=? AND category_id IN "
                         "(SELECT id FROM proxy_categories WHERE subscription_id=?)", (key, subscription_id))
            conn.execute("DELETE FROM node_health_latest WHERE subscription_id=? AND node_key=?", (subscription_id, key))
            conn.execute("""UPDATE subscriptions SET config_revision=config_revision+1,
                         cache_state=CASE WHEN cache_state='empty' THEN 'empty' ELSE 'stale' END,
                         updated_at=? WHERE id=?""", (utcnow_iso(), subscription_id))
        conn.commit()
    if not cur.rowcount:
        raise HTTPException(404, "手动节点不存在")
