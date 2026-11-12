"""Integration + load stress for savvy-progress: synthetic DB boundaries, concurrency, timing.

Covers what the unit test cannot: the day window edges on a controlled DB, provider vs table
mixing, orphan usage rows, concurrent snapshot writers, and the cost of one snapshot on the
tool-call hot path.
Run: python3 test_integration.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

MOD_PATH = Path(os.environ.get("SAVVY_MODULE") or (Path(__file__).resolve().parents[2] / "plugins/savvy-progress/__init__.py"))
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
    spec = importlib.util.spec_from_file_location("savvy_progress_mod2", MOD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["savvy_progress_mod2"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_db(path: Path, now: float) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, parent_session_id TEXT, created_source TEXT, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
            ended_at REAL, started_at REAL, last_activity_at REAL
        );
        CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT, api_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
            last_seen REAL
        );
        """
    )
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    rows = [
        # today at 00:00:01, priced by table only
        ("s-in", midnight + 1, 0.0, 0.0),
        # later today, provider-reported cost
        ("s-prov", min(now - 60, midnight + 3600), 0.5, 0.0),
        # yesterday 23:59:59, must be excluded
        ("s-out", midnight - 1, 9.0, 0.0),
        # no start, but recent activity: must still count
        ("s-activity", None, 0.25, 0.0),
    ]
    for sid, started, actual, estimated in rows:
        conn.execute(
            "INSERT INTO sessions (id, parent_session_id, created_source, model, started_at, last_activity_at)"
            " VALUES (?, NULL, 'tui', 'deepseek-v4.1-flash', ?, ?)",
            (sid, started, now - 600),
        )
        conn.execute(
            "INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count,"
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, estimated_cost_usd,"
            " actual_cost_usd, last_seen) VALUES (?, 'deepseek-v4.1-flash', 'custom', 1, 1000, 100, 1000000, 0, ?, ?, ?)",
            (sid, estimated, actual, now - 600),
        )
    # orphan usage row: no sessions row, must be ignored by the JOIN
    conn.execute(
        "INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, estimated_cost_usd,"
        " actual_cost_usd, last_seen) VALUES ('ghost', 'deepseek-v4.1-flash', 'custom', 1, 5000, 500, 0, 0, 0, 0, ?)",
        (now,),
    )
    conn.commit()
    conn.close()


def main() -> int:
    m = load()
    now = time.time()

    # ---- day window on a controlled DB ---------------------------------------
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "state.db"
        make_db(db, now)
        original = m._db_path
        m._db_path = lambda: db  # type: ignore[assignment]
        try:
            day = m._day_spend(now)
        finally:
            m._db_path = original  # type: ignore[assignment]

        # table: 1M cache reads = $0.006, 1000 in = $0.0003, 100 out = $0.00012 -> ~0.00642
        expected_table = (1000 / 1e6 * 0.30) + (100 / 1e6 * 1.20) + (1e6 / 1e6 * 0.006)
        check("day: calendar-day sessions counted, yesterday excluded", day["sessions"] == 3, str(day))
        check("day: provider cost beats table", day["cost_source"] == "provider", day["cost_source"])
        expected_total = round(0.5 + 0.25 + expected_table, 6)
        check(
            "day: total = provider + table rows",
            abs(day["cost_usd"] - expected_total) < 1e-4,
            f"{day['cost_usd']} vs {expected_total}",
        )
        check("day: yesterday's session excluded", day["sessions"] == 3 and day["cost_usd"] < 9.0, str(day))
        check("day: orphan usage row ignored", day["cost_usd"] < 1.0, str(day["cost_usd"]))

        # a 24h window in the future of that DB = nothing
        far = day
        for _ in range(1):
            pass
        m2 = load()
        m2._db_path = lambda: db  # type: ignore[assignment]
        future = m2._day_spend(now + 48 * 3600)
        check("day: future clock sees only later rows", future["sessions"] == 0, str(future))

    # ---- hot path timing on the live DB --------------------------------------
    sid = None
    try:
        conn = sqlite3.connect(f"file:{Path.home()}/.hermes/state.db?mode=ro", uri=True)
        row = conn.execute(
            "SELECT id FROM sessions WHERE created_source='tui' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        sid = row[0] if row else None
        conn.close()
    except sqlite3.Error as exc:
        print(f"      db read failed: {exc}")

    if sid:
        m._last_write = 0.0
        t0 = time.perf_counter()
        payload, entry = m._build_snapshot(sid, None, None, None, time.time(), None)
        single = (time.perf_counter() - t0) * 1000
        check(f"snapshot build under 100ms (got {single:.1f}ms)", single < 100.0, f"{single:.1f}ms")

        t0 = time.perf_counter()
        samples = 10
        for _ in range(samples):
            m._build_snapshot(sid, None, None, None, time.time(), None)
        avg = (time.perf_counter() - t0) * 1000 / samples
        check(f"10 builds average under 60ms (got {avg:.1f}ms)", avg < 60.0, f"{avg:.1f}ms")

        # ---- concurrent writers -------------------------------------------
        errors: list[str] = []

        def writer(n: int) -> None:
            try:
                for _ in range(5):
                    m._last_write = 0.0
                    m.snapshot(sid)
            except Exception as exc:  # noqa: BLE001 — the test wants any failure
                errors.append(f"thread{n}:{type(exc).__name__}:{exc}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        check("concurrent snapshot: no exceptions", not errors, "; ".join(errors[:3]))

        snap_path = m._snapshot_path()
        sessions_path = m._sessions_path()
        for label, path in (("snapshot", snap_path), ("sessions", sessions_path)):
            try:
                data = json.loads(path.read_text())
                ok = isinstance(data, dict) and bool(data)
            except Exception as exc:  # noqa: BLE001
                ok = False
                data = str(exc)
            check(f"concurrent snapshot: {label} file is valid JSON", ok, str(data)[:120])
        check("concurrent snapshot: still holds the new fields", "day_sessions" in json.loads(snap_path.read_text()))

        # ---- the /svprogress handler must never traceback ------------------
        text = m._cmd_progress("")
        check("command returns a card", isinstance(text, str) and len(text) > 40, str(text)[:80])
        check("command card has the new rows", "burn/hour" in text and "cache saved" in text and "today" in text)

        # a snapshot with no data must degrade, not raise
        empty = m.render_card({})
        check("render_card({}) survives", isinstance(empty, str) and "burn" in empty, empty[:60])

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
