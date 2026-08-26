from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import socket
import tempfile
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
from ..security import utcnow_iso
from .cache import upstream_paths
from .manual_nodes import load_manual_nodes, node_key_for
from .parser import NormalizedNode, ParseResult, merge_nodes, parse_subscription


_task: asyncio.Task[None] | None = None
_task_scope: tuple[int | None, str | None] | None = None
_task_run_id: int | None = None
_lock = asyncio.Lock()
_scheduler_last_poll: str | None = None
_scheduler_next_run: str | None = None


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
    manual = load_manual_nodes(subscription_id)
    if manual:
        parsed.append(ParseResult("manual", manual))
    nodes, _ = merge_nodes(parsed)
    with db() as conn:
        snapshots = {str(x["node_key"]): dict(x) for x in conn.execute(
            "SELECT node_key,final_name,protocol,source_name FROM node_snapshots WHERE subscription_id=?",
            (subscription_id,),
        )}
    result: list[NormalizedNode] = []
    for node in nodes:
        node.node_key = node_key_for(subscription_id, node.fingerprint)
        snap = snapshots.get(node.node_key)
        if not snap:
            continue
        node.name = str(snap["final_name"])
        node.source_name = str(snap["source_name"])
        if node.proxy is not None:
            node.proxy["name"] = node.name
        result.append(node)
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
        conn.execute("DELETE FROM node_health_results WHERE id NOT IN (SELECT id FROM node_health_results ORDER BY tested_at DESC,id DESC LIMIT 50000)")
        conn.execute("DELETE FROM health_check_runs WHERE id NOT IN (SELECT id FROM health_check_runs ORDER BY started_at DESC,id DESC LIMIT 2000)")
        conn.commit()


def _store_result(run_id: int, sub_id: int, node: NormalizedNode, result: dict[str, Any]) -> None:
    now = utcnow_iso()
    status = str(result["status"])
    with db() as conn:
        old = conn.execute("SELECT consecutive_failures FROM node_health_latest WHERE subscription_id=? AND node_key=?",
                           (sub_id, node.node_key)).fetchone()
        failures = (int(old[0]) + 1 if old else 1) if status == "unavailable" else 0
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
                     (int(status in {"healthy", "google_blocked", "connectivity_target_failed"}),
                      int(status == "unavailable"), int(status == "skipped"), run_id))
        conn.commit()


async def _delay(client: httpx.AsyncClient, base: str, headers: dict[str, str], name: str,
                 url: str, timeout_s: int, semaphore: asyncio.Semaphore) -> tuple[bool, int | None]:
    async with semaphore:
        try:
            response = await client.get(f"{base}/proxies/{quote(name, safe='')}/delay", headers=headers,
                                        params={"url": url, "timeout": timeout_s * 1000},
                                        timeout=timeout_s + 2)
            if response.status_code == 200:
                return True, int(response.json().get("delay", 0)) or None
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return False, None


async def _run(run_id: int, subscription_id: int | None, node_key: str | None) -> None:
    global _task, _task_scope, _task_run_id
    process: asyncio.subprocess.Process | None = None
    temp_dir: Path | None = None
    try:
        selected = _selected(subscription_id, node_key)
        with db() as conn:
            conn.execute("UPDATE health_check_runs SET total=? WHERE id=?", (len(selected), run_id))
            conn.commit()
        supported = [(sub_id, node) for sub_id, node in selected if node.proxy is not None]
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
            config = {"external-controller": f"127.0.0.1:{port}", "secret": secret,
                      "log-level": "silent", "mode": "direct", "proxies": proxies}
            path = temp_dir / "config.yaml"
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
                    cf_ok, cf_delay = cloudflare
                    google_ok, google_delay = google
                    status = "healthy" if cf_ok and google_ok else (
                        "google_blocked" if cf_ok else ("connectivity_target_failed" if google_ok else "unavailable"))
                    _store_result(run_id, sub_id, node, {"status": status, "connectivity_ok": cf_ok,
                                  "connectivity_latency_ms": cf_delay, "google_ok": google_ok,
                                  "google_latency_ms": google_delay,
                                  "error_code": None if cf_ok or google_ok else "both_targets_failed"})
                await asyncio.gather(*(test_one(sub_id, node) for sub_id, node in supported))
        with db() as conn:
            conn.execute("UPDATE health_check_runs SET status='completed',finished_at=? WHERE id=?", (utcnow_iso(), run_id))
            conn.commit()
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
                            trigger: str = "manual") -> dict[str, Any]:
    global _task, _task_scope, _task_run_id
    if node_key and not subscription_id:
        raise HTTPException(400, "测试单个节点时必须指定订阅组")
    scope = (subscription_id, node_key)
    async with _lock:
        if _task and not _task.done():
            if _task_scope == scope:
                return {"run_id": _task_run_id, "reused": True}
            raise HTTPException(409, "已有节点测活任务正在运行")
        run_id = _new_run(subscription_id, node_key, trigger)
        _task_scope, _task_run_id = scope, run_id
        _task = asyncio.create_task(_run(run_id, subscription_id, node_key), name=f"health-check-{run_id}")
        return {"run_id": run_id, "reused": False}


async def health_scheduler_loop() -> None:
    global _scheduler_last_poll, _scheduler_next_run
    await asyncio.sleep(300)
    while True:
        try:
            _scheduler_last_poll = utcnow_iso()
            hours = int(get_setting("health_check_interval_hours", "6"))
            with db() as conn:
                row = conn.execute("SELECT started_at FROM health_check_runs WHERE trigger='scheduler' ORDER BY id DESC LIMIT 1").fetchone()
            last = parse_iso(str(row[0])) if row else None
            due = last is None or datetime.now(timezone.utc) >= last + timedelta(hours=hours)
            _scheduler_next_run = ((last + timedelta(hours=hours)) if last else datetime.now(timezone.utc)).isoformat()
            if due and get_setting("health_check_enabled", "1") == "1" and not (_task and not _task.done()):
                await start_health_test(trigger="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(60)


def health_runtime() -> dict[str, Any]:
    return {"health_check_running": bool(_task and not _task.done()), "health_check_run_id": _task_run_id,
            "health_check_last_poll": _scheduler_last_poll, "health_check_next_run": _scheduler_next_run,
            "mihomo_available": Path(MIHOMO_BINARY).exists(), "mihomo_version": MIHOMO_VERSION}


def stop_health_task() -> asyncio.Task[None] | None:
    if _task and not _task.done():
        _task.cancel()
        return _task
    return None
