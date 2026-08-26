from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from ..config import SESSION_COOKIE
from ..db import db
from ..security import token_digest, utcnow_iso


def require_auth(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "未登录")
    digest = token_digest(token)
    now = datetime.now(timezone.utc)
    with db() as conn:
        row = conn.execute("SELECT id,expires_at FROM sessions WHERE token_hash=?", (digest,)).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) <= now:
            if row:
                conn.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
                conn.commit()
            raise HTTPException(401, "登录已失效")
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (utcnow_iso(), row["id"]))
        conn.commit()
