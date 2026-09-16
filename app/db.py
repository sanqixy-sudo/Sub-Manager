from __future__ import annotations

import sqlite3
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import (
    BACKUP_DIR,
    BOOTSTRAP_ADMIN_PASSWORD,
    BOOTSTRAP_ADMIN_USERNAME,
    BOOTSTRAP_PUBLIC_BASE_URL,
    BOOTSTRAP_SITE_NAME,
    CACHE_DIR,
    DATA_DIR,
    DB_PATH,
    SECRETS_DIR,
    UPSTREAM_CACHE_DIR,
)
from .security import hash_password, utcnow_iso


SCHEMA_VERSION = 11


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _backup_existing(old_version: int) -> None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    label = "v2" if old_version < 3 else f"v{old_version}"
    target = BACKUP_DIR / f"submanager-{label}-{stamp}.db"
    if not target.exists():
        source = sqlite3.connect(DB_PATH)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()


def init_db() -> None:
    for path in (DATA_DIR, CACHE_DIR, UPSTREAM_CACHE_DIR, BACKUP_DIR, SECRETS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    existing = DB_PATH.exists()
    old_version = 0
    if existing:
        with db() as conn:
            old_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if old_version > SCHEMA_VERSION:
            raise RuntimeError("数据库版本高于当前程序；回滚必须恢复升级前备份")
        if old_version < SCHEMA_VERSION:
            _backup_existing(old_version)

    with db() as conn:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                interval_minutes INTEGER NOT NULL DEFAULT 30,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_refresh_at TEXT,
                last_refresh_status TEXT,
                last_error TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                refresh_count INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upstreams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                client_type TEXT NOT NULL,
                name TEXT NOT NULL,
                slug TEXT NOT NULL,
                update_interval_minutes INTEGER NOT NULL DEFAULT 60,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(subscription_id, slug)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                user_agent TEXT,
                ip_hint TEXT
            );
            CREATE TABLE IF NOT EXISTS refresh_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                upstream_total INTEGER NOT NULL DEFAULT 0,
                upstream_success INTEGER NOT NULL DEFAULT 0,
                output_total INTEGER NOT NULL DEFAULT 0,
                output_success INTEGER NOT NULL DEFAULT 0,
                node_count INTEGER NOT NULL DEFAULT 0,
                filtered_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS node_snapshots (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                final_name TEXT NOT NULL,
                source_name TEXT NOT NULL,
                protocol TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(subscription_id, position)
            );
            CREATE TABLE IF NOT EXISTS node_preferences (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                node_key TEXT NOT NULL,
                alias TEXT,
                baseline_name TEXT NOT NULL,
                current_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                previous_name TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(subscription_id, node_key)
            );
            CREATE TABLE IF NOT EXISTS manual_nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                name TEXT NOT NULL, protocol TEXT NOT NULL, node_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL DEFAULT '', ciphertext BLOB NOT NULL, nonce BLOB NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(subscription_id,node_key)
            );
            CREATE TABLE IF NOT EXISTS health_check_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE CASCADE,
                node_key TEXT, trigger TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT,
                total INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
                available INTEGER NOT NULL DEFAULT 0, unavailable INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0, error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS node_health_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES health_check_runs(id) ON DELETE CASCADE,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                node_key TEXT NOT NULL, node_name TEXT NOT NULL, protocol TEXT NOT NULL,
                status TEXT NOT NULL, connectivity_ok INTEGER, connectivity_latency_ms INTEGER,
                google_ok INTEGER, google_latency_ms INTEGER, error_code TEXT, tested_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS node_health_latest (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                node_key TEXT NOT NULL, status TEXT NOT NULL,
                connectivity_ok INTEGER, connectivity_latency_ms INTEGER,
                google_ok INTEGER, google_latency_ms INTEGER,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, tested_at TEXT NOT NULL,
                PRIMARY KEY(subscription_id,node_key)
            );
            CREATE TABLE IF NOT EXISTS proxy_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                name TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(subscription_id,name)
            );
            CREATE TABLE IF NOT EXISTS proxy_category_sources (
                category_id INTEGER NOT NULL REFERENCES proxy_categories(id) ON DELETE CASCADE,
                upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
                PRIMARY KEY(category_id,upstream_id)
            );
            CREATE TABLE IF NOT EXISTS proxy_category_nodes (
                category_id INTEGER NOT NULL REFERENCES proxy_categories(id) ON DELETE CASCADE,
                node_key TEXT NOT NULL,
                PRIMARY KEY(category_id,node_key)
            );
            CREATE TABLE IF NOT EXISTS revoked_tokens (
                token TEXT PRIMARY KEY,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                revoked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS node_identities (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                canonical_key TEXT NOT NULL, node_key TEXT NOT NULL,
                PRIMARY KEY(subscription_id,canonical_key)
            );
            CREATE TABLE IF NOT EXISTS health_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, events TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_upstreams_subscription ON upstreams(subscription_id, sort_order);
            CREATE INDEX IF NOT EXISTS idx_refresh_runs_finished ON refresh_runs(finished_at DESC);
            CREATE INDEX IF NOT EXISTS idx_refresh_runs_subscription ON refresh_runs(subscription_id, finished_at DESC);
            CREATE INDEX IF NOT EXISTS idx_node_preferences_status ON node_preferences(subscription_id,status);
            CREATE INDEX IF NOT EXISTS idx_manual_nodes_subscription ON manual_nodes(subscription_id,sort_order,id);
            CREATE INDEX IF NOT EXISTS idx_health_results_node ON node_health_results(subscription_id,node_key,tested_at DESC);
            CREATE INDEX IF NOT EXISTS idx_health_results_tested ON node_health_results(tested_at DESC);
            CREATE INDEX IF NOT EXISTS idx_health_runs_started ON health_check_runs(started_at DESC);
            """
        )
        for name, ddl in {
            "last_attempt_at": "TEXT",
            "last_success_at": "TEXT",
            "refresh_count": "INTEGER NOT NULL DEFAULT 0",
            "note": "TEXT NOT NULL DEFAULT ''",
            "node_count": "INTEGER NOT NULL DEFAULT 0",
            "filtered_count": "INTEGER NOT NULL DEFAULT 0",
            "last_duration_ms": "INTEGER",
            "cache_state": "TEXT NOT NULL DEFAULT 'empty'",
            "config_revision": "INTEGER NOT NULL DEFAULT 1",
            "rename_mode": "TEXT NOT NULL DEFAULT 'passthrough'",
            "rename_ignore": "TEXT NOT NULL DEFAULT ''",
            "rename_template": "TEXT NOT NULL DEFAULT '{index}-{flag}-{name}-{traffic}-{reset}'",
            "node_tracking_initialized": "INTEGER NOT NULL DEFAULT 0",
            "ip_whitelist": "TEXT NOT NULL DEFAULT ''",
        }.items():
            _ensure_column(conn, "subscriptions", name, ddl)
        for name, ddl in {
            "last_status": "TEXT",
            "last_http_status": "INTEGER",
            "last_content_type": "TEXT",
            "last_bytes": "INTEGER NOT NULL DEFAULT 0",
            "last_duration_ms": "INTEGER",
            "last_success_at": "TEXT",
            "last_error": "TEXT",
            "source_format": "TEXT",
            "node_count": "INTEGER NOT NULL DEFAULT 0",
            "filtered_count": "INTEGER NOT NULL DEFAULT 0",
            "used_cache": "INTEGER NOT NULL DEFAULT 0",
            "rename_policy": "TEXT NOT NULL DEFAULT 'inherit'",
            "rename_ignore": "TEXT NOT NULL DEFAULT ''",
            "rename_template": "TEXT NOT NULL DEFAULT '{index}|{flag}|{name}|{traffic}|{reset}'",
        }.items():
            _ensure_column(conn, "upstreams", name, ddl)
        for name, ddl in {
            "node_key": "TEXT NOT NULL DEFAULT ''",
            "rule_name": "TEXT NOT NULL DEFAULT ''",
            "alias": "TEXT",
            "confirmation_status": "TEXT NOT NULL DEFAULT 'confirmed'",
            "previous_name": "TEXT",
            "traffic": "TEXT NOT NULL DEFAULT ''",
            "reset": "TEXT NOT NULL DEFAULT ''",
            'source_id': 'INTEGER',
        }.items():
            _ensure_column(conn, "node_snapshots", name, ddl)
        _ensure_column(conn, "node_preferences", "sort_order", "INTEGER NOT NULL DEFAULT 0")
        # V8 persists effective node order independently from volatile names and traffic counters.
        if old_version < 8:
            conn.execute("""UPDATE node_preferences SET sort_order=COALESCE(
                (SELECT position FROM node_snapshots n WHERE n.subscription_id=node_preferences.subscription_id
                 AND n.node_key=node_preferences.node_key), sort_order)""")

        if old_version < 6:
            conn.execute(
                "UPDATE subscriptions SET rename_template=? WHERE rename_template=?",
                ("{index}|{flag}|{name}|{traffic}|{reset}", "{index}-{flag}-{name}-{traffic}-{reset}"),
            )

        ts = utcnow_iso()
        legacy_hours = conn.execute("SELECT value FROM settings WHERE key='health_check_interval_hours'").fetchone()
        if legacy_hours:
            try:
                minutes = max(10, min(10080, int(legacy_hours[0]) * 60))
            except (ValueError, TypeError):
                minutes = 30
            conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('health_check_interval_minutes',?,?)", (str(minutes), ts))
        conn.execute("UPDATE health_check_runs SET status='interrupted',finished_at=?,error_code='service_restarted' WHERE status='running'", (ts,))
        defaults = {
            "site_name": BOOTSTRAP_SITE_NAME,
            "public_base_url": BOOTSTRAP_PUBLIC_BASE_URL,
            "admin_username": BOOTSTRAP_ADMIN_USERNAME,
            "admin_password_hash": hash_password(BOOTSTRAP_ADMIN_PASSWORD),
            "default_refresh_interval_minutes": "30",
            "default_output_interval_minutes": "60",
            "default_client_type": "mihomo",
            "upstream_timeout_seconds": "30",
            "converter_timeout_seconds": "90",
            "pseudo_node_filter": r"(?i)(剩余流量|套餐到期|距离下次重置|建议每天更新|如果很少节点可用|官网更新)",
            "scheduler_enabled": "1",
            "stale_cache_fallback": "1",
            "skip_failed_upstreams": "1",
            "upstream_user_agent": "ClashVergeRev/2.4 SubManager/3.0",
            "scheduler_concurrency": "3",
            "health_check_enabled": "1",
            "health_check_interval_minutes": "30",
            "health_check_concurrency": "5",
            "health_check_timeout_seconds": "8",
            "health_notify_enabled": "0",
            "health_notify_webhook": "",
            "health_notify_threshold": "3",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                (key, value, ts),
            )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('node_key_secret',?,?)",
            (secrets.token_hex(32), ts),
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_settings(values: dict[str, str]) -> None:
    ts = utcnow_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, ts),
            )
        conn.commit()
