from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.db as dbmod
import app.services.cache as cachemod
from app.main import app
from app.operations import confirm_nodes, prepare_node_state, store_node_snapshot, update_node_preference
from app.repository import create_subscription
from app.repository import parse_iso
from app.schemas import SubscriptionIn
from app.services import refresh
from app.services.renamer import DEFAULT_TEMPLATE, rename_nodes
from app.services.parser import NormalizedNode, ParseResult


def configure_database(tmp_path, monkeypatch) -> None:
    for name, value in {
        "DB_PATH": tmp_path / "submanager.db", "BACKUP_DIR": tmp_path / "backups",
        "CACHE_DIR": tmp_path / "cache", "UPSTREAM_CACHE_DIR": tmp_path / "upstreams", "DATA_DIR": tmp_path,
    }.items():
        monkeypatch.setattr(dbmod, name, value)
    monkeypatch.setattr(cachemod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cachemod, "UPSTREAM_CACHE_DIR", tmp_path / "upstreams")


def test_legacy_naive_sqlite_time_is_treated_as_utc() -> None:
    parsed = parse_iso("2026-08-25 02:31:22")
    assert parsed is not None and parsed.utcoffset() is not None


def test_v4_to_v5_migration_preserves_group_and_adds_operations_tables(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    conn = dbmod.connect()
    conn.executescript("""
      CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,name TEXT NOT NULL,token TEXT NOT NULL UNIQUE,interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL,last_refresh_at TEXT,last_refresh_status TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,config_revision INTEGER NOT NULL DEFAULT 1,rename_mode TEXT NOT NULL DEFAULT 'passthrough',rename_ignore TEXT NOT NULL DEFAULT '',rename_template TEXT NOT NULL DEFAULT '{index}-{flag}-{name}-{traffic}-{reset}');
      CREATE TABLE upstreams(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,name TEXT NOT NULL,url TEXT NOT NULL,enabled INTEGER NOT NULL,sort_order INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE outputs(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,client_type TEXT NOT NULL,name TEXT NOT NULL,slug TEXT NOT NULL,update_interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(subscription_id,slug));
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
      PRAGMA user_version=4;
      INSERT INTO subscriptions VALUES(9,'V4 group','unchanged-token',30,1,NULL,'ok',NULL,'now','now',7,'smart','DMIT','{index}-{name}');
    """)
    conn.commit(); conn.close(); dbmod.init_db()
    with dbmod.db() as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 8
        assert tuple(upgraded.execute("SELECT id,token,config_revision FROM subscriptions").fetchone()) == (9, "unchanged-token", 7)
        assert upgraded.execute("SELECT COUNT(*) FROM refresh_runs").fetchone()[0] == 0
        assert upgraded.execute("SELECT COUNT(*) FROM node_snapshots").fetchone()[0] == 0
    assert len(list((tmp_path / "backups").glob("submanager-v4-*.db"))) == 1


def test_v5_to_v6_migration_preserves_tokens_templates_and_snapshots(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    conn = dbmod.connect()
    conn.executescript("""
      CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,name TEXT NOT NULL,token TEXT NOT NULL UNIQUE,interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL,last_refresh_at TEXT,last_refresh_status TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,config_revision INTEGER NOT NULL DEFAULT 1,rename_mode TEXT NOT NULL DEFAULT 'passthrough',rename_ignore TEXT NOT NULL DEFAULT '',rename_template TEXT NOT NULL DEFAULT '{index}-{flag}-{name}-{traffic}-{reset}');
      CREATE TABLE upstreams(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,name TEXT NOT NULL,url TEXT NOT NULL,enabled INTEGER NOT NULL,sort_order INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE outputs(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,client_type TEXT NOT NULL,name TEXT NOT NULL,slug TEXT NOT NULL,update_interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(subscription_id,slug));
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE node_snapshots(subscription_id INTEGER NOT NULL,position INTEGER NOT NULL,original_name TEXT NOT NULL,final_name TEXT NOT NULL,source_name TEXT NOT NULL,protocol TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(subscription_id,position));
      PRAGMA user_version=5;
      INSERT INTO subscriptions VALUES(1,'Default','token-default',30,1,NULL,'ok',NULL,'now','now',2,'smart','DMIT','{index}-{flag}-{name}-{traffic}-{reset}');
      INSERT INTO subscriptions VALUES(2,'Custom','token-custom',30,1,NULL,'ok',NULL,'now','now',3,'smart','DMIT','{index}/{name}');
      INSERT INTO upstreams VALUES(10,1,'Provider','https://provider.invalid/sub',1,0,'now','now');
      INSERT INTO node_snapshots VALUES(1,1,'old name','final name','Provider','vless','now');
    """)
    conn.commit(); conn.close(); dbmod.init_db()
    with dbmod.db() as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 8
        groups = [tuple(row) for row in upgraded.execute(
            "SELECT token,rename_template FROM subscriptions ORDER BY id"
        )]
        assert groups == [
            ("token-default", "{index}|{flag}|{name}|{traffic}|{reset}"),
            ("token-custom", "{index}/{name}"),
        ]
        upstream = upgraded.execute(
            "SELECT rename_policy,rename_ignore,rename_template FROM upstreams WHERE id=10"
        ).fetchone()
        assert tuple(upstream) == ("inherit", "", "{index}|{flag}|{name}|{traffic}|{reset}")
        snapshot = upgraded.execute(
            "SELECT original_name,final_name,confirmation_status FROM node_snapshots"
        ).fetchone()
        assert tuple(snapshot) == ("old name", "final name", "confirmed")
        assert upgraded.execute("SELECT value FROM settings WHERE key='node_key_secret'").fetchone()[0]
    assert len(list((tmp_path / "backups").glob("submanager-v5-*.db"))) == 1

@pytest.mark.asyncio
async def test_singleflight_stores_one_run_and_redacted_node_snapshot(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch); dbmod.init_db()
    group = create_subscription(SubscriptionIn.model_validate({
        "name": "Operations", "interval_minutes": 30, "enabled": True,
        "upstreams": [{"name": "Provider", "url": "https://provider.invalid/sub", "enabled": True}],
        "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo", "update_interval_minutes": 60, "enabled": True}],
    }))
    calls = 0
    async def fake_fetch(subscription):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.03)
        node = NormalizedNode("https://secret.invalid/path?token=hidden", "fingerprint", "Provider", proxy={
            "name": "https://secret.invalid/path?token=hidden", "type": "trojan", "server": "secret.invalid",
            "port": 443, "password": "super-secret",
        })
        return [{"id": subscription["upstreams"][0]["id"], "name": "Provider", "ok": True, "fresh": True,
                 "cached": False, "status": "ok", "status_code": 200, "content_type": "application/yaml",
                 "bytes": 20, "duration_ms": 2, "error": None, "parsed": ParseResult("clash_yaml", [node]),
                 "source_format": "clash_yaml", "node_count": 1, "filtered_count": 0}]
    monkeypatch.setattr(refresh.fetcher, "fetch_group", fake_fetch)
    results = await asyncio.gather(*(refresh.refresh_subscription(group["id"]) for _ in range(10)))
    assert calls == 1 and all(item["status"] == "ok" for item in results)
    with dbmod.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM refresh_runs WHERE subscription_id=?", (group["id"],)).fetchone()[0] == 1
        snapshot = conn.execute("SELECT original_name,final_name,protocol FROM node_snapshots").fetchone()
        assert tuple(snapshot) == ("[上游链接]", "[上游链接]", "trojan")


def test_node_alias_lifecycle_keeps_dynamic_usage_and_requires_manual_confirmation(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch); dbmod.init_db()
    group = create_subscription(SubscriptionIn.model_validate({
        "name": "Aliases", "interval_minutes": 30, "enabled": True,
        "rename_mode": "smart", "rename_ignore": "DMIT-US", "rename_template": DEFAULT_TEMPLATE,
        "upstreams": [{"name": "Provider", "url": "https://provider.invalid/sub", "enabled": True}],
        "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo", "update_interval_minutes": 60, "enabled": True}],
    }))
    upstreams = [{"id": group["upstreams"][0]["id"], "rename_policy": "inherit"}]

    def prepare(name: str, fingerprint: str = "same-connection"):
        node = NormalizedNode(name, fingerprint, "Provider", source_id=group["upstreams"][0]["id"])
        rename_nodes([node], "smart", "DMIT-US", DEFAULT_TEMPLATE, upstreams)
        preferences = prepare_node_state(group["id"], [node])
        return node, preferences

    baseline, preferences = prepare("DMIT-US-01-US-双ISP家宽|📊192.83GB")
    assert baseline.confirmation_status == "confirmed"
    assert baseline.name == "01|🇺🇸|双ISP家宽|192.83GB"
    store_node_snapshot(group["id"], [baseline], preferences)
    update_node_preference(group["id"], baseline.node_key, "美国动态01家宽", True)

    usage_changed, preferences = prepare("DMIT-US-01-US-双ISP家宽|📊150GB|25D")
    assert usage_changed.confirmation_status == "confirmed"
    assert usage_changed.name == "美国动态01家宽|150GB|25D"
    store_node_snapshot(group["id"], [usage_changed], preferences)

    renamed, preferences = prepare("DMIT-US-01-US-住宅家宽|📊149GB|24D")
    assert renamed.confirmation_status == "pending_changed"
    assert renamed.previous_name and "双ISP家宽" in renamed.previous_name
    assert renamed.name == "⚠️美国动态01家宽|149GB|24D"
    store_node_snapshot(group["id"], [renamed], preferences)

    still_pending, preferences = prepare("DMIT-US-01-US-住宅家宽|📊148GB|23D")
    assert still_pending.confirmation_status == "pending_changed"
    assert still_pending.name.startswith("⚠️美国动态01家宽|")
    store_node_snapshot(group["id"], [still_pending], preferences)
    assert confirm_nodes(group["id"], [still_pending.node_key]) == 1

    confirmed, preferences = prepare("DMIT-US-01-US-住宅家宽|📊147GB|22D")
    assert confirmed.confirmation_status == "confirmed"
    assert confirmed.name == "美国动态01家宽|147GB|22D"
    store_node_snapshot(group["id"], [confirmed], preferences)

    added, preferences = prepare("DMIT-US-02-HK-新节点|88GB", "new-connection")
    assert added.confirmation_status == "pending_new"
    assert added.name.startswith("⚠️02|🇭🇰|新节点|88GB")
    store_node_snapshot(group["id"], [added], preferences)
    repeated, _ = prepare("DMIT-US-02-HK-新节点|87GB", "new-connection")
    assert repeated.confirmation_status == "pending_new"
    assert repeated.name.startswith("⚠️")

def test_operations_apis_and_inspector_never_return_connection_secrets(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        inspected = client.post("/api/tools/inspect", json={
            "content": "vless://11111111-1111-4111-8111-111111111111@example.invalid:443#US-01-Test",
            "source_name": "Sandbox", "rename_mode": "passthrough",
        })
        assert inspected.status_code == 200
        assert "11111111-1111-4111-8111-111111111111" not in inspected.text
        assert "example.invalid" not in inspected.text
        assert inspected.json()["nodes"][0]["protocol"] == "vless"
        assert client.get("/api/overview").status_code == 200
        assert client.get("/api/runs").status_code == 200


def test_nodes_outside_smart_rename_never_require_confirmation(tmp_path, monkeypatch) -> None:
    configure_database(tmp_path, monkeypatch); dbmod.init_db()
    group = create_subscription(SubscriptionIn.model_validate({
        "name": "Confirmation scope", "interval_minutes": 30, "enabled": True,
        "rename_mode": "smart", "rename_ignore": "EDGE", "rename_template": DEFAULT_TEMPLATE,
        "upstreams": [{"name": "Raw", "url": "https://provider.invalid/sub", "enabled": True,
                       "rename_policy": "disabled"}],
        "outputs": [{"client_type": "mihomo", "name": "Main", "slug": "mihomo",
                     "update_interval_minutes": 60, "enabled": True}],
    }))
    upstream_id = group["upstreams"][0]["id"]

    managed = NormalizedNode("EDGE-01-US-Home", "same", "Raw", source_id=upstream_id)
    rename_nodes([managed], "smart", "EDGE", DEFAULT_TEMPLATE, [{
        "id": upstream_id, "rename_policy": "smart", "rename_ignore": "EDGE",
        "rename_template": DEFAULT_TEMPLATE,
    }])
    preferences = prepare_node_state(group["id"], [managed])
    store_node_snapshot(group["id"], [managed], preferences)

    raw = NormalizedNode("Completely changed raw name", "same", "Raw", source_id=upstream_id)
    rename_nodes([raw], "smart", "EDGE", DEFAULT_TEMPLATE, [{
        "id": upstream_id, "rename_policy": "disabled",
    }])
    raw_preferences = prepare_node_state(group["id"], [raw])
    assert raw.confirmation_status == "confirmed"
    assert raw.name == "Completely changed raw name"
    assert not raw.name.startswith("⚠️")
    assert raw_preferences[0]["status"] == "confirmed"
    store_node_snapshot(group["id"], [raw], raw_preferences)

    manual = NormalizedNode("My manual node", "manual-new", "手动节点")
    rename_nodes([manual], "smart", "EDGE", DEFAULT_TEMPLATE)
    manual_preferences = prepare_node_state(group["id"], [manual])
    assert manual.confirmation_status == "confirmed"
    assert manual.name == "My manual node"
    assert manual_preferences[0]["status"] == "confirmed"