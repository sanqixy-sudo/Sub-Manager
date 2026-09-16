"""Seed fictional nodes exclusively in a fresh .test-* data directory."""
import asyncio
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import DATA_DIR, DB_PATH

if not DATA_DIR.name.startswith('.test-') or DB_PATH.exists():
    raise SystemExit('Refusing to seed an existing or non-test database')

from app.db import init_db, set_settings
from app.repository import create_subscription
from app.schemas import SubscriptionIn, ProxyCategoryIn
from app.services.refresh import refresh_subscription
from app.services.health import _new_run, _store_result, _load_group_nodes
from app.categories import save_category

init_db()
set_settings({'scheduler_enabled': '0', 'health_check_enabled': '0'})
content = '\n'.join(f'trojan://fixture-only@node-{i}.example.invalid:443#测试节点{i:02d}|很长的自定义名称|192.83GB|25D' for i in range(30))
group = create_subscription(SubscriptionIn.model_validate({'name': '测试订阅组', 'manual_content': content, 'outputs': [{'name': '中文订阅', 'slug': 'mihomo', 'client_type': 'mihomo'}]}))
asyncio.run(refresh_subscription(group['id']))
nodes = _load_group_nodes(group['id'])
save_category(group['id'], ProxyCategoryIn(name='家宽分类', node_keys=[n.node_key for n in nodes[:15]]))
asyncio.run(refresh_subscription(group['id']))
run = _new_run(group['id'], None, 'manual')
for i, node in enumerate(nodes):
    _store_result(run, group['id'], node, {'status': 'healthy' if i % 3 else 'unavailable', 'connectivity_ok': bool(i % 3), 'google_ok': bool(i % 3), 'connectivity_latency_ms': 100+i*20, 'google_latency_ms': 110+i*20})
print('Seeded fictional UI data; no remote subscriptions were requested')
