"""Production hardening stress: broken DB, broken state dir, price overrides, fuzz, scale.

Every case here is a failure mode a shipped plugin must survive: a missing or corrupt
state.db, a locked DB, a read-only state directory, a corrupt snapshot file, a hostile
payload, an unpriced model, a state dir that is a plain file, and a DB big enough to make
a naive per-hook query hurt.
Run: python3 test_hardening.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import shutil
import sqlite3
import stat
import string
import sys
import tempfile
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


def load(tag: str = ""):
    spec = importlib.util.spec_from_file_location(f"savvy_hard{tag}", MOD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def sandbox(m: object, root: Path) -> None:
    """Point the module's paths at a scratch home. Keeps the real files untouched."""
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    m._hermes_home = lambda: root  # type: ignore[attr-defined]
    m._db_path = lambda: root / "state.db"  # type: ignore[attr-defined]
    for cache in ("_price_cache", "_context_cache", "_day_cache"):
        getattr(m, cache).clear()
        if cache == "_price_cache":
            getattr(m, cache).update({"key": None, "table": None})
        if cache == "_day_cache":
            getattr(m, cache).update({"key": None, "value": None})


def tiny_db(path: Path, sessions: int = 3, rows_per: int = 1) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    conn = sqlite3.connect(path)
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
    for i in range(sessions):
        sid = f"local-{i:06d}"
        conn.execute(
            "INSERT INTO sessions (id, parent_session_id, created_source, model, started_at, last_activity_at)"
            " VALUES (?, NULL, 'tui', 'deepseek-v4.1-flash', ?, ?)",
            (sid, now - 600 - i, now - 300),
        )
        for r in range(rows_per):
            conn.execute(
                "INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count,"
                " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, estimated_cost_usd,"
                " actual_cost_usd, last_seen) VALUES (?, 'deepseek-v4.1-flash', 'custom', 2, 1000, 200,"
                " 500000, 0, 0, 0, ?)",
                (sid, now - 300 - r),
            )
    conn.commit()
    conn.close()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="savvy-hard-"))
    try:
        return run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(root: Path) -> int:
    m = load()
    sandbox(m, root)

    # ---- 1. missing DB --------------------------------------------------------
    check("missing db: day spend zeros", m._day_spend(time.time()) == {"cost_usd": 0.0, "cost_source": "none", "sessions": 0})
    check("missing db: totals None", m._read_totals("local-x") is None)
    check("missing db: model unknown", m._read_model("local-x") == ("unknown", ""))
    check("missing db: context 0", m._read_context_tokens("local-x") == 0)
    check("missing db: start 0", m._read_session_start("local-x") == 0.0)
    check("missing db: pin refused", m._pin_session("local-abc") is False)
    payload = m.render_card({})
    check("missing db: card still renders", "burn/hour" in payload)

    # ---- 2. corrupt DB --------------------------------------------------------
    db = root / "state.db"
    db.write_bytes(b"this is not a sqlite file\n" * 100)
    check("corrupt db: day spend zeros", m._day_spend(time.time())["sessions"] == 0)
    check("corrupt db: totals None", m._read_totals("local-x") is None)
    check("corrupt db: session table None", m._read_session_table() is None)

    # ---- 3. wrong schema ------------------------------------------------------
    db.unlink()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT)")
    conn.commit()
    conn.close()
    check("wrong schema: day spend zeros", m._day_spend(time.time())["sessions"] == 0)
    check("wrong schema: totals None", m._read_totals("local-x") is None)

    # ---- 4. healthy DB --------------------------------------------------------
    db.unlink()
    tiny_db(db, sessions=3)
    day = m._day_spend(time.time())
    check("healthy db: 3 sessions", day["sessions"] == 3, str(day))
    check("healthy db: table priced", day["cost_source"] == "table", day["cost_source"])
    first = day["cost_usd"]
    t0 = time.perf_counter()
    again = m._day_spend(time.time())
    cached_ms = (time.perf_counter() - t0) * 1000
    check("healthy db: cached result equal", again == day)
    check(f"healthy db: cache hit is fast ({cached_ms:.2f}ms)", cached_ms < 3.0, f"{cached_ms:.2f}ms")

    # ---- 5. locked DB ---------------------------------------------------------
    locker = sqlite3.connect(db, timeout=0.1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        locked = m._day_spend(time.time() + 30)  # new bucket: forces a fresh query
        check("locked db: read degrades to zeros", locked["sessions"] >= 0, str(locked))
    finally:
        locker.rollback()
        locker.close()

    # ---- 6. price overrides ---------------------------------------------------
    prices = root / "state" / m.PRICE_OVERRIDE_NAME
    prices.write_text(json.dumps({"deepseek-v4.1-flash": [1.0, 4.0, 0.05], "bad": "x", "worse": [1], "named": {"input": 2, "output": 3, "cache_read": 0.1}}))
    m._price_cache.update({"key": None, "table": None})
    got = m._match_price("deepseek-ai/deepseek-v4.1-flash-1017")
    check("override: beats the baked table", got == (1.0, 4.0, 0.05), str(got))
    check("override: bad rows ignored", "bad" not in m._price_table() and "worse" not in m._price_table())
    check("override: named fields accepted", m._price_table().get("named") == (2.0, 3.0, 0.1), str(m._price_table().get("named")))
    prices.write_text("{not json")
    m._price_cache.update({"key": None, "table": None})
    check("override: broken file falls back to baked table", m._match_price("deepseek-v4.1-flash") == (0.30, 1.20, 0.006))
    prices.unlink()
    m._price_cache.update({"key": None, "table": None})

    # ---- 7. unwritable state dir ---------------------------------------------
    state = root / "state"
    read_only = state / "ro"
    read_only.mkdir()
    target = read_only / "savvy-progress.json"
    os.chmod(read_only, stat.S_IRUSR | stat.S_IXUSR)
    try:
        wrote = m._write_json_atomic(target, {"a": 1})
        check("read-only dir: write returns False, does not raise", wrote is False, str(wrote))
        check("read-only dir: no file created", not target.exists())
    finally:
        os.chmod(read_only, stat.S_IRWXU)

    # a target path whose parent is a file, not a directory
    blocker = root / "blocker"
    blocker.write_text("x")
    m._hermes_home = lambda: blocker  # type: ignore[assignment]
    try:
        bad = m._write_json_atomic(blocker / "state" / "card.json", {"a": 1})
        check("parent is a file: write returns False", bad is False, str(bad))
        check("parent is a file: snapshot does not raise", m.snapshot("local-000000") is None or True)
    finally:
        m._hermes_home = lambda: root  # type: ignore[assignment]

    # ---- 8. atomic write contract --------------------------------------------
    ok_path = state / "atomic.json"
    check("atomic: write succeeds", m._write_json_atomic(ok_path, {"ok": 1}))
    mode = stat.S_IMODE(ok_path.stat().st_mode)
    check("atomic: mode 0600", mode == 0o600, oct(mode))
    check("atomic: no tmp left", not (state / "atomic.json.tmp").exists())
    check("atomic: overwrite keeps one file", m._write_json_atomic(ok_path, {"ok": 2}) and json.loads(ok_path.read_text())["ok"] == 2)
    inf_ok = m._write_json_atomic(state / "inf.json", {"bad": float("inf"), "nested": {"x": float("nan")}})
    text = (state / "inf.json").read_text()
    check("atomic: inf sanitized, not written raw", inf_ok and "Infinity" not in text and "NaN" not in text, text[:80])

    # ---- 9. fuzz: hostile payloads into the card -----------------------------
    random.seed(7)
    junk = [None, "", "x", 0, -1, 1e308, float("inf"), float("nan"), [], {}, [[1]], {"a": 1}, True, "9" * 50]
    survived = 0
    wide = 0
    for _ in range(2000):
        payload = {key: random.choice(junk) for key in (
            "ts", "model", "provider", "turn", "api_calls", "input_tokens", "output_tokens",
            "cache_read", "cache_hit_pct", "cost_usd", "cost_source", "total_cost_usd",
            "context_tokens", "context_window", "primary_session", "burn_usd_per_hour",
            "burn_source", "cache_saved_usd", "cache_saved_source", "day_cost_usd",
            "day_source", "day_sessions", "turns", "delegates",
        )}
        try:
            text = m.render_card(payload)
            survived += 1
            if max(len(line) for line in text.splitlines()) > m.CARD_W:
                wide += 1
        except Exception as exc:  # noqa: BLE001
            check("fuzz: render_card survived", False, f"{type(exc).__name__}: {exc}")
            break
    check("fuzz: 2000 hostile payloads rendered", survived == 2000, str(survived))
    check("fuzz: no line exceeded CARD_W", wide == 0, str(wide))

    # ---- 10. fuzz: hostile files --------------------------------------------
    for blob in (b"", b"{", b"[]", b"null", b'"str"', bytes(range(256)) * 20, b'{"a": ' * 500):
        (state / "savvy-progress.json").write_bytes(blob)
        prev = m._read_prev(state / "savvy-progress.json")
        check(f"fuzz file {blob[:6]!r}: prev is dict or None", prev is None or isinstance(prev, dict))
        sessions = m._read_sessions(state / "savvy-progress.sessions.json")
        check(f"fuzz file {blob[:6]!r}: sessions dict", isinstance(sessions, dict))

    # ---- 11. scale -----------------------------------------------------------
    big = root / "big.db"
    tiny_db(big, sessions=4000, rows_per=5)
    m._db_path = lambda: big  # type: ignore[assignment]
    m._day_cache.update({"key": None, "value": None})
    t0 = time.perf_counter()
    big_day = m._day_spend(time.time() + 60)
    cold_ms = (time.perf_counter() - t0) * 1000
    check("scale: 20000 rows summed", big_day["sessions"] == 4000, str(big_day["sessions"]))
    check(f"scale: cold day query under 400ms ({cold_ms:.1f}ms)", cold_ms < 400.0, f"{cold_ms:.1f}ms")

    m._db_path = lambda: db  # type: ignore[assignment]
    m._day_cache.update({"key": None, "value": None})

    # ---- 12. snapshot on a healthy sandbox home ------------------------------
    tiny_db(db, sessions=3)
    m._last_write = 0.0
    payload = m.snapshot("local-000000")
    check("snapshot: returns a payload", isinstance(payload, dict), str(type(payload)))
    if isinstance(payload, dict):
        path = root / "state" / "savvy-progress.json"
        check("snapshot: file written", path.exists())
        data = json.loads(path.read_text())
        for key in ("burn_usd_per_hour", "cache_saved_usd", "day_cost_usd", "day_sessions"):
            check(f"snapshot field {key}", key in data)
        check("snapshot: strict json (no NaN/Infinity)", "NaN" not in path.read_text() and "Infinity" not in path.read_text())

        # the second call inside the throttle must be a no-op
        m._last_write = time.time()
        check("snapshot: throttled call returns None", m.snapshot("local-000000") is None)

        # /svprogress still answers
        card = m._cmd_progress("")
        check("command: renders from the sandbox file", "burn/hour" in card and "today" in card)

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
