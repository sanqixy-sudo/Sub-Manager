"""Real AnyTLS transports and StatsService; skipped unless SING_BOX_BINARY is provided."""
from __future__ import annotations

import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.request

import pytest

from agent import control_agent
from agent.stats import decode_counters, query
from app.control import service
from app.control.models import AssignmentIn, AssignmentUpdate, NodeIn, PackageIn, ServerIn, UserIn
from tests.test_proxy_control import control, setup_access


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_port(port, process):
    until = time.monotonic()+8
    while time.monotonic() < until:
        assert process.poll() is None, 'sing-box exited before listening'
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail('sing-box did not open the expected local port')


def start_core(binary, directory, config):
    path = directory/'core.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding='utf-8')
    checked = subprocess.run([binary, 'check', '-c', str(path)], capture_output=True, timeout=15)
    assert checked.returncode == 0, 'Generated configuration rejected by the real core: ' + checked.stderr.decode(errors='replace')[:200]
    return subprocess.Popen([binary, 'run', '-c', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_via(port, target):
    # Explicit proxy avoids system NO_PROXY silently bypassing the real transport.
    with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
        from urllib.parse import urlsplit
        sock.sendall(f'GET {target} HTTP/1.1\r\nHost: {urlsplit(target).netloc}\r\nConnection: close\r\n\r\n'.encode())
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                if chunks:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
    return b''.join(chunks)


@pytest.mark.parametrize('protocol', ['anytls', 'vless', 'trojan'])
def test_real_proxy_chain_multiuser_and_revoke(control, monkeypatch, protocol):
    binary = os.environ.get('SING_BOX_BINARY')
    if not binary:
        pytest.skip('Set SING_BOX_BINARY to a verified sing-box 1.14.4 executable')
    sid, nid, uid, pid, aid = setup_access(control, port=free_port())
    second_user = service.save_user(UserIn(username='bob', password='test-bob-password'))['id']
    service.assign_package(AssignmentIn(user_id=second_user, package_id=pid))
    exit_server = service.save_server(ServerIn(name='Exit',host='127.0.0.1'))
    exit_port, entry_stats, exit_stats, local_port = free_port(), free_port(), free_port(), free_port()
    exit_node = service.save_node(NodeIn(server_id=exit_server,name='Exit',protocol=protocol,port=exit_port,server_name='localhost'))
    from app.db import db
    with db() as conn:
        entry_row = service.required(conn,'proxy_nodes',nid)
    service.save_node(NodeIn(server_id=sid,name='AnyTLS entry',port=entry_row['port'],server_name='localhost',next_node_id=exit_node), nid)
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'proxy-chain-verified-'*2048)
        def log_message(self, *args):
            pass
    origin = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=origin.serve_forever,daemon=True); thread.start()
    processes = []
    try:
        with db() as conn:
            entry = service.configuration(conn,sid)['config']
            exit_config = service.configuration(conn,exit_server)['config']
            gids = [r[0] for r in conn.execute('SELECT id FROM proxy_grants ORDER BY id')]
        entry['experimental']['v2ray_api']['listen'] = f'127.0.0.1:{entry_stats}'
        exit_config['experimental']['v2ray_api']['listen'] = f'127.0.0.1:{exit_stats}'
        processes.append(start_core(binary,control/'exit',exit_config)); wait_port(exit_port,processes[-1])
        entry_process = start_core(binary,control/'entry',entry); processes.append(entry_process); wait_port(entry_row['port'],entry_process)
        token = service.user_links(uid,'')['singbox'].split('/')[2]
        client = json.loads(service.user_subscription(token,'singbox')[0])
        client['inbounds'][0]['listen_port'] = local_port
        processes.append(start_core(binary,control/'client',client)); wait_port(local_port,processes[-1])
        result = get_via(local_port,f'http://127.0.0.1:{origin.server_port}/fixture')
        assert b'200 OK' in result and b'proxy-chain-verified' in result
        counters = query(f'127.0.0.1:{entry_stats}')
        assert counters[gids[0]]['download'] > 40000
        assert gids[1] not in counters  # Bob was provisioned but used no traffic.
        assert query(f'127.0.0.1:{exit_stats}') == {}  # Transit is not billed again.
        # Feed the real core through Agent's persistence layer and verify no duplicate collection.
        agent = control_agent.Agent({'state_dir':str(control/'agent'), 'binary':binary, 'stats_address':f'127.0.0.1:{entry_stats}'})
        agent.process = entry_process
        agent.collect(); first_total = json.loads(json.dumps(agent.totals)); agent.collect()
        assert agent.totals == first_total
        restored = control_agent.Agent(agent.settings)
        assert restored.totals == first_total
        # Revoke Alice and apply a fresh real-core config; her old subscription credentials must fail.
        service.update_assignment(aid,AssignmentUpdate(enabled=False))
        entry_process.terminate(); entry_process.wait(timeout=5)
        with db() as conn:
            revoked = service.configuration(conn,sid)['config']
        revoked['experimental']['v2ray_api']['listen'] = f'127.0.0.1:{entry_stats}'
        processes.append(start_core(binary,control/'revoked',revoked)); wait_port(entry_row['port'],processes[-1])
        # Restart client to force a new TLS authentication rather than reuse an old session.
        processes[2].terminate(); processes[2].wait(timeout=5)
        processes.append(start_core(binary,control/'client-reconnect',client)); wait_port(local_port,processes[-1])
        try:
            denied = get_via(local_port,f'http://127.0.0.1:{origin.server_port}/fixture')
        except (OSError, TimeoutError):
            denied = b''
        assert b'proxy-chain-verified' not in denied
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=5)
        origin.shutdown(); origin.server_close()


def test_stats_parser_rejects_truncation_and_oversize():
    with pytest.raises(ValueError):
        decode_counters(b'\x0a\xff\xff')


def test_agent_rejects_corrupted_config_revision(tmp_path):
    agent = control_agent.Agent({'state_dir':str(tmp_path),'binary':'must-not-be-executed'})
    with pytest.raises(ValueError):
        agent.apply({'revision':'wrong','config':{'inbounds':[]}})


def test_real_agent_restores_last_good_after_port_conflict(control):
    binary = os.environ.get('SING_BOX_BINARY')
    if not binary:
        pytest.skip('Set SING_BOX_BINARY for real agent runtime validation')
    sid, *_ = setup_access(control,port=free_port())
    from app.db import db
    with db() as conn:
        desired = service.configuration(conn,sid)
    desired['config']['experimental']['v2ray_api']['listen'] = f'127.0.0.1:{free_port()}'
    desired['revision'] = hashlib.sha256(json.dumps(desired['config'],sort_keys=True).encode()).hexdigest()
    settings = {'state_dir':str(control/'agent-rollback'),'binary':binary,
                'stats_address':desired['config']['experimental']['v2ray_api']['listen']}
    agent = control_agent.Agent(settings)
    agent.deadline = time.monotonic()+60
    blocker = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
        blocker.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
    blocker.bind(('127.0.0.1',0)); blocker.listen()
    try:
        agent.apply(desired)
        assert agent.process and agent.process.poll() is None and agent.revision == desired['revision']
        broken = json.loads(json.dumps(desired))
        broken['config']['inbounds'][0]['listen'] = '127.0.0.1'
        broken['config']['inbounds'][0]['listen_port'] = blocker.getsockname()[1]
        broken['revision'] = hashlib.sha256(json.dumps(broken['config'],sort_keys=True).encode()).hexdigest()
        agent.apply(broken)
        assert agent.error == 'start_failed' and agent.revision == desired['revision']
        assert agent.process and agent.process.poll() is None
        assert json.loads((agent.directory/'active.json').read_text()) == desired['config']
    finally:
        agent.stop(); blocker.close()


def test_real_mihomo_anytls_certificate_pin(control):
    core, mihomo = os.environ.get('SING_BOX_BINARY'), os.environ.get('MIHOMO_BINARY')
    if not core or not mihomo:
        pytest.skip('Set SING_BOX_BINARY and MIHOMO_BINARY for client interoperability')
    import yaml
    from app.db import db
    sid, nid, uid, *_ = setup_access(control,port=free_port())
    with db() as conn:
        desired = service.configuration(conn,sid)['config']
    desired['experimental']['v2ray_api']['listen'] = f'127.0.0.1:{free_port()}'
    token = service.user_links(uid,'')['mihomo'].split('/')[2]
    document = yaml.safe_load(service.user_subscription(token,'mihomo')[0])
    local_port = free_port()
    document.update({'mixed-port':local_port,'rules':['MATCH,PROXY'],'log-level':'debug',
                     'dns':{'enable':False},'external-controller':'',
                     'proxy-groups':[{'name':'PROXY','type':'select','proxies':[document['proxies'][0]['name']]}]})
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'mihomo-anytls-pin-verified')
        def log_message(self,*args): pass
    origin = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=origin.serve_forever,daemon=True).start()
    processes = []
    try:
        processes.append(start_core(core,control/'server',desired))
        wait_port(desired['inbounds'][0]['listen_port'],processes[-1])
        client_dir=control/'mihomo'; client_dir.mkdir()
        config_file=client_dir/'config.yaml'; config_file.write_text(yaml.safe_dump(document,allow_unicode=True),encoding='utf-8')
        log_path = client_dir/'test.log'
        with log_path.open('wb') as log:
            processes.append(subprocess.Popen([mihomo,'-d',str(client_dir),'-f',str(config_file)],stdout=log,stderr=log))
        wait_port(local_port,processes[-1])
        response = get_via(local_port,f'http://127.0.0.1:{origin.server_port}/')
        assert b'mihomo-anytls-pin-verified' in response, log_path.read_text(encoding='utf-8',errors='replace')[-1800:]
        # A wrong pinned certificate must reject the same server.
        processes[-1].terminate(); processes[-1].wait(timeout=5)
        document['proxies'][0]['fingerprint']='0'*64
        config_file.write_text(yaml.safe_dump(document,allow_unicode=True),encoding='utf-8')
        processes.append(subprocess.Popen([mihomo,'-d',str(client_dir),'-f',str(config_file)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        wait_port(local_port,processes[-1])
        try: denied=get_via(local_port,f'http://127.0.0.1:{origin.server_port}/')
        except (OSError,TimeoutError): denied=b''
        assert b'mihomo-anytls-pin-verified' not in denied
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=5)
        origin.shutdown(); origin.server_close()
