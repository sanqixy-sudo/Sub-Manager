from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from threading import RLock

from fastapi import HTTPException
from cryptography import x509

from ..db import db
from ..security import hash_password, token_digest, utcnow_iso
from . import secrets as vault

lock = RLock()
GIB = 1024**3
AGENT_VERSION = '1.0.0'


def now():
    return datetime.now(timezone.utc)


def required(conn, table, ident):
    # table is always a module-owned literal, never request input.
    row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
    if not row:
        raise HTTPException(404, '记录不存在')
    return dict(row)


def _chain_check(conn):
    nodes = {r['id']: dict(r) for r in conn.execute('SELECT * FROM proxy_nodes')}
    for node in nodes.values():
        seen = set()
        current = node
        while current:
            if current['id'] in seen:
                raise HTTPException(400, '链式线路不能形成环路')
            seen.add(current['id'])
            if len(seen) > 8:
                raise HTTPException(400, '链式线路最多 8 跳')
            target = current['next_node_id']
            if target and target not in nodes:
                raise HTTPException(400, '下一跳节点不存在')
            current = nodes.get(target)


def save_server(payload, ident=None):
    with lock, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if ident:
            required(conn, 'proxy_servers', ident)
            conn.execute('UPDATE proxy_servers SET name=?,host=?,enabled=? WHERE id=?',
                         (payload.name, payload.host, int(payload.enabled), ident))
        else:
            ident = conn.execute('INSERT INTO proxy_servers(name,host,enabled,created_at) VALUES(?,?,?,?)',
                                 (payload.name, payload.host, int(payload.enabled), utcnow_iso())).lastrowid
        conn.commit()
    return ident


def pairing(ident):
    token = secrets.token_urlsafe(36)
    with lock, db() as conn:
        required(conn, 'proxy_servers', ident)
        conn.execute('UPDATE proxy_servers SET bootstrap_hash=?,bootstrap_expires=? WHERE id=?',
                     (token_digest(token), (now()+timedelta(minutes=15)).isoformat(), ident))
        conn.commit()
    return token


def bootstrap_server(token):
    with db() as conn:
        row = conn.execute('SELECT id FROM proxy_servers WHERE bootstrap_hash=? AND bootstrap_expires>? AND enabled=1',
                           (token_digest(token), utcnow_iso())).fetchone()
    if not row:
        raise HTTPException(404, '安装链接已失效，请在面板重新生成')
    return int(row[0])


def enroll(payload):
    runtime_token = secrets.token_urlsafe(48)
    with lock, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT id FROM proxy_servers WHERE bootstrap_hash=? AND bootstrap_expires>? AND enabled=1',
                           (token_digest(payload.token), utcnow_iso())).fetchone()
        if not row:
            raise HTTPException(401, '配对令牌无效或已使用')
        conn.execute('''UPDATE proxy_servers SET bootstrap_hash=NULL,bootstrap_expires=NULL,
                     agent_hash=?,instance_id=?,applied_revision=NULL,last_seen=NULL,runtime_status='offline' WHERE id=?''',
                     (token_digest(runtime_token), payload.instance_id, row[0]))
        conn.commit()
    return {'server_id': row[0], 'token': runtime_token, 'poll_seconds': 10}


def agent_server(token):
    with db() as conn:
        row = conn.execute('SELECT * FROM proxy_servers WHERE agent_hash=?', (token_digest(token),)).fetchone()
    if not row:
        raise HTTPException(401, 'Agent 认证失败')
    return dict(row)


def save_node(payload, ident=None):
    with lock, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        required(conn, 'proxy_servers', payload.server_id)
        previous = required(conn, 'proxy_nodes', ident) if ident else None
        if previous and previous['server_id'] != payload.server_id:
            raise HTTPException(400, '已有节点不能迁移服务器，请新建节点')
        if previous and previous['protocol'] != payload.protocol:
            raise HTTPException(400, '已有节点不能切换协议，请新建节点')
        if previous and not payload.certificate_pem:
            if payload.server_name != previous['server_name']:
                raise HTTPException(400, '修改 TLS 名称时请同时上传匹配的新证书与私钥')
            encrypted, fingerprint, expires = previous['secrets'], previous['cert_fingerprint'], previous['cert_expires']
        else:
            try:
                cert, key, fingerprint, expires = vault.certificate(payload.server_name, payload.certificate_pem, payload.private_key_pem)
            except (ValueError, TypeError, x509.ExtensionNotFound):
                raise HTTPException(400, '证书无效、已过期、未覆盖 TLS 名称或与私钥不匹配') from None
            transit = vault.unseal(previous['secrets'])['transit'] if previous else str(uuid.uuid4())
            encrypted = vault.seal({'certificate': cert, 'key': key, 'transit': transit})
        values = (payload.name, payload.protocol, payload.port, payload.server_name, int(payload.enabled),
                  payload.next_node_id, payload.multiplier, encrypted, fingerprint, expires)
        try:
            if previous:
                conn.execute('''UPDATE proxy_nodes SET name=?,protocol=?,port=?,server_name=?,enabled=?,next_node_id=?,
                             multiplier=?,secrets=?,cert_fingerprint=?,cert_expires=? WHERE id=?''', (*values, ident))
            else:
                ident = conn.execute('''INSERT INTO proxy_nodes(name,protocol,port,server_name,enabled,next_node_id,
                                     multiplier,secrets,cert_fingerprint,cert_expires,server_id,created_at)
                                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (*values, payload.server_id, utcnow_iso())).lastrowid
            _chain_check(conn)
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, '服务器端口已被节点占用，或下一跳节点不存在') from None
    return ident


def save_user(payload, ident=None):
    initial = None
    with lock, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if ident:
                previous = required(conn, 'proxy_users', ident)
                password_hash = hash_password(payload.password) if payload.password else previous['password_hash']
                conn.execute('UPDATE proxy_users SET username=?,password_hash=?,enabled=? WHERE id=?',
                             (payload.username, password_hash, int(payload.enabled), ident))
                if payload.password or not payload.enabled:
                    conn.execute('DELETE FROM proxy_sessions WHERE user_id=?', (ident,))
            else:
                initial = payload.password or secrets.token_urlsafe(18)
                token = secrets.token_urlsafe(36)
                ident = conn.execute('''INSERT INTO proxy_users(username,password_hash,enabled,subscription_hash,
                                     subscription_secret,created_at) VALUES(?,?,?,?,?,?)''',
                                     (payload.username, hash_password(initial), int(payload.enabled), token_digest(token),
                                      vault.seal(token), utcnow_iso())).lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, '用户名已存在') from None
    return {'id': ident, 'initial_password': initial}


def _ensure_grants(conn):
    rows = conn.execute('''SELECT a.id AS assignment_id,pn.node_id FROM proxy_assignments a
                         JOIN proxy_package_nodes pn ON pn.package_id=a.package_id
                         LEFT JOIN proxy_grants g ON g.assignment_id=a.id AND g.node_id=pn.node_id WHERE g.id IS NULL''').fetchall()
    for row in rows:
        conn.execute('INSERT INTO proxy_grants(assignment_id,node_id,secret) VALUES(?,?,?)',
                     (row['assignment_id'], row['node_id'], vault.seal(str(uuid.uuid4()))))


def save_package(payload, ident=None):
    with lock, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for node_id in set(payload.node_ids):
            required(conn, 'proxy_nodes', node_id)
        values = (payload.name, round(payload.quota_gb*GIB), payload.cycle_days, payload.duration_days, int(payload.enabled))
        if ident:
            required(conn, 'proxy_packages', ident)
            conn.execute('UPDATE proxy_packages SET name=?,quota_bytes=?,cycle_days=?,duration_days=?,enabled=? WHERE id=?', (*values, ident))
            conn.execute('DELETE FROM proxy_package_nodes WHERE package_id=?', (ident,))
        else:
            ident = conn.execute('INSERT INTO proxy_packages(name,quota_bytes,cycle_days,duration_days,enabled) VALUES(?,?,?,?,?)', values).lastrowid
        conn.executemany('INSERT INTO proxy_package_nodes(package_id,node_id) VALUES(?,?)', [(ident, x) for x in set(payload.node_ids)])
        _ensure_grants(conn)
        conn.commit()
    return ident


def assign_package(payload):
    stamp = now()
    with lock, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        required(conn, 'proxy_users', payload.user_id)
        package = required(conn, 'proxy_packages', payload.package_id)
        ident = conn.execute('''INSERT INTO proxy_assignments(user_id,package_id,expires_at,cycle_start,created_at)
                             VALUES(?,?,?,?,?)''', (payload.user_id, payload.package_id,
                             (stamp+timedelta(days=package['duration_days'])).isoformat(), stamp.isoformat(), stamp.isoformat())).lastrowid
        _ensure_grants(conn)
        conn.commit()
    return ident


def _totals(conn, assignment_id):
    return tuple(conn.execute('SELECT COALESCE(SUM(upload),0),COALESCE(SUM(download),0) FROM proxy_grants WHERE assignment_id=?',
                              (assignment_id,)).fetchone())


def update_assignment(ident, payload):
    with lock, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = required(conn, 'proxy_assignments', ident)
        expiry = datetime.fromisoformat(row['expires_at'])
        if payload.extend_days:
            expiry = max(expiry, now()) + timedelta(days=payload.extend_days)
        enabled = row['enabled'] if payload.enabled is None else int(payload.enabled)
        conn.execute('UPDATE proxy_assignments SET enabled=?,expires_at=? WHERE id=?', (enabled, expiry.isoformat(), ident))
        if payload.reset_traffic:
            up, down = _totals(conn, ident)
            conn.execute('UPDATE proxy_assignments SET baseline_up=?,baseline_down=?,cycle_start=? WHERE id=?', (up, down, utcnow_iso(), ident))
        conn.commit()


def _roll_cycles(conn, stamp):
    for row in conn.execute('SELECT a.*,p.cycle_days FROM proxy_assignments a JOIN proxy_packages p ON p.id=a.package_id').fetchall():
        start = datetime.fromisoformat(row['cycle_start'])
        elapsed = (stamp-start).total_seconds()
        period = row['cycle_days']*86400
        if elapsed >= period:
            up, down = _totals(conn, row['id'])
            start += timedelta(seconds=int(elapsed//period)*period)
            conn.execute('UPDATE proxy_assignments SET baseline_up=?,baseline_down=?,cycle_start=? WHERE id=?', (up, down, start.isoformat(), row['id']))


def _state(conn):
    stamp = now()
    _roll_cycles(conn, stamp)
    servers = {r['id']: dict(r) for r in conn.execute('SELECT * FROM proxy_servers')}
    nodes = {r['id']: dict(r) for r in conn.execute('SELECT * FROM proxy_nodes')}
    assignments = {}
    for row in conn.execute('''SELECT a.*,p.name AS package_name,p.quota_bytes,p.cycle_days,p.enabled AS package_enabled,
                             u.username,u.enabled AS user_enabled FROM proxy_assignments a
                             JOIN proxy_packages p ON p.id=a.package_id JOIN proxy_users u ON u.id=a.user_id'''):
        item = dict(row)
        up, down = _totals(conn, row['id'])
        item.update(upload=max(0, up-row['baseline_up']), download=max(0, down-row['baseline_down']))
        item['status'] = ('disabled' if not (row['enabled'] and row['package_enabled'] and row['user_enabled']) else
                          'expired' if datetime.fromisoformat(row['expires_at']) <= stamp else
                          'exhausted' if row['quota_bytes'] and item['upload']+item['download'] >= row['quota_bytes'] else 'active')
        assignments[item['id']] = item
    grants = [dict(r) for r in conn.execute('''SELECT g.* FROM proxy_grants g JOIN proxy_assignments a ON a.id=g.assignment_id
                        JOIN proxy_package_nodes pn ON pn.package_id=a.package_id AND pn.node_id=g.node_id''')]
    def usable(node_id, seen=None):
        seen = set() if seen is None else seen
        if node_id in seen or node_id not in nodes:
            return False
        seen.add(node_id)
        node = nodes[node_id]
        if not node['enabled'] or not servers[node['server_id']]['enabled'] or datetime.fromisoformat(node['cert_expires']) <= stamp:
            return False
        return not node['next_node_id'] or usable(node['next_node_id'], seen)
    available = {ident for ident in nodes if usable(ident)}
    active_grants = [g for g in grants if assignments[g['assignment_id']]['status'] == 'active' and g['node_id'] in available]
    return servers, nodes, assignments, active_grants, available


def _identity(protocol, name, credential):
    return {'name': name, 'uuid' if protocol == 'vless' else 'password': credential}


def _outbound(node, server, credential, tag):
    secret = vault.unseal(node['secrets'])
    outbound = {'type': node['protocol'], 'tag': tag, 'server': server['host'], 'server_port': node['port'],
                'tls': {'enabled': True, 'server_name': node['server_name'], 'certificate': [secret['certificate']]}}
    outbound['uuid' if node['protocol'] == 'vless' else 'password'] = credential
    return outbound


def configuration(conn, server_id, state=None):
    servers, nodes, assignments, grants, available = state or _state(conn)
    server = servers[server_id]
    inbounds, outbounds, rules, users = [], [{'type': 'direct', 'tag': 'direct'}], [], []
    for node in nodes.values():
        if node['server_id'] != server_id or node['id'] not in available:
            continue
        secret = vault.unseal(node['secrets'])
        node_grants = [g for g in grants if g['node_id'] == node['id']]
        clients = [_identity(node['protocol'], f'g{g["id"]}', vault.unseal(g['secret'])) for g in node_grants]
        users.extend(f'g{g["id"]}' for g in node_grants)
        # Transit accounts exist only when another active node routes here. Never bill them as end users.
        if any(n['id'] in available and n['next_node_id'] == node['id'] for n in nodes.values()):
            clients.append(_identity(node['protocol'], f'transit{node["id"]}', secret['transit']))
        if not clients:
            continue
        tag = f'n{node["id"]}'
        inbounds.append({'type': node['protocol'], 'tag': tag, 'listen': '::', 'listen_port': node['port'],
                         'users': clients, 'tls': {'enabled': True, 'certificate': [secret['certificate']], 'key': [secret['key']]}})
        if node['next_node_id']:
            target = nodes[node['next_node_id']]
            outbound_tag = f'chain{node["id"]}'
            outbounds.append(_outbound(target, servers[target['server_id']], vault.unseal(target['secrets'])['transit'], outbound_tag))
            rules.append({'inbound': [tag], 'action': 'route', 'outbound': outbound_tag})
    config = {'log': {'level': 'fatal'}, 'inbounds': inbounds, 'outbounds': outbounds,
              'route': {'rules': rules, 'final': 'direct'},
              'experimental': {'v2ray_api': {'listen': '127.0.0.1:19091', 'stats': {'enabled': True, 'users': users,
                                             'inbounds': [i['tag'] for i in inbounds]}}}}
    revision = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    expiry = min([now()+timedelta(seconds=120), *[datetime.fromisoformat(assignments[g['assignment_id']]['expires_at'])
                                                             for g in grants if nodes[g['node_id']]['server_id'] == server_id]])
    return {'revision': revision, 'config': config, 'lease_seconds': max(0, min(120, int((expiry-now()).total_seconds()))),
            'poll_seconds': 10, 'agent_version': AGENT_VERSION}


def sync(server, payload):
    with lock, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = required(conn, 'proxy_servers', server['id'])
        if payload.instance_id != current['instance_id'] or current['agent_hash'] != server['agent_hash']:
            raise HTTPException(401, 'Agent 实例已失效，请重新配对')
        # Attribute delayed samples to the old period before advancing the baseline.
        for sample in payload.counters:
            row = conn.execute('''SELECT g.id,n.multiplier FROM proxy_grants g JOIN proxy_nodes n ON n.id=g.node_id
                                WHERE g.id=? AND n.server_id=?''', (sample.grant_id, server['id'])).fetchone()
            if not row:
                raise HTTPException(403, '不能上报其他服务器的授权用量')
            old = conn.execute('''SELECT upload,download FROM proxy_counter_samples
                                 WHERE server_id=? AND instance_id=? AND grant_id=?''',
                                 (server['id'], payload.instance_id, sample.grant_id)).fetchone()
            before_up, before_down = (old['upload'], old['download']) if old else (0, 0)
            up, down = max(before_up, sample.upload), max(before_down, sample.download)
            delta_up = round((up-before_up)*row['multiplier'])
            delta_down = round((down-before_down)*row['multiplier'])
            conn.execute('''INSERT INTO proxy_counter_samples VALUES(?,?,?,?,?) ON CONFLICT(server_id,instance_id,grant_id)
                         DO UPDATE SET upload=excluded.upload,download=excluded.download''',
                         (server['id'], payload.instance_id, sample.grant_id, up, down))
            conn.execute('UPDATE proxy_grants SET upload=upload+?,download=download+? WHERE id=?', (delta_up, delta_down, sample.grant_id))
            conn.execute('''INSERT INTO proxy_traffic_daily(day,grant_id,upload,download) VALUES(?,?,?,?)
                         ON CONFLICT(day,grant_id) DO UPDATE SET upload=upload+excluded.upload,download=download+excluded.download''',
                         (now().date().isoformat(), sample.grant_id, delta_up, delta_down))
        conn.execute('''UPDATE proxy_servers SET last_seen=?,applied_revision=?,runtime_status=?,error_code=?,metrics=? WHERE id=?''',
                     (utcnow_iso(), payload.applied_revision, payload.runtime_status, payload.error_code or None,
                      json.dumps(payload.metrics.model_dump()), server['id']))
        result = configuration(conn, server['id'])
        conn.commit()
    return result


def overview():
    with lock, db() as conn:
        servers, nodes, assignments, grants, available = _state(conn)
        server_items = []
        for server in servers.values():
            fresh = server['last_seen'] and now()-datetime.fromisoformat(server['last_seen']) < timedelta(seconds=45)
            expected = configuration(conn, server['id'], (servers, nodes, assignments, grants, available))['revision']
            server_items.append({k: server[k] for k in ('id','name','host','enabled','last_seen','runtime_status','error_code')}
                                | {'online': bool(fresh), 'synced': server['applied_revision'] == expected,
                                   'metrics': json.loads(server['metrics']), 'paired': bool(server['agent_hash'])})
        node_items = [{k: n[k] for k in ('id','server_id','name','protocol','port','server_name','enabled','next_node_id','multiplier','cert_expires')}
                      | {'available': n['id'] in available} for n in nodes.values()]
        users = [dict(r) for r in conn.execute('SELECT id,username,enabled,created_at FROM proxy_users ORDER BY id')]
        packages = []
        for row in conn.execute('SELECT * FROM proxy_packages'):
            packages.append(dict(row) | {'quota_gb': row['quota_bytes']/GIB, 'node_ids': [r[0] for r in conn.execute(
                'SELECT node_id FROM proxy_package_nodes WHERE package_id=?', (row['id'],))]})
        conn.commit()
    return {'servers': server_items, 'nodes': node_items, 'users': users, 'packages': packages,
            'assignments': list(assignments.values()), 'agent_version': AGENT_VERSION}


def user_links(ident, base):
    with db() as conn:
        user = required(conn, 'proxy_users', ident)
    token = vault.unseal(user['subscription_secret'])
    return {'mihomo': f'{base}/p/{token}/mihomo', 'singbox': f'{base}/p/{token}/singbox', 'portal': f'{base}/#/portal'}


def rotate_subscription(ident):
    token = secrets.token_urlsafe(36)
    with lock, db() as conn:
        required(conn, 'proxy_users', ident)
        conn.execute('UPDATE proxy_users SET subscription_hash=?,subscription_secret=? WHERE id=?',
                     (token_digest(token), vault.seal(token), ident))
        conn.commit()


def user_subscription(token, client_type):
    from ..services.renderer import build_mihomo_document
    from ..services.parser import NormalizedNode, canonical_fingerprint
    with lock, db() as conn:
        user = conn.execute('SELECT id FROM proxy_users WHERE subscription_hash=? AND enabled=1', (token_digest(token),)).fetchone()
        if not user:
            raise HTTPException(404, '订阅不存在')
        servers, nodes, assignments, grants, available = _state(conn)
        selected = [g for g in grants if assignments[g['assignment_id']]['user_id'] == user['id']]
        proxies, outbounds = [], []
        for grant in selected:
            node = nodes[grant['node_id']]
            credential = vault.unseal(grant['secret'])
            name = f'{node["name"]} · {assignments[grant["assignment_id"]]["package_name"]} #{grant["assignment_id"]}.{node["id"]}'
            proxy = {'type': node['protocol'], 'name': name, 'server': servers[node['server_id']]['host'], 'port': node['port'],
                     'tls': True, 'sni' if node['protocol'] == 'anytls' else 'servername': node['server_name'],
                     'fingerprint': node['cert_fingerprint'], 'skip-cert-verify': False,
                     'uuid' if node['protocol'] == 'vless' else 'password': credential, 'udp': True}
            proxies.append(NormalizedNode(name, canonical_fingerprint(proxy), '托管节点', proxy=proxy))
            outbounds.append(_outbound(node, servers[node['server_id']], credential, name))
        conn.commit()
    if client_type == 'singbox':
        # An empty entitlement returns a reject configuration, never a direct fallback.
        routes = {'final': 'PROXY'} if outbounds else {'rules': [{'action': 'reject'}]}
        if outbounds:
            outbounds.insert(0, {'type': 'selector', 'tag': 'PROXY', 'outbounds': [o['tag'] for o in outbounds]})
        return json.dumps({'inbounds': [{'type': 'mixed', 'tag': 'local', 'listen': '127.0.0.1', 'listen_port': 2080}],
                           'outbounds': outbounds, 'route': routes}, ensure_ascii=False).encode(), 'application/json'
    if client_type != 'mihomo':
        raise HTTPException(404, '不支持的客户端格式')
    if not proxies:
        import yaml
        return yaml.safe_dump({'proxies': [], 'proxy-groups': [{'name':'PROXY','type':'select','proxies':['REJECT']}],
                               'rules':['MATCH,PROXY']}).encode(), 'text/plain; charset=utf-8'
    return build_mihomo_document(proxies, 60), 'text/plain; charset=utf-8'
