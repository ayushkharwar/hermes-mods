"""savvy-progress plugin smoke test.

Loads the plugin package from this directory through a fake PluginContext, fires the
``post_tool_call`` hook against the real state DB, and checks the data contract, the
one-write-per-second throttle and the no-write-when-unchanged rule.

Run: python3 tests/savvy-progress/test_plugin.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "savvy-progress"
GOLDEN = Path(__file__).resolve().parent / "golden_single_session.json"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
DB = HERMES_HOME / "state.db"

# The 14 keys of the pre-change contract. They must keep their exact meaning.
BASE_KEYS = {
    "ts",
    "session_id",
    "model",
    "provider",
    "turn",
    "api_calls",
    "input_tokens",
    "output_tokens",
    "cache_read",
    "cache_write",
    "cache_hit_pct",
    "cost_usd",
    "cost_source",
    "turns",
}

# The five keys added by the primary/delegate split and the context bar.
NEW_KEYS = {
    "primary_session",
    "delegates",
    "total_cost_usd",
    "context_tokens",
    "context_window",
}
CONTRACT_KEYS = BASE_KEYS | NEW_KEYS
DELEGATE_KEYS = {"count", "input_tokens", "output_tokens", "cache_read", "cost_usd", "cost_source"}

COST_SOURCES = {"provider", "table", "none"}

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class FakeContext:
    def __init__(self) -> None:
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, dict] = {}

    def register_hook(self, hook_name, callback, **kwargs):
        self.hooks.setdefault(hook_name, []).append(callback)
        return None

    def register_command(self, name, handler, description="", **kwargs):
        self.commands[name] = {"handler": handler, "description": description}
        return None


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "savvy_progress_plugin",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {PLUGIN_DIR / '__init__.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def real_session_id() -> str:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=1.0)
    try:
        row = conn.execute(
            "SELECT session_id FROM session_model_usage ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else "test-session"


def mtime(path: Path) -> int:
    return path.stat().st_mtime_ns


def temp_usage_db(rows: list[tuple]) -> Path:
    """A throwaway state.db that holds the cost-resolution cases. Never the real DB."""
    scratch = Path(__file__).resolve().parent / ".testdb"
    scratch.mkdir(exist_ok=True)
    path = scratch / f"usage-{time.time_ns()}.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE session_model_usage (
               session_id TEXT NOT NULL,
               model TEXT NOT NULL,
               billing_provider TEXT NOT NULL DEFAULT '',
               api_call_count INTEGER NOT NULL DEFAULT 0,
               input_tokens INTEGER NOT NULL DEFAULT 0,
               output_tokens INTEGER NOT NULL DEFAULT 0,
               cache_read_tokens INTEGER NOT NULL DEFAULT 0,
               cache_write_tokens INTEGER NOT NULL DEFAULT 0,
               actual_cost_usd REAL NOT NULL DEFAULT 0,
               estimated_cost_usd REAL NOT NULL DEFAULT 0,
               last_seen REAL
           )"""
    )
    conn.executemany(
        "INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count,"
        " input_tokens, output_tokens, cache_read_tokens, actual_cost_usd, estimated_cost_usd)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def expected_table_cost(session_id: str, table: dict) -> float:
    """Independent recomputation: SQL totals per row, price lookup done here."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=1.0)
    try:
        rows = conn.execute(
            "SELECT model, input_tokens, output_tokens, cache_read_tokens,"
            " actual_cost_usd, estimated_cost_usd FROM session_model_usage WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    total = 0.0
    for model, tin, tout, cache, actual, estimated in rows:
        if (actual or 0) > 0 or (estimated or 0) > 0:
            total += max(actual or 0, estimated or 0)
            continue
        name = (model or "").lower()
        for key in sorted(table, key=len, reverse=True):
            if key in name:
                pin, pout, pcache = table[key]
                total += tin / 1e6 * pin + tout / 1e6 * pout + cache / 1e6 * pcache
                break
    return round(total, 6)


def init_usage_db(path: Path) -> None:
    """An empty throwaway usage DB with the real schema."""
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE session_model_usage (
               session_id TEXT NOT NULL,
               model TEXT NOT NULL,
               billing_provider TEXT NOT NULL DEFAULT '',
               api_call_count INTEGER NOT NULL DEFAULT 0,
               input_tokens INTEGER NOT NULL DEFAULT 0,
               output_tokens INTEGER NOT NULL DEFAULT 0,
               cache_read_tokens INTEGER NOT NULL DEFAULT 0,
               cache_write_tokens INTEGER NOT NULL DEFAULT 0,
               actual_cost_usd REAL NOT NULL DEFAULT 0,
               estimated_cost_usd REAL NOT NULL DEFAULT 0,
               last_seen REAL
           )"""
    )
    conn.commit()
    conn.close()


def set_usage(db: Path, session_id: str, calls: int, tin: int, tout: int, cache: int = 0,
              last_seen: float | None = None, actual: float = 0.0) -> None:
    """One row per session, rewritten to the new running totals before each hook call."""
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM session_model_usage WHERE session_id = ?", (session_id,))
    conn.execute(
        "INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count,"
        " input_tokens, output_tokens, cache_read_tokens, actual_cost_usd, last_seen)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, "deepseek-v4.1-flash", "custom", calls, tin, tout, cache, actual,
         time.time() if last_seen is None else last_seen),
    )
    conn.commit()
    conn.close()


def init_sessions_table(db: Path) -> None:
    """Add the ``sessions`` parent-link table to a throwaway DB.

    Column names follow the live schema for every column this plugin reads, so the
    production SQL is what the test exercises.
    """
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE sessions (
               id TEXT PRIMARY KEY,
               source TEXT NOT NULL,
               created_source TEXT,
               model TEXT,
               parent_session_id TEXT,
               started_at REAL NOT NULL,
               ended_at REAL,
               input_tokens INTEGER DEFAULT 0,
               output_tokens INTEGER DEFAULT 0,
               cache_read_tokens INTEGER DEFAULT 0,
               cache_write_tokens INTEGER DEFAULT 0,
               estimated_cost_usd REAL,
               actual_cost_usd REAL,
               api_call_count INTEGER DEFAULT 0,
               last_activity_at REAL,
               FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
           )"""
    )
    conn.commit()
    conn.close()


def add_session_row(db: Path, session_id: str, *, parent=None, model="deepseek-v4.1-flash",
                    tin=0, tout=0, cache=0, actual=None, estimated=None,
                    last_activity_at=None, source="subagent", created_source=None,
                    started_at=None, ended_at=None) -> None:
    """One ``sessions`` row. ``last_activity_at`` defaults to now.

    ``created_source`` is ``tui`` for the user's own chat, ``cli`` for a spawned worker.
    It is what ranks two parentless ``local-`` sessions against each other.
    """
    conn = sqlite3.connect(db)
    seen = time.time() if last_activity_at is None else last_activity_at
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id, source, created_source, model, parent_session_id,"
        " started_at, ended_at, input_tokens, output_tokens, cache_read_tokens,"
        " cache_write_tokens, estimated_cost_usd, actual_cost_usd, api_call_count,"
        " last_activity_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, source, created_source or source, model, parent,
         seen if started_at is None else started_at, ended_at, tin, tout, cache, 0,
         estimated, actual, 0, seen),
    )
    conn.commit()
    conn.close()


def fire(plugin, session_id: str):
    """One hook call with the 1 s throttle cleared, so each call is testable."""
    setattr(plugin, "_last_write", 0.0)
    return plugin.snapshot(session_id)


def drop_entry(plugin, session_id: str) -> None:
    """Remove one session's history entry, deleting the file when it holds nothing else."""
    path = plugin._sessions_path()
    store = plugin._read_sessions(path)
    if store.pop(session_id, None) is None:
        return
    if store:
        plugin._write_sessions(path, store)
    else:
        path.unlink(missing_ok=True)


@contextlib.contextmanager
def scratch_env(plugin):
    """Isolated DB plus state dir under $HERMES_HOME/state, removed on exit."""
    state = plugin._hermes_home() / "state" / ".savvy-test"
    state.mkdir(parents=True, exist_ok=True)
    db = state / "scratch.db"
    snap = state / "snap.json"
    store = state / "sessions.json"
    pin = state / "pin.json"
    session_dir = state / "session-files"
    for path in (db, snap, store, pin, state / "snap.json.tmp", state / "sessions.json.tmp",
                 state / "pin.json.tmp"):
        path.unlink(missing_ok=True)
    if session_dir.is_dir():
        for leftover in session_dir.iterdir():
            leftover.unlink()
        session_dir.rmdir()
    init_usage_db(db)

    real = (plugin._db_path, plugin._snapshot_path, plugin._sessions_path,
            plugin._pin_path, plugin._session_dir)
    setattr(plugin, "_db_path", lambda: db)
    setattr(plugin, "_snapshot_path", lambda: snap)
    setattr(plugin, "_sessions_path", lambda: store)
    setattr(plugin, "_pin_path", lambda: pin)
    setattr(plugin, "_session_dir", lambda: session_dir)
    try:
        yield db, snap, store
    finally:
        setattr(plugin, "_db_path", real[0])
        setattr(plugin, "_snapshot_path", real[1])
        setattr(plugin, "_sessions_path", real[2])
        setattr(plugin, "_pin_path", real[3])
        setattr(plugin, "_session_dir", real[4])
        for path in (db, snap, store, pin, state / "snap.json.tmp", state / "sessions.json.tmp",
                     state / "pin.json.tmp"):
            path.unlink(missing_ok=True)
        if session_dir.is_dir():
            for leftover in session_dir.iterdir():
                leftover.unlink()
            session_dir.rmdir()
        try:
            state.rmdir()
        except OSError:
            pass



def main() -> int:
    plugin = load_plugin()
    ctx = FakeContext()

    # 1. Registration
    plugin.register(ctx)
    live_pin = plugin._read_pin()
    check("register() registered post_tool_call", "post_tool_call" in ctx.hooks,
          f"hooks={sorted(ctx.hooks)}")
    check("register() registered one callback", len(ctx.hooks.get("post_tool_call", [])) == 1)
    check("register() ignored command conflicts safely", isinstance(ctx.commands, dict),
          f"commands={sorted(ctx.commands)}")

    callback = ctx.hooks["post_tool_call"][0]
    session_id = plugin._primary_session_id() or real_session_id()
    print(f"\nsession_id = {session_id}")

    snapshot_path = plugin._snapshot_path()
    print(f"snapshot   = {snapshot_path}")
    print(f"db         = {plugin._db_path()}")

    # 2. Cold start: no file, one hook call writes the first snapshot.
    #    The store entry for this session is dropped too, so reruns start from zero.
    if snapshot_path.exists():
        snapshot_path.unlink()
    drop_entry(plugin, session_id)
    setattr(plugin, "_last_write", 0.0)

    callback(tool_name="terminal", args={}, result="ok", status="ok", session_id=session_id)
    check("snapshot file exists after one hook call", snapshot_path.exists())

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    missing = CONTRACT_KEYS - set(payload)
    check("snapshot has every data-contract key", not missing, f"missing={sorted(missing)}")
    check("session_id matches", payload["session_id"] == session_id, payload["session_id"])
    check("turns is a list", isinstance(payload["turns"], list), f"len={len(payload['turns'])}")
    check("turn is an int", isinstance(payload["turn"], int), f"turn={payload['turn']}")

    first_mtime = mtime(snapshot_path)

    # 3. Throttle: a pending data change inside the 1 s window must not rewrite the file.
    real_reader = plugin._read_totals
    real_totals = real_reader(session_id)
    fake_totals = dict(real_totals)
    fake_totals["input_tokens"] = real_totals["input_tokens"] + 1000
    fake_totals["output_tokens"] = real_totals["output_tokens"] + 200
    fake_totals["cost_usd"] = round(real_totals["cost_usd"] + 0.5, 6)
    setattr(plugin, "_read_totals", lambda sid: dict(fake_totals))

    time.sleep(0.05)
    callback(tool_name="terminal", args={}, result="ok", status="ok", session_id=session_id)
    check("throttled call did not rewrite the file", mtime(snapshot_path) == first_mtime,
          f"mtime {first_mtime} -> {mtime(snapshot_path)}")

    # 4. Same data, throttle window expired: still no write (nothing changed).
    setattr(plugin, "_read_totals", real_reader)
    setattr(plugin, "_last_write", 0.0)
    callback(tool_name="terminal", args={}, result="ok", status="ok", session_id=session_id)
    check("unchanged data did not rewrite the file", mtime(snapshot_path) == first_mtime)

    # 5. Changed data, throttle window expired: write happens.
    setattr(plugin, "_read_totals", lambda sid: dict(fake_totals))
    setattr(plugin, "_last_write", 0.0)
    time.sleep(0.02)
    callback(tool_name="terminal", args={}, result="ok", status="ok", session_id=session_id)
    changed = json.loads(snapshot_path.read_text(encoding="utf-8"))
    check("changed data rewrote the file", mtime(snapshot_path) > first_mtime)
    check("rewritten tokens are the new totals", changed["input_tokens"] == fake_totals["input_tokens"],
          f"in={changed['input_tokens']}")
    check("one delta appended", len(changed["turns"]) == 1, f"turns={changed['turns']}")

    # 6. Final clean snapshot from the real DB (no synthetic numbers left behind).
    #    The history entry this test wrote for the real session is dropped first, so the
    #    turn counter and the trend series restart from the real totals.
    setattr(plugin, "_read_totals", real_reader)
    snapshot_path.unlink()
    drop_entry(plugin, session_id)
    setattr(plugin, "_last_write", 0.0)
    callback(tool_name="terminal", args={}, result="ok", status="ok", session_id=session_id)
    final = json.loads(snapshot_path.read_text(encoding="utf-8"))

    # 7. Contract maths, recomputed from the DB independently.
    totals = real_reader(session_id)
    expected_pct = round(totals["cache_read"] / (totals["input_tokens"] + totals["cache_read"]) * 100, 1)
    check("cache_hit_pct matches recomputation", abs(final["cache_hit_pct"] - expected_pct) < 1e-9,
          f"{final['cache_hit_pct']} vs {expected_pct}")
    check("cost_usd matches recomputation", abs(final["cost_usd"] - totals["cost_usd"]) < 1e-9,
          f"{final['cost_usd']} vs {totals['cost_usd']}")
    check("api_calls matches recomputation", final["api_calls"] == totals["api_calls"],
          f"{final['api_calls']} vs {totals['api_calls']}")
    check("turn equals the last delta index", final["turn"] == (final["turns"][-1]["i"] if final["turns"] else 0),
          f"turn={final['turn']} turns={len(final['turns'])}")

    # 8. Cost resolution: provider actual / provider estimate / price table / no match.
    check("cost_source is a contract value", final["cost_source"] in COST_SOURCES, final["cost_source"])
    check("cost_usd stays numeric", isinstance(final["cost_usd"], (int, float)), repr(final["cost_usd"]))

    price = plugin._match_price("deepseek-ai/DeepSeek-V4.1-Flash")
    check("price lookup is case-insensitive", price == (0.30, 1.20, 0.006), repr(price))
    check("price lookup prefers the longest key",
          plugin._match_price("deepseek-ai/deepseek-v4.1-flash-1017") == (0.30, 1.20, 0.006))
    check("price lookup returns None for an unknown model",
          plugin._match_price("acme/mystery-9") is None)

    check("actual cost wins when > 0",
          plugin._resolve_cost("deepseek-v4.1-flash", 1_000_000, 0, 0, 1.25, 9.0) == (1.25, "provider"))
    check("estimated cost is used when actual is 0",
          plugin._resolve_cost("deepseek-v4.1-flash", 1_000_000, 0, 0, 0.0, 0.3) == (0.3, "provider"))
    table_cost, table_src = plugin._resolve_cost("deepseek-v4.1-flash", 1_000_000, 1_000_000, 1_000_000, 0, 0)
    # Hand check: 1M x 0.30/1e6 = 0.30, 1M x 1.20/1e6 = 1.20, 1M cache x 0.006/1e6 = 0.006;
    # sum 1.506. Cache tokens must take the cache rate, not the input rate.
    check("table prices input+output+cache_read", table_src == "table" and abs(table_cost - 1.506) < 1e-9,
          f"{table_cost} via {table_src}")
    none_cost, none_src = plugin._resolve_cost("acme/mystery-9", 5000, 500, 0, 0.0, 0.0)
    check("no price match marks the row", (none_cost, none_src) == (0.0, "none"), f"{none_cost} via {none_src}")

    # 9. Same resolution through _read_totals against a throwaway DB (not the real state.db).
    fake_db = temp_usage_db([
        ("t-table", "deepseek-v4.1-flash", "custom", 2, 1_000_000, 1_000_000, 1_000_000, 0.0, 0.0),
        ("t-actual", "deepseek-ai/DeepSeek-V4.1-Flash", "custom", 1, 10, 10, 0, 0.125, 0.0),
        ("t-est", "deepseek-ai/DeepSeek-V4.1-Flash-1017", "custom:api.river.ai", 1, 10, 10, 0, 0.0, 0.0625),
        ("t-none", "acme/mystery-9", "custom", 1, 5000, 500, 0, 0.0, 0.0),
    ])
    real_db_path = plugin._db_path
    setattr(plugin, "_db_path", lambda: fake_db)
    try:
        t_table = plugin._read_totals("t-table")
        t_actual = plugin._read_totals("t-actual")
        t_est = plugin._read_totals("t-est")
        t_none = plugin._read_totals("t-none")
        unknown = plugin._read_totals("no-such-session")
    finally:
        setattr(plugin, "_db_path", real_db_path)
        fake_db.unlink(missing_ok=True)
        try:
            fake_db.parent.rmdir()  # keep the stage dir clean; fails if not empty
        except OSError:
            pass

    check("_read_totals prices an unpriced row from the table",
          t_table["cost_source"] == "table" and abs(t_table["cost_usd"] - 1.506) < 1e-9,
          f"{t_table['cost_usd']} via {t_table['cost_source']}")
    check("_read_totals prefers actual_cost_usd",
          t_actual["cost_source"] == "provider" and t_actual["cost_usd"] == 0.125, f"{t_actual['cost_usd']}")
    check("_read_totals falls back to estimated_cost_usd",
          t_est["cost_source"] == "provider" and t_est["cost_usd"] == 0.0625, f"{t_est['cost_usd']}")
    check("_read_totals marks an unpriced session as none",
          t_none["cost_source"] == "none" and t_none["cost_usd"] == 0.0, f"{t_none['cost_usd']}")
    check("_read_totals still returns None for an unknown session", unknown is None)

    # 10. The real DB now yields a nonzero cost from the table, matching a test-side sum.
    expected = expected_table_cost(session_id, plugin.PRICE_TABLE)
    check("real session cost_usd is nonzero", final["cost_usd"] > 0,
          f"cost_usd={final['cost_usd']} cost_source={final['cost_source']}")
    check("real cost matches the independent recomputation", abs(final["cost_usd"] - expected) < 1e-9,
          f"{final['cost_usd']} vs {expected}")

    # 11. /svprogress prints a card, never JSON. Fake ctx command handler, three cost sources.
    check("register() registered the svprogress command", "svprogress" in ctx.commands,
          f"commands={sorted(ctx.commands)}")
    handler = ctx.commands["svprogress"]["handler"]

    def run_command(payload):
        """Call the real handler with the snapshot reader pinned to a fixed payload."""
        real_snapshot, real_prev = plugin.snapshot, plugin._read_prev
        setattr(plugin, "snapshot", lambda sid: dict(payload) if payload is not None else None)
        setattr(plugin, "_read_prev", lambda path: dict(payload) if payload is not None else None)
        try:
            return handler("")
        finally:
            setattr(plugin, "snapshot", real_snapshot)
            setattr(plugin, "_read_prev", real_prev)

    card_seed = {
        "ts": time.time(),
        "session_id": "card-check",
        "model": "deepseek-ai/deepseek-v4.1-flash",
        "provider": "custom",
        "turn": 13,
        "api_calls": 26,
        "input_tokens": 93888,
        "output_tokens": 43318,
        "cache_read": 966412,
        "cache_write": 0,
        "cache_hit_pct": 93.9,
        "cost_usd": 0.0355,
        "turns": [
            {"i": 12, "in": 12345, "out": 678, "cost": 0.0021},
            {"i": 13, "in": 9001, "out": 512, "cost": 0.0018},
        ],
    }
    cards = {source: run_command({**card_seed, "cost_source": source})
             for source in ("table", "provider", "none")}

    for source, card in cards.items():
        lines = card.splitlines()
        check(f"card[{source}] has no JSON braces", "{" not in card and "}" not in card)
        check(f"card[{source}] has no repr artifacts",
              "pprint" not in card and "'ts'" not in card and "None" not in card)
        check(f"card[{source}] keeps every line within 42 columns",
              all(len(line) <= 42 for line in lines), f"max={max(len(line) for line in lines)}")
        check(f"card[{source}] uses thousands separators", "93,888" in card and "93888" not in card)
        check(f"card[{source}] has the same row count",
              len(lines) == len(cards["table"].splitlines()), f"rows={len(lines)}")

    check("card[table] marks the estimate", "~$0.0355" in cards["table"] and "list price" in cards["table"])
    check("card[provider] prints a plain dollar amount",
          "$0.0355" in cards["provider"] and "~" not in cards["provider"]
          and "list price" not in cards["provider"])
    check("card[none] prints $n/a with no suffix",
          "$n/a" in cards["none"] and "list price" not in cards["none"] and "~" not in cards["none"])
    check("card header shows the model tail and provider",
          cards["table"].splitlines()[0] == "PROGRESS  deepseek-v4.1-flash  custom",
          cards["table"].splitlines()[0])

    missing = run_command(None)
    check("a missing snapshot prints one short line",
          len(missing.splitlines()) == 1 and missing.startswith("savvy-progress:"), missing)
    check("a missing snapshot stays within 42 columns", len(missing) <= 42, f"len={len(missing)}")

    stale = run_command({**card_seed, "cost_source": "table", "ts": time.time() - 900})
    check("a stale snapshot still prints the card and one age note",
          "last update" in stale and "PROGRESS" in stale
          and all(len(line) <= 42 for line in stale.splitlines()),
          stale.splitlines()[-1])

    live_card = handler("")
    check("live /svprogress reads the real snapshot without JSON",
          "PROGRESS" in live_card and "{" not in live_card, live_card.splitlines()[0])

    delta = plugin._delta(
        {"input_tokens": 100, "output_tokens": 10, "cache_read": 5, "cost_usd": 0.25},
        {"input_tokens": 400, "output_tokens": 40, "cache_read": 30, "cost_usd": 0.75},
    )
    check("_delta computes per-turn differences",
          delta == {"in": 300, "out": 30, "cache": 25, "cost": 0.5}, json.dumps(delta))
    check("_cache_hit_pct guards division by zero", plugin._cache_hit_pct(0, 0) == 0.0)

    # 12. Per-session continuity: A, B, A, B, A with growing totals. Neither series may
    #     reset, and B's history may never be diffed against A's snapshot.
    with scratch_env(plugin) as (db, snap, store):
        set_usage(db, "A", 1, 1000, 100)
        a1 = fire(plugin, "A")
        set_usage(db, "B", 1, 2000, 200)
        b1 = fire(plugin, "B")
        set_usage(db, "A", 2, 4000, 400)
        a2 = fire(plugin, "A")
        set_usage(db, "B", 2, 5000, 500)
        b2 = fire(plugin, "B")
        set_usage(db, "A", 4, 9000, 900)
        a3 = fire(plugin, "A")

        sessions = plugin._read_sessions(store)
        entry_a, entry_b = sessions["A"], sessions["B"]
        check("A's turn continues after B fired", a1["turn"] == 0 and a2["turn"] == 1 and a3["turn"] == 2,
              f"A turns={a1['turn']},{a2['turn']},{a3['turn']}")
        check("A's series is preserved across B's calls", len(entry_a["turns"]) == 2
              and [t["i"] for t in entry_a["turns"]] == [1, 2],
              f"turns={[t['i'] for t in entry_a['turns']]}")
        check("A's first delta is measured against A's own baseline",
              entry_a["turns"][0]["in"] == 3000 and entry_a["turns"][1]["in"] == 5000,
              f"in={[t['in'] for t in entry_a['turns']]}")
        check("B keeps its own separate series", entry_b["turn"] == 1 and len(entry_b["turns"]) == 1
              and entry_b["turns"][0]["in"] == 3000,
              f"turn={entry_b['turn']} in={[t['in'] for t in entry_b['turns']]}")
        check("B's first call did not inherit A's totals", b1["turn"] == 0 and b2["turn"] == 1,
              f"B turns={b1['turn']},{b2['turn']}")
        check("snapshot describes the session that fired last",
              a3["session_id"] == "A" and a3["turn"] == 2 and len(a3["turns"]) == 2,
              f"sid={a3['session_id']} turn={a3['turn']} turns={len(a3['turns'])}")
        check("sessions file is mode 0600", (store.stat().st_mode & 0o777) == 0o600,
              oct(store.stat().st_mode & 0o777))

        # The throttle covers both files: a call inside the 1 s window touches neither.
        store_m, snap_m = store.stat().st_mtime_ns, snap.stat().st_mtime_ns
        set_usage(db, "A", 9, 20000, 2000)
        throttled = plugin.snapshot("A")
        check("throttled call writes neither file",
              throttled is None and store.stat().st_mtime_ns == store_m
              and snap.stat().st_mtime_ns == snap_m,
              f"payload={throttled}")

    # 12b. A corrupt history file is ignored, never raised, and gets rebuilt.
    with scratch_env(plugin) as (db, snap, store):
        store.write_text("{not json", encoding="utf-8")
        os.chmod(store, 0o600)
        set_usage(db, "corrupt", 1, 1000, 100)
        try:
            payload = fire(plugin, "corrupt")
            ok = isinstance(payload, dict) and payload["turn"] == 0
        except Exception as exc:  # a hook must never raise
            ok = False
            payload = repr(exc)
        check("a corrupt sessions file is ignored, not raised", ok, str(payload)[:60])
        check("the corrupt file is replaced with a valid store",
              plugin._read_sessions(store).get("corrupt", {}).get("turn") == 0)

    # 13. Caps: 8 most recently touched sessions, 24 turns each.
    with scratch_env(plugin) as (db, snap, store):
        for n in range(12):
            set_usage(db, f"S{n}", 1, 1000 + n, 100)
            fire(plugin, f"S{n}")
            time.sleep(0.005)

        sessions = plugin._read_sessions(store)
        check("12 sessions leave 8 entries", len(sessions) == 8, f"len={len(sessions)}")
        check("the 8 kept entries are the newest ones",
              set(sessions) == {f"S{n}" for n in range(4, 12)},
              f"kept={sorted(sessions, key=lambda s: int(s[1:]))}")

        for i in range(2, 32):
            set_usage(db, "S11", i, 1000 * i, 100 * i)
            fire(plugin, "S11")
        entry = plugin._read_sessions(store)["S11"]
        check("turn counts every delta", entry["turn"] == 30, f"turn={entry['turn']}")
        check("turns list is capped at 24", len(entry["turns"]) == 24, f"len={len(entry['turns'])}")
        check("the cap keeps the newest deltas", entry["turns"][0]["i"] == 7 and entry["turns"][-1]["i"] == 30,
              f"i={entry['turns'][0]['i']}..{entry['turns'][-1]['i']}")

    # 14. Entries untouched for 6 hours are pruned.
    with scratch_env(plugin) as (db, snap, store):
        stale = {
            "sessions": {
                "stale": {"turn": 4, "turns": [{"i": 4, "in": 1, "out": 1, "cache": 0, "cost": 0.0}],
                          "totals": {"api_calls": 4, "in": 10, "out": 10, "cache": 0,
                                     "cache_write": 0, "cost": 0.0},
                          "ts": time.time() - 7 * 3600},
                "fresh": {"turn": 1, "turns": [], "totals": {"api_calls": 1, "in": 5, "out": 5, "cache": 0,
                                                             "cache_write": 0, "cost": 0.0},
                          "ts": time.time() - 60},
            }
        }
        store.write_text(json.dumps(stale), encoding="utf-8")
        os.chmod(store, 0o600)
        set_usage(db, "now", 1, 1000, 100)
        fire(plugin, "now")
        sessions = plugin._read_sessions(store)
        check("an entry older than 6 h is dropped", "stale" not in sessions, f"keys={sorted(sessions)}")
        check("a recent entry survives the prune", "fresh" in sessions, f"keys={sorted(sessions)}")

    # 15. Single-session run: exactly the same 14 keys and values as the pre-change build.
    #     golden_single_session.json was captured from the committed pre-change plugin.
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    with scratch_env(plugin) as (db, snap, store):
        run = []
        shapes = []
        for calls, tin, tout, cache in ((3, 1000, 100, 500), (5, 4000, 400, 2000), (9, 12000, 900, 8000)):
            set_usage(db, "golden-A", calls, tin, tout, cache)
            payload = fire(plugin, "golden-A")
            shapes.append(set(payload))
            run.append({k: v for k, v in payload.items() if k in BASE_KEYS and k != "ts"})
    check("contract is exactly 19 keys", len(CONTRACT_KEYS) == 19, f"n={len(CONTRACT_KEYS)}")
    check("every single-session payload has exactly the 19 contract keys",
          all(shape == CONTRACT_KEYS for shape in shapes),
          f"n_keys={[len(s) for s in shapes]}")
    golden_cmp = [
        {k: v for k, v in entry.items() if k in BASE_KEYS and k != "ts"} for entry in golden
    ]
    check("the 14 original keys keep their exact pre-change values",
          run == golden_cmp, "identical" if run == golden_cmp else json.dumps(run)[:120])

    # 15b. post_api_request: second trigger, same writer. A synthetic payload carries the same
    #      session_id. (a) the hook is registered, (b) unchanged DB totals write nothing,
    #      (c) an empty or corrupt payload is swallowed, (d) new totals do refresh the card.
    api_hooks = ctx.hooks.get("post_api_request") or []
    check("register() registered post_api_request", callable(api_hooks[0]) if api_hooks else False,
          f"hooks={sorted(ctx.hooks)}")
    check("post_api_request has exactly one callback", len(api_hooks) == 1, f"n={len(api_hooks)}")

    if api_hooks:
        api_callback = api_hooks[0]
        with scratch_env(plugin) as (db, snap, store):
            set_usage(db, "api-A", 2, 2000, 200)
            first = fire(plugin, "api-A")
            snap_m, store_m = snap.stat().st_mtime_ns, store.stat().st_mtime_ns

            # (b) Same totals in the DB: the hook must not touch either file.
            setattr(plugin, "_last_write", 0.0)
            api_callback(
                session_id="api-A", api_call_count=2, finish_reason="stop",
                usage={"input_tokens": 2000, "output_tokens": 200, "total_tokens": 2200},
                response={"usage": {"input_tokens": 2000, "output_tokens": 200}},
            )
            check("unchanged totals leave the snapshot untouched",
                  snap.stat().st_mtime_ns == snap_m and store.stat().st_mtime_ns == store_m,
                  f"snap {snap_m} -> {snap.stat().st_mtime_ns}")
            check("unchanged totals keep the same turn count",
                  json.loads(snap.read_text(encoding="utf-8"))["turn"] == first["turn"])

            # (d) New totals: the API hook alone must move the panel forward.
            set_usage(db, "api-A", 3, 3000, 300)
            setattr(plugin, "_last_write", 0.0)
            api_callback(session_id="api-A", api_call_count=3,
                         usage={"input_tokens": 3000, "output_tokens": 300})
            moved = json.loads(snap.read_text(encoding="utf-8"))
            check("changed totals do refresh the snapshot from the API hook",
                  snap.stat().st_mtime_ns > snap_m and moved["input_tokens"] == 3000
                  and moved["turn"] == first["turn"] + 1,
                  f"mtime {snap_m} -> {snap.stat().st_mtime_ns} in={moved['input_tokens']}")

            # A row still being filled in (lower totals) is skipped, so real data is never zeroed.
            snap_m, store_m = snap.stat().st_mtime_ns, store.stat().st_mtime_ns
            set_usage(db, "api-A", 3, 0, 0)
            setattr(plugin, "_last_write", 0.0)
            api_callback(session_id="api-A", usage={"input_tokens": 0, "output_tokens": 0})
            check("a totals regression writes nothing",
                  snap.stat().st_mtime_ns == snap_m and store.stat().st_mtime_ns == store_m)
            check("a totals regression keeps the stored tokens",
                  json.loads(snap.read_text(encoding="utf-8"))["input_tokens"] == 3000)

            # (c) Empty, odd and corrupt payloads are swallowed by the hook.
            bad_payloads = [
                {},
                {"session_id": None},
                {"session_id": ""},
                {"session_id": 12345},
                {"session_id": "api-A", "usage": "garbage", "api_call_count": "x"},
                {"session_id": ["a"], "usage": {"input_tokens": None}},
            ]
            raised = []
            for payload in bad_payloads:
                try:
                    api_callback(**payload)
                except Exception as exc:  # a hook must never raise
                    raised.append(f"{payload!r}: {exc!r}")
            check("empty and corrupt payloads never raise", not raised, "; ".join(raised)[:80])

            store.write_text("{not json", encoding="utf-8")
            os.chmod(store, 0o600)
            try:
                api_callback(session_id="api-A", usage={})
                corrupt_ok = True
            except Exception as exc:
                corrupt_ok = False
                print(f"raised: {exc!r}")
            check("a corrupt history file never raises from the API hook", corrupt_ok)


    # 17. Parent linkage. sessions.parent_session_id decides who is a delegate: a fresh
    #     session with no parent must never be counted, and the depth cap must hold.
    now = time.time()
    PARENT = "local-50ccc51cc86cf3e727fc505bdff80e08949c5ef08c19fac196a355f196412e7d"
    CHILD1 = "20261111_221141_526bde"
    CHILD2 = "20261111_221624_b5879a"
    GRAND = "20261111_221700_aa0001"
    GREAT = "20261111_221700_bb0002"
    TOO_DEEP = "20261111_221700_cc0003"
    UNRELATED = "20261111_221730_dd0004"

    with scratch_env(plugin) as (db, snap, store):
        init_sessions_table(db)
        add_session_row(db, PARENT, source="local", created_source="tui", tin=1000,
                        last_activity_at=now - 300)
        add_session_row(db, CHILD1, parent=PARENT, tin=2000, last_activity_at=now - 60)
        add_session_row(db, CHILD2, parent=PARENT, tin=3000, last_activity_at=now - 40)
        add_session_row(db, GRAND, parent=CHILD1, tin=4000, last_activity_at=now - 30)
        add_session_row(db, GREAT, parent=GRAND, tin=5000, last_activity_at=now - 20)
        add_session_row(db, TOO_DEEP, parent=GREAT, tin=6000, last_activity_at=now - 10)
        # Newest session in the database, but it has no parent: not a delegate.
        add_session_row(db, UNRELATED, tin=7000, last_activity_at=now - 5)

        table = plugin._read_session_table()
        check("sessions table reads as a dict keyed by id",
              isinstance(table, dict) and len(table) == 7, f"n={len(set(table)) if table else 0}")

        picked = plugin._primary_session_id(now, table)
        check("the newest parentless non-local session does not take the card",
              picked == PARENT, f"picked={picked}")

        ids = plugin._delegate_ids(PARENT, table, now)
        check("a session with no parent is not a delegate", UNRELATED not in ids, f"ids={len(ids)}")
        check("children and descendants count as delegates up to depth 3",
              set(ids) == {CHILD1, CHILD2, GRAND, GREAT}, f"ids={sorted(ids)}")
        check("a link deeper than depth 3 is ignored", TOO_DEEP not in ids)

        agg = plugin._delegate_totals(PARENT, table, now)
        check("delegate tokens are summed over the linked tree",
              agg["count"] == 4 and agg["input_tokens"] == 14000,
              f"count={agg['count']} in={agg['input_tokens']}")

        def rec(sid, parent=None, tin=0):
            return {"id": sid, "parent": parent, "seen": now, "model": "deepseek-v4.1-flash",
                    "input_tokens": tin, "output_tokens": 0, "cache_read_tokens": 0,
                    "cache_write_tokens": 0, "estimated_cost_usd": 0.0, "actual_cost_usd": 0.0}

        cycle = {"a": rec("a", "b"), "b": rec("b", "a")}
        cyc_ids = plugin._delegate_ids("a", cycle, now)
        check("a parent cycle is walked once, not forever",
              sorted(cyc_ids) == ["b"], f"ids={sorted(cyc_ids)}")

    # 18. Full split: one local parent, two priced children, one stale child, one unrelated
    #     fresh session. The card is the parent; the total is parent + children.
    with scratch_env(plugin) as (db, snap, store):
        init_sessions_table(db)
        # Session rows: the delegate path reads these columns.
        add_session_row(db, PARENT, source="local", created_source="tui", tin=200_000, cache=300_000,
                        last_activity_at=now - 300)
        add_session_row(db, CHILD1, parent=PARENT, tin=100_000, tout=10_000,
                        last_activity_at=now - 60)
        add_session_row(db, CHILD2, parent=PARENT, model="acme/mystery-9", tin=5_000, tout=500,
                        actual=0.75, last_activity_at=now - 40)
        add_session_row(db, "20261111_210000_ff0006", parent=PARENT, tin=999,
                        last_activity_at=now - 7 * 3600)          # child, but stale
        add_session_row(db, UNRELATED, tin=888, last_activity_at=now - 1)
        add_session_row(db, "local-older", source="local", tin=777, last_activity_at=now - 900)

        # Usage rows: the primary's fine-grained path is unchanged.
        set_usage(db, PARENT, 10, 200_000, 0, 300_000, last_seen=now - 300)
        set_usage(db, CHILD1, 4, 1_000, 100, last_seen=now - 60)

        payload = fire(plugin, PARENT)
        check("card session is the primary", payload["session_id"] == PARENT
              and payload["primary_session"] == PARENT, payload["session_id"][:24])
        check("model, calls, tokens and cache come from the primary only",
              payload["model"] == "deepseek-v4.1-flash" and payload["api_calls"] == 10
              and payload["input_tokens"] == 200_000 and payload["output_tokens"] == 0
              and payload["cache_read"] == 300_000,
              f"calls={payload['api_calls']} in={payload['input_tokens']} cache={payload['cache_read']}")
        check("primary cost is the primary's own table estimate",
              abs(payload["cost_usd"] - 0.0618) < 1e-9, f"{payload['cost_usd']}")

        expected_delegates = {
            "count": 2,
            "input_tokens": 105_000,
            "output_tokens": 10_500,
            "cache_read": 0,
            "cost_usd": 0.792,
            "cost_source": "provider",
        }
        check("delegates object has exactly the six documented fields",
              set(payload["delegates"]) == DELEGATE_KEYS, f"keys={sorted(payload['delegates'])}")
        check("delegate sums equal child1 + child2, stale child and unrelated session excluded",
              payload["delegates"] == expected_delegates, json.dumps(payload["delegates"]))
        check("total_cost_usd is primary + delegates",
              abs(payload["total_cost_usd"] - 0.8538) < 1e-9, f"{payload['total_cost_usd']}")
        check("total source follows the delegate bill",
              payload["cost_source"] == "provider", payload["cost_source"])
        check("context_tokens is the per-call prompt size of the primary",
              payload["context_tokens"] == 50_000, f"{payload['context_tokens']}")
        check("context_window comes from config.yaml model.context_length",
              payload["context_window"] == 1_000_000, f"{payload['context_window']}")
        check("contract is 19 keys and none of the original 14 changed shape",
              set(payload) == CONTRACT_KEYS and len(payload) == 19, f"n={len(payload)}")

        own = plugin._read_totals(PARENT)
        check("the 14 original keys still describe the primary exactly",
              payload["api_calls"] == own["api_calls"]
              and payload["cost_usd"] == own["cost_usd"]
              and payload["cache_hit_pct"] == plugin._cache_hit_pct(own["cache_read"], own["input_tokens"]),
              f"calls={payload['api_calls']} cost={payload['cost_usd']}")

        # A delegate firing must not steal a single field.
        delegate_payload = fire(plugin, CHILD1)
        stolen = [k for k in ("session_id", "primary_session", "model", "api_calls",
                              "input_tokens", "output_tokens", "cache_read", "cost_usd",
                              "context_tokens", "total_cost_usd")
                  if delegate_payload.get(k) != payload.get(k)]
        check("a firing delegate changes no card field", not stolen, f"stolen={stolen}")
        sessions = plugin._read_sessions(store)
        check("the per-session history file still records both sessions",
              PARENT in sessions and CHILD1 in sessions, f"keys={sorted(sessions)[:3]}")

        split_card = run_command({k: v for k, v in payload.items()})
        split_lines = split_card.splitlines()
        check("card shows the delegate row with their added cost",
              any("agents" in line and "+$0.7920" in line for line in split_lines),
              str([line for line in split_lines if "agents" in line]))
        check("card cost row shows the total, not the primary share",
              any("$0.8538" in line for line in split_lines),
              str([line for line in split_lines if "$" in line]))
        check("card names the primary tail and the delegate count",
              any("primary" in line and PARENT[-12:] in line and "2 agents" in line
                  for line in split_lines),
              str([line for line in split_lines if "primary" in line]))
        check("every split card line stays within 42 columns",
              all(len(line) <= 42 for line in split_lines),
              f"max={max(len(line) for line in split_lines)}")
        print("\n--- /svprogress card, primary + delegates ---")
        print(split_card)

    # 19. Last resort: without a sessions table the id-prefix rule still decides.
    with scratch_env(plugin) as (db, snap, store):
        set_usage(db, "local-stale", 1, 1000, 100, last_seen=now - 7 * 3600)
        set_usage(db, "20261111_200000_ee0005", 2, 2000, 200, last_seen=now - 60)
        check("with a stale local- session the fallback takes the newest session",
              plugin._primary_session_id(now) == "20261111_200000_ee0005",
              str(plugin._primary_session_id(now)))
        set_usage(db, "local-fresh", 1, 500, 50, last_seen=now - 30)
        check("a fresh local- session wins over a delegate-shaped id",
              plugin._primary_session_id(now) == "local-fresh", str(plugin._primary_session_id(now)))

    # 20. Context window parsing: yaml, a damaged file, and a missing key all fall back.
    home = Path(tempfile.mkdtemp(prefix="savvy-home-"))
    real_home = plugin._hermes_home
    try:
        (home / "config.yaml").write_text("model:\n  context_length: 500000\n", encoding="utf-8")
        setattr(plugin, "_hermes_home", lambda: home)
        check("context_window reads model.context_length",
              plugin._context_window() == 500_000, f"{plugin._context_window()}")

        (home / "config.yaml").write_text("model:\n  name: x\n", encoding="utf-8")
        check("a missing key falls back to 1000000",
              plugin._context_window() == 1_000_000, f"{plugin._context_window()}")

        (home / "config.yaml").write_text("::: not yaml :::\n", encoding="utf-8")
        check("a damaged config.yaml never raises and falls back",
              plugin._context_window() == 1_000_000, f"{plugin._context_window()}")

        (home / "config.yaml").unlink()
        check("a missing config.yaml falls back", plugin._context_window() == 1_000_000)
    finally:
        setattr(plugin, "_hermes_home", real_home)
        for leftover in home.iterdir():
            leftover.unlink()
        home.rmdir()

    # 21. New chat, first API call. The bug: the card showed the previous chat until the
    #     first usage row landed. The pin makes the firing tui session the card at once.
    api_callback = (ctx.hooks.get("post_api_request") or [None])[0]
    check("post_api_request callback is available to the pin tests", callable(api_callback))
    NEW_A = "local-aaaa1111-prev-chat"
    NEW_B = "local-bbbb2222-new-chat"
    with scratch_env(plugin) as (db, snap, store):
        init_sessions_table(db)
        add_session_row(db, NEW_A, source="local", created_source="tui",
                        started_at=now - 1200, last_activity_at=now - 60)
        set_usage(db, NEW_A, 10, 300_000, 5_000, 100_000, actual=0.5, last_seen=now - 60)

        before = fire(plugin, NEW_A)
        check("21 the previous chat owns the card before the new one starts",
              before["primary_session"] == NEW_A and abs(before["cost_usd"] - 0.5) < 1e-9,
              f"primary={before['primary_session']} cost={before['cost_usd']}")

        # The new chat exists in `sessions` but has no usage row yet.
        add_session_row(db, NEW_B, source="local", created_source="tui",
                        started_at=now, last_activity_at=now)

        setattr(plugin, "_last_write", 0.0)
        api_callback(session_id=NEW_B, api_call_count=1,
                     usage={"input_tokens": 0, "output_tokens": 0})
        fresh = json.loads(snap.read_text(encoding="utf-8"))
        fresh_card = plugin.render_card(fresh)

        check("21 the first API call pins the new chat",
              plugin._read_pin()[0] == NEW_B, f"pin={plugin._read_pin()[0]}")
        check("21 the pin file is mode 0600",
              (plugin._pin_path().stat().st_mode & 0o777) == 0o600,
              oct(plugin._pin_path().stat().st_mode & 0o777))
        check("21 the card is the new chat, not the previous one",
              fresh["primary_session"] == NEW_B and fresh["session_id"] == NEW_B,
              f"primary={fresh['primary_session'][:24]}")
        check("21 a primary with no usage row reports zeros",
              fresh["api_calls"] == 0 and fresh["input_tokens"] == 0
              and fresh["output_tokens"] == 0 and fresh["cache_read"] == 0,
              f"calls={fresh['api_calls']} in={fresh['input_tokens']} out={fresh['output_tokens']}")
        check("21 the new chat's cost is 0, not the previous chat's 0.5",
              fresh["cost_usd"] == 0.0 and fresh["total_cost_usd"] == 0.0,
              f"cost={fresh['cost_usd']} total={fresh['total_cost_usd']}")
        check("21 total_cost_usd is the delegate sum only, and there are no delegates",
              fresh["total_cost_usd"] == fresh["cost_usd"] + fresh["delegates"]["cost_usd"]
              and fresh["delegates"]["count"] == 0,
              f"total={fresh['total_cost_usd']} delegates={fresh['delegates']['cost_usd']}")
        check("21 the card shows none of the previous chat's numbers",
              "300,000" not in fresh_card and "$0.5000" not in fresh_card
              and "100,000" not in fresh_card,
              fresh_card.splitlines()[3])
        check("21 the new chat got no phantom turn",
              (plugin._read_sessions(store).get(NEW_B) or {}).get("turn") == 0
              and (plugin._read_sessions(store).get(NEW_B) or {}).get("turns") == [],
              json.dumps((plugin._read_sessions(store).get(NEW_B) or {}).get("turns")))
        check("21 the new chat has its own per-session file",
              plugin._session_file_path(NEW_B).exists()
              and set(json.loads(plugin._session_file_path(NEW_B).read_text())) == CONTRACT_KEYS,
              str(plugin._session_file_path(NEW_B).name))

        # Its first real usage row lands: the card becomes the new chat's own numbers only.
        set_usage(db, NEW_B, 3, 1500, 150, actual=0.02, last_seen=now + 1)
        setattr(plugin, "_last_write", 0.0)
        api_callback(session_id=NEW_B, api_call_count=3,
                     usage={"input_tokens": 1500, "output_tokens": 150})
        grown = json.loads(snap.read_text(encoding="utf-8"))
        grown_card = plugin.render_card(grown)

        check("21 the new chat's cost becomes its own 0.02",
              abs(grown["cost_usd"] - 0.02) < 1e-9 and abs(grown["total_cost_usd"] - 0.02) < 1e-9,
              f"cost={grown['cost_usd']} total={grown['total_cost_usd']}")
        check("21 the new chat's tokens are its own",
              grown["input_tokens"] == 1500 and grown["output_tokens"] == 150
              and grown["api_calls"] == 3,
              f"in={grown['input_tokens']} out={grown['output_tokens']} calls={grown['api_calls']}")
        check("21 the previous chat's 0.5 is not added to the new chat",
              "300,000" not in grown_card and "0.5200" not in grown_card
              and "0.5000" not in grown_card, grown_card.splitlines()[7])
        check("21 the new chat's first delta is one turn",
              (plugin._read_sessions(store).get(NEW_B) or {}).get("turn") == 1
              and (plugin._read_sessions(store).get(NEW_B) or {}).get("turns", [{}])[-1].get("in") == 1500,
              json.dumps((plugin._read_sessions(store).get(NEW_B) or {}).get("turns")))

    # 22. Pin lifetime and who may pin: a delegate never pins, a cli worker never pins,
    #     a pin older than 12 h or on a session that ended over 30 min ago is ignored.
    PIN_P = "local-pin-parent-tui"
    PIN_C = "20261112_000001_delegate-child"
    PIN_E = "local-other-newest-tui"
    PIN_D = "local-ended-one-hour-ago"
    PIN_F = "local-cli-worker"
    with scratch_env(plugin) as (db, snap, store):
        init_sessions_table(db)
        add_session_row(db, PIN_P, source="local", created_source="tui",
                        started_at=now - 600, last_activity_at=now - 120)
        add_session_row(db, PIN_C, parent=PIN_P, source="subagent", created_source="tui",
                        started_at=now - 60, last_activity_at=now)
        set_usage(db, PIN_P, 5, 1000, 100, actual=0.1, last_seen=now - 120)
        set_usage(db, PIN_C, 2, 500, 50, actual=0.05, last_seen=now)

        check("22 a parentless tui session is pinned", plugin._pin_session(PIN_P) is True
              and plugin._read_pin()[0] == PIN_P, f"pin={plugin._read_pin()[0]}")
        check("22 a delegate child is never pinned", plugin._pin_session(PIN_C) is False
              and plugin._read_pin()[0] == PIN_P, f"pin={plugin._read_pin()[0]}")

        setattr(plugin, "_last_write", 0.0)
        api_callback(session_id=PIN_C, api_call_count=2,
                     usage={"input_tokens": 500, "output_tokens": 50})
        check("22 a delegate firing post_api_request leaves the pin alone",
              plugin._read_pin()[0] == PIN_P, f"pin={plugin._read_pin()[0]}")
        check("22 a delegate firing does not take the card",
              json.loads(snap.read_text(encoding="utf-8"))["primary_session"] == PIN_P)

        add_session_row(db, PIN_D, source="local", created_source="tui",
                        started_at=now - 5000, last_activity_at=now - 4000,
                        ended_at=now - 3600)
        plugin._write_pin(PIN_D, now - 100)      # fresh pin, but the session ended 1 h ago
        check("22 a pin on a session ended over 30 min ago is dropped",
              plugin._primary_session_id(now) == PIN_P,
              str(plugin._primary_session_id(now)))

        add_session_row(db, PIN_E, source="local", created_source="tui",
                        started_at=now - 60, last_activity_at=now - 60)
        check("22 with the stale pin dropped the newest tui session wins",
              plugin._primary_session_id(now) == PIN_E, str(plugin._primary_session_id(now)))

        plugin._write_pin(PIN_P, now)
        check("22 a live pin beats the newest session", plugin._primary_session_id(now) == PIN_P,
              str(plugin._primary_session_id(now)))

        plugin._write_pin(PIN_P, now - 13 * 3600)
        check("22 a pin older than 12 h is ignored", plugin._primary_session_id(now) == PIN_E,
              str(plugin._primary_session_id(now)))

        # A pin whose row is gone from `sessions` is dead too.
        plugin._write_pin("local-session-that-is-gone", now)
        check("22 a pin on a missing session row falls back to the time rule",
              plugin._primary_session_id(now) == PIN_E, str(plugin._primary_session_id(now)))

        # created_source decides, not the clock: a newer spawned cli worker never wins.
        add_session_row(db, PIN_F, source="cli", created_source="cli",
                        started_at=now + 5, last_activity_at=now + 5)
        plugin._drop_pin()
        check("22 a newer cli worker does not take the card from the older tui chat",
              plugin._primary_session_id(now) == PIN_E, str(plugin._primary_session_id(now)))
        check("22 a cli worker firing a hook never pins", plugin._pin_session(PIN_F) is False)

        plugin._write_pin(PIN_E, now)
        set_usage(db, PIN_F, 1, 4000, 400, actual=0.03, last_seen=now + 6)
        setattr(plugin, "_last_write", 0.0)
        api_callback(session_id=PIN_F, api_call_count=1,
                     usage={"input_tokens": 4000, "output_tokens": 400})
        check("22 a cli session firing post_api_request leaves the tui pin alone",
              plugin._read_pin()[0] == PIN_E, f"pin={plugin._read_pin()[0]}")
        check("22 a cli session firing does not take the card",
              json.loads(snap.read_text(encoding="utf-8"))["primary_session"] == PIN_E,
              json.loads(snap.read_text(encoding="utf-8"))["primary_session"])

    # 23. Strict per-chat scoping. Chat A (2 children) and chat B (3 children) plus one
    #     orphan child whose parent is in neither. Each chat counts only what it spawned,
    #     and a child of B firing changes nothing that belongs to A.
    SC_A = "local-chat-a-tui"
    SC_B = "local-chat-b-tui"
    SC_A1, SC_A2 = "20261112_000101_a1", "20261112_000102_a2"
    SC_B1, SC_B2, SC_B3 = "20261112_000201_b1", "20261112_000202_b2", "20261112_000203_b3"
    SC_ORPHAN = "20261112_000301_orphan"
    with scratch_env(plugin) as (db, snap, store):
        init_sessions_table(db)
        add_session_row(db, SC_A, source="local", created_source="tui",
                        started_at=now - 900, last_activity_at=now - 100)
        add_session_row(db, SC_B, source="local", created_source="tui",
                        started_at=now - 800, last_activity_at=now - 60)
        set_usage(db, SC_A, 4, 1000, 100, actual=0.04, last_seen=now - 100)
        set_usage(db, SC_B, 6, 2000, 200, actual=0.06, last_seen=now - 60)
        for sid, parent, tin, actual in (
            (SC_A1, SC_A, 100, 0.01), (SC_A2, SC_A, 200, 0.02),
            (SC_B1, SC_B, 300, 0.03), (SC_B2, SC_B, 400, 0.04), (SC_B3, SC_B, 500, 0.05),
        ):
            add_session_row(db, sid, parent=parent,
                            source="subagent", created_source="subagent",
                            started_at=now - 50, last_activity_at=now - 30,
                            tin=tin, actual=actual)
        add_session_row(db, SC_ORPHAN, parent="local-parent-that-does-not-exist",
                        source="subagent", created_source="subagent",
                        started_at=now - 40, last_activity_at=now - 20,
                        tin=9999, actual=9.99)

        table = plugin._read_session_table()
        ids_a = plugin._delegate_ids(SC_A, table, now)
        ids_b = plugin._delegate_ids(SC_B, table, now)
        check("23 chat A counts exactly its own two children",
              set(ids_a) == {SC_A1, SC_A2}, f"ids={sorted(ids_a)}")
        check("23 chat B counts exactly its own three children",
              set(ids_b) == {SC_B1, SC_B2, SC_B3}, f"ids={sorted(ids_b)}")
        check("23 an orphan child whose parent is missing counts for no chat",
              SC_ORPHAN not in ids_a and SC_ORPHAN not in ids_b, f"ids={sorted(ids_a)}|{sorted(ids_b)}")
        check("23 A never sees a child of B and B never sees a child of A",
              not set(ids_a) & {SC_B1, SC_B2, SC_B3} and not set(ids_b) & {SC_A1, SC_A2})

        plugin._pin_session(SC_A)
        fire(plugin, SC_A)
        own_a = json.loads(plugin._session_file_path(SC_A).read_text(encoding="utf-8"))
        check("23 chat A's own file has 2 delegates and 0.03 of child cost",
              own_a["delegates"]["count"] == 2 and abs(own_a["delegates"]["cost_usd"] - 0.03) < 1e-9,
              json.dumps(own_a["delegates"]))
        check("23 chat A's own file is its own numbers, 19 keys",
              own_a["session_id"] == SC_A and own_a["primary_session"] == SC_A
              and own_a["input_tokens"] == 1000 and set(own_a) == CONTRACT_KEYS,
              f"in={own_a['input_tokens']} keys={len(own_a)}")

        fire(plugin, SC_B)
        own_b = json.loads(plugin._session_file_path(SC_B).read_text(encoding="utf-8"))
        check("23 chat B's own file has 3 delegates and 0.12 of child cost",
              own_b["delegates"]["count"] == 3 and abs(own_b["delegates"]["cost_usd"] - 0.12) < 1e-9,
              json.dumps(own_b["delegates"]))
        check("23 chat B's own file is its own numbers",
              own_b["session_id"] == SC_B and own_b["input_tokens"] == 2000,
              f"in={own_b['input_tokens']}")

        # A is pinned, then a child of B fires: A's file, A's numbers and the pin stay put.
        plugin._pin_session(SC_A)
        a_bytes = plugin._session_file_path(SC_A).read_bytes()
        a_mtime = plugin._session_file_path(SC_A).stat().st_mtime_ns
        a_snap = json.loads(snap.read_text(encoding="utf-8"))
        set_usage(db, SC_B1, 3, 7000, 700, actual=0.5, last_seen=now + 1)
        setattr(plugin, "_last_write", 0.0)
        api_callback(session_id=SC_B1, api_call_count=3,
                     usage={"input_tokens": 7000, "output_tokens": 700})
        check("23 a child of B firing leaves the pin on A", plugin._read_pin()[0] == SC_A,
              f"pin={plugin._read_pin()[0]}")
        check("23 a child of B firing does not touch A's file",
              plugin._session_file_path(SC_A).read_bytes() == a_bytes
              and plugin._session_file_path(SC_A).stat().st_mtime_ns == a_mtime)
        after_a = json.loads(snap.read_text(encoding="utf-8"))
        check("23 a child of B firing does not change A's numbers on the card",
              after_a["primary_session"] == SC_A and after_a["input_tokens"] == a_snap["input_tokens"]
              and after_a["cost_usd"] == a_snap["cost_usd"],
              f"primary={after_a['primary_session']} in={after_a['input_tokens']}")
        check("23 A's delegate total still excludes B's children after B's child ran",
              abs(after_a["delegates"]["cost_usd"] - 0.03) < 1e-9,
              json.dumps(after_a["delegates"]))
        own_b1 = json.loads(plugin._session_file_path(SC_B1).read_text(encoding="utf-8"))
        check("23 the firing delegate gets its own file with its own numbers",
              own_b1["session_id"] == SC_B1 and own_b1["input_tokens"] == 7000
              and own_b1["delegates"]["count"] == 0,
              f"in={own_b1['input_tokens']} delegates={own_b1['delegates']['count']}")

    # 24. The per-session directory keeps the 16 most recently written files.
    with scratch_env(plugin) as (db, snap, store):
        init_sessions_table(db)
        for n in range(18):
            sid = f"local-cap-{n:02d}"
            add_session_row(db, sid, source="cli", created_source="cli",
                            started_at=now - 30 - n, last_activity_at=now - 30 - n)
            set_usage(db, sid, 1, 100 + n, 10, last_seen=now - 30 - n)
            fire(plugin, sid)
            time.sleep(0.006)

        files = sorted(p.name for p in plugin._session_dir().glob("*.json"))
        check("24 18 firing sessions leave 16 files", len(files) == 16, f"files={len(files)}")
        check("24 the two oldest files are the ones deleted",
              "local-cap-00.json" not in files and "local-cap-01.json" not in files
              and "local-cap-17.json" in files,
              f"first={files[0]} last={files[-1]}")
        check("24 the kept session files are mode 0600",
              all((p.stat().st_mode & 0o777) == 0o600 for p in plugin._session_dir().glob("*.json")))


    print("\n--- /svprogress card, three cost sources (fake ctx handler) ---")
    for source in ("table", "provider", "none"):
        print(f"[cost_source={source}]")
        print(cards[source])
        print()

    print("--- /svprogress card, live snapshot ---")
    print(live_card)
    print()

    print("--- missing snapshot ---")
    print(missing)
    print()

    # 16. Leave no synthetic state behind: this file only ever held the test's own run.
    drop_entry(plugin, session_id)
    if live_pin[0]:
        plugin._write_pin(live_pin[0], live_pin[1])
    else:
        plugin._drop_pin()
    check("test cleaned its synthetic session entries",
          session_id not in plugin._read_sessions(plugin._sessions_path()))

    print(f"\n{len(failures)} failure(s)" if failures else "\nALL CHECKS PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
