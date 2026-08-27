from __future__ import annotations

import asyncio
import base64
import copy
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from ..config import CLIENT_TYPES, INTERNAL_BASE_URL, RULE_CONFIG, SUBCONVERTER_URL
from ..db import get_setting
from ..security import redact, utcnow_iso
from .cache import write_output
from .parser import NormalizedNode, proxy_to_uri


@dataclass
class InternalSource:
    body: bytes
    extension: str
    expires_at: float


_sources: dict[str, list[InternalSource]] = {}
_sources_lock = asyncio.Lock()


async def register_sources(items: list[tuple[bytes, str]], ttl: int = 300) -> tuple[str, list[str]]:
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    async with _sources_lock:
        for old in [key for key, values in _sources.items() if not values or values[0].expires_at < now]:
            _sources.pop(old, None)
        _sources[token] = [InternalSource(body, ext, now + ttl) for body, ext in items]
    urls = [f"{INTERNAL_BASE_URL}/internal/source/{token}/{idx}.{item.extension}" for idx, item in enumerate(_sources[token])]
    return token, urls


async def get_internal_source(token: str, index: int) -> InternalSource | None:
    async with _sources_lock:
        values = _sources.get(token)
        if not values or index < 0 or index >= len(values):
            return None
        if values[index].expires_at < time.monotonic():
            _sources.pop(token, None)
            return None
        return values[index]


async def remove_sources(token: str) -> None:
    async with _sources_lock:
        _sources.pop(token, None)


def build_mihomo_document(nodes: list[NormalizedNode], update_interval_minutes: int, subscription_id: int | None = None) -> bytes | None:
    """Render Clash/Mihomo-native inputs without a lossy converter round trip."""
    if not nodes or any(node.proxy is None for node in nodes):
        return None
    proxies = [copy.deepcopy(node.proxy) for node in nodes if node.proxy is not None]
    names = [str(proxy["name"]) for proxy in proxies]
    categories: list[dict[str, Any]] = []
    if subscription_id:
        from ..categories import category_groups
        categories = category_groups(subscription_id, nodes)
    entry_groups = [item["name"] for item in categories]
    document = {
        "mixed-port": 7890,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "ipv6": True,
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "PROXY", "type": "select",
                "proxies": [*entry_groups, "♻️ 自动选择", "DIRECT", *names],
            },
            {
                "name": "♻️ 自动选择", "type": "url-test", "proxies": names,
                "url": "http://www.gstatic.com/generate_204",
                "interval": max(300, int(update_interval_minutes) * 60), "tolerance": 50,
            },
            *categories,
        ],
        "rules": [
            "DOMAIN-SUFFIX,local,DIRECT",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
            "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
            "GEOSITE,CN,DIRECT",
            "GEOIP,CN,DIRECT,no-resolve",
            "MATCH,PROXY",
        ],
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()


async def render_output(
    client: httpx.AsyncClient,
    subscription: dict[str, Any],
    output: dict[str, Any],
    source_urls: list[str],
    upstream_results: list[dict[str, Any]],
    node_count: int,
    filtered_count: int,
    normalized_nodes: list[NormalizedNode],
) -> dict[str, Any]:
    info = CLIENT_TYPES[output["client_type"]]
    converter_urls = source_urls
    compatible_nodes = node_count
    if not info.get("rules"):
        uri_urls = [url for url in source_urls if url.endswith(".txt")]
        if uri_urls: converter_urls = uri_urls
        compatible_nodes = sum(1 for node in normalized_nodes if node.uri is not None or node.protocol == "vless")
    skipped_nodes = max(0, node_count - compatible_nodes)
    params: dict[str, str] = {
        "target": str(info["target"]), "url": "|".join(converter_urls), "filename": output["name"],
        "emoji": "true", "udp": "true", "list": "false", "sort": "false",
    }
    if info.get("rules"):
        params["config"] = RULE_CONFIG
        params["interval"] = str(int(output["update_interval_minutes"]) * 60)
    if info.get("ver"):
        params["ver"] = str(info["ver"])
    started = time.monotonic()
    try:
        if output["client_type"] == "v2ray":
            uri_values = [value for node in normalized_nodes if (value := proxy_to_uri(node)) is not None]
            if not uri_values:
                raise ValueError("当前节点协议不受 V2Ray / Shadowrocket 输出支持")
            raw = ("\n".join(uri_values) + "\n").encode()
            content = base64.b64encode(raw)
            skipped = max(0, node_count - len(uri_values))
            meta = {
                "updated_at": utcnow_iso(), "content_type": "text/plain; charset=utf-8",
                "profile_update_interval": output["update_interval_minutes"], "bytes": len(content),
                "config_revision": subscription.get("config_revision", 1), "node_count": node_count,
                "filtered_count": filtered_count, "skipped_nodes": skipped, "renderer": "native-uri-base64",
                "upstreams": [{k: x.get(k) for k in ("name", "status", "cached", "status_code", "content_type", "bytes", "duration_ms", "error", "source_format", "node_count", "filtered_count")} for x in upstream_results],
            }
            write_output(subscription["token"], output["slug"], content, meta)
            return {"ok": True, "name": output["name"],
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "bytes": len(content), "renderer": "native-uri-base64", "skipped_nodes": skipped}

        native_mihomo = build_mihomo_document(normalized_nodes, int(output["update_interval_minutes"]), int(subscription["id"])) \
            if output["client_type"] in {"mihomo", "clash"} else None
        if native_mihomo is not None:
            meta = {
                "updated_at": utcnow_iso(), "content_type": "application/yaml; charset=utf-8",
                "profile_update_interval": output["update_interval_minutes"], "bytes": len(native_mihomo),
                "config_revision": subscription.get("config_revision", 1), "node_count": node_count,
                "filtered_count": filtered_count, "skipped_nodes": 0, "renderer": "native-mihomo",
                "upstreams": [{k: x.get(k) for k in ("name", "status", "cached", "status_code", "content_type", "bytes", "duration_ms", "error", "source_format", "node_count", "filtered_count")} for x in upstream_results],
            }
            write_output(subscription["token"], output["slug"], native_mihomo, meta)
            return {"ok": True, "name": output["name"], "duration_ms": round((time.monotonic() - started) * 1000),
                    "bytes": len(native_mihomo), "renderer": "native-mihomo", "skipped_nodes": 0}
        timeout_s = int(get_setting("converter_timeout_seconds", "90"))
        response = await client.get(
            f"{SUBCONVERTER_URL}/sub", params=params,
            timeout=httpx.Timeout(timeout_s, connect=min(15, timeout_s)), follow_redirects=True,
        )
        if response.status_code >= 400:
            raise ValueError(f"转换器 HTTP {response.status_code}: {redact(response.text)}")
        if not response.content:
            raise ValueError("转换器返回空内容")
        if compatible_nodes == 0: raise ValueError("当前节点协议不受此输出客户端支持")
        meta = {
            "updated_at": utcnow_iso(), "content_type": response.headers.get("content-type", "text/plain; charset=utf-8"),
            "profile_update_interval": output["update_interval_minutes"], "bytes": len(response.content),
            "config_revision": subscription.get("config_revision", 1), "node_count": node_count,
            "filtered_count": filtered_count, "renderer": "subconverter", "skipped_nodes": skipped_nodes,
            "upstreams": [{k: x.get(k) for k in ("name", "status", "cached", "status_code", "content_type", "bytes", "duration_ms", "error", "source_format", "node_count", "filtered_count")} for x in upstream_results],
        }
        write_output(subscription["token"], output["slug"], response.content, meta)
        return {"ok": True, "name": output["name"], "duration_ms": round((time.monotonic() - started) * 1000),
                "bytes": len(response.content), "renderer": "subconverter", "skipped_nodes": skipped_nodes}
    except Exception as exc:
        return {"ok": False, "name": output["name"], "duration_ms": round((time.monotonic() - started) * 1000), "error": redact(exc, known_tokens=(subscription["token"],))}
