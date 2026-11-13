"""Regression tests for the second adversarial review (xhigh, gpt-6.1-sol).

Every case here failed before the fix commit and passes after it. Each test names the
finding it locks down, so a future refactor cannot quietly reintroduce it.

Run: python3 tests/savvy-progress/test_regressions.py
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "savvy-progress"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "savvy_progress_regressions",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {PLUGIN_DIR / '__init__.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def codex_line(model: str | None = None, total: int | None = None,
               last: dict | None = None) -> str:
    if model is not None:
        return json.dumps({"payload": {"model": model}})
    info = {}
    if total is not None:
        info["total_token_usage"] = {"input_tokens": total, "total_tokens": total}
    if last is not None:
        info["last_token_usage"] = last
    return json.dumps({"payload": {"type": "token_count", "info": info}})


def run() -> int:
    plugin = load_plugin()
    real_read_totals = plugin._read_totals
    real_agent_files = plugin._agent_files
    base = Path(tempfile.mkdtemp(prefix="savvy-regress-"))
    snapshot = base / "snappy.json"
    setattr(plugin, "_snapshot_path", lambda: snapshot)
    setattr(plugin, "_rows_path", lambda: base / "rows.json")

    # 1. High: a repeated codex notification must not count twice.
    def lines_repeated(_path, _deadline=None):
        return iter([
            codex_line(model="gpt-6.1-sol"),
            codex_line(total=1000, last={"input_tokens": 1000, "total_tokens": 1000}),
            codex_line(total=1000, last={"input_tokens": 1000, "total_tokens": 1000}),
        ])

    setattr(plugin, "_iter_lines", lines_repeated)
    tally = plugin._scan_codex_file(Path("virtual"))
    check("repeated codex notification counts once", tally["tally"]["tokens"] == 1000,
          str(tally["tally"]["tokens"]))
    check("repeated codex notification counts one call", tally["tally"]["calls"] == 1,
          str(tally["tally"]["calls"]))

    # 2. Medium: one delta plus a legacy cumulative record keeps the whole session.
    def lines_mixed(_path, _deadline=None):
        return iter([
            codex_line(model="gpt-6.1-sol"),
            codex_line(total=100),
            codex_line(total=200, last={"input_tokens": 100, "total_tokens": 100}),
        ])

    setattr(plugin, "_iter_lines", lines_mixed)
    mixed = plugin._scan_codex_file(Path("virtual"))
    check("mixed legacy and delta keeps the cumulative total",
          mixed["tally"]["tokens"] == 200, str(mixed["tally"]["tokens"]))
    gap = mixed["models"].get("unknown")
    check("the unbooked part is booked as unknown, unpriced",
          gap is not None and gap["tokens"] == 100 and gap["cost_source"] == "none",
          json.dumps(mixed["models"], default=str)[:160])

    # 3. Medium: an empty delta must not suppress the cumulative fallback.
    def lines_empty_delta(_path, _deadline=None):
        return iter([codex_line(model="gpt-6.1-sol"), codex_line(total=200, last={})])

    setattr(plugin, "_iter_lines", lines_empty_delta)
    empty = plugin._scan_codex_file(Path("virtual"))
    check("an empty delta falls back to the cumulative total",
          empty["tally"]["tokens"] == 200, str(empty["tally"]["tokens"]))

    # 4. High: a delegate view must not become the canonical payload.
    plugin._last_payload = {"session_id": "local-primary", "hidden_rows": []}
    setattr(plugin, "_read_session_table", lambda: {"local-primary": {}, "child": {}})
    setattr(plugin, "_read_totals", lambda sid: {**plugin._zero_totals(),
                                                 "input_tokens": 100 if sid == "local-primary" else 10})
    setattr(plugin, "_primary_session_id", lambda *a: "local-primary")
    setattr(plugin, "_read_model", lambda sid: (sid, ""))
    setattr(plugin, "_delegate_totals", lambda *a: dict(plugin.DELEGATE_DEFAULTS))
    setattr(plugin, "_read_session_start", lambda _sid: 0)
    setattr(plugin, "_day_spend", lambda _now: {"cost_usd": 0, "cost_source": "none",
                                                "sessions": 0, "tokens": 0, "input_tokens": 0,
                                                "output_tokens": 0, "cache_read": 0,
                                                "cache_write": 0})
    setattr(plugin, "_read_context_tokens", lambda _sid: 0)
    setattr(plugin, "_context_window", lambda: 1_000_000)
    setattr(plugin, "_others_totals", lambda *a, **k: plugin._others_payload([], 1))
    setattr(plugin, "_hidden_rows", lambda: [])
    setattr(plugin, "_publish_session_file", lambda *a: True)
    setattr(plugin, "_read_prev", lambda _p: None)
    setattr(plugin, "_read_sessions", lambda _p: {})
    setattr(plugin, "_write_sessions", lambda _p, _v: True)
    setattr(plugin, "_write_snapshot", lambda _p, _v: True)

    plugin._build_snapshot("child", None, None, self_view=True)
    check("a self view does not replace the canonical payload",
          plugin._last_payload.get("session_id") == "local-primary",
          str(plugin._last_payload.get("session_id")))

    # 5. Medium: republications must keep each other's changes.
    plugin._last_payload = {"session_id": "local-primary", "hidden_rows": [], "others_tokens": 0}
    plugin._republish_others({"agents": [], "tokens": 123, "cost_usd": 0.0,
                              "cost_source": "none", "unpriced": False, "ts": 1})
    plugin._republish(hidden_rows=["dsh"])
    plugin._republish_others({"agents": [], "tokens": 456, "cost_usd": 0.0,
                              "cost_source": "none", "unpriced": False, "ts": 2})
    published = json.loads(snapshot.read_text(encoding="utf-8"))
    check("a later scan keeps the hidden rows", published["hidden_rows"] == ["dsh"],
          str(published["hidden_rows"]))
    check("a later toggle keeps the newest totals", published["others_tokens"] == 456,
          str(published["others_tokens"]))

    # 6. High: a decompressor that runs past the deadline is stopped and marked.
    plugin._ZSTD_BIN = sys.executable
    setattr(plugin, "_agent_files", lambda *a: ([Path("import time; time.sleep(5)")], False))
    setattr(plugin, "_agent_root", lambda *a: base)
    plugin.AGENT_SOURCES = (("dsh", "unused", "unused", "unused"),)
    real_budget = plugin.AGENTS_SCAN_BUDGET_S
    plugin.AGENTS_SCAN_BUDGET_S = 1.0
    started = time.monotonic()
    scanned = plugin._scan_others(time.time())
    elapsed = time.monotonic() - started
    plugin.AGENTS_SCAN_BUDGET_S = real_budget
    check("a slow decompressor is stopped near the deadline", elapsed < 4.0, f"{elapsed:.2f}s")
    check("a stopped decompressor reports truncated",
          scanned["agents"][0]["truncated"] is True, str(scanned["agents"][0]["truncated"]))

    # 7. Medium: a failed decompressor is not spend.
    raw = "\n".join([
        json.dumps({"data": {"message": {"source": {"model": "gpt-6.1-sol"}}}}),
        json.dumps({"data": {"chunk": {"type": "usage",
                                       "usage": {"inputTokens": 100, "totalTokens": 100}}}}),
    ]) + "\n"
    code = "import sys; sys.stdout.write(" + repr(raw) + "); sys.exit(1)"
    failed = plugin._scan_dsh_file(Path(code))
    check("a failed decompressor yields no tally", failed is None, str(failed))

    # 8. Medium: reset republishes and reports a failed write.
    setattr(plugin, "_rows_path", lambda: base / "rows.json")
    plugin._last_payload = {"session_id": "local-primary", "hidden_rows": ["dsh"]}
    reset_message = plugin._cmd_progress("reset")
    published = json.loads(snapshot.read_text(encoding="utf-8"))
    check("reset republishes an empty hidden list", published["hidden_rows"] == [],
          str(published["hidden_rows"]))
    real_writer = plugin._write_hidden_rows
    setattr(plugin, "_write_hidden_rows", lambda rows: False)
    failed_reset = plugin._cmd_progress("reset")
    check("a failed reset is reported, not claimed", "not saved" in failed_reset, failed_reset)
    setattr(plugin, "_write_hidden_rows", real_writer)

    # 9. Medium: a corrupt cache row is rejected before it reaches the card.
    bad = {"ts": time.time(), "agents": [{"name": "pi", "available": True, "models": 1}],
           "tokens": 10, "cost_usd": 0.25, "cost_source": "agent", "unpriced": False}
    check("a cache with a malformed agent row is rejected",
          plugin._valid_others_payload(bad) is False)
    huge = {**bad, "agents": [], "tokens": 10 ** 400}
    check("a cache with an absurd total is rejected",
          plugin._valid_others_payload(huge) is False)

    # 10. Medium: an agent-reported cost is drawn, not n/a.
    agent = plugin._blank_agent("pi")
    agent.update(available=True, tokens=10, cost_usd=0.25, cost_source="agent",
                 models=[dict(model="paid-model", tokens=10, cost_usd=0.25,
                              cost_source="agent", calls=1, unpriced=False)])
    def card_of(entries, hidden=None):
        built = plugin._others_payload(entries, 1)
        return "\n".join(plugin._others_lines(
            {"others": built["agents"], "others_tokens": built["tokens"],
             "others_cost_usd": built["cost_usd"], "others_source": built["cost_source"],
             "others_unpriced": built["unpriced"]}, hidden))

    card = card_of([agent])
    check("an agent cost prints as an estimate", "~$0.2500" in card, card.replace("\n", " | ")[:120])
    check("an agent cost is not printed as n/a", "$n/a" not in card, card.replace("\n", " | ")[:120])

    # 11. Medium: cache savings follow each model's own rate.
    plugin._read_totals = real_read_totals
    setattr(plugin, "_safe_rows", lambda *a: [
        ("gpt-6.1-sol", 20, 0, 0, 1_000_000, 0, 0, 0),
        ("gpt-6.1-terra", 10, 0, 0, 1_000_000, 0, 0, 0),
    ])
    totals = plugin._read_totals("s")
    check("cache savings sum per model", abs(totals["cache_saved_usd"] - 2.85) < 1e-6,
          str(totals["cache_saved_usd"]))

    # 12. Medium: hiding `others` drops the total row only, like the widget.
    card_all = card_of([agent])
    card_off = card_of([agent], ["others"])
    check("the full block has a total row", "total 1" in card_all, card_all.replace("\n", " | "))
    check("hiding others drops only the total row",
          "total 1" not in card_off and "pi" in card_off, card_off.replace("\n", " | ")[:120])

    # 13. Medium: an oversized transcript is reported, not silently dropped.
    plugin._agent_files = real_agent_files
    big = base / "codex"
    big.mkdir(exist_ok=True)
    oversized = big / "huge.jsonl"
    oversized.write_text("{}\n", encoding="utf-8")
    real_cap = plugin.AGENT_MAX_FILE_BYTES
    plugin.AGENT_MAX_FILE_BYTES = 1
    files, truncated = plugin._agent_files(big, "*.jsonl")
    plugin.AGENT_MAX_FILE_BYTES = real_cap
    check("an oversized transcript leaves no file and marks truncation",
          files == [] and truncated is True, f"{files} {truncated}")

    print()
    if failures:
        print(f"{len(failures)} failure(s): " + "; ".join(failures[:6]))
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
