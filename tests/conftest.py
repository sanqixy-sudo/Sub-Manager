from __future__ import annotations

import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="submanager-v3-tests-")
os.environ["INTERNAL_BASE_URL"] = "http://127.0.0.1:7777"
