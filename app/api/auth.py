from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response

from ..config import SESSION_COOKIE, SESSION_TTL_SECONDS
from ..db import db, get_setting, set_settings
from ..schemas import LoginPayload
from ..security import hash_password, new_token, token_digest, utcnow_iso, verify_password
from .deps import require_auth


router = APIRouter(prefix="/api/auth", tags=["auth"])
_attempts: dict[str, deque[float]] = defaultdict(deque)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login")
def login(payload: LoginPayload, request: Request, response: Response) -> dict[str, object]:
    key = _client_key(request)
    now = time.monotonic()
    attempts = _attempts[key]
    while attempts and attempts[0] < now - 600:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(429, "登录失败次数过多，请稍后再试")
    username = get_setting("admin_username", "admin")
    valid, needs_rehash = verify_password(payload.password, get_setting("admin_password_hash"))
    if not (hmac.compare_digest(payload.username, username) and valid):
        attempts.append(now)
        raise HTTPException(401, "用户名或密码错误")
    attempts.clear()
    if needs_rehash:
        set_settings({"admin_password_hash": hash_password(payload.password)})
    token = new_token(36)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(seconds=SESSION_TTL_SECONDS)
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (created.isoformat(),))
        conn.execute(
            "INSERT INTO sessions(token_hash,created_at,expires_at,last_seen_at,user_agent,ip_hint) VALUES(?,?,?,?,?,?)",
            (token_digest(token), created.isoformat(), expires.isoformat(), created.isoformat(),
             request.headers.get("user-agent", "")[:300], hashlib.sha256(key.encode()).hexdigest()[:16]),
        )
        conn.commit()
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")
    return {"ok": True, "default_credentials": username == "admin" and payload.password == "admin"}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(token),))
            conn.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/session")
def session(request: Request) -> dict[str, object]:
    require_auth(request)
    username = get_setting("admin_username", "admin")
    valid, _ = verify_password("admin", get_setting("admin_password_hash"))
    return {"authenticated": True, "username": username, "default_credentials": username == "admin" and valid}


# V2 compatibility: old UI/scripts may still call /api/login during a rolling upgrade.
legacy_router = APIRouter(tags=["auth"])


@legacy_router.post("/api/login")
def legacy_login(payload: LoginPayload, request: Request, response: Response) -> dict[str, object]:
    return login(payload, request, response)
