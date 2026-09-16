from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from ..config import MAX_REDIRECTS, MAX_UPSTREAM_BYTES
from ..db import get_setting, db
from ..repository import update_upstream_result
from ..security import redact, utcnow_iso
from .cache import upstream_paths
from .parser import ParseResult, parse_subscription
from .coordination import group_lock


class UpstreamFetcher:
    def __init__(self) -> None:
        self.client: httpx.AsyncClient | None = None
        self.limit = asyncio.Semaphore(10)

    async def start(self) -> None:
        self.limit = asyncio.Semaphore(10)
        self.client = httpx.AsyncClient(follow_redirects=True, max_redirects=MAX_REDIRECTS)

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _download(self, url, headers, timeout_s, result):
        assert self.client is not None
        async with self.limit:
            async with self.client.stream('GET', url, headers=headers, timeout=httpx.Timeout(timeout_s, connect=min(15, timeout_s))) as response:
                result['status_code'] = response.status_code
                result['content_type'] = redact(response.headers.get('content-type', ''), 200)
                if response.status_code != 200: raise ValueError(f'HTTP {response.status_code}')
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_UPSTREAM_BYTES: raise ValueError('上游响应超过大小限制')
                    chunks.append(chunk)
                return b''.join(chunks)

    async def fetch(self, subscription_id: int, upstream: dict[str, Any], pattern: re.Pattern[str]) -> dict[str, Any]:
        assert self.client is not None
        body_path, meta_path = upstream_paths(subscription_id, upstream)
        started = time.monotonic()
        result: dict[str, Any] = {
            "id": upstream["id"], "name": upstream["name"], "ok": False, "fresh": False,
            "cached": False, "status": "error", "status_code": None, "content_type": None,
            "bytes": 0, "duration_ms": 0, "error": None,
            "parsed": None, "source_format": None, "node_count": 0, "filtered_count": 0,
        }
        timeout_s = int(get_setting("upstream_timeout_seconds", "30"))
        headers = {
            "User-Agent": get_setting("upstream_user_agent", "ClashVergeRev/2.4 SubManager/3.0"),
            "Accept": "*/*", "Cache-Control": "no-cache",
        }
        try:
            body = await asyncio.wait_for(self._download(str(upstream['url']), headers, timeout_s, result), timeout_s)
            head = body[:512].lstrip().lower()
            if not body:
                raise ValueError("返回内容为空")
            if b"<html" in head or b"<!doctype html" in head:
                raise ValueError("返回了 HTML 页面，不是订阅内容")
            parsed = parse_subscription(body, upstream["name"], pattern)
            for node in parsed.nodes:
                node.source_id = int(upstream["id"])
            with group_lock(subscription_id):
                with db() as conn:
                    current = conn.execute('SELECT 1 FROM upstreams WHERE subscription_id=? AND id=? AND url=? AND enabled=1', (subscription_id, upstream['id'], str(upstream['url']))).fetchone()
                if not current: raise ValueError('节点来源已更改，本次结果丢弃')
                body_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = body_path.with_suffix('.body.tmp')
                tmp.touch(mode=0o600, exist_ok=True)
                tmp.write_bytes(body)
                tmp.replace(body_path)
                meta_path.write_text(json.dumps({'updated_at': utcnow_iso(), 'status_code': result['status_code'], 'content_type': result['content_type']}, ensure_ascii=False), 'utf-8')
            result.update(ok=True, fresh=True, status="ok", bytes=len(body), parsed=parsed,
                          source_format=parsed.source_format, node_count=len(parsed.nodes),
                          filtered_count=parsed.filtered_count, success_at=utcnow_iso())
        except Exception as exc:
            result["error"] = redact(exc)
            if get_setting("stale_cache_fallback", "1") == "1" and body_path.exists():
                try:
                    body = body_path.read_bytes()
                    parsed = parse_subscription(body, upstream["name"], pattern)
                    for node in parsed.nodes:
                        node.source_id = int(upstream["id"])
                    old_meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {}
                    result.update(ok=True, cached=True, status="stale", bytes=len(body), parsed=parsed,
                                  source_format=parsed.source_format, node_count=len(parsed.nodes),
                                  filtered_count=parsed.filtered_count,
                                  content_type=old_meta.get("content_type"))
                except Exception as cache_exc:
                    result["error"] = redact(f"{result['error']}; 缓存无效: {cache_exc}")
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        with group_lock(subscription_id):
            with db() as conn:
                current = conn.execute('SELECT 1 FROM upstreams WHERE subscription_id=? AND id=? AND url=?', (subscription_id, upstream['id'], str(upstream['url']))).fetchone()
            if current: update_upstream_result(int(upstream['id']), result)
        return result

    async def fetch_group(self, subscription: dict[str, Any]) -> list[dict[str, Any]]:
        enabled = [x for x in subscription["upstreams"] if x["enabled"]]
        if not enabled:
            raise ValueError("没有启用的上游订阅")
        try:
            pattern = re.compile(get_setting("pseudo_node_filter"))
        except re.error as exc:
            raise ValueError(f"伪节点过滤正则无效: {exc}") from exc
        return await asyncio.gather(*(self.fetch(int(subscription["id"]), x, pattern) for x in enabled))


fetcher = UpstreamFetcher()
