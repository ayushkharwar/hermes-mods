"""Soak: 400 snapshot writes while a reader parses the file every 5 ms.

Proves the card file is never observed torn, never observed with a non-finite literal, and
that repeated writes do not leak file handles or slow down.
Run: python3 test_soak.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

MOD_PATH = Path(os.environ.get("SAVVY_MODULE") or (Path(__file__).resolve().parents[2] / "plugins/savvy-progress/__init__.py"))
WRITES = 400
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


def load():
    spec = importlib.util.spec_from_file_location("savvy_soak", MOD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["savvy_soak"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="savvy-soak-"))
    try:
        return run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(root: Path) -> int:
    (root / "state").mkdir(parents=True, exist_ok=True)
    db = root / "state.db"
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
    sid = "local-soak0001"
    conn.execute(
        "INSERT INTO sessions (id, parent_session_id, created_source, model, started_at, last_activity_at)"
        " VALUES (?, NULL, 'tui', 'deepseek-v4.1-flash', ?, ?)",
        (sid, now - 1800, now),
    )
    conn.execute(
        "INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, estimated_cost_usd,"
        " actual_cost_usd, last_seen) VALUES (?, 'deepseek-v4.1-flash', 'custom', 1, 1000, 200,"
        " 200000, 0, 0, 0, ?)",
        (sid, now),
    )
    conn.commit()
    conn.close()

    m = load()
    m._hermes_home = lambda: root  # type: ignore[assignment]
    m._db_path = lambda: db  # type: ignore[assignment]
    for cache in ("_price_cache", "_context_cache"):
        getattr(m, cache).clear()

    card = root / "state" / "savvy-progress.json"
    stop = threading.Event()
    problems: list[str] = []
    reads = 0

    def reader() -> None:
        nonlocal reads
        while not stop.is_set():
            try:
                raw = card.read_text()
            except FileNotFoundError:
                time.sleep(0.005)
                continue
            except OSError as exc:
                problems.append(f"read: {exc}")
                break
            if raw.strip():
                reads += 1
                try:
                    def reject(x):
                        raise ValueError(x)
                    data = json.loads(raw, parse_constant=reject)
                    if not isinstance(data, dict):
                        problems.append("not an object")
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"parse: {exc}: {raw[:60]!r}")
                    break
            time.sleep(0.005)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    fds_before = len(os.listdir("/proc/self/fd"))
    t0 = time.perf_counter()
    errors = 0
    for i in range(WRITES):
        m._last_write = 0.0
        try:
            payload = m.snapshot(sid)
            if not isinstance(payload, dict):
                errors += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            problems.append(f"write {i}: {type(exc).__name__}: {exc}")
            break
    elapsed = time.perf_counter() - t0
    fds_after = len(os.listdir("/proc/self/fd"))

    stop.set()
    thread.join(timeout=5)

    check("400 writes, no exception", errors == 0, f"{errors} errors")
    check("reader parsed the file repeatedly", reads > 20, str(reads))
    check("never read a torn or non-finite file", not problems, "; ".join(problems[:3]))
    check("no file descriptor leak", fds_after - fds_before <= 2, f"{fds_before} -> {fds_after}")
    per_write = elapsed / WRITES * 1000
    check(f"write path averages under 25ms ({per_write:.1f}ms)", per_write < 25.0, f"{per_write:.1f}ms")
    check("no .tmp left behind", not [p for p in (root / "state").iterdir() if p.suffix == ".tmp"])
    print(f"      {WRITES} writes in {elapsed:.2f}s, {reads} parses by the reader")

    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    if FAILS:
        print("FAILURES:")
        for f in FAILS:
            print("  -", f)
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
