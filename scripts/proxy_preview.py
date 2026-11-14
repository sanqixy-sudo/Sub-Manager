"""Disposable local UI fixture; never points at the production data directory."""
from __future__ import annotations
import os
import tempfile

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='submanager-proxy-preview-')
os.environ['ADMIN_PASSWORD'] = 'preview-only-password'
from app.db import init_db, set_settings
from app.control import service
from app.control.models import AssignmentIn, NodeIn, PackageIn, ServerIn, UserIn

init_db()
set_settings({'scheduler_enabled':'0','health_check_enabled':'0','public_base_url':'https://preview.example.test'})
a=service.save_server(ServerIn(name='香港入口',host='hk.example.test'))
b=service.save_server(ServerIn(name='日本落地',host='jp.example.test'))
exit_node=service.save_node(NodeIn(server_id=b,name='日本 AnyTLS',server_name='jp.example.test'))
entry=service.save_node(NodeIn(server_id=a,name='香港 → 日本',server_name='hk.example.test',next_node_id=exit_node))
user=service.save_user(UserIn(username='preview',password='preview-only-password'))['id']
package=service.save_package(PackageIn(name='标准套餐',quota_gb=100,node_ids=[entry]))
service.assign_package(AssignmentIn(user_id=user,package_id=package))
import uvicorn
uvicorn.run('app.main:app',host='127.0.0.1',port=17781,access_log=False)
