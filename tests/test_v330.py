from __future__ import annotations

import base64
import json
import re

import yaml
from fastapi.testclient import TestClient

import app.db as dbmod
import app.services.cache as cachemod
import app.services.manual_nodes as manualmod
from app.categories import category_groups, reorder_categories, save_category
from app.main import app
from app.schemas import ProxyCategoryIn, SubscriptionIn
from app.repository import create_subscription
from app.services.manual_nodes import import_manual_nodes, load_manual_nodes
from app.services.renderer import build_mihomo_document
from app.services.parser import NormalizedNode, parse_subscription, proxy_to_uri, uri_to_proxy


def configure(tmp_path, monkeypatch) -> None:
    for name, value in {
        "DB_PATH": tmp_path / "submanager.db", "BACKUP_DIR": tmp_path / "backups",
        "CACHE_DIR": tmp_path / "cache", "UPSTREAM_CACHE_DIR": tmp_path / "upstreams",
        "DATA_DIR": tmp_path,
    }.items():
        monkeypatch.setattr(dbmod, name, value)
    monkeypatch.setattr(cachemod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cachemod, "UPSTREAM_CACHE_DIR", tmp_path / "upstreams")
    monkeypatch.setattr(manualmod, "MANUAL_NODE_KEY_PATH", tmp_path / "secrets" / "manual-node.key")
    dbmod.init_db()


def make_group() -> SubscriptionIn:
    return SubscriptionIn.model_validate({
        "name": "V330", "upstreams": [{"name": "Provider", "url": "https://provider.invalid/sub"}],
        "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo"}],
    })


def test_vless_reality_clash_node_has_converter_uri_and_mihomo_proxy() -> None:
    node = NormalizedNode("US Reality", "fp", "Provider", proxy={
        "name": "US Reality", "type": "vless", "server": "node.invalid", "port": 443,
        "uuid": "11111111-1111-4111-8111-111111111111", "network": "tcp", "tls": True,
        "servername": "www.example.com", "client-fingerprint": "chrome", "flow": "xtls-rprx-vision",
        "reality-opts": {"public-key": "public-key-value", "short-id": "abcd1234"},
    })
    uri = proxy_to_uri(node)
    assert uri and uri.startswith("vless://")
    assert "security=reality" in uri and "pbk=public-key-value" in uri and "flow=xtls-rprx-vision" in uri
    rebuilt = uri_to_proxy(uri, "US Reality")
    assert rebuilt and rebuilt["type"] == "vless" and rebuilt["reality-opts"]["short-id"] == "abcd1234"


def test_manual_node_is_encrypted_and_can_be_rebuilt_for_health(tmp_path, monkeypatch) -> None:
    configure(tmp_path, monkeypatch)
    group = create_subscription(make_group())
    uri = "vless://11111111-1111-4111-8111-111111111111@secret.example:443?security=reality&sni=www.example.com&pbk=public-key-value&sid=abcd#Manual"
    import_manual_nodes(group["id"], uri)
    raw = (tmp_path / "submanager.db").read_bytes()
    assert uri.encode() not in raw and b"secret.example" not in raw and b"11111111-1111" not in raw
    with dbmod.db() as conn:
        row = conn.execute("SELECT fingerprint,ciphertext,node_key FROM manual_nodes").fetchone()
        assert row["fingerprint"] == "" and len(row["ciphertext"]) > 32 and len(row["node_key"]) == 64
    loaded = load_manual_nodes(group["id"])
    assert len(loaded) == 1 and loaded[0].proxy and loaded[0].proxy["type"] == "vless"


def test_categories_support_whole_source_and_multi_assignment(tmp_path, monkeypatch) -> None:
    configure(tmp_path, monkeypatch)
    group = create_subscription(make_group())
    upstream_id = group["upstreams"][0]["id"]
    nodes = [
        NormalizedNode("A", "a", "Provider", source_id=upstream_id, node_key="a" * 64, proxy={"name": "A", "type": "ss"}),
        NormalizedNode("Manual", "b", "手动节点", node_key="b" * 64, proxy={"name": "Manual", "type": "ss"}),
    ]
    with dbmod.db() as conn:
        now = "2026-08-25T00:00:00+00:00"
        conn.executemany(
            "INSERT INTO node_preferences(subscription_id,node_key,baseline_name,current_name,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            [(group["id"], node.node_key, node.name, node.name, now, now) for node in nodes],
        )
        conn.commit()
    save_category(group["id"], ProxyCategoryIn(name="美国", icon="🇺🇸", source_ids=[upstream_id], node_keys=[nodes[1].node_key]))
    save_category(group["id"], ProxyCategoryIn(name="家宽", node_keys=[nodes[0].node_key, nodes[1].node_key]))
    result = category_groups(group["id"], nodes)
    assert result[0]["proxies"] == ["A", "Manual"]
    assert result[1]["proxies"] == ["A", "Manual"]


def test_public_subscription_is_inline_and_download_is_opt_in(tmp_path, monkeypatch) -> None:
    configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        group = client.post("/api/subscriptions", json=make_group().model_dump(mode="json")).json()
        detail = client.get(f"/api/subscriptions/{group['id']}").json()
        token = None
        with dbmod.db() as conn:
            token = conn.execute("SELECT token FROM subscriptions WHERE id=?", (group["id"],)).fetchone()[0]
        cachemod.write_output(token, "mihomo", b"proxies: []\n", {
            "updated_at": "2099-01-01T00:00:00+00:00", "config_revision": detail["config_revision"],
            "content_type": "application/yaml; charset=utf-8",
        })
        inline = client.get(f"/s/{token}/mihomo")
        download = client.get(f"/s/{token}/mihomo?download=1")
        assert inline.status_code == 200 and inline.headers["content-disposition"].startswith("inline;")
        assert inline.headers["content-type"].startswith("text/plain")
        assert download.headers["content-disposition"].startswith("attachment;")


def test_uri_import_never_places_proxy_uri_in_validation_output() -> None:
    secret = "vless://11111111-1111-4111-8111-111111111111@hidden.invalid:443#Private"
    result = parse_subscription(secret.encode(), "Manual", re.compile("$^"))
    serialized = json.dumps([{"name": node.name, "protocol": node.protocol} for node in result.nodes])
    assert "hidden.invalid" not in serialized and "11111111" not in serialized


def test_dynamic_display_name_does_not_change_node_identity() -> None:
    def vmess(name: str) -> str:
        payload = {"v": "2", "ps": name, "add": "node.example", "port": "443",
                   "id": "11111111-1111-4111-8111-111111111111", "aid": "0",
                   "scy": "auto", "net": "tcp", "tls": "tls", "sni": "www.example.com"}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"vmess://{encoded}"
    before = parse_subscription(vmess("美国动态01家宽|192.83GB|25D").encode(), "Provider", re.compile("$^"))
    after = parse_subscription(vmess("美国动态01家宽|188.20GB|24D").encode(), "Provider", re.compile("$^"))
    assert before.nodes[0].fingerprint == after.nodes[0].fingerprint


def test_category_order_and_flat_proxy_group(tmp_path, monkeypatch) -> None:
    configure(tmp_path, monkeypatch)
    group = create_subscription(make_group())
    upstream_id = group["upstreams"][0]["id"]
    node = NormalizedNode("Node A", "a", "Provider", source_id=upstream_id, node_key="a" * 64,
                          proxy={"name": "Node A", "type": "ss", "server": "example", "port": 443,
                                 "cipher": "aes-128-gcm", "password": "secret"})
    with dbmod.db() as conn:
        now = "2026-08-25T00:00:00+00:00"
        conn.execute("INSERT INTO node_preferences(subscription_id,node_key,baseline_name,current_name,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                     (group["id"], node.node_key, node.name, node.name, now, now))
        conn.commit()
    first = save_category(group["id"], ProxyCategoryIn(name="第一", node_keys=[node.node_key]))
    second = save_category(group["id"], ProxyCategoryIn(name="第二", node_keys=[node.node_key]))
    assert [x["name"] for x in reorder_categories(group["id"], [second["id"], first["id"]])] == ["第二", "第一"]
    document = yaml.safe_load(build_mihomo_document([node], 60, group["id"]))
    groups = document["proxy-groups"]
    assert [x["name"] for x in groups] == ["PROXY", "♻️ 自动选择", "第二", "第一"]
    assert "全部节点" not in groups[0]["proxies"]
    assert groups[0]["proxies"] == ["第二", "第一", "♻️ 自动选择", "DIRECT", "Node A"]
