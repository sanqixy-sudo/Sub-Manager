from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app.db as dbmod
import app.services.cache as cachemod
from app.main import app
from app.security import ip_allowed


def configure_database(tmp_path, monkeypatch) -> None:
    for name, value in {"DB_PATH": tmp_path / "submanager.db", "BACKUP_DIR": tmp_path / "backups", "CACHE_DIR": tmp_path / "cache", "UPSTREAM_CACHE_DIR": tmp_path / "upstreams", "DATA_DIR": tmp_path}.items():
        monkeypatch.setattr(dbmod, name, value)
    monkeypatch.setattr(cachemod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cachemod, "UPSTREAM_CACHE_DIR", tmp_path / "upstreams")


def test_ip_allowed_empty_whitelist_allows_everyone() -> None:
    assert ip_allowed("", "203.0.113.8")
    assert ip_allowed("   \n  ", "203.0.113.8")
    assert ip_allowed("", "not-an-ip")


def test_ip_allowed_exact_and_cidr() -> None:
    assert ip_allowed("203.0.113.8", "203.0.113.8")
    assert not ip_allowed("203.0.113.8", "203.0.113.9")
    assert ip_allowed("2001:db8::/32", "2001:db8:1234::1")
    assert not ip_allowed("2001:db8::/32", "2001:db9::1")
    # IPv4/IPv6 混排时只按同族匹配
    assert ip_allowed("2001:db8::/32\n203.0.113.0/24", "203.0.113.10")
    assert not ip_allowed("2001:db8::/32", "203.0.113.8")


def test_ip_allowed_ignores_comments_and_invalid_lines() -> None:
    whitelist = "# 备注\n\n不是IP的一行\n203.0.113.8"
    assert ip_allowed(whitelist, "203.0.113.8")
    assert not ip_allowed(whitelist, "198.51.100.1")


def test_ip_allowed_rejects_invalid_client_ip() -> None:
    assert not ip_allowed("203.0.113.8", "garbage")
    assert not ip_allowed("203.0.113.8", "")


def _create_whitelisted_group(client: TestClient, whitelist: str) -> tuple[int, str]:
    created = client.post("/api/subscriptions", json={
        "name": "Whitelist test", "note": "", "interval_minutes": 30, "enabled": True,
        "ip_whitelist": whitelist,
        "upstreams": [],
        "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo", "update_interval_minutes": 60, "enabled": True}],
    })
    assert created.status_code == 200
    group_id = created.json()["id"]
    detail = client.get(f"/api/subscriptions/{group_id}").json()
    assert detail["ip_whitelist"] == whitelist
    public_url = client.get(f"/api/subscriptions/{group_id}/public-urls").json()["urls"][0]["url"]
    token = public_url.split("/s/")[1].split("/")[0]
    cachemod.write_output(token, "mihomo", b"proxies: []\n", {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "config_revision": int(detail["config_revision"]),
        "content_type": "application/yaml",
    })
    return group_id, public_url


def test_public_subscription_enforces_ip_whitelist(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        _, public_url = _create_whitelisted_group(client, "203.0.113.0/24")

        # 白名单命中（X-Forwarded-For 第一个地址）→ 正常返回
        allowed = client.get(public_url, headers={"X-Forwarded-For": "203.0.113.8, 10.0.0.1"})
        assert allowed.status_code == 200 and allowed.content == b"proxies: []\n"

        # 未命中与不带头（直连 IP 不是合法 IP）均返回与“订阅不存在”相同的 404
        for headers in ({"X-Forwarded-For": "198.51.100.1"}, {}):
            denied = client.get(public_url, headers=headers)
            assert denied.status_code == 404 and denied.json()["detail"] == "订阅不存在"

        # 白名单留空则不限制
        _, open_url = _create_whitelisted_group(client, "")
        assert client.get(open_url).status_code == 200


def test_ip_whitelist_schema_validation(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        base = {
            "name": "Bad whitelist", "interval_minutes": 30, "enabled": True,
            "upstreams": [],
            "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo", "update_interval_minutes": 60, "enabled": True}],
        }
        invalid = client.post("/api/subscriptions", json={**base, "ip_whitelist": "203.0.113.8\nnot-an-ip"})
        assert invalid.status_code == 422 and "白名单" in invalid.text
        too_long = client.post("/api/subscriptions", json={**base, "ip_whitelist": "\n".join(["203.0.113.8"] * 101)})
        assert too_long.status_code == 422

        # 更新路径同样持久化该字段
        _, public_url = _create_whitelisted_group(client, "203.0.113.8")
        group_id = int(client.get("/api/subscriptions").json()[0]["id"])
        detail = client.get(f"/api/subscriptions/{group_id}").json()
        updated = client.put(f"/api/subscriptions/{group_id}", json={
            "name": detail["name"], "note": detail["note"],
            "interval_minutes": detail["interval_minutes"], "enabled": detail["enabled"],
            "ip_whitelist": "198.51.100.0/24",
            "upstreams": [], "outputs": [
                {"id": detail["outputs"][0]["id"], "client_type": "mihomo", "name": "Main",
                 "slug": "mihomo", "update_interval_minutes": 60, "enabled": True},
            ],
        })
        assert updated.status_code == 200 and updated.json()["ip_whitelist"] == "198.51.100.0/24"
        # 更新后白名单生效：旧 IP 被拒，新网段不再返回白名单 404
        denied = client.get(public_url, headers={"X-Forwarded-For": "203.0.113.8"})
        assert denied.status_code == 404 and denied.json()["detail"] == "订阅不存在"
        allowed = client.get(public_url, headers={"X-Forwarded-For": "198.51.100.9"})
        assert allowed.status_code != 404
