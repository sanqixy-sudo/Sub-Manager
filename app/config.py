from __future__ import annotations

import os
from pathlib import Path


APP_VERSION = "3.4.1"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "submanager.db"
CACHE_DIR = DATA_DIR / "cache"
UPSTREAM_CACHE_DIR = DATA_DIR / "upstreams"
BACKUP_DIR = DATA_DIR / "backups"
SECRETS_DIR = DATA_DIR / "secrets"
MANUAL_NODE_KEY_PATH = SECRETS_DIR / "manual-node.key"
STATIC_DIR = Path(__file__).parent / "static"

SUBCONVERTER_URL = os.getenv("SUBCONVERTER_URL", "http://127.0.0.1:25500").rstrip("/")
INTERNAL_BASE_URL = os.getenv("INTERNAL_BASE_URL", "http://127.0.0.1:7777").rstrip("/")
RULE_CONFIG = os.getenv("RULE_CONFIG", "config/submanager.ini")
SESSION_COOKIE = "sm_session"

BOOTSTRAP_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
BOOTSTRAP_PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
BOOTSTRAP_SITE_NAME = os.getenv("SITE_NAME", "Sub Manager").strip() or "Sub Manager"

MAX_UPSTREAM_BYTES = int(os.getenv("MAX_UPSTREAM_BYTES", str(8 * 1024 * 1024)))
MAX_REDIRECTS = int(os.getenv("MAX_REDIRECTS", "5"))
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
MIHOMO_BINARY = os.getenv("MIHOMO_BINARY", "/usr/local/bin/mihomo")
MIHOMO_VERSION = "1.19.30"
HEALTH_CONNECTIVITY_URL = "https://cp.cloudflare.com/generate_204"
HEALTH_GOOGLE_URL = "https://www.gstatic.com/generate_204"


CLIENT_TYPES: dict[str, dict[str, object]] = {
    "mihomo": {"label": "Mihomo / Clash Verge / Clash Meta（推荐）", "target": "clash", "ext": "yaml", "rules": True},
    "clash": {"label": "Clash YAML（兼容 Mihomo）", "target": "clash", "ext": "yaml", "rules": True},
    "clashr": {"label": "ClashR", "target": "clashr", "ext": "yaml", "rules": True},
    "surge2": {"label": "Surge 2", "target": "surge", "ver": "2", "ext": "conf", "rules": True},
    "surge3": {"label": "Surge 3", "target": "surge", "ver": "3", "ext": "conf", "rules": True},
    "surge4": {"label": "Surge 4 / 5", "target": "surge", "ver": "4", "ext": "conf", "rules": True},
    "quan": {"label": "Quantumult", "target": "quan", "ext": "conf", "rules": True},
    "quanx": {"label": "Quantumult X", "target": "quanx", "ext": "conf", "rules": True},
    "loon": {"label": "Loon", "target": "loon", "ext": "conf", "rules": True},
    "surfboard": {"label": "Surfboard", "target": "surfboard", "ext": "conf", "rules": True},
    "v2ray": {"label": "V2Ray / Shadowrocket", "target": "v2ray", "ext": "txt", "rules": False},
    "ss": {"label": "SS / Shadowrocket", "target": "ss", "ext": "txt", "rules": False},
    "sssub": {"label": "SS Android", "target": "sssub", "ext": "txt", "rules": False},
    "ssd": {"label": "SSD", "target": "ssd", "ext": "txt", "rules": False},
    "ssr": {"label": "SSR", "target": "ssr", "ext": "txt", "rules": False},
}

RULE_PRESET = {
    "id": "cn-direct",
    "name": "大陆直连，其他代理",
    "description": "局域网与中国大陆域名/IP 直连，其余流量交给 PROXY。",
    "groups": ["PROXY", "♻️ 自动选择"],
}
