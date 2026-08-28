from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)
_PROXY_RE = re.compile(r"(?:vmess|vless|trojan|ss|ssr|hy2|hysteria2|tuic|anytls)://[^\s'\"<>]+", re.I)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
_SECRET_PAIR_RE = re.compile(r"(?i)(token|password|passwd|pwd|uuid|secret)=([^&\s]+)")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, encoded: str) -> tuple[bool, bool]:
    """Return (valid, needs_rehash), accepting the V2 PBKDF2 format."""
    if encoded.startswith("$argon2"):
        try:
            valid = _ph.verify(encoded, password)
            return bool(valid), bool(valid and _ph.check_needs_rehash(encoded))
        except (VerifyMismatchError, InvalidHashError):
            return False, False
    try:
        algo, iterations_s, salt_hex, digest_hex = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False, False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations_s)
        )
        valid = hmac.compare_digest(digest.hex(), digest_hex)
        return valid, valid
    except Exception:
        return False, False


def new_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redact(value: object, limit: int = 600, known_tokens: tuple[str, ...] = ()) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = _PROXY_RE.sub("[代理凭据]", text)
    text = _URL_RE.sub("[上游链接]", text)
    text = _UUID_RE.sub("[UUID]", text)
    text = _SECRET_PAIR_RE.sub(lambda m: f"{m.group(1)}=[已隐藏]", text)
    for token in known_tokens:
        if token:
            text = text.replace(token, "[TOKEN]")
    return text[:limit] or "未知错误"


def masked_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "未知主机"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}/•••"
    except Exception:
        return "[上游链接]"


def safe_filename(value: str, default: str = "subscription") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return cleaned[:120] or default


def ip_allowed(whitelist: str, client_ip: str) -> bool:
    """Per-subscription public fetch gate: empty whitelist allows everyone."""
    if not whitelist.strip():
        return True
    try:
        client = ipaddress.ip_address(client_ip.strip())
    except ValueError:
        return False
    for raw in whitelist.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            network = ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        try:
            if client in network:
                return True
        except TypeError:
            continue
    return False
