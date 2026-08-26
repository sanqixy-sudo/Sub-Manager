from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import app.db as dbmod
import app.services.cache as cachemod
from app.main import app
from app.repository import create_subscription
from app.schemas import SubscriptionIn
from app.services.parser import NormalizedNode, ParseResult
from app.services import refresh


@pytest.mark.asyncio
async def test_singleflight_reuses_one_running_group_refresh(monkeypatch) -> None:
    calls = 0

    async def fake_run(sub_id: int):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.03)
        return {"status": "ok", "success": 1}

    monkeypatch.setattr(refresh, "_run_refresh", fake_run)
    results = await asyncio.gather(*(refresh.refresh_subscription(42) for _ in range(12)))
    assert calls == 1
    assert all(item["status"] == "ok" for item in results)


@pytest.mark.asyncio
async def test_group_fetches_upstreams_once_then_renders_all_outputs(monkeypatch) -> None:
    fetch_calls = 0
    render_calls = 0
    subscription = {"id": 9, "token": "group-token", "config_revision": 1,
                    "upstreams": [{"id": i, "enabled": True} for i in range(5)],
                    "outputs": [{"name": f"output-{i}", "slug": f"o{i}", "client_type": "mihomo", "enabled": True, "update_interval_minutes": 60} for i in range(5)]}

    class DummyConnection:
        def execute(self, *args): return self
        def commit(self): pass
    class DummyContext:
        def __enter__(self): return DummyConnection()
        def __exit__(self, *args): pass

    async def fake_fetch_group(group):
        nonlocal fetch_calls
        fetch_calls += 1
        return [{"name": f"upstream-{i}", "ok": True, "fresh": True, "cached": False,
                 "parsed": object()} for i in range(5)]

    async def fake_render(*args):
        nonlocal render_calls
        render_calls += 1
        return {"ok": True, "name": f"output-{render_calls}"}

    monkeypatch.setattr(refresh, "load_subscription", lambda _: subscription)
    monkeypatch.setattr(refresh, "db", lambda: DummyContext())
    monkeypatch.setattr(refresh.fetcher, "fetch_group", fake_fetch_group)
    monkeypatch.setattr(refresh, "merge_nodes", lambda _: ([object()] * 10, 2))
    monkeypatch.setattr(refresh, "rename_nodes", lambda *args, **kwargs: None)
    monkeypatch.setattr(refresh, "prepare_node_state", lambda *args, **kwargs: [])
    monkeypatch.setattr(refresh, "render_internal_sources", lambda _: [(b"source", "txt")])
    monkeypatch.setattr(refresh, "register_sources", lambda _: asyncio.sleep(0, result=("source-token", ["http://internal/source"])))
    monkeypatch.setattr(refresh, "remove_sources", lambda _: asyncio.sleep(0))
    monkeypatch.setattr(refresh, "render_output", fake_render)
    monkeypatch.setattr(refresh, "_store_group_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(refresh, "store_node_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(refresh, "store_refresh_run", lambda *args, **kwargs: None)

    result = await refresh._run_refresh(9)
    assert fetch_calls == 1
    assert render_calls == 5
    assert result["status"] == "ok"


def test_lifespan_scheduler_refreshes_due_group_without_public_request(tmp_path, monkeypatch) -> None:
    for name, value in {
        "DB_PATH": tmp_path / "submanager.db", "BACKUP_DIR": tmp_path / "backups",
        "CACHE_DIR": tmp_path / "cache", "UPSTREAM_CACHE_DIR": tmp_path / "upstreams",
        "DATA_DIR": tmp_path,
    }.items():
        monkeypatch.setattr(dbmod, name, value)
    monkeypatch.setattr(cachemod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cachemod, "UPSTREAM_CACHE_DIR", tmp_path / "upstreams")
    dbmod.init_db()
    group = create_subscription(SubscriptionIn.model_validate({
        "name": "Scheduled", "interval_minutes": 30, "enabled": True,
        "upstreams": [{"name": "Provider", "url": "https://provider.invalid/sub", "enabled": True}],
        "outputs": [{"client_type": "mihomo", "name": "Scheduled", "slug": "mihomo", "update_interval_minutes": 60, "enabled": True}],
    }))

    async def fake_fetch_group(subscription):
        node = NormalizedNode("Scheduled Node", "scheduled", "Provider", proxy={
            "name": "Scheduled Node", "type": "trojan", "server": "node.invalid",
            "port": 443, "password": "secret",
        })
        return [{
            "id": subscription["upstreams"][0]["id"], "name": "Provider", "ok": True,
            "fresh": True, "cached": False, "status": "ok", "status_code": 200,
            "content_type": "application/yaml", "bytes": 100, "duration_ms": 5,
            "error": None,
            "parsed": ParseResult("clash_yaml", [node]), "source_format": "clash_yaml",
            "node_count": 1, "filtered_count": 0,
        }]

    monkeypatch.setattr(refresh.fetcher, "fetch_group", fake_fetch_group)
    # Starting the application starts scheduler_loop. Do not call the public
    # subscription route or the manual refresh API in this test.
    with TestClient(app):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with dbmod.db() as conn:
                row = conn.execute(
                    "SELECT last_refresh_status,last_success_at,refresh_count FROM subscriptions WHERE id=?",
                    (group["id"],),
                ).fetchone()
            if row["last_refresh_status"] == "ok":
                break
            time.sleep(0.02)
        assert row["last_refresh_status"] == "ok"
        assert row["last_success_at"] and row["refresh_count"] == 1
        assert cachemod.read_output(group["token"], "mihomo") is not None
