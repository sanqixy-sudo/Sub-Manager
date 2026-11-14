from __future__ import annotations

import json
import uuid
from datetime import timedelta

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
import yaml

import app.config as config
import app.db as database
from app.control import service, secrets as vault
from app.control.models import AgentSync, AssignmentIn, AssignmentUpdate, EnrollmentIn, NodeIn, PackageIn, ServerIn, UserIn
from app.main import app


@pytest.fixture
def control(tmp_path, monkeypatch):
    for name, path in {'DB_PATH':tmp_path/'db.sqlite', 'DATA_DIR':tmp_path, 'CACHE_DIR':tmp_path/'cache',
                      'UPSTREAM_CACHE_DIR':tmp_path/'upstreams', 'BACKUP_DIR':tmp_path/'backups', 'SECRETS_DIR':tmp_path/'secrets'}.items():
        monkeypatch.setattr(database, name, path)
    monkeypatch.setattr(config, 'SECRETS_DIR', tmp_path/'secrets')
    database.init_db()
    database.set_settings({'scheduler_enabled':'0', 'health_check_enabled':'0', 'public_base_url':'https://panel.example.test'})
    return tmp_path


def setup_access(control, *, server_id=None, node_name='AnyTLS', protocol='anytls', port=34443, next_node_id=None):
    sid = server_id or service.save_server(ServerIn(name='Server', host='127.0.0.1'))
    nid = service.save_node(NodeIn(server_id=sid, name=node_name, protocol=protocol, port=port,
                                  server_name='localhost', next_node_id=next_node_id))
    uid = service.save_user(UserIn(username='alice', password='example-password'))['id']
    pid = service.save_package(PackageIn(name='Standard', quota_gb=1, node_ids=[nid]))
    aid = service.assign_package(AssignmentIn(user_id=uid, package_id=pid))
    return sid, nid, uid, pid, aid


def enrolled(sid):
    instance = uuid.uuid4().hex
    pair = service.pairing(sid)
    result = service.enroll(EnrollmentIn(token=pair, instance_id=instance))
    return service.agent_server(result['token']), instance, result['token']


def test_pairing_one_use_and_distinct_auth_domains(control):
    sid = service.save_server(ServerIn(name='A', host='server.example.test'))
    pair = service.pairing(sid)
    payload = EnrollmentIn(token=pair, instance_id=uuid.uuid4().hex)
    result = service.enroll(payload)
    with pytest.raises(HTTPException) as error:
        service.enroll(payload)
    assert error.value.status_code == 401
    with pytest.raises(HTTPException):
        service.agent_server(pair)
    assert service.agent_server(result['token'])['id'] == sid
    with database.db() as conn:
        raw = conn.execute('SELECT agent_hash,bootstrap_hash FROM proxy_servers').fetchone()
    assert result['token'] not in str(tuple(raw)) and raw['bootstrap_hash'] is None
    _, _, fresh = enrolled(sid)
    assert fresh != result['token']
    with pytest.raises(HTTPException):
        service.agent_server(result['token'])


def test_unique_passwords_encrypted_and_native_subscriptions(control):
    sid, nid, uid, pid, aid = setup_access(control)
    service.assign_package(AssignmentIn(user_id=uid, package_id=pid))
    with database.db() as conn:
        desired = service.configuration(conn, sid)
        rows = conn.execute('SELECT secret FROM proxy_grants').fetchall()
    clients = desired['config']['inbounds'][0]['users']
    assert len(clients) == 2 and clients[0]['password'] != clients[1]['password']
    assert all(c['password'] not in str(rows) for c in clients)
    snapshot = json.dumps(service.overview())
    assert all(c['password'] not in snapshot for c in clients)
    assert 'private_key' not in snapshot and 'BEGIN PRIVATE KEY' not in snapshot
    token = service.user_links(uid, '')['mihomo'].split('/')[2]
    body, _ = service.user_subscription(token, 'mihomo')
    proxies = yaml.safe_load(body)['proxies']
    assert len(proxies) == 2 and all(p['fingerprint'] and not p['skip-cert-verify'] for p in proxies)
    singbox = json.loads(service.user_subscription(token, 'singbox')[0])
    assert len(singbox['outbounds']) == 3 and 'certificate' in singbox['outbounds'][1]['tls']


def test_chain_uses_transit_account_and_rejects_cycle(control):
    sid, first, uid, pid, aid = setup_access(control)
    sid2 = service.save_server(ServerIn(name='Exit', host='exit.example.test'))
    last = service.save_node(NodeIn(server_id=sid2, name='Exit', protocol='trojan', port=443, server_name='exit.example.test'))
    service.save_node(NodeIn(server_id=sid, name='Entry', port=34443, server_name='localhost', next_node_id=last), first)
    with database.db() as conn:
        entry = service.configuration(conn, sid)['config']
        exit_config = service.configuration(conn, sid2)['config']
    assert entry['route']['rules'][0]['outbound'] == f'chain{first}'
    assert entry['outbounds'][1]['password'] == exit_config['inbounds'][0]['users'][0]['password']
    assert exit_config['inbounds'][0]['users'][0]['name'].startswith('transit')
    assert not exit_config['experimental']['v2ray_api']['stats']['users']
    with pytest.raises(HTTPException) as error:
        service.save_node(NodeIn(server_id=sid2, name='Exit', protocol='trojan', port=443,
                                 server_name='exit.example.test', next_node_id=first), last)
    assert error.value.status_code == 400
    # Stopping a downstream node must not silently turn the entry into direct egress.
    service.save_node(NodeIn(server_id=sid2, name='Exit', protocol='trojan', port=443,
                             server_name='exit.example.test', enabled=False), last)
    with database.db() as conn:
        assert not service.configuration(conn, sid)['config']['inbounds']


def test_traffic_idempotent_out_of_order_and_quota_enforced(control):
    sid, nid, uid, pid, aid = setup_access(control)
    server, instance, _ = enrolled(sid)
    with database.db() as conn:
        gid = conn.execute('SELECT id FROM proxy_grants').fetchone()[0]
    def report(up, down):
        return service.sync(server, AgentSync(instance_id=instance, counters=[{'grant_id':gid,'upload':up,'download':down}]))
    report(100, 200)
    report(100, 200)
    report(50, 150)
    row = service.overview()['assignments'][0]
    assert (row['upload'], row['download']) == (100, 200)
    result = report(service.GIB, 200)
    assert not result['config']['inbounds']
    assert service.overview()['assignments'][0]['status'] == 'exhausted'
    service.update_assignment(aid, AssignmentUpdate(reset_traffic=True))
    assert report(service.GIB, 200)['config']['inbounds']
    row = service.overview()['assignments'][0]
    assert row['upload'] == row['download'] == 0


def test_server_cannot_forge_other_server_traffic(control):
    sid, *_ = setup_access(control)
    second = service.save_server(ServerIn(name='Other',host='other.example.test'))
    server, instance, _ = enrolled(second)
    with database.db() as conn:
        gid = conn.execute('SELECT id FROM proxy_grants').fetchone()[0]
    with pytest.raises(HTTPException) as error:
        service.sync(server, AgentSync(instance_id=instance,counters=[{'grant_id':gid,'upload':999,'download':0}]))
    assert error.value.status_code == 403
    assert service.overview()['assignments'][0]['upload'] == 0


def test_empty_package_and_template_removal_revoke_entitlements(control):
    sid, nid, uid, pid, aid = setup_access(control)
    service.save_package(PackageIn(name='Standard',node_ids=[]), pid)
    with database.db() as conn:
        assert not service.configuration(conn, sid)['config']['inbounds']
    service.save_package(PackageIn(name='Standard',node_ids=[nid]), pid)
    with database.db() as conn:
        assert len(conn.execute('SELECT * FROM proxy_grants').fetchall()) == 1
        assert service.configuration(conn, sid)['config']['inbounds']


def test_expiry_cycle_reset_and_renewal_keep_credentials(control):
    sid, nid, uid, pid, aid = setup_access(control)
    with database.db() as conn:
        secret = conn.execute('SELECT secret FROM proxy_grants').fetchone()[0]
        conn.execute('UPDATE proxy_grants SET upload=500,download=1000')
        conn.execute('UPDATE proxy_assignments SET expires_at=?,cycle_start=?',
                     ((service.now()-timedelta(days=1)).isoformat(), (service.now()-timedelta(days=31)).isoformat()))
        conn.commit()
    row = service.overview()['assignments'][0]
    assert row['status'] == 'expired' and row['upload'] == row['download'] == 0
    service.update_assignment(aid, AssignmentUpdate(extend_days=30))
    assert service.overview()['assignments'][0]['status'] == 'active'
    with database.db() as conn:
        assert conn.execute('SELECT secret FROM proxy_grants').fetchone()[0] == secret


def test_port_conflicts_and_reserve_statistics_port(control):
    sid, *_ = setup_access(control)
    with pytest.raises(HTTPException) as error:
        service.save_node(NodeIn(server_id=sid,name='Conflict',port=34443,server_name='localhost'))
    assert error.value.status_code == 409
    with pytest.raises(ValueError):
        NodeIn(server_id=sid,name='Reserved',port=19091,server_name='localhost')


def test_missing_key_never_replaced_and_invalid_certificate_is_atomic(control):
    sid, *_ = setup_access(control)
    (config.SECRETS_DIR/'control.key').unlink()
    with pytest.raises(HTTPException) as error:
        service.save_user(UserIn(username='bob'))
    assert error.value.status_code == 503 and not (config.SECRETS_DIR/'control.key').exists()
    with pytest.raises(HTTPException):
        service.save_node(NodeIn(server_id=sid,name='Bad',port=34444,server_name='localhost',
                                 certificate_pem='invalid',private_key_pem='invalid'))
    with database.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM proxy_nodes').fetchone()[0] == 1


def test_portal_cannot_access_admin_or_other_user_entitlements(control):
    sid, nid, uid, pid, aid = setup_access(control)
    service.save_user(UserIn(username='bob',password='bob-password'))
    with TestClient(app) as client:
        assert client.get('/api/control/overview').status_code == 401
        assert client.post('/api/portal/login',json={'username':'bob','password':'bob-password'}).status_code == 200
        data = client.get('/api/portal/me').json()
        assert not data['assignments'] and not data['nodes'] and data['username'] == 'bob'
        assert client.get('/api/control/overview').status_code == 401
        assert client.post('/api/agent/sync',json={'instance_id':uuid.uuid4().hex}).status_code == 401
        assert client.post('/api/portal/logout').status_code == 200
        assert client.get('/api/portal/me').status_code == 401


def test_admin_install_flow_and_subscription_revocation(control):
    sid, nid, uid, pid, aid = setup_access(control)
    with TestClient(app) as client:
        client.post('/api/auth/login',json={'username':'admin','password':'admin'})
        result = client.post(f'/api/control/servers/{sid}/pairing').json()
        token = result['command'].split('/install/')[1].split()[0]
        script = client.get('/api/agent/install/'+token).text
        assert 'core-manifest' in script and '@BASE@' not in script and '@CONTROL_AGENT' not in script
        assert 'set -x' not in script
        data = client.post(f'/api/control/users/{uid}/links').json()
        assert client.get(data['mihomo']).status_code == 200
        assert client.post(f'/api/control/users/{uid}/rotate-subscription').status_code == 200
        assert client.get(data['mihomo']).status_code == 404
        listing = client.get('/api/control/overview').text
        assert token not in listing and 'BEGIN PRIVATE KEY' not in listing


def test_migration_preserves_v11_subscriptions(control):
    with database.db() as conn:
        conn.execute('PRAGMA user_version=11')
        conn.execute("INSERT INTO subscriptions(name,token,created_at,updated_at) VALUES('Legacy','legacy-token','2026-01-01','2026-01-01')")
        conn.commit()
    database.init_db()
    with database.db() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 12
        assert conn.execute('SELECT token FROM subscriptions').fetchone()[0] == 'legacy-token'
    assert list((control/'backups').glob('submanager-v11-*'))
