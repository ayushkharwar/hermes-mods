"""Read back the produced snapshot file and print it.

Run: python3 tests/savvy-progress/readback.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

SNAPSHOT = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "state" / "savvy-progress.json"

path = SNAPSHOT
print(f"file   : {path}")
if not path.exists():
    raise SystemExit("file missing — run test_plugin.py first")

stat = path.stat()
print(f"size   : {stat.st_size} bytes")
print(f"mtime  : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}")

data = json.loads(path.read_text(encoding="utf-8"))
print(f"keys   : {', '.join(data)}")
print(f"age_s  : {round(time.time() - data['ts'], 3)}")
print("\n--- JSON ---")
print(json.dumps(data, indent=2))
