from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex


def render_installer(base, token):
    from .service import bootstrap_server
    root = Path(__file__).resolve().parents[2]
    template = (root/'agent/install.sh').read_text('utf-8')
    replacements = {'@BASE@': shlex.quote(base), '@PAIRING@': json.dumps({'base': base, 'token': token, 'server_id': bootstrap_server(token)})}
    for name in ('control_agent.py', 'stats.py'):
        replacements['@'+name.upper().replace('.', '_')+'_SHA@'] = hashlib.sha256((root/'agent'/name).read_bytes()).hexdigest()
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template
