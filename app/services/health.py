from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import socket
import tempfile
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from fastapi import HTTPException

from ..config import HEALTH_CONNECTIVITY_URL, HEALTH_GOOGLE_URL, MIHOMO_BINARY, MIHOMO_VERSION
from ..db import db, get_setting
from ..repository import list_subscriptions, load_subscription, parse_iso
from ..security import utcnow_iso, redact
from .cache import upstream_paths
from .manual_nodes import load_manual_nodes, node_key_for, resolve_node_key, node_key_resolver
from .parser import NormalizedNode, ParseResult, merge_nodes, parse_subscription


_task: asyncio.Task[None] | None = None
_task_scope: tuple[Any, ...] | None = None
_task_run_id: int | None = None
_lock = asyncio.Lock()
_scheduler_last_poll: str | None = None
_scheduler_next_run: str | None = None
_kernel_verified = False
_kernel_version: str | None = None


async def verify_mihomo() -> None:
    global _kernel_verified, _kernel_version
    _kernel_verified, _kernel_version = False, None
    if not Path(MIHOMO_BINARY).exists(): return
    process = await asyncio.create_subprocess_exec(MIHOMO_BINARY, '-v', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        raw = await asyncio.wait_for(process.stdout.read(512), 3)
        await asyncio.wait_for(process.wait(), 3)
        match = re.search(r'Mihomo[^\r\n]*?v?(\d+\.\d+\.\d+)', raw.decode('utf-8', 'replace'), re.I)
        _kernel_version = match.group(1) if match else None
        _kernel_verified = process.returncode == 0 and _kernel_version == MIHOMO_VERSION
    except Exception:
        pass
    finally:
        if process.returncode is None:
            process.kill(); await process.wait()


def seed_node_identities() -> None:
    # Read old last-good BEFORE fetching a changed source format on upgrade.
    with db() as conn:
        ids = [int(r[0]) for r in conn.execute('SELECT id FROM subscriptions')]
    for sid in ids:
        for node in _load_group_nodes(sid):
            if not node.fingerprint: continue
            with db() as conn:
                conn.execute('INSERT OR IGNORE INTO node_identities(subscription_id,canonical_key,node_key) VALUES(?,?,?)', (sid, node_key_for(sid, node.fingerprint), node.node_key))
                conn.commit()


def _scope(subscription_id: int | None, node_key: str | None) -> str:
    return "node" if node_key else ("group" if subscription_id else "all")


def _new_run(subscription_id: int | None, node_key: str | None, trigger: str) -> int:
    now = utcnow_iso()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO health_check_runs(scope,subscription_id,node_key,trigger,status,started_at) VALUES(?,?,?,?,?,?)",
            (_scope(subscription_id, node_key), subscription_id, node_key, trigger, "running", now),
        )
        conn.commit()
        return int(cur.lastrowid)


def _load_group_nodes(subscription_id: int) -> list[NormalizedNode]:
    group = load_subscription(subscription_id)
    try:
        pattern = re.compile(get_setting("pseudo_node_filter"), re.I)
    except re.error:
        return []
    parsed: list[ParseResult] = []
    for upstream in group["upstreams"]:
        if not upstream["enabled"]:
            continue
        body_path, _ = upstream_paths(subscription_id, upstream)
        try:
            item = parse_subscription(body_path.read_bytes(), upstream["name"], pattern)
            for node in item.nodes:
                node.source_id = int(upstream["id"])
            parsed.append(item)
        except Exception:
            continue
    try:
        manual = load_manual_nodes(subscription_id)
    except HTTPException:
        manual = []
    if manual:
        parsed.append(ParseResult("manual", manual))
    nodes, _ = merge_nodes(parsed)
    with db() as conn:
        snapshots = {str(x["node_key"]): dict(x) for x in conn.execute(
            "SELECT node_key,final_name,protocol,source_name FROM node_snapshots WHERE subscription_id=?",
            (subscription_id,),
        )}
    result: list[NormalizedNode] = []
    resolve_key = node_key_resolver(subscription_id)
    for node in nodes:
        node.node_key = resolve_key(node)
        snap = snapshots.get(node.node_key)
        if not snap:
            continue
        node.name = str(snap["final_name"])
        node.source_name = str(snap["source_name"])
        if node.proxy is not None:
            node.proxy["name"] = node.name
        result.append(node)
    found = {n.node_key for n in result}
    for key, snap in snapshots.items():
        if key not in found:
            result.append(NormalizedNode(str(snap['final_name']), '', str(snap['source_name']), node_key=key, protocol_hint=str(snap['protocol'])))
    return result


def _selected(subscription_id: int | None, node_key: str | None) -> list[tuple[int, NormalizedNode]]:
    if subscription_id:
        ids = [subscription_id]
    else:
        ids = [int(x["id"]) for x in list_subscriptions() if x["enabled"]]
    values: list[tuple[int, NormalizedNode]] = []
    for sub_id in ids:
        for node in _load_group_nodes(sub_id):
            if node_key is None or node.node_key == node_key:
                values.append((sub_id, node))
    return values


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


async def _wait_controller(client: httpx.AsyncClient, base: str, headers: dict[str, str]) -> None:
    for _ in range(40):
        try:
            if (await client.get(f"{base}/version", headers=headers, timeout=0.5)).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("mihomo_start_failed")


def _cleanup_history() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with db() as conn:
        conn.execute("DELETE FROM node_health_results WHERE tested_at<?", (cutoff,))
        conn.execute("DELETE FROM health_check_runs WHERE started_at<? AND status!='running'", (cutoff,))
        conn.execute("DELETE FROM node_health_results WHERE id NOT IN (SELECT id FROM node_health_results ORDER BY tested_at DESC,id DESC LIMIT 50000)")
        conn.execute("DELETE FROM health_check_runs WHERE id NOT IN (SELECT id FROM health_check_runs ORDER BY started_at DESC,id DESC LIMIT 2000)")
        conn.execute("DELETE FROM health_notifications WHERE created_at<? OR id NOT IN (SELECT id FROM health_notifications ORDER BY id DESC LIMIT 1000)", (cutoff,))
        conn.commit()


_AVAILABLE_STATUSES = {"healthy", "google_blocked", "connectivity_target_failed"}


def _notify_event(status: str, failures: int, prev_failures: int, threshold: int) -> str | None:
    """连续失败达到阈值时告警一次（仅越线当次），之后恢复时再通知一次。"""
    if threshold <= 0:
        return None
    if status == "unavailable" and failures == threshold:
        return "down"
    if status in _AVAILABLE_STATUSES and prev_failures >= threshold:
        return "recovered"
    return None


def _store_result(run_id: int, sub_id: int, node: NormalizedNode, result: dict[str, Any],
                  notify_threshold: int = 0) -> tuple[str, int] | None:
    now = utcnow_iso()
    status = str(result["status"])
    with db() as conn:
        if not conn.execute('SELECT 1 FROM subscriptions WHERE id=?', (sub_id,)).fetchone(): return None
        old = conn.execute("SELECT consecutive_failures FROM node_health_latest WHERE subscription_id=? AND node_key=?",
                           (sub_id, node.node_key)).fetchone()
        prev_failures = int(old[0]) if old else 0
        failures = (prev_failures + 1 if old else 1) if status == "unavailable" else 0
        values = (int(result["connectivity_ok"]) if result.get("connectivity_ok") is not None else None,
                  result.get("connectivity_latency_ms"),
                  int(result["google_ok"]) if result.get("google_ok") is not None else None,
                  result.get("google_latency_ms"))
        conn.execute(
            """INSERT INTO node_health_results(run_id,subscription_id,node_key,node_name,protocol,status,
               connectivity_ok,connectivity_latency_ms,google_ok,google_latency_ms,error_code,tested_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, sub_id, node.node_key, node.name[:160], node.protocol, status, *values,
             result.get("error_code"), now),
        )
        if status != "skipped":
            conn.execute(
                """INSERT INTO node_health_latest(subscription_id,node_key,status,connectivity_ok,
                   connectivity_latency_ms,google_ok,google_latency_ms,consecutive_failures,error_code,tested_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(subscription_id,node_key) DO UPDATE SET
                   status=excluded.status,connectivity_ok=excluded.connectivity_ok,
                   connectivity_latency_ms=excluded.connectivity_latency_ms,google_ok=excluded.google_ok,
                   google_latency_ms=excluded.google_latency_ms,consecutive_failures=excluded.consecutive_failures,
                   error_code=excluded.error_code,tested_at=excluded.tested_at""",
                (sub_id, node.node_key, status, *values, failures, result.get("error_code"), now),
            )
        conn.execute("UPDATE health_check_runs SET completed=completed+1,available=available+?,unavailable=unavailable+?,skipped=skipped+? WHERE id=?",
                     (int(status in _AVAILABLE_STATUSES),
                      int(status == "unavailable"), int(status == "skipped"), run_id))
        conn.commit()
    if status == "skipped":
        return None
    kind = _notify_event(status, failures, prev_failures, notify_threshold)
    return (kind, failures) if kind else None


async def _send_notification(events: list[tuple[str, str, str, int]]) -> None:
    """把测活告警推送到 Webhook，消息体兼容企业微信/钉钉机器人 text 格式。"""
    webhook = get_setting("health_notify_webhook").strip()
    if not webhook:
        return
    lines = [f"[Sub Manager] 节点测活通知（{len(events)} 条）"]
    for kind, group, name, failures in events[:20]:
        lines.append(f"√ {group} / {name} 已恢复" if kind == "recovered"
                     else f"× {group} / {name} 连续失败 {failures} 次")
    if len(events) > 20:
        lines.append(f"… 其余 {len(events) - 20} 条省略")
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        response = await client.post(webhook, json={'msgtype': 'text', 'text': {'content': redact('\n'.join(lines), 4000)}})
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and str(body.get('errcode', body.get('code', 0))) not in {'0', '200'}:
            raise RuntimeError('webhook_rejected')


_notification_lock = asyncio.Lock()


async def deliver_notifications() -> None:
    if not get_setting('health_notify_webhook').strip(): return
    async with _notification_lock:
        with db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM health_notifications WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT 10", (utcnow_iso(),))]
        for row in rows:
            attempts = int(row['attempts']) + 1
            try:
                await _send_notification(json.loads(row['events']))
                status, code = 'delivered', None
            except Exception:
                status, code = ('failed' if attempts >= 5 else 'pending'), 'delivery_failed'
            retry = (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 60 * 2 ** attempts))).isoformat()
            with db() as conn:
                conn.execute('UPDATE health_notifications SET status=?,attempts=?,error_code=?,updated_at=?,next_attempt_at=? WHERE id=?', (status, attempts, code, utcnow_iso(), retry, row['id']))
                conn.commit()


async def _validate_proxies(proxies: list[dict[str, Any]], directory: Path) -> list[dict[str, Any]]:
    """Fast path one offline check; bisect invalid configs to isolate bad nodes."""
    path = directory / 'validate.yaml'
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(yaml.safe_dump({'log-level': 'silent', 'mode': 'direct', 'proxies': proxies}, allow_unicode=True), 'utf-8')
    process = await asyncio.create_subprocess_exec(MIHOMO_BINARY, '-t', '-d', str(directory), '-f', str(path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        code = await asyncio.wait_for(process.wait(), 10)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    finally:
        path.unlink(missing_ok=True)
    if code == 0: return proxies
    if len(proxies) <= 1: return []
    middle = len(proxies) // 2
    return await _validate_proxies(proxies[:middle], directory) + await _validate_proxies(proxies[middle:], directory)


_ERROR_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeout", ("timeout", "deadline")),
    ("refused", ("refused",)),
    ("reset", ("reset",)),
    ("dns", ("dns", "no such host", "resolve")),
    ("tls", ("tls", "certificate", "handshake")),
    ("auth", ("auth", "unauthorized", "407")),
)


def _classify_error(message: str) -> str:
    """Map a raw error message to a coarse category; never returns the raw text
    (it may contain node addresses and must not reach the database)."""
    text = message.lower()
    for kind, needles in _ERROR_KINDS:
        if any(needle in text for needle in needles):
            return kind
    return "other"


async def _delay(client: httpx.AsyncClient, base: str, headers: dict[str, str], name: str,
                 url: str, timeout_s: int, semaphore: asyncio.Semaphore) -> tuple[bool, int | None, str | None]:
    async with semaphore:
        try:
            response = await client.get(f"{base}/proxies/{quote(name, safe='')}/delay", headers=headers,
                                        params={"url": url, "timeout": timeout_s * 1000},
                                        timeout=timeout_s + 2)
            if response.status_code == 200:
                return True, int(response.json().get("delay", 0)) or None, None
            try:
                message = str(response.json().get("message") or "")
            except (ValueError, TypeError, AttributeError):
                message = response.text[:200]
            return False, None, _classify_error(message)
        except httpx.TimeoutException:
            return False, None, "timeout"
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return False, None, _classify_error(str(exc))


async def _run(run_id: int, subscription_id: int | None, node_key: str | None,
               selections: tuple[tuple[int, str], ...] | None = None) -> None:
    global _task, _task_scope, _task_run_id
    process: asyncio.subprocess.Process | None = None
    temp_dir: Path | None = None
    try:
        if selections:
            wanted = set(selections)
            selected = [(sid, n) for sid in sorted({s for s, _ in wanted}) for n in _load_group_nodes(sid) if (sid, n.node_key) in wanted]
        else:
            selected = _selected(subscription_id, node_key)
        with db() as conn:
            conn.execute("UPDATE health_check_runs SET total=? WHERE id=?", (len(selected), run_id))
            conn.commit()
        supported = [(sub_id, node) for sub_id, node in selected if node.proxy is not None]
        notify_threshold = (int(get_setting("health_notify_threshold", "3"))
                            if get_setting("health_notify_enabled", "0") == "1" else 0)
        group_names = {int(x["id"]): str(x["name"]) for x in list_subscriptions()}
        events: list[tuple[str, str, str, int]] = []
        for sub_id, node in selected:
            if node.proxy is None:
                _store_result(run_id, sub_id, node, {"status": "skipped", "error_code": "unsupported_protocol"})
        if supported:
            if not Path(MIHOMO_BINARY).exists():
                raise RuntimeError("mihomo_missing")
            temp_dir = Path(tempfile.mkdtemp(prefix="submanager-health-"))
            os.chmod(temp_dir, 0o700)
            port, secret = _free_port(), secrets.token_urlsafe(32)
            proxies: list[dict[str, Any]] = []
            internal_names: dict[tuple[int, str], str] = {}
            for index, (sub_id, node) in enumerate(supported):
                name = f"n{index}-{secrets.token_hex(5)}"
                proxy = dict(node.proxy or {})
                proxy["name"] = name
                proxies.append(proxy)
                internal_names[(sub_id, node.node_key)] = name
            proxies = await _validate_proxies(proxies, temp_dir)
            valid_names = {p['name'] for p in proxies}
            for sub_id, node in supported:
                if internal_names[(sub_id, node.node_key)] not in valid_names:
                    _store_result(run_id, sub_id, node, {'status': 'skipped', 'error_code': 'invalid_configuration'})
            supported = [(sid, n) for sid, n in supported if internal_names[(sid, n.node_key)] in valid_names]
            config = {"external-controller": f"127.0.0.1:{port}", "secret": secret,
                      "log-level": "silent", "mode": "direct",
                      "dns": {"enable": True, "nameserver": ["223.5.5.5", "119.29.29.29", "8.8.8.8", "1.1.1.1"],
                              "fallback-filter": {"geoip": False}},
                      "proxies": proxies}
            path = temp_dir / "config.yaml"
            path.touch(mode=0o600, exist_ok=True)
            path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), "utf-8")
            os.chmod(path, 0o600)
            process = await asyncio.create_subprocess_exec(MIHOMO_BINARY, "-d", str(temp_dir), "-f", str(path),
                                                            stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.DEVNULL)
            base, headers = f"http://127.0.0.1:{port}", {"Authorization": f"Bearer {secret}"}
            timeout_s = int(get_setting("health_check_timeout_seconds", "8"))
            semaphore = asyncio.Semaphore(int(get_setting("health_check_concurrency", "5")))
            async with httpx.AsyncClient() as client:
                await _wait_controller(client, base, headers)
                async def test_one(sub_id: int, node: NormalizedNode) -> None:
                    name = internal_names[(sub_id, node.node_key)]
                    cloudflare, google = await asyncio.gather(
                        _delay(client, base, headers, name, HEALTH_CONNECTIVITY_URL, timeout_s, semaphore),
                        _delay(client, base, headers, name, HEALTH_GOOGLE_URL, timeout_s, semaphore),
                    )
                    if not cloudflare[0] and not google[0]:
                        # 双目标首轮全失败多为冷启动握手抖动，隔 0.5s 重试一次，以重试结果为准
                        await asyncio.sleep(0.5)
                        cloudflare, google = await asyncio.gather(
                            _delay(client, base, headers, name, HEALTH_CONNECTIVITY_URL, timeout_s, semaphore),
                            _delay(client, base, headers, name, HEALTH_GOOGLE_URL, timeout_s, semaphore),
                        )
                    cf_ok, cf_delay, cf_err = cloudflare
                    google_ok, google_delay, google_err = google
                    status = "healthy" if cf_ok and google_ok else (
                        "google_blocked" if cf_ok else ("connectivity_target_failed" if google_ok else "unavailable"))
                    error_code: str | None = None
                    if not cf_ok and not google_ok:
                        error_code = f"cf:{cf_err or 'other'},google:{google_err or 'other'}"
                    elif not google_ok:
                        error_code = f"google:{google_err or 'other'}"
                    elif not cf_ok:
                        error_code = f"cf:{cf_err or 'other'}"
                    event = _store_result(run_id, sub_id, node, {"status": status, "connectivity_ok": cf_ok,
                                  "connectivity_latency_ms": cf_delay, "google_ok": google_ok,
                                  "google_latency_ms": google_delay, "error_code": error_code}, notify_threshold)
                    if event:
                        events.append((event[0], group_names.get(sub_id, ""), node.name, event[1]))
                await asyncio.gather(*(test_one(sub_id, node) for sub_id, node in supported))
        with db() as conn:
            conn.execute("UPDATE health_check_runs SET status='completed',finished_at=? WHERE id=?", (utcnow_iso(), run_id))
            conn.commit()
        if events:
            events = [(kind, redact(group, 100), redact(name, 160), failures) for kind, group, name, failures in events]
            with db() as conn:
                conn.execute("INSERT INTO health_notifications(events,status,created_at,updated_at,next_attempt_at) VALUES(?,'pending',?,?,?)", (json.dumps(events, ensure_ascii=False), utcnow_iso(), utcnow_iso(), utcnow_iso()))
                conn.commit()
            await deliver_notifications()
    except asyncio.CancelledError:
        with db() as conn:
            conn.execute("UPDATE health_check_runs SET status='interrupted',finished_at=?,error_code='interrupted' WHERE id=?",
                         (utcnow_iso(), run_id)); conn.commit()
        raise
    except Exception as exc:
        code = str(exc) if str(exc) in {"mihomo_missing", "mihomo_start_failed"} else "runtime_error"
        with db() as conn:
            conn.execute("UPDATE health_check_runs SET status='error',finished_at=?,error_code=? WHERE id=?",
                         (utcnow_iso(), code, run_id)); conn.commit()
    finally:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        _cleanup_history()
        async with _lock:
            if _task_run_id == run_id:
                _task = None
                _task_scope = None
                _task_run_id = None


async def start_health_test(subscription_id: int | None = None, node_key: str | None = None,
                            trigger: str = "manual", nodes: list[tuple[int, str]] | None = None) -> dict[str, Any]:
    global _task, _task_scope, _task_run_id
    if node_key and not subscription_id:
        raise HTTPException(400, "测试单个节点时必须指定订阅组")
    if nodes and (subscription_id or node_key):
        raise HTTPException(400, '批量节点与单组范围不能同时指定')
    selections = tuple(sorted(set(nodes))) if nodes else None
    scope = (subscription_id, node_key, selections)
    async with _lock:
        if _task and not _task.done():
            if _task_scope == scope:
                return {"run_id": _task_run_id, "reused": True}
            raise HTTPException(409, "已有节点测活任务正在运行")
        run_id = _new_run(subscription_id, node_key, trigger)
        if selections:
            with db() as conn:
                conn.execute("UPDATE health_check_runs SET scope='batch' WHERE id=?", (run_id,)); conn.commit()
        _task_scope, _task_run_id = scope, run_id
        coroutine = _run(run_id, subscription_id, node_key, selections) if selections else _run(run_id, subscription_id, node_key)
        _task = asyncio.create_task(coroutine, name=f"health-check-{run_id}")
        return {"run_id": run_id, "reused": False}


async def health_scheduler_loop() -> None:
    global _scheduler_last_poll, _scheduler_next_run
    _scheduler_next_run = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    await asyncio.sleep(300)
    while True:
        try:
            _scheduler_last_poll = utcnow_iso()
            await deliver_notifications()
            # 分钟级周期；旧库仅有小时键时按 小时×60 兼容
            minutes = int(get_setting("health_check_interval_minutes")
                          or int(get_setting("health_check_interval_hours", "6")) * 60)
            with db() as conn:
                row = conn.execute("SELECT started_at FROM health_check_runs WHERE trigger='scheduler' ORDER BY id DESC LIMIT 1").fetchone()
            last = parse_iso(str(row[0])) if row else None
            due = last is None or datetime.now(timezone.utc) >= last + timedelta(minutes=minutes)
            _scheduler_next_run = ((last + timedelta(minutes=minutes)) if last else datetime.now(timezone.utc)).isoformat()
            if due and get_setting("health_check_enabled", "1") == "1" and not (_task and not _task.done()):
                await start_health_test(trigger="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(60)


def health_runtime() -> dict[str, Any]:
    with db() as conn:
        delivery = conn.execute('SELECT status,attempts,error_code,updated_at FROM health_notifications ORDER BY id DESC LIMIT 1').fetchone()
    return {"health_check_running": bool(_task and not _task.done()), "health_check_run_id": _task_run_id,
            'notification_delivery': dict(delivery) if delivery else None,
            'health_check_enabled': get_setting('health_check_enabled', '1') == '1',
            "health_check_last_poll": _scheduler_last_poll, "health_check_next_run": _scheduler_next_run,
            "mihomo_available": _kernel_verified and Path(MIHOMO_BINARY).exists(), "mihomo_version": _kernel_version,
            'mihomo_expected_version': MIHOMO_VERSION}


def stop_health_task() -> asyncio.Task[None] | None:
    if _task and not _task.done():
        _task.cancel()
        return _task
    return None
