"""savvy-progress row-toggle test.

Covers the `/svprogress hide|show|reset|rows` subcommands, the
``state/savvy-progress.rows.json`` contract, and the fact that a hidden row leaves no line
behind in the printed card.

No network, no DB. Run: python3 tests/savvy-progress/test_rows.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
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
        "savvy_progress_rows",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {PLUGIN_DIR / '__init__.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CARD = {
    "ts": 1_700_000_000.0,
    "session_id": "local-abc",
    "primary_session": "local-abc",
    "model": "deepseek-v4.1-flash",
    "provider": "river",
    "api_calls": 42,
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read": 9000,
    "cache_write": 0,
    "cache_hit_pct": 90.0,
    "context_tokens": 20_000,
    "context_window": 1_000_000,
    "cost_usd": 0.0123,
    "cost_source": "table",
    "total_cost_usd": 0.0123,
    "burn_usd_per_hour": 0.5,
    "burn_source": "table",
    "cache_saved_usd": 0.24,
    "cache_saved_source": "table",
    "day_cost_usd": 2.18,
    "day_source": "table",
    "day_sessions": 3,
    "day_tokens": 18_400_000,
    "delegates": {"count": 1, "input_tokens": 0, "output_tokens": 0,
                  "cache_read": 0, "cost_usd": 0.04, "cost_source": "table"},
    "others": [
        {"name": "codex", "available": True, "tokens": 3300, "cost_usd": 0.0,
         "cost_source": "none", "unpriced": True, "sessions": 1,
         "models": [{"model": "gpt-6.1-terra", "tokens": 3300, "cost_usd": 0.0,
                     "cost_source": "none", "calls": 1}]},
        {"name": "pi", "available": True, "tokens": 1_000_728, "cost_usd": 0.55,
         "cost_source": "table", "unpriced": True, "sessions": 1,
         "models": [{"model": "deepseek-v4.1-flash", "tokens": 1_000_000, "cost_usd": 0.30,
                     "cost_source": "table", "calls": 1}]},
        {"name": "dsh", "available": True, "tokens": 1_000_000, "cost_usd": 0.30,
         "cost_source": "table", "unpriced": False, "sessions": 1,
         "models": [{"model": "deepseek-v4.1-flash", "tokens": 1_000_000, "cost_usd": 0.30,
                     "cost_source": "table", "calls": 1}]},
    ],
    "others_tokens": 2_004_028,
    "others_cost_usd": 0.85,
    "others_source": "table",
    "others_unpriced": True,
    "others_ts": 1_700_000_000.0,
    "turns": [],
}


def main() -> int:
    plugin = load_plugin()
    base = Path(tempfile.mkdtemp(prefix="savvy-rows-"))
    rows_file = base / "savvy-progress.rows.json"
    setattr(plugin, "_rows_path", lambda: rows_file)

    # 1. Vocabulary.
    check("aliases map to row keys", plugin._normalize_row("tok/day") == "daytok"
          and plugin._normalize_row("delegates") == "agents"
          and plugin._normalize_row("context") == "ctx")
    check("an unknown name is not a row", plugin._normalize_row("warp-drive") is None)
    check("row keys cover every widget row",
          {"model", "tokens", "cache", "ctx", "cost", "burn", "saved", "today",
           "daytok", "codex", "pi", "dsh", "others", "agents"} <= set(plugin.ROW_KEYS))

    # 2. No config file hides nothing.
    check("missing config hides nothing", plugin._hidden_rows() == [], str(plugin._hidden_rows()))

    # 3. A hand-written config is normalized, ordered, and drops junk names.
    rows_file.write_text(json.dumps({"hidden": ["tok/day", "burn", "nonsense"]}), encoding="utf-8")
    plugin._rows_cache["key"] = None
    check("config is normalized and ordered",
          plugin._hidden_rows() == ["burn", "daytok"], str(plugin._hidden_rows()))

    # 4. A broken config hides nothing rather than raising.
    rows_file.write_text("{not json", encoding="utf-8")
    plugin._rows_cache["key"] = None
    check("a corrupt config hides nothing", plugin._hidden_rows() == [], str(plugin._hidden_rows()))

    # 5. The command toggles, and the file is what the next process reads.
    rows_file.unlink(missing_ok=True)
    plugin._rows_cache["key"] = None
    message = plugin._cmd_progress("hide codex pi")
    check("hide reports what it hid", "hidden codex, pi" in message, message)
    check("hide persists to the rows file",
          json.loads(rows_file.read_text(encoding="utf-8"))["hidden"] == ["codex", "pi"],
          rows_file.read_text(encoding="utf-8"))
    check("hide survives a fresh read", plugin._hidden_rows() == ["codex", "pi"],
          str(plugin._hidden_rows()))

    message = plugin._cmd_progress("show codex")
    check("show reports what it restored", "shown codex" in message, message)
    check("show leaves the other row hidden", plugin._hidden_rows() == ["pi"],
          str(plugin._hidden_rows()))

    message = plugin._cmd_progress("hide warp-drive")
    check("an unknown row is rejected, not written", "unknown row" in message, message)
    check("a rejected row changes nothing", plugin._hidden_rows() == ["pi"])

    check("a bare toggle verb prints the summary", "shown:" in plugin._cmd_progress("hide"),
          plugin._cmd_progress("hide"))
    check("rows subcommand prints the summary", "hidden: pi" in plugin._cmd_progress("rows"),
          plugin._cmd_progress("rows"))

    message = plugin._cmd_progress("reset")
    check("reset clears the list", plugin._hidden_rows() == [] and "every row shown" in message,
          f"{plugin._hidden_rows()} {message}")

    # 6. The card honours the toggles: a hidden row draws no line at all.
    full = plugin.render_card(dict(CARD))
    for label in ("api calls", "input", "output", "cache read", "context", "cost",
                  "burn/hour", "cache saved", "today", "tokens/day", "agents", "codex", "pi"):
        check(f"full card prints {label}", label in full, full.replace("\n", " | ")[:120])

    hidden_card = plugin.render_card({**CARD, "hidden_rows": ["burn", "saved", "codex"]})
    check("hidden burn line is gone", "burn/hour" not in hidden_card)
    check("hidden saved line is gone", "cache saved" not in hidden_card)
    check("hidden codex line is gone", "codex" not in hidden_card)
    check("other rows survive", "today" in hidden_card and "dsh" in hidden_card,
          hidden_card.replace("\n", " | ")[:160])
    check("no empty gap is left behind",
          "\n\n" not in hidden_card and "   \n" not in hidden_card,
          json.dumps(hidden_card)[:120])

    tokens_off = plugin.render_card({**CARD, "hidden_rows": ["tokens"]})
    check("hiding tokens drops input/output/cache read at once",
          all(word not in tokens_off for word in ("input", "output", "cache read")))
    check("api calls survives hiding tokens", "api calls" in tokens_off)

    others_off = plugin.render_card({**CARD, "hidden_rows": ["others"]})
    check("hiding others drops the whole block", "other agents" not in others_off)

    everything = plugin.render_card({**CARD, "hidden_rows": list(plugin.ROW_KEYS)})
    check("hiding every row still prints the header", everything.splitlines()[0].startswith("PROGRESS"),
          everything.splitlines()[0])
    check("hiding every row prints no data row", "cost" not in everything
          and "today" not in everything, everything.replace("\n", " | ")[:160])

    # 7. The rows file the widget reads is written atomically and privately.
    plugin._write_hidden_rows(["dsh"])
    check("rows file is mode 0600", oct(rows_file.stat().st_mode)[-3:] == "600",
          oct(rows_file.stat().st_mode)[-3:])
    check("rows file holds a plain list",
          json.loads(rows_file.read_text(encoding="utf-8")) == {"hidden": ["dsh"]},
          rows_file.read_text(encoding="utf-8"))

    shutil.rmtree(base, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} failure(s): " + "; ".join(failures[:6]))
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
