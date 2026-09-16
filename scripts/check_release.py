"""Fail CI when an artifact advertises a different release version."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = (root / 'VERSION').read_text().strip()
assert json.loads((root / 'frontend/package.json').read_text())['version'] == version
assert f'SUB_MANAGER_VERSION:-{version}' in (root / 'compose.yaml').read_text()
assert f'V{version}' in (root / 'CHANGELOG.md').read_text(encoding='utf-8')
print(f'Release metadata consistent: {version}')
