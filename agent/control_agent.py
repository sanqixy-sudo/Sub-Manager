#!/usr/bin/env python3
"""Pull-only proxy agent. Executes sing-box configs, never remote shell commands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

try:
    from .stats import query
except ImportError:
    from stats import query

VERSION = '1.0.0'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    os.chmod(path, 0o600)


def metrics():
    data = {}
    try:
        data['uptime'] = float(Path('/proc/uptime').read_text().split()[0])
        memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        data['memory_total'] = int(memory['MemTotal'].split()[0])*1024
        data['memory_available'] = int(memory.get('MemAvailable', memory['MemFree']).split()[0])*1024
        data['load'] = os.getloadavg()[0]
        up, down = 0, 0
        for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
            interface, counters = line.split(':', 1)
            if interface.strip() == 'lo':
                continue
            values = counters.split()
            down += int(values[0])
            up += int(values[8])
        data.update(network_upload=up, network_download=down)
    except (OSError, KeyError, ValueError, AttributeError):
        pass
    return data


def post(base, path, payload, token):
    parsed = urlsplit(base)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
        raise ValueError('https_required')
    request = urllib.request.Request(base.rstrip('/')+path, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+token})
    # Never forward the bearer token through a redirect to another origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
        raw = response.read(8*1024*1024+1)
        if len(raw) > 8*1024*1024:
            raise ValueError('response_too_large')
        return json.loads(raw)


class Agent:
    def __init__(self, settings):
        self.settings = settings
        self.directory = Path(settings.get('state_dir', '/var/lib/sub-manager-agent'))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory/'counters.json'
        # A corrupt ledger must never silently reset previously reported totals.
        self.totals = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.previous = {}
        self.process = None
        self.revision = ''
        self.error = ''
        self.deadline = 0
        self.running = True
        self.runtime_lock = threading.RLock()

    def collect(self):
        with self.runtime_lock:
            self._collect_locked()

    def _collect_locked(self):
        if not self.process or self.process.poll() is not None:
            return
        current = query(self.settings.get('stats_address', '127.0.0.1:19091'))
        for ident, counters in current.items():
            key = str(ident)
            total = self.totals.setdefault(key, {'upload': 0, 'download': 0})
            before = self.previous.get(key, {'upload': 0, 'download': 0})
            for direction in ('upload', 'download'):
                value = counters[direction]
                total[direction] += value-before[direction] if value >= before[direction] else value
            self.previous[key] = counters
        atomic_json(self.state_path, self.totals)

    def stop(self):
        with self.runtime_lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
            self.previous = {}

    def _start(self, path):
        with self.runtime_lock:
            if time.monotonic() >= self.deadline:
                return False
            self.process = subprocess.Popen([self.settings['binary'], 'run', '-c', str(path)],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            stable = 0
            for _ in range(30):
                if self.process.poll() is not None or time.monotonic() >= self.deadline:
                    return False
                try:
                    query(self.settings.get('stats_address', '127.0.0.1:19091'))
                    stable += 1
                    if stable >= 5:
                        return True
                except Exception:
                    stable = 0
                time.sleep(0.2)
            return False

    def apply(self, desired):
        config = desired['config']
        import hashlib
        if hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest() != desired['revision']:
            raise ValueError('invalid_revision')
        if desired['revision'] == self.revision and (not config['inbounds'] or (self.process and self.process.poll() is None)):
            return
        pending = self.directory/'pending.json'
        active = self.directory/'active.json'
        atomic_json(pending, config)
        result = subprocess.run([self.settings['binary'], 'check', '-c', str(pending)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode:
            self.error = 'config_invalid'
            return
        self.collect()
        old = active.read_bytes() if active.exists() else None
        self.stop()
        os.replace(pending, active)
        if config['inbounds'] and not self._start(active):
            self.stop()
            self.error = 'start_failed'
            if old:
                # Restore both disk state and last-good runtime on a failed bind/start.
                atomic_json(active, json.loads(old))
                if json.loads(old)['inbounds']:
                    self._start(active)
            return
        self.revision = desired['revision']
        self.error = ''

    def step(self):
        try:
            self.collect()
        except Exception:
            self.error = 'stats_unavailable'
            self.stop()
        payload = {'instance_id': self.settings['instance_id'], 'applied_revision': self.revision,
                   'runtime_status': 'error' if self.error else 'running' if self.process and self.process.poll() is None else 'stopped',
                   'error_code': self.error, 'agent_version': VERSION, 'metrics': metrics(),
                   'counters': [dict(grant_id=int(key), **value) for key, value in self.totals.items()]}
        desired = post(self.settings['base'], '/api/agent/sync', payload, self.settings['token'])
        self.deadline = time.monotonic()+max(0, min(120, desired['lease_seconds']))
        self.apply(desired)

    def run(self):
        def watchdog():
            while self.running:
                if self.deadline and time.monotonic() >= self.deadline:
                    self.stop()
                    self.error = 'lease_expired'
                time.sleep(0.25)
        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()
        try:
            while self.running:
                try:
                    self.step()
                except Exception:
                    # Do not log URLs, tokens, configs, or the contents of network exceptions.
                    print('control_sync_failed', flush=True)
                if time.monotonic() >= self.deadline:
                    try:
                        self.collect()
                    except Exception:
                        pass
                    self.stop()
                    self.error = 'lease_expired'
                for _ in range(10):
                    if not self.running:
                        break
                    time.sleep(1)
        finally:
            self.running = False
            try:
                self.collect()
            except Exception:
                pass
            self.stop()
            watcher.join(timeout=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/sub-manager-agent/config.json')
    args = parser.parse_args()
    settings = json.loads(Path(args.config).read_text())
    agent = Agent(settings)
    def shutdown(*_):
        agent.running = False
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    agent.run()


if __name__ == '__main__':
    main()
