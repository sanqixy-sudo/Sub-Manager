from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, unquote, urlsplit

import yaml


URI_SCHEMES = {"vless", "vmess", "trojan", "ss", "ssr", "hysteria2", "hy2", "tuic", "anytls"}
URI_RE = re.compile(r"(?im)^\s*((?:vless|vmess|trojan|ss|ssr|hysteria2|hy2|tuic|anytls)://\S+)\s*$")


@dataclass
class NormalizedNode:
    name: str
    fingerprint: str
    source_name: str
    uri: str | None = None
    proxy: dict[str, Any] | None = None
    original_name: str = ""
    source_id: int | None = None
    node_key: str = ""
    rule_name: str = ""
    alias: str | None = None
    confirmation_status: str = "confirmed"
    previous_name: str | None = None
    traffic: str = ""
    reset: str = ""

    def __post_init__(self) -> None:
        if not self.original_name:
            self.original_name = self.name

    @property
    def protocol(self) -> str:
        if self.proxy is not None:
            return str(self.proxy.get("type") or "unknown").lower()[:32]
        if self.uri:
            return urlsplit(self.uri).scheme.lower()[:32]
        return "unknown"


@dataclass
class ParseResult:
    source_format: str
    nodes: list[NormalizedNode] = field(default_factory=list)
    filtered_count: int = 0


def _decode_base64(raw: bytes) -> bytes | None:
    compact = b"".join(raw.split())
    if len(compact) < 12:
        return None
    try:
        decoded = base64.urlsafe_b64decode(compact + b"=" * (-len(compact) % 4))
        return decoded if URI_RE.search(decoded.decode("utf-8", "ignore")) else None
    except Exception:
        return None


def _uri_name(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.fragment:
        return unquote(parsed.fragment)[:160]
    if parsed.scheme == "vmess":
        try:
            payload = parsed.netloc + parsed.path
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return str(data.get("ps") or data.get("add") or "VMess 节点")[:160]
        except Exception:
            pass
    return f"{parsed.scheme.upper()} 节点"


def _uri_fingerprint(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme == "vmess":
        encoded = parsed.netloc + parsed.path
        try:
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            # Traffic/reset counters are display metadata and must not change node identity.
            for key in ("ps", "remark", "remarks"):
                payload.pop(key, None)
            raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            raw = encoded
    elif parsed.scheme == "ssr":
        encoded = parsed.netloc + parsed.path
        try:
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            main, marker, extra = decoded.partition("/?")
            options = [(key, value) for key, value in parse_qsl(extra, keep_blank_values=True)
                       if key.lower() not in {"remarks", "remark"}]
            raw = main + (marker + urlencode(sorted(options)) if marker else "")
        except Exception:
            raw = encoded
    else:
        query = "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        raw = f"{parsed.scheme}|{parsed.username}|{parsed.password}|{parsed.hostname}|{parsed.port}|{query}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _proxy_fingerprint(proxy: dict[str, Any]) -> str:
    ignored = {"name", "udp", "tfo", "skip-cert-verify"}
    stable = {k: proxy[k] for k in sorted(proxy) if k not in ignored}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _query_map(uri: str) -> tuple[Any, dict[str, str]]:
    parsed = urlsplit(uri)
    return parsed, {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}


def uri_to_proxy(uri: str, name: str) -> dict[str, Any] | None:
    """Build a Mihomo proxy without ever exposing the URI outside this process."""
    parsed, query = _query_map(uri)
    scheme = parsed.scheme.lower()
    try:
        if scheme == "vmess":
            raw = parsed.netloc + parsed.path
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            proxy: dict[str, Any] = {
                "name": name, "type": "vmess", "server": str(data["add"]), "port": int(data["port"]),
                "uuid": str(data["id"]), "alterId": int(data.get("aid") or 0),
                "cipher": str(data.get("scy") or "auto"), "udp": True,
            }
            network = str(data.get("net") or "tcp")
            if network != "tcp": proxy["network"] = network
            tls = str(data.get("tls") or "").lower()
            if tls in {"tls", "1", "true"}:
                proxy["tls"] = True
                if data.get("sni") or data.get("host"): proxy["servername"] = str(data.get("sni") or data.get("host"))
            if network == "ws":
                proxy["ws-opts"] = {"path": str(data.get("path") or "/"),
                                    "headers": {"Host": str(data.get("host"))} if data.get("host") else {}}
            if network == "grpc": proxy["grpc-opts"] = {"grpc-service-name": str(data.get("path") or "")}
            return proxy
        if scheme in {"vless", "trojan", "hysteria2", "hy2", "tuic", "anytls"}:
            if not parsed.hostname or not parsed.port: return None
            secret = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            type_name = "hysteria2" if scheme == "hy2" else scheme
            proxy = {"name": name, "type": type_name, "server": parsed.hostname, "port": parsed.port, "udp": True}
            if type_name == "vless": proxy["uuid"] = secret
            elif type_name == "tuic": proxy.update(uuid=secret, password=password)
            else: proxy["password"] = secret + ((":" + password) if password else "")
            network = query.get("type", "tcp").lower()
            if type_name in {"vless", "trojan"}:
                if network != "tcp": proxy["network"] = network
                security = query.get("security", "").lower()
                if security in {"tls", "reality"} or type_name == "trojan": proxy["tls"] = True
                if query.get("sni"): proxy["servername"] = query["sni"]
                if query.get("fp"): proxy["client-fingerprint"] = query["fp"]
                if query.get("flow"): proxy["flow"] = query["flow"]
                if security == "reality":
                    proxy["reality-opts"] = {"public-key": query.get("pbk", ""), "short-id": query.get("sid", "")}
                if network == "ws":
                    proxy["ws-opts"] = {"path": unquote(query.get("path", "/")),
                                        "headers": {"Host": query["host"]} if query.get("host") else {}}
                if network == "grpc":
                    proxy["grpc-opts"] = {"grpc-service-name": unquote(query.get("serviceName", query.get("servicename", "")))}
            else:
                if query.get("sni"): proxy["sni"] = query["sni"]
                if query.get("insecure", "").lower() in {"1", "true"}: proxy["skip-cert-verify"] = True
            return proxy
        if scheme == "ss":
            if not parsed.hostname or not parsed.port: return None
            user = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            if not password and user:
                decoded = base64.urlsafe_b64decode(user + "=" * (-len(user) % 4)).decode()
                user, password = decoded.split(":", 1)
            return {"name": name, "type": "ss", "server": parsed.hostname, "port": parsed.port,
                    "cipher": user, "password": password, "udp": True}
        if scheme == "ssr":
            raw = parsed.netloc + parsed.path
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
            main, _, extra = decoded.partition("/?")
            server, port, protocol, cipher, obfs, password64 = main.rsplit(":", 5)
            password = base64.urlsafe_b64decode(password64 + "=" * (-len(password64) % 4)).decode()
            opts = {k: v for k, v in parse_qsl(extra)}
            result = {"name": name, "type": "ssr", "server": server, "port": int(port), "protocol": protocol,
                      "cipher": cipher, "obfs": obfs, "password": password, "udp": True}
            for source, target in (("obfsparam", "obfs-param"), ("protoparam", "protocol-param")):
                if opts.get(source):
                    value = opts[source]
                    result[target] = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
            return result
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return None


def parse_subscription(body: bytes, source_name: str, pseudo_filter: re.Pattern[str]) -> ParseResult:
    text = body.decode("utf-8", "replace").lstrip("\ufeff").strip()
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("proxies"), list):
        result = ParseResult("clash_yaml")
        for item in document["proxies"]:
            if not isinstance(item, dict) or not item.get("name") or not item.get("type"):
                continue
            name = str(item["name"])[:160]
            if pseudo_filter.search(name):
                result.filtered_count += 1
                continue
            clean = dict(item)
            clean["name"] = name
            result.nodes.append(NormalizedNode(name, _proxy_fingerprint(clean), source_name, proxy=clean))
        return result

    decoded = _decode_base64(body)
    if decoded is not None:
        text = decoded.decode("utf-8", "replace")
        source_format = "base64_uri"
    else:
        source_format = "uri_list"
    uris = [match.group(1).strip() for match in URI_RE.finditer(text)]
    if not uris:
        raise ValueError("无法识别订阅格式或未发现受支持节点")
    result = ParseResult(source_format)
    for uri in uris:
        name = _uri_name(uri)
        if pseudo_filter.search(name):
            result.filtered_count += 1
            continue
        result.nodes.append(NormalizedNode(name, _uri_fingerprint(uri), source_name, uri=uri,
                                           proxy=uri_to_proxy(uri, name)))
    return result


def merge_nodes(parsed: list[ParseResult]) -> tuple[list[NormalizedNode], int]:
    seen: set[str] = set()
    merged: list[NormalizedNode] = []
    filtered = sum(x.filtered_count for x in parsed)
    for result in parsed:
        for node in result.nodes:
            if node.fingerprint in seen:
                filtered += 1
                continue
            seen.add(node.fingerprint)
            merged.append(node)
    counts: dict[str, int] = {}
    for node in merged:
        counts[node.name] = counts.get(node.name, 0) + 1
    used: dict[str, int] = {}
    for node in merged:
        if counts[node.name] > 1:
            used[node.name] = used.get(node.name, 0) + 1
            new_name = f"{node.name} · {node.source_name}"
            if used[node.name] > 1:
                new_name += f" {used[node.name]}"
            node.name = new_name[:160]
            if node.proxy is not None:
                node.proxy["name"] = node.name
            elif node.uri and "#" in node.uri:
                node.uri = node.uri.rsplit("#", 1)[0] + "#" + node.name
    return merged, filtered

def proxy_to_uri(node: NormalizedNode) -> str | None:
    proxy = node.proxy
    if not proxy or str(proxy.get("type", "")).lower() != "vless":
        return node.uri
    server, port, uuid = proxy.get("server"), proxy.get("port"), proxy.get("uuid")
    if not server or not port or not uuid:
        return None
    query: dict[str, str] = {"encryption": "none", "type": str(proxy.get("network") or "tcp")}
    if proxy.get("tls"):
        reality = proxy.get("reality-opts") if isinstance(proxy.get("reality-opts"), dict) else {}
        query["security"] = "reality" if reality else "tls"
        if proxy.get("servername"):
            query["sni"] = str(proxy["servername"])
        if proxy.get("client-fingerprint"):
            query["fp"] = str(proxy["client-fingerprint"])
        if reality.get("public-key"):
            query["pbk"] = str(reality["public-key"])
        if reality.get("short-id"):
            query["sid"] = str(reality["short-id"])
        if proxy.get("flow"):
            query["flow"] = str(proxy["flow"])
    network = str(proxy.get("network") or "tcp")
    opts = proxy.get(f"{network}-opts")
    if isinstance(opts, dict):
        if opts.get("path"):
            query["path"] = str(opts["path"])
        headers = opts.get("headers")
        if isinstance(headers, dict) and headers.get("Host"):
            query["host"] = str(headers["Host"])
        if network == "grpc" and opts.get("grpc-service-name"):
            query["serviceName"] = str(opts["grpc-service-name"])
    if proxy.get("flow"):
        query["flow"] = str(proxy["flow"])
    host = f"[{server}]" if ":" in str(server) and not str(server).startswith("[") else str(server)
    return f"vless://{quote(str(uuid), safe='')}@{host}:{int(port)}?{urlencode(query)}#{quote(node.name)}"


def render_internal_sources(nodes: list[NormalizedNode]) -> list[tuple[bytes, str]]:
    sources: list[tuple[bytes, str]] = []
    proxies = [node.proxy for node in nodes if node.proxy is not None]
    uris = [value for node in nodes if (value := proxy_to_uri(node)) is not None]
    if proxies:
        sources.append((yaml.safe_dump({"proxies": proxies}, allow_unicode=True, sort_keys=False).encode(), "yaml"))
    if uris:
        sources.append((("\n".join(uris) + "\n").encode(), "txt"))
    return sources
