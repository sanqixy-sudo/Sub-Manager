from __future__ import annotations

import base64
import json

from app.services.health import _classify_error, _notify_event
from app.services.parser import uri_to_proxy


def test_vless_allow_insecure_sets_skip_cert_verify() -> None:
    proxy = uri_to_proxy(
        "vless://uuid-1@example.com:443?security=tls&sni=example.com&allowInsecure=1#node", "node")
    assert proxy is not None
    assert proxy["skip-cert-verify"] is True
    assert proxy["tls"] is True


def test_trojan_insecure_param_sets_skip_cert_verify() -> None:
    proxy = uri_to_proxy("trojan://pass@example.com:443?insecure=true#node", "node")
    assert proxy is not None
    assert proxy["skip-cert-verify"] is True
    # 无该参数时不得出现 None 值字段
    clean = uri_to_proxy("trojan://pass@example.com:443#node", "node")
    assert clean is not None and "skip-cert-verify" not in clean
    assert all(value is not None for value in clean.values())


def test_hysteria2_obfs_and_alpn_are_mapped() -> None:
    proxy = uri_to_proxy(
        "hy2://pass@example.com:443?obfs=salamander&obfs-password=secret&alpn=h3,h2#node", "node")
    assert proxy is not None
    assert proxy["type"] == "hysteria2"
    assert proxy["obfs"] == "salamander"
    assert proxy["obfs-password"] == "secret"
    assert proxy["alpn"] == ["h3", "h2"]


def test_tuic_congestion_controller_and_alpn_are_mapped() -> None:
    proxy = uri_to_proxy(
        "tuic://uuid-1:pass@example.com:443?congestion_controller=bbr&alpn=h3#node", "node")
    assert proxy is not None
    assert proxy["congestion-controller"] == "bbr"
    assert proxy["alpn"] == ["h3"]
    dashed = uri_to_proxy("tuic://uuid-1:pass@example.com:443?congestion-controller=cubic#node", "node")
    assert dashed is not None and dashed["congestion-controller"] == "cubic"


def _vmess_uri(payload: dict) -> str:
    return "vmess://" + base64.urlsafe_b64encode(
        json.dumps(payload).encode()).decode().rstrip("=")


def test_vmess_verify_false_sets_skip_cert_verify() -> None:
    base = {"add": "example.com", "port": 443, "id": "uuid-1", "aid": 0, "net": "tcp", "tls": "tls"}
    proxy = uri_to_proxy(_vmess_uri({**base, "verify": False}), "node")
    assert proxy is not None and proxy["skip-cert-verify"] is True
    clean = uri_to_proxy(_vmess_uri(base), "node")
    assert clean is not None and "skip-cert-verify" not in clean


def test_vmess_allow_insecure_sets_skip_cert_verify() -> None:
    base = {"add": "example.com", "port": 443, "id": "uuid-1", "aid": 0, "net": "tcp", "tls": "tls"}
    proxy = uri_to_proxy(_vmess_uri({**base, "allowInsecure": True}), "node")
    assert proxy is not None and proxy["skip-cert-verify"] is True


def test_notify_event_threshold_crossing() -> None:
    # 连续失败恰好在阈值当次告警一次，越过阈值后不再重复
    assert _notify_event("unavailable", 1, 0, 3) is None
    assert _notify_event("unavailable", 2, 1, 3) is None
    assert _notify_event("unavailable", 3, 2, 3) == "down"
    assert _notify_event("unavailable", 4, 3, 3) is None
    # 从告警状态恢复时通知一次
    assert _notify_event("healthy", 0, 3, 3) == "recovered"
    assert _notify_event("google_blocked", 0, 5, 3) == "recovered"
    # 未达阈值的历史失败恢复不通知
    assert _notify_event("healthy", 0, 2, 3) is None
    # 未启用（阈值 0）一律不通知
    assert _notify_event("unavailable", 1, 0, 0) is None
    assert _notify_event("healthy", 0, 5, 0) is None


def test_classify_error_categories() -> None:
    assert _classify_error("context deadline exceeded") == "timeout"
    assert _classify_error("i/o timeout") == "timeout"
    assert _classify_error("connect: connection refused") == "refused"
    assert _classify_error("read: connection reset by peer") == "reset"
    assert _classify_error("lookup node.example.com: no such host") == "dns"
    assert _classify_error("dns resolve failed") == "dns"
    assert _classify_error("tls: handshake failure") == "tls"
    assert _classify_error("x509: certificate has expired") == "tls"
    assert _classify_error("proxy authentication required") == "auth"
    assert _classify_error("unexpected status 407") == "auth"
    assert _classify_error("something weird happened") == "other"


def test_classify_error_never_leaks_raw_message() -> None:
    sensitive = "dial tcp 203.0.113.7:443: i/o timeout"
    kind = _classify_error(sensitive)
    assert kind == "timeout"
    assert "203.0.113.7" not in kind and sensitive not in kind
