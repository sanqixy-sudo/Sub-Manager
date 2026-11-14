from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from ..api.auth import _attempts, _client_key
from ..api.deps import require_auth
from ..db import db, get_setting
from ..schemas import LoginPayload
from ..security import new_token, token_digest, verify_password
from . import service
from .models import AgentSync, AssignmentIn, AssignmentUpdate, EnrollmentIn, NodeIn, PackageIn, ServerIn, UserIn

admin = APIRouter(prefix='/api/control', dependencies=[Depends(require_auth)], tags=['proxy-control'])
agent = APIRouter(prefix='/api/agent', tags=['agent'])
portal = APIRouter(prefix='/api/portal', tags=['user-portal'])
public = APIRouter()
COOKIE = 'sm_proxy_session'


def _base(request):
    return (get_setting('public_base_url') or str(request.base_url)).rstrip('/')


def _https_base(request):
    from urllib.parse import urlsplit
    base = _base(request)
    if urlsplit(base).scheme != 'https':
        raise HTTPException(400, '请先在系统设置配置 HTTPS 公开访问地址，再生成安装命令')
    return base


@admin.get('/overview')
def overview():
    return service.overview()


@admin.post('/servers')
def create_server(payload: ServerIn):
    return {'id': service.save_server(payload)}


@admin.put('/servers/{ident}')
def update_server(ident: int, payload: ServerIn):
    return {'id': service.save_server(payload, ident)}


@admin.post('/servers/{ident}/pairing')
def pairing(ident: int, request: Request):
    import shlex
    base = _https_base(request)
    token = service.pairing(ident)
    return {'command': f'curl -fsSL {shlex.quote(base+"/api/agent/install/"+token)} | sudo bash', 'expires_minutes': 15,
            'replaces_agent_on_enrollment': True}


@admin.post('/nodes')
def create_node(payload: NodeIn):
    return {'id': service.save_node(payload)}


@admin.put('/nodes/{ident}')
def update_node(ident: int, payload: NodeIn):
    return {'id': service.save_node(payload, ident)}


@admin.post('/users')
def create_user(payload: UserIn):
    return service.save_user(payload)


@admin.put('/users/{ident}')
def update_user(ident: int, payload: UserIn):
    return service.save_user(payload, ident)


@admin.post('/users/{ident}/links')
def links(ident: int, request: Request):
    return service.user_links(ident, _base(request))


@admin.post('/users/{ident}/rotate-subscription')
def rotate(ident: int):
    service.rotate_subscription(ident)
    return {'ok': True}


@admin.post('/packages')
def create_package(payload: PackageIn):
    return {'id': service.save_package(payload)}


@admin.put('/packages/{ident}')
def update_package(ident: int, payload: PackageIn):
    return {'id': service.save_package(payload, ident)}


@admin.post('/assignments')
def assign(payload: AssignmentIn):
    return {'id': service.assign_package(payload)}


@admin.patch('/assignments/{ident}')
def update_assignment(ident: int, payload: AssignmentUpdate):
    service.update_assignment(ident, payload)
    return {'ok': True}


@agent.post('/enroll')
def enroll(payload: EnrollmentIn):
    return service.enroll(payload)


@agent.post('/sync')
def sync(payload: AgentSync, request: Request):
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer '):
        raise HTTPException(401, 'Agent 认证失败')
    return service.sync(service.agent_server(header[7:]), payload)


@agent.get('/install/{token}', include_in_schema=False)
def install(token: str, request: Request):
    from .installer import render_installer
    service.bootstrap_server(token)
    return Response(render_installer(_https_base(request), token), media_type='text/x-shellscript',
                    headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@agent.get('/files/{name}', include_in_schema=False)
def agent_file(name: str):
    if name not in {'control_agent.py', 'stats.py'}:
        raise HTTPException(404)
    path = Path(__file__).resolve().parents[2] / 'agent' / name
    return Response(path.read_bytes(), media_type='text/plain', headers={'Cache-Control': 'no-cache'})


@agent.get('/core-manifest', include_in_schema=False)
def core_manifest():
    import hashlib
    directory = Path(__file__).resolve().parents[2] / 'agent/bin'
    result = {}
    for arch in ('amd64','arm64'):
        path = directory/f'sing-box-linux-{arch}'
        if not path.is_file():
            raise HTTPException(503, '托管内核尚未构建，请构建新版 Docker 镜像或执行 scripts/build_proxy_core.py')
        result[arch] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@agent.get('/core/{arch}', include_in_schema=False)
def core_binary(arch: str):
    if arch not in {'amd64','arm64'}:
        raise HTTPException(404)
    path = Path(__file__).resolve().parents[2] / 'agent/bin' / f'sing-box-linux-{arch}'
    if not path.is_file():
        raise HTTPException(503, '托管内核尚未构建')
    return FileResponse(path, media_type='application/octet-stream')


@agent.get('/upstream/{name}', include_in_schema=False)
def upstream_source(name: str):
    if name not in {'sing-box-LICENSE','sing-box-source.zip'}:
        raise HTTPException(404)
    path = Path(__file__).resolve().parents[2] / 'agent/bin' / name
    if not path.is_file():
        raise HTTPException(503, '源码文件尚未构建')
    return FileResponse(path)


def portal_user(request: Request):
    token = request.cookies.get(COOKIE, '')
    with db() as conn:
        row = conn.execute('''SELECT u.id,u.username FROM proxy_sessions s JOIN proxy_users u ON u.id=s.user_id
                           WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1''',
                           (token_digest(token), service.now().isoformat())).fetchone()
    if not row:
        raise HTTPException(401, '请登录用户中心')
    return dict(row)


@portal.post('/login')
def login(payload: LoginPayload, request: Request, response: Response):
    import time
    key = 'portal:' + _client_key(request)
    attempts = _attempts[key]
    stamp = time.monotonic()
    while attempts and attempts[0] < stamp-600:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(429, '登录失败次数过多，请稍后再试')
    with db() as conn:
        user = conn.execute('SELECT * FROM proxy_users WHERE username=?', (payload.username,)).fetchone()
        valid, _ = verify_password(payload.password, user['password_hash'] if user else get_setting('admin_password_hash'))
        if not user or not valid or not user['enabled']:
            attempts.append(stamp)
            raise HTTPException(401, '用户名或密码错误')
        attempts.clear()
        token = new_token(36)
        conn.execute('DELETE FROM proxy_sessions WHERE expires_at<=?', (service.now().isoformat(),))
        conn.execute('INSERT INTO proxy_sessions VALUES(?,?,?)',
                     (token_digest(token), user['id'], (service.now()+timedelta(days=7)).isoformat()))
        conn.commit()
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == 'https', samesite='strict', max_age=604800, path='/')
    return {'ok': True}


@portal.post('/logout')
def logout(request: Request, response: Response):
    with db() as conn:
        conn.execute('DELETE FROM proxy_sessions WHERE token_hash=?', (token_digest(request.cookies.get(COOKIE, '')),))
        conn.commit()
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


@portal.get('/me')
def me(request: Request, user=Depends(portal_user)):
    with service.lock, db() as conn:
        servers, nodes, assignments, grants, available = service._state(conn)
        items = [a for a in assignments.values() if a['user_id'] == user['id']]
        visible = [{'id': n['id'], 'name': n['name'], 'protocol': n['protocol'], 'available': n['id'] in available}
                   for n in nodes.values() if any(g['node_id'] == n['id'] and assignments[g['assignment_id']]['user_id'] == user['id'] for g in grants)]
        history = [dict(r) for r in conn.execute('''SELECT d.day,SUM(d.upload) AS upload,SUM(d.download) AS download
                    FROM proxy_traffic_daily d JOIN proxy_grants g ON g.id=d.grant_id
                    JOIN proxy_assignments a ON a.id=g.assignment_id WHERE a.user_id=?
                    GROUP BY d.day ORDER BY d.day DESC LIMIT 30''', (user['id'],))]
        conn.commit()
    return {**user, 'assignments': items, 'nodes': visible, 'history': history, 'links': service.user_links(user['id'], _base(request))}


@public.get('/p/{token}/{client_type}', include_in_schema=False)
def user_subscription(token: str, client_type: str):
    body, media = service.user_subscription(token, client_type)
    return Response(body, media_type=media, headers={'Cache-Control': 'no-store', 'Profile-Update-Interval': '1'})
