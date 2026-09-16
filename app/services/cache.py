from __future__ import annotations

import hashlib
import base64
from threading import RLock
import json
import re
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR, UPSTREAM_CACHE_DIR

_cache_lock = RLock()


def output_paths(token: str, slug: str) -> tuple[Path, Path]:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"{token}_{slug}")
    return CACHE_DIR / f"{safe}.body", CACHE_DIR / f"{safe}.json"


def read_output(token: str, slug: str) -> tuple[bytes, dict[str, Any]] | None:
    body_path, meta_path = output_paths(token, slug)
    try:
        with _cache_lock:
            meta = json.loads(meta_path.read_text('utf-8'))
            encoded = meta.pop('_body_base64', None)
            body = base64.b64decode(encoded, validate=True) if encoded is not None else body_path.read_bytes()
            return body, meta
    except (OSError, ValueError):
        return None


def write_output(token: str, slug: str, body: bytes, meta: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    body_path, meta_path = output_paths(token, slug)
    body_tmp, meta_tmp = body_path.with_suffix(".body.tmp"), meta_path.with_suffix(".json.tmp")
    # The metadata file is a complete atomic generation. Legacy body remains
    # for backup/rollback tooling; readers never combine different generations.
    with _cache_lock:
        body_tmp.write_bytes(body)
        meta_tmp.write_text(json.dumps({**meta, '_body_base64': base64.b64encode(body).decode()}, ensure_ascii=False), 'utf-8')
        body_tmp.replace(body_path)
        meta_tmp.replace(meta_path)


def upstream_paths(subscription_id: int, upstream: dict[str, Any] | str) -> tuple[Path, Path]:
    url = upstream if isinstance(upstream, str) else str(upstream["url"])
    ident = hashlib.sha256(url.encode()).hexdigest()[:20]
    base = UPSTREAM_CACHE_DIR / f"sub_{subscription_id}_{ident}"
    return base.with_suffix(".body"), base.with_suffix(".json")


def delete_upstream(subscription_id: int, url: str) -> None:
    for path in upstream_paths(subscription_id, url):
        path.unlink(missing_ok=True)


def delete_group(subscription_id: int, token: str, slugs: list[str], upstream_urls: list[str]) -> None:
    for slug in slugs:
        for path in output_paths(token, slug):
            path.unlink(missing_ok=True)
    for url in upstream_urls:
        delete_upstream(subscription_id, url)


def move_token(old_token: str, new_token: str, slugs: list[str]) -> None:
    for slug in slugs:
        old = output_paths(old_token, slug)
        new = output_paths(new_token, slug)
        for source, target in zip(old, new):
            if source.exists():
                source.replace(target)
