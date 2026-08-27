from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
import yaml

from app.services.parser import NormalizedNode
from app.services.renderer import build_mihomo_document, render_output
import app.services.renderer as renderer


def test_native_mihomo_preserves_proxy_fields_and_builds_fixed_groups() -> None:
    proxy = {
        "name": "AnyTLS Home", "type": "anytls", "server": "node.invalid", "port": 443,
        "password": "private-password", "client-fingerprint": "chrome", "udp": True,
    }
    body = build_mihomo_document([
        NormalizedNode("AnyTLS Home", "fingerprint", "Home", proxy=proxy)
    ], 60)
    assert body is not None
    document = yaml.safe_load(body)
    assert document["proxies"][0] == proxy
    assert document["proxy-groups"][0]["name"] == "PROXY"
    assert document["proxy-groups"][0]["proxies"] == ["♻️ 自动选择", "DIRECT", "AnyTLS Home"]
    assert all(group["name"] != "全部节点" for group in document["proxy-groups"])
    assert document["rules"][-1] == "MATCH,PROXY"


def test_native_mihomo_falls_back_when_uri_conversion_is_required() -> None:
    node = NormalizedNode("URI node", "fingerprint", "Provider", uri="vless://example.invalid")
    assert build_mihomo_document([node], 60) is None

def test_v2ray_reality_uses_native_base64_uri_output(monkeypatch, tmp_path) -> None:
    saved = {}
    monkeypatch.setattr(renderer, "write_output", lambda token, slug, body, meta: saved.update(body=body, meta=meta))
    node = NormalizedNode("Reality", "fp", "Provider", proxy={
        "name": "Reality", "type": "vless", "server": "node.invalid", "port": 443,
        "uuid": "11111111-1111-4111-8111-111111111111", "network": "tcp", "tls": True,
        "servername": "www.example.com", "client-fingerprint": "chrome", "flow": "xtls-rprx-vision",
        "reality-opts": {"public-key": "public-key-value", "short-id": "abcd1234"},
    })
    class Client:
        async def get(self, *args, **kwargs):
            raise AssertionError("native V2 output must not call subconverter")
    result = asyncio.run(render_output(
        Client(), {"id": 1, "token": "group-token", "config_revision": 3},
        {"client_type": "v2ray", "name": "V2", "slug": "v2", "update_interval_minutes": 60},
        ["http://internal/source.txt"], [], 1, 0, [node],
    ))
    decoded = base64.b64decode(saved["body"]).decode()
    assert result["ok"] and result["renderer"] == "native-uri-base64"
    assert decoded.startswith("vless://") and "security=reality" in decoded and "pbk=public-key-value" in decoded
    assert saved["meta"]["skipped_nodes"] == 0


def test_clash_output_uses_native_yaml_for_vless(monkeypatch) -> None:
    saved = {}
    monkeypatch.setattr(renderer, "write_output", lambda token, slug, body, meta: saved.update(body=body, meta=meta))
    node = NormalizedNode("Reality", "fp", "Provider", proxy={
        "name": "Reality", "type": "vless", "server": "node.invalid", "port": 443,
        "uuid": "11111111-1111-4111-8111-111111111111", "tls": True,
    })

    class Client:
        async def get(self, *args, **kwargs):
            raise AssertionError("Clash YAML must use native rendering for normalized proxy nodes")

    result = asyncio.run(render_output(
        Client(), {"id": 0, "token": "group-token", "config_revision": 4},
        {"client_type": "clash", "name": "Clash", "slug": "clash", "update_interval_minutes": 60},
        ["http://internal/source.yaml"], [], 1, 0, [node],
    ))
    document = yaml.safe_load(saved["body"])
    assert result["ok"] and result["renderer"] == "native-mihomo"
    assert document["proxies"][0]["type"] == "vless"
    assert saved["meta"]["config_revision"] == 4