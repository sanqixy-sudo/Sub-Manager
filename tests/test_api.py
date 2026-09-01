from __future__ import annotations

import base64

from fastapi.testclient import TestClient
import yaml
import app.db as dbmod
import app.services.cache as cachemod
from app.main import app
from app.services.fetcher import fetcher
from app.services.parser import NormalizedNode, ParseResult
from app.schemas import SubscriptionIn
from app.repository import create_subscription


def configure_database(tmp_path, monkeypatch) -> None:
    for name, value in {"DB_PATH":tmp_path/"submanager.db","BACKUP_DIR":tmp_path/"backups","CACHE_DIR":tmp_path/"cache","UPSTREAM_CACHE_DIR":tmp_path/"upstreams","DATA_DIR":tmp_path}.items(): monkeypatch.setattr(dbmod,name,value)
    monkeypatch.setattr(cachemod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cachemod, "UPSTREAM_CACHE_DIR", tmp_path / "upstreams")


def test_cookie_auth_and_secret_scoped_apis(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/subscriptions").status_code == 401
        login=client.post("/api/auth/login",json={"username":"admin","password":"admin"})
        assert login.status_code==200 and "httponly" in login.headers["set-cookie"].lower()
        created=client.post("/api/subscriptions",json={"name":"Secret test","note":"","interval_minutes":30,"enabled":True,"upstreams":[{"name":"Provider","url":"https://provider.invalid/private?token=secret","enabled":True}],"outputs":[{"client_type":"mihomo","name":"Main","slug":"mihomo","update_interval_minutes":60,"enabled":True}]})
        assert created.status_code==200 and "token" not in created.json()
        summary=client.get("/api/subscriptions").json()[0]
        assert "token" not in summary and "url" not in summary["upstreams"][0]
        assert summary["upstreams"][0]["url_masked"]=="https://provider.invalid/•••"
        detail=client.get(f"/api/subscriptions/{summary['id']}").json()
        assert detail["upstreams"][0]["url"].endswith("token=secret") and "token" not in detail
        first=client.get(f"/api/subscriptions/{summary['id']}/public-urls").json()["urls"][0]["url"]
        second=client.post(f"/api/subscriptions/{summary['id']}/rotate-token").json()["urls"][0]["url"]
        assert first!=second
        # 旧 Token 不返回 404，而是下发毒丸配置（泄露场景对方更新即自毁）
        poisoned_yaml=client.get(first, headers={"user-agent":"ClashMetaforAndroid/2.11"})
        assert poisoned_yaml.status_code==200 and "订阅已失效" in poisoned_yaml.text and "127.0.0.1" in poisoned_yaml.text
        poisoned_uri=base64.b64decode(client.get(first).text).decode()
        assert poisoned_uri.startswith("ss://") and "127.0.0.1:9" in poisoned_uri
        old_token=first.split("/s/")[1].split("/")[0]
        assert client.get(f"/api/public/health/{old_token}").status_code==404
        # 从未存在过的 Token 仍然 404
        assert client.get("/s/"+"f"*40+"/mihomo").status_code==404

        secret = "validation-secret-token"
        invalid = client.post("/api/subscriptions", json={
            "name": "Invalid", "interval_minutes": 30, "enabled": True,
            "upstreams": [{"name": "Provider", "url": f"not-a-url?token={secret}", "enabled": True}],
            "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo", "update_interval_minutes": 60, "enabled": True}],
        })
        assert invalid.status_code == 422 and secret not in invalid.text and "not-a-url" not in invalid.text


def test_output_slugs_can_swap_and_settings_invalidate_group_revision(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).status_code == 200
        created = client.post("/api/subscriptions", json={
            "name": "Swap test", "note": "", "interval_minutes": 30, "enabled": True,
            "upstreams": [{"name": "Provider", "url": "https://provider.invalid/sub", "enabled": True}],
            "outputs": [
                {"client_type": "mihomo", "name": "Main", "slug": "mihomo", "update_interval_minutes": 60, "enabled": True},
                {"client_type": "surge4", "name": "Surge", "slug": "surge", "update_interval_minutes": 60, "enabled": True},
            ],
        }).json()
        group_id = created["id"]
        detail = client.get(f"/api/subscriptions/{group_id}").json()
        first, second = detail["outputs"]
        update = {
            "name": detail["name"], "note": detail["note"],
            "interval_minutes": detail["interval_minutes"], "enabled": detail["enabled"],
            "upstreams": [{k: x[k] for k in ("id", "name", "url", "enabled")} for x in detail["upstreams"]],
            "outputs": [
                {"id": first["id"], "client_type": first["client_type"], "name": first["name"], "slug": second["slug"], "update_interval_minutes": first["update_interval_minutes"], "enabled": first["enabled"]},
                {"id": second["id"], "client_type": second["client_type"], "name": second["name"], "slug": first["slug"], "update_interval_minutes": second["update_interval_minutes"], "enabled": second["enabled"]},
            ],
        }
        swapped = client.put(f"/api/subscriptions/{group_id}", json=update)
        assert swapped.status_code == 200
        assert [x["slug"] for x in swapped.json()["outputs"]] == ["surge", "mihomo"]
        assert swapped.json()["config_revision"] == 2

        settings = client.get("/api/settings").json()
        settings_update = {k: v for k, v in settings.items() if k not in {"default_credentials", "listen_port", "data_dir"}}
        changed = client.put("/api/settings", json=settings_update)
        assert changed.status_code == 200 and not changed.json()["reauth_required"]
        assert client.get(f"/api/subscriptions/{group_id}").json()["config_revision"] == 3


def test_native_mihomo_refresh_creates_public_last_good_without_converter(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)

    async def fake_fetch_group(group):
        nodes = [
            NormalizedNode("DMIT-US-01-US-双ISP家宽|📊193.18GB", "one", "Provider", proxy={
                "name": "DMIT-US-01-US-双ISP家宽|📊193.18GB", "type": "vless", "server": "node.invalid", "port": 443,
                "uuid": "11111111-1111-4111-8111-111111111111", "tls": True,
            }),
            NormalizedNode("DMIT-US-04-HK-BoilHKT-Home|📊500.00GB|⌛25D", "two", "Provider", proxy={
                "name": "DMIT-US-04-HK-BoilHKT-Home|📊500.00GB|⌛25D", "type": "trojan", "server": "hk.invalid", "port": 443,
                "password": "secret", "sni": "hk.invalid",
            }),
        ]
        return [{
            "id": group["upstreams"][0]["id"], "name": "Provider", "ok": True, "fresh": True,
            "cached": False, "status": "ok", "status_code": 200, "content_type": "application/yaml",
            "bytes": 100, "duration_ms": 8, "error": None,
            "parsed": ParseResult("clash_yaml", nodes), "source_format": "clash_yaml",
            "node_count": 2, "filtered_count": 0,
        }]

    monkeypatch.setattr(fetcher, "fetch_group", fake_fetch_group)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        created = client.post("/api/subscriptions", json={
            "name": "Native", "note": "", "interval_minutes": 30, "enabled": True,
            "rename_mode": "smart", "rename_ignore": "DMIT-US",
            "rename_template": "{index}-{flag}-{name}-{traffic}-{reset}",
            "upstreams": [{"name": "Provider", "url": "https://provider.invalid/sub", "enabled": True}],
            "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo", "update_interval_minutes": 60, "enabled": True}],
        }).json()
        refreshed = client.post(f"/api/subscriptions/{created['id']}/refresh")
        assert refreshed.status_code == 200 and refreshed.json()["status"] == "ok"
        public_url = client.get(f"/api/subscriptions/{created['id']}/public-urls").json()["urls"][0]["url"]
        public = client.get(public_url)
        assert public.status_code == 200 and public.headers["x-submanager-cache"] == "fresh"
        assert "subscription-userinfo" not in public.headers
        document = yaml.safe_load(public.content)
        assert [x["name"] for x in document["proxies"]] == ["01-🇺🇸-双ISP家宽-193.18GB", "04-🇭🇰-BoilHKT-Home-500GB-25D"]
        assert document["proxy-groups"][0]["proxies"] == [
            "♻️ 自动选择", "DIRECT", *[node["name"] for node in document["proxies"]]
        ]
        assert all(group["name"] != "全部节点" for group in document["proxy-groups"])

        # V3.1.0 caches may still contain the old aggregate traffic metadata.
        # The public route must not forward it even before the next refresh.
        body_path, meta_path = cachemod.output_paths(
            client.get(f"/api/subscriptions/{created['id']}/public-urls").json()["urls"][0]["url"].split("/s/")[1].split("/")[0],
            "mihomo",
        )
        import json
        old_meta = json.loads(meta_path.read_text("utf-8"))
        old_meta["subscription_userinfo"] = "upload=1; download=2; total=3; expire=4"
        meta_path.write_text(json.dumps(old_meta), "utf-8")
        assert "subscription-userinfo" not in client.get(public_url).headers


def test_v2_migration_preserves_token_and_creates_backup(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    conn=dbmod.connect();conn.executescript("""
      CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,name TEXT NOT NULL,token TEXT NOT NULL UNIQUE,interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL,last_refresh_at TEXT,last_refresh_status TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE upstreams(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,name TEXT NOT NULL,url TEXT NOT NULL,enabled INTEGER NOT NULL,sort_order INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE outputs(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,client_type TEXT NOT NULL,name TEXT NOT NULL,slug TEXT NOT NULL,update_interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(subscription_id,slug));
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
      INSERT INTO subscriptions VALUES(7,'Existing','fixed-token-unchanged',30,1,NULL,NULL,NULL,'now','now');
    """);conn.commit();conn.close();dbmod.init_db()
    with dbmod.db() as upgraded:
        row=upgraded.execute("SELECT id,token,config_revision FROM subscriptions").fetchone()
        assert tuple(row)==(7,"fixed-token-unchanged",1)
        assert upgraded.execute("PRAGMA user_version").fetchone()[0]==10
        assert row["id"] == 7
    assert len(list((tmp_path/"backups").glob("submanager-v2-*.db")))==1


def test_v3_migration_adds_rename_settings_and_creates_backup(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    conn = dbmod.connect()
    conn.executescript("""
      CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,name TEXT NOT NULL,token TEXT NOT NULL UNIQUE,interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL,last_refresh_at TEXT,last_refresh_status TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,config_revision INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE upstreams(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,name TEXT NOT NULL,url TEXT NOT NULL,enabled INTEGER NOT NULL,sort_order INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE outputs(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,client_type TEXT NOT NULL,name TEXT NOT NULL,slug TEXT NOT NULL,update_interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(subscription_id,slug));
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
      PRAGMA user_version=3;
      INSERT INTO subscriptions VALUES(8,'V3 Existing','v3-token',30,1,NULL,'ok',NULL,'now','now',5);
    """)
    conn.commit()
    conn.close()
    dbmod.init_db()
    with dbmod.db() as upgraded:
        row = upgraded.execute("SELECT token,config_revision,rename_mode,rename_ignore,rename_template FROM subscriptions").fetchone()
        assert tuple(row) == ("v3-token", 5, "passthrough", "", "{index}|{flag}|{name}|{traffic}|{reset}")
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 10
    assert len(list((tmp_path / "backups").glob("submanager-v3-*.db"))) == 1


def test_brand_assets_are_served_from_allowlist() -> None:
    with TestClient(app) as client:
        for name in ("brand-logo.png", "brand-logo-white.png", "favicon.ico", "favicon-32.png",
                     "apple-touch-icon.png", "icon-192.png", "icon-512.png", "site.webmanifest"):
            response = client.get(f"/{name}")
            assert response.status_code == 200 and response.content
        assert client.get("/not-a-public-static-file.txt").status_code == 404


def test_upstream_can_disable_group_rename_without_migration(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    dbmod.init_db()
    payload = SubscriptionIn.model_validate({
        "name": "Per upstream policy",
        "rename_mode": "smart",
        "rename_ignore": "EDGE",
        "rename_template": "{flag}|{name}",
        "upstreams": [{
            "name": "Raw source", "url": "https://provider.invalid/sub",
            "rename_policy": "disabled",
        }],
        "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo"}],
    })
    group = create_subscription(payload)
    assert group["upstreams"][0]["rename_policy"] == "disabled"

