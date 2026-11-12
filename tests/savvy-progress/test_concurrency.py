"""Multi-process stress: four Hermes-like processes firing the same hook at once.

The threading lock cannot help across processes, so this drives the real file lock and the
atomic replace: torn JSON, lost session entries and leftover `.tmp` files would all show up
here. Also checks that a write failure in one process never escapes as an exception.
Run: python3 test_concurrency.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MOD_PATH = Path(os.environ.get("SAVVY_MODULE") or (Path(__file__).resolve().parents[2] / "plugins/savvy-progress/__init__.py"))
WORKERS = 4
CALLS = 12
FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(f"{name}{(' :: ' + detail) if detail else ''}")
        print(f"FAIL  {name}  {detail}")
    else:
        print(f"ok    {name}")


WORKER_SRC = r'''
import importlib.util, sys, time
from pathlib import Path

home = Path(sys.argv[1])
session = sys.argv[2]
calls = int(sys.argv[3])

spec = importlib.util.spec_from_file_location("savvy_worker", sys.argv[4])
mod = importlib.util.module_from_spec(spec)
sys.modules["savvy_worker"] = mod
spec.loader.exec_module(mod)

errors = 0
for _ in range(calls):
    mod._last_write = 0.0
    try:
        mod.snapshot(session)
        time.sleep(0.005)
    except Exception:
        errors += 1
print(f"{session} errors={errors}")
sys.exit(1 if errors else 0)
'''


def seed_home(home: Path) -> None:
    (home / "state").mkdir(parents=True, exist_ok=True)
    db = home / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, created_source TEXT,
            model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
            ended_at REAL, started_at REAL, last_activity_at REAL);
        CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT,
            api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER, estimated_cost_usd REAL,
            actual_cost_usd REAL, last_seen REAL);
        """
    )
    now = time.time()
    for i in range(WORKERS):
        sid = f"local-{i:06d}"
        conn.execute(
            "INSERT INTO sessions (id, parent_session_id, created_source, model, started_at, last_activity_at)"
            " VALUES (?, NULL, 'tui', 'deepseek-v4.1-flash', ?, ?)",
            (sid, now - 1200 + i, now - 60),
        )
        conn.execute(
            "INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count,"
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, estimated_cost_usd,"
            " actual_cost_usd, last_seen) VALUES (?, 'deepseek-v4.1-flash', 'custom', 3, 5000, 800,"
            " 400000, 0, 0, 0, ?)",
            (sid, now - 60),
        )
    conn.commit()
    conn.close()


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="savvy-conc-"))
    workdir = Path(tempfile.mkdtemp(prefix="savvy-conc-src-"))
    worker = workdir / "worker.py"
    worker.write_text(WORKER_SRC)
    try:
        seed_home(home)
        env = dict(os.environ, HERMES_HOME=str(home))

        procs = [
            subprocess.Popen(
                [sys.executable, str(worker), str(home), f"local-{i:06d}", str(CALLS), str(MOD_PATH)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
            for i in range(WORKERS)
        ]
        outs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            outs.append((proc.returncode, out.strip(), err.strip()))

        for code, out, err in outs:
            check(f"worker exits 0 ({out or 'no output'})", code == 0, err[:200])

        snap = home / "state" / "savvy-progress.json"
        hist = home / "state" / "savvy-progress.sessions.json"
        check("snapshot file exists", snap.exists())
        check("history file exists", hist.exists())

        try:
            data = json.loads(snap.read_text())
            ok = isinstance(data, dict)
        except Exception as exc:  # noqa: BLE001
            ok = False
            data = str(exc)
        check("snapshot is valid JSON after 4 writers", ok, str(data)[:150])
        check("snapshot carries the new fields", isinstance(data, dict) and "day_sessions" in data)

        try:
            history = json.loads(hist.read_text())
            entries = history.get("sessions") if isinstance(history, dict) else None
        except Exception as exc:  # noqa: BLE001
            entries = None
            history = str(exc)
        check("history is valid JSON", isinstance(entries, dict), str(history)[:150])
        check(f"history kept all {WORKERS} sessions", isinstance(entries, dict) and len(entries) >= WORKERS, str(len(entries or {})))

        orphans = [p.name for p in (home / "state").iterdir() if p.name.endswith(".tmp")]
        check("no leftover .tmp files", not orphans, str(orphans[:3]))

        modes = {p.name: oct(p.stat().st_mode & 0o777) for p in (snap, hist)}
        check("files are 0600", all(mode == "0o600" for mode in modes.values()), str(modes))

        print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
        if FAILS:
            print("FAILURES:")
            for f in FAILS:
                print("  -", f)
            return 1
        print("RESULT: PASS")
        return 0
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
