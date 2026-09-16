from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import app.db as database
import app.services.cache as cache
import app.services.manual_nodes as manual
from app.main import app
from app.repository import create_subscription, update_subscription, load_subscription, is_due
from app.schemas import SubscriptionIn
from app.services import refresh, health
from app.services.parser import parse_subscription, merge_nodes, proxy_to_uri, _uri_fingerprint
from app.operations import list_node_snapshots, update_node_preference, update_node_order


URI = 'trojan://demo-password@node.example.invalid:443?sni=tls.example.invalid#Example'


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, path in {'DB_PATH': tmp_path/'db.sqlite', 'DATA_DIR': tmp_path, 'CACHE_DIR': tmp_path/'cache',
                       'UPSTREAM_CACHE_DIR': tmp_path/'upstreams', 'BACKUP_DIR': tmp_path/'backups', 'SECRETS_DIR': tmp_path/'secrets'}.items():
        monkeypatch.setattr(database, name, path)
    monkeypatch.setattr(cache, 'CACHE_DIR', tmp_path/'cache')
    monkeypatch.setattr(cache, 'UPSTREAM_CACHE_DIR', tmp_path/'upstreams')
    monkeypatch.setattr(manual, 'MANUAL_NODE_KEY_PATH', tmp_path/'secrets/manual.key')
    database.init_db()
    database.set_settings({'scheduler_enabled': '0', 'health_check_enabled': '0'})
    return tmp_path


def payload(**kwargs):
    return SubscriptionIn.model_validate({'name': 'Example', 'upstreams': [],
        'outputs': [{'name': '中文订阅', 'client_type': 'mihomo', 'slug': 'mihomo'}], **kwargs})


def test_remove_all_remote_sources(isolated):
    group = create_subscription(payload(upstreams=[{'name': 'old', 'url': 'https://old.example.invalid/sub'}]))
    updated, removed = update_subscription(group['id'], payload(manual_content=URI))
    assert updated['upstreams'] == []
    assert removed == ['https://old.example.invalid/sub']
    assert len(updated['manual_nodes']) == 1


def test_invalid_manual_create_is_atomic(isolated):
    from fastapi import HTTPException
    with pytest.raises(HTTPException): create_subscription(payload(manual_content='<html>not a node</html>'))
    with database.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_manual_success_fresh_and_chinese_title(isolated):
    group = create_subscription(payload(manual_content=URI))
    result = await refresh.refresh_subscription(group['id'])
    current = load_subscription(group['id'])
    assert result['status'] == 'ok'
    assert current['cache_state'] == 'fresh' and current['last_success_at']
    assert not is_due(current)
    response = TestClient(app).get(f"/s/{group['token']}/mihomo")
    assert response.status_code == 200
    assert base64.b64decode(response.headers['profile-title'].split(':', 1)[1]).decode() == '中文订阅'


@pytest.mark.parametrize('minutes', [None, '75'])
def test_legacy_interval_preserves_explicit_minutes(isolated, minutes):
    with database.db() as conn:
        conn.execute("DELETE FROM settings WHERE key='health_check_interval_minutes'")
        conn.execute("INSERT INTO settings VALUES('health_check_interval_hours','6','now')")
        if minutes: conn.execute("INSERT INTO settings VALUES('health_check_interval_minutes',?,'now')", (minutes,))
        conn.execute('PRAGMA user_version=10'); conn.commit()
    database.init_db()
    assert database.get_setting('health_check_interval_minutes') == (minutes or '360')
    assert list((isolated/'backups').glob('submanager-v10-*.db'))


def test_future_schema_is_not_downgraded(isolated):
    with database.db() as conn:
        conn.execute('PRAGMA user_version=999'); conn.commit()
    with pytest.raises(RuntimeError): database.init_db()
    with database.db() as conn: assert conn.execute('PRAGMA user_version').fetchone()[0] == 999


def test_uri_yaml_identity_and_tls_behavior():
    uri = parse_subscription(URI.encode(), 'uri', re.compile(r'^$'))
    proxy = dict(uri.nodes[0].proxy)
    proxy.pop('udp'); proxy.pop('tls')
    document = parse_subscription(yaml.safe_dump({'proxies': [proxy]}).encode(), 'yaml', re.compile(r'^$'))
    assert len(merge_nodes([uri, document])[0]) == 1
    proxy['skip-cert-verify'] = True
    insecure = parse_subscription(yaml.safe_dump({'proxies': [proxy]}).encode(), 'yaml', re.compile(r'^$'))
    assert len(merge_nodes([uri, insecure])[0]) == 2
    assert proxy_to_uri(document.nodes[0]).startswith('trojan://')


@pytest.mark.parametrize('kind, fields', [
    ('ss', {'cipher': 'aes-128-gcm', 'password': 'demo'}),
    ('ssr', {'cipher': 'aes-128-cfb', 'password': 'demo', 'protocol': 'origin', 'obfs': 'plain'}),
    ('vmess', {'uuid': '11111111-1111-4111-8111-111111111111'}),
    ('vless', {'uuid': '11111111-1111-4111-8111-111111111111'}),
    ('trojan', {'password': 'demo'}), ('hysteria2', {'password': 'demo'}),
    ('tuic', {'uuid': '11111111-1111-4111-8111-111111111111', 'password': 'demo'}), ('anytls', {'password': 'demo'}),
])
def test_yaml_protocol_uri_roundtrip(kind, fields):
    proxy = {'name': 'new name', 'server': 'node.example.invalid', 'port': 443, 'type': kind, **fields}
    original = parse_subscription(yaml.safe_dump({'proxies': [proxy]}).encode(), 'yaml', re.compile(r'^$')).nodes[0]
    uri = proxy_to_uri(original)
    assert uri
    rebuilt = parse_subscription(uri.encode(), 'uri', re.compile(r'^$')).nodes[0]
    assert rebuilt.proxy['server'] == proxy['server']
    assert rebuilt.proxy['type'] == kind


@pytest.mark.asyncio
async def test_edit_during_render_cannot_publish_old_alias(isolated, monkeypatch):
    group = create_subscription(payload(manual_content=URI))
    await refresh.refresh_subscription(group['id'])
    key = list_node_snapshots(group['id'])['nodes'][0]['node_key']
    entered, resume = asyncio.Event(), asyncio.Event()
    real_render = refresh.render_output
    calls = 0
    async def gated(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await resume.wait()
        return await real_render(*args)
    monkeypatch.setattr(refresh, 'render_output', gated)
    running = asyncio.create_task(refresh.refresh_subscription(group['id']))
    await entered.wait()
    update_node_preference(group['id'], key, 'My alias', True)
    resume.set()
    assert (await running)['status'] == 'ok'
    assert calls == 2
    body, meta = cache.read_output(group['token'], 'mihomo')
    assert 'My alias' in body.decode()
    assert meta['config_revision'] == load_subscription(group['id'])['config_revision']
    assert list_node_snapshots(group['id'])['nodes'][0]['alias'] == 'My alias'
    with database.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM refresh_runs').fetchone()[0] == 2


@pytest.mark.asyncio
async def test_stale_never_replaces_good_output_or_snapshot(isolated, monkeypatch):
    group = create_subscription(payload(upstreams=[{'name': 'remote', 'url': 'https://sub.example.invalid'}]))
    async def source(_):
        return [{'ok': True, 'fresh': fresh[0], 'name': 'remote', 'cached': not fresh[0],
                 'parsed': parse_subscription(URI.encode(), 'remote', re.compile('^$'))}]
    fresh = [True]
    monkeypatch.setattr(refresh.fetcher, 'fetch_group', source)
    await refresh.refresh_subscription(group['id'])
    original = cache.read_output(group['token'], 'mihomo')
    snapshot = list_node_snapshots(group['id'])
    fresh[0] = False
    assert (await refresh.refresh_subscription(group['id']))['status'] == 'stale'
    assert cache.read_output(group['token'], 'mihomo') == original
    assert list_node_snapshots(group['id']) == snapshot


def test_atomic_cache_does_not_mix_legacy_body(isolated):
    cache.write_output('token', 'slug', b'first', {'generation': 1})
    body, _ = cache.output_paths('token', 'slug')
    body.write_bytes(b'incomplete second generation')
    assert cache.read_output('token', 'slug') == (b'first', {'generation': 1})


def test_trusted_proxy_and_spoofing(isolated):
    group = create_subscription(payload(ip_whitelist='203.0.113.8'))
    cache.write_output(group['token'], 'mihomo', b'cached', {'updated_at': datetime.now(timezone.utc).isoformat(), 'config_revision': 1})
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=['10.0.0.1'])
    path = f"/s/{group['token']}/mihomo"
    spoofed = TestClient(wrapped, client=('198.51.100.2', 123)).get(path, headers={'X-Forwarded-For': '203.0.113.8'})
    assert spoofed.status_code == 404
    trusted = TestClient(wrapped, client=('10.0.0.1', 123))
    assert trusted.get(path, headers={'X-Forwarded-For': '203.0.113.8'}).status_code == 200
    assert trusted.get(path, headers={'X-Forwarded-For': '203.0.113.8, 198.51.100.2'}).status_code == 404
    assert TestClient(app).get(f"/api/public/health/{group['token']}", headers={'X-Forwarded-For': '203.0.113.8'}).status_code == 404


@pytest.mark.asyncio
async def test_batch_singleflight_same_set(isolated, monkeypatch):
    gate = asyncio.Event()
    captured = []
    async def run(*args): captured.append(args); await gate.wait()
    monkeypatch.setattr(health, '_run', run)
    monkeypatch.setattr(health, '_task', None)
    nodes = [(1, 'a'*64), (2, 'b'*64)]
    first = await health.start_health_test(nodes=nodes)
    second = await health.start_health_test(nodes=list(reversed(nodes)))
    assert first['run_id'] == second['run_id'] and second['reused']
    await asyncio.sleep(0)
    assert len(captured) == 1 and captured[0][3] == tuple(nodes)
    gate.set(); await health._task


@pytest.mark.asyncio
async def test_notification_rejection_is_retried(isolated, monkeypatch):
    database.set_settings({'health_notify_webhook': 'https://notify.example.invalid/secret'})
    async def fail(_): raise RuntimeError('do not persist this secret')
    monkeypatch.setattr(health, '_send_notification', fail)
    with database.db() as conn:
        conn.execute("INSERT INTO health_notifications(events,status,created_at,updated_at,next_attempt_at) VALUES('[]','pending','now','now','2000')"); conn.commit()
    await health.deliver_notifications()
    with database.db() as conn:
        row = dict(conn.execute('SELECT * FROM health_notifications').fetchone())
    assert row['attempts'] == 1 and row['status'] == 'pending'
    assert row['error_code'] == 'delivery_failed' and 'secret' not in str(row)


@pytest.mark.asyncio
async def test_invalid_kernel_node_is_isolated(isolated, monkeypatch):
    class Process:
        returncode = 0
        def __init__(self, code): self.code = code
        async def wait(self): return self.code
    async def spawn(*args, **kwargs):
        with open(args[args.index('-f')+1], encoding='utf-8') as file: document = yaml.safe_load(file)
        return Process(int(any(x['type'] == 'invalid' for x in document['proxies'])))
    monkeypatch.setattr(health.asyncio, 'create_subprocess_exec', spawn)
    proxies = [{'name': 'a', 'type': 'trojan'}, {'name': 'b', 'type': 'invalid'}, {'name': 'c', 'type': 'ss'}]
    assert [p['name'] for p in await health._validate_proxies(proxies, isolated)] == ['a', 'c']
    assert not (isolated/'validate.yaml').exists()


@pytest.mark.asyncio
async def test_legacy_key_survives_format_change_after_seed(isolated, monkeypatch):
    from app.operations import store_node_snapshot, prepare_node_state
    group = create_subscription(payload(upstreams=[{'name': 'remote', 'url': 'https://sub.example.invalid'}]))
    node = parse_subscription(URI.encode(), 'remote', re.compile('^$')).nodes[0]
    node.fingerprint = _uri_fingerprint(URI)
    node.source_id = group['upstreams'][0]['id']
    preferences = prepare_node_state(group['id'], [node])
    store_node_snapshot(group['id'], [node], preferences)
    old_key = node.node_key
    update_node_preference(group['id'], old_key, 'Legacy alias', True)
    path, _ = cache.upstream_paths(group['id'], group['upstreams'][0])
    path.write_bytes(URI.encode())
    health.seed_node_identities()
    document = yaml.safe_dump({'proxies': [node.proxy]}).encode()
    async def fetch(_): return [{'ok': True, 'fresh': True, 'name': 'remote', 'parsed': parse_subscription(document, 'remote', re.compile('^$'))}]
    monkeypatch.setattr(refresh.fetcher, 'fetch_group', fetch)
    await refresh.refresh_subscription(group['id'])
    current = list_node_snapshots(group['id'])['nodes'][0]
    assert current['node_key'] == old_key and current['alias'] == 'Legacy alias'


@pytest.mark.asyncio
async def test_sql_pagination_sorts_global_nodes(isolated):
    from app.api.health import nodes as list_health
    group = create_subscription(payload(manual_content='\n'.join(URI.replace('#Example', f'#{i}').replace(':443', f':{443+i}') for i in range(4))))
    await refresh.refresh_subscription(group['id'])
    values = health._load_group_nodes(group['id'])
    run = health._new_run(group['id'], None, 'manual')
    for node, delay in zip(values, [900, 600, 300, 100]):
        health._store_result(run, group['id'], node, {'status': 'healthy', 'connectivity_latency_ms': delay})
    first = list_health(page_size=1, sort='latency')
    second = list_health(page_size=1, page=2, sort='latency')
    assert first['total'] == 4 and first['items'][0]['connectivity_latency_ms'] == 100
    assert second['items'][0]['connectivity_latency_ms'] == 300
    assert first['protocols'] == ['trojan'] and len(first['items'][0]['history']) == 1


@pytest.mark.asyncio
async def test_removed_upstream_cannot_recreate_secret_cache(isolated, monkeypatch):
    from app.services.fetcher import UpstreamFetcher
    group = create_subscription(payload(upstreams=[{'name': 'old', 'url': 'https://old.example.invalid/sub'}]))
    entered, release = asyncio.Event(), asyncio.Event()
    async def response(request):
        entered.set(); await release.wait()
        return httpx.Response(200, content=URI.encode())
    fetcher = UpstreamFetcher()
    fetcher.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    pending = asyncio.create_task(fetcher.fetch_group(group))
    await entered.wait()
    update_subscription(group['id'], payload(manual_content=URI))
    release.set()
    assert not (await pending)[0]['fresh']
    assert not cache.upstream_paths(group['id'], group['upstreams'][0])[0].exists()
    await fetcher.close()


def test_full_backup_includes_wal_data_and_secret_key(isolated):
    import sqlite3
    from app.backup import backup_data
    source = isolated/'backup-source'; source.mkdir()
    (source/'secrets').mkdir(); (source/'secrets/manual-node.key').write_bytes(b'test-key')
    with sqlite3.connect(source/'submanager.db') as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE example(value TEXT)')
        conn.execute("INSERT INTO example VALUES('committed')"); conn.commit()
        backup_data(source, isolated/'backup-target')
    with sqlite3.connect(isolated/'backup-target/submanager.db') as conn:
        assert conn.execute('SELECT value FROM example').fetchone()[0] == 'committed'
    assert (isolated/'backup-target/secrets/manual-node.key').read_bytes() == b'test-key'
    with pytest.raises(ValueError): backup_data(source, isolated/'backup-target')
    with pytest.raises(ValueError): backup_data(source, source/'inside')


def test_yaml_cycles_and_excessive_depth_rejected():
    for content in ['proxies: &a [*a]', 'proxies: ' + '['*100 + '0' + ']'*100]:
        with pytest.raises(ValueError): parse_subscription(content.encode(), 'bad', re.compile('^$'))


@pytest.mark.asyncio
async def test_webhook_robot_error_is_not_success(isolated, monkeypatch):
    database.set_settings({'health_notify_webhook': 'https://notify.example.invalid/test'})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(health.httpx, 'AsyncClient', lambda **kwargs: client_type(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'errcode': 40013})), **kwargs))
    with pytest.raises(RuntimeError, match='webhook_rejected'):
        await health._send_notification([('down', 'group', 'node', 3)])


@pytest.mark.asyncio
async def test_delete_during_render_does_not_recreate_output(isolated, monkeypatch):
    from app.api.subscriptions import delete_group_api
    from fastapi import HTTPException
    group = create_subscription(payload(manual_content=URI))
    await refresh.refresh_subscription(group['id'])
    entered, release = asyncio.Event(), asyncio.Event()
    real_render = refresh.render_output
    async def render(*args):
        entered.set(); await release.wait()
        return await real_render(*args)
    monkeypatch.setattr(refresh, 'render_output', render)
    pending = asyncio.create_task(refresh.refresh_subscription(group['id']))
    await entered.wait()
    delete_group_api(group['id'])
    release.set()
    with pytest.raises(HTTPException): await pending
    assert cache.read_output(group['token'], 'mihomo') is None


@pytest.mark.asyncio
async def test_token_reset_during_render_only_publishes_new_token(isolated, monkeypatch):
    from app.api.subscriptions import rotate_token
    from starlette.requests import Request
    group = create_subscription(payload(manual_content=URI))
    await refresh.refresh_subscription(group['id'])
    entered, release = asyncio.Event(), asyncio.Event()
    real_render, calls = refresh.render_output, []
    async def render(*args):
        calls.append(1)
        if len(calls) == 1: entered.set(); await release.wait()
        return await real_render(*args)
    monkeypatch.setattr(refresh, 'render_output', render)
    pending = asyncio.create_task(refresh.refresh_subscription(group['id']))
    await entered.wait()
    request = Request({'type': 'http', 'scheme': 'http', 'server': ('testserver', 80), 'path': '/', 'root_path': '', 'headers': []})
    rotate_token(group['id'], request)
    new = load_subscription(group['id'])
    release.set(); assert (await pending)['status'] == 'ok'
    assert new['token'] != group['token']
    assert cache.read_output(group['token'], 'mihomo') is None
    assert cache.read_output(new['token'], 'mihomo') is not None


@pytest.mark.asyncio
async def test_missing_manual_key_preserves_last_good(isolated):
    group = create_subscription(payload(manual_content=URI))
    await refresh.refresh_subscription(group['id'])
    before = cache.read_output(group['token'], 'mihomo')
    key_path = manual.MANUAL_NODE_KEY_PATH
    key_path.rename(key_path.with_suffix('.saved'))
    assert (await refresh.refresh_subscription(group['id']))['status'] == 'stale'
    assert cache.read_output(group['token'], 'mihomo') == before
    assert not key_path.exists()
