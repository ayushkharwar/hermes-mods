"""savvy-progress cross-agent (codex / pi / dsh) scan test.

Builds a throwaway codex tree, pi tree and dsh tree in a temp dir, points the plugin at
them through its env overrides, and checks the token totals, the cost rules and the
failure modes: no tree, broken lines, and the 60 s cache.

No network and no real transcript is read. Run: python3 tests/savvy-progress/test_agents.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
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
        "savvy_progress_agents",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {PLUGIN_DIR / '__init__.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- fixtures ---------------------------------------------------------------------
# Every number below is asserted further down, so the expectation is a hand total, not a
# copy of whatever the code happens to return.
CODEX_LINES = [
    {"type": "turn_context", "payload": {"model": "gpt-6.1-terra"}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": 1000, "cached_input_tokens": 400,
        "output_tokens": 100, "total_tokens": 1100}}}},
    # The last token_count event of a rollout is the session total, so this one wins.
    {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": 3000, "cached_input_tokens": 1000,
        "output_tokens": 300, "total_tokens": 3300}}}},
]
CODEX_TOKENS = 3300

PI_LINES = [
    # Table-priced: 1.0M fresh input at $0.30/M = $0.30
    {"type": "message", "message": {"role": "assistant", "model": "deepseek-v4.1-flash",
                                    "usage": {"input": 1_000_000, "output": 0, "cacheRead": 0,
                                              "cacheWrite": 0, "cost": {"total": 0}}}},
    # Provider-priced: the agent billed $0.25 itself, which always wins over the table.
    {"type": "message", "message": {"role": "assistant", "model": "paid-model",
                                    "usage": {"input": 10, "output": 5, "cacheRead": 0,
                                              "cacheWrite": 0, "cost": {"total": 0.25}}}},
    # Local model: no provider cost, no table price, so its money is unknown, not zero.
    {"type": "message", "message": {"role": "assistant", "model": "Spark-X2.6-4B-Tema",
                                    "usage": {"input": 700, "output": 13, "cacheRead": 0,
                                              "cacheWrite": 0, "cost": {"total": 0}}}},
    # A truncated line must be skipped, never crash the scan.
    "{not json at all",
]
PI_TOKENS = 1_000_000 + 15 + 713

DSH_LINES = [
    {"type": "assistant/message", "data": {"message": {"role": "assistant", "source": {
        "kind": "model", "provider": "river", "model": "deepseek-v4.1-flash"}}}},
    {"type": "assistant/chunk", "data": {"chunk": {"type": "usage", "usage": {
        "inputTokens": 1_000_000, "outputTokens": 0,
        "cacheReadTokens": 0, "totalTokens": 1_000_000}}}},
]
DSH_TOKENS = 1_000_000


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row if isinstance(row, str) else json.dumps(row))
            handle.write("\n")


def write_dsh(path: Path, rows: list) -> bool:
    """Compress the dsh log with the real zstd binary. False when zstd is not installed."""
    zstd = shutil.which("zstd")
    if not zstd:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8")
    proc = subprocess.run([zstd, "-q", "-f", "-o", str(path), "-"],
                          input=raw, capture_output=True, check=False)
    return proc.returncode == 0 and path.exists()


def build_tree(base: Path) -> tuple[Path, Path, Path]:
    codex = base / "codex"
    pi = base / "pi"
    dsh = base / "dsh"
    write_jsonl(codex / "2026" / "11" / "13" / "rollout-a.jsonl", CODEX_LINES)
    write_jsonl(pi / "--home-zen--" / "a.jsonl", PI_LINES)
    write_dsh(dsh / "--home-zen--" / "s1" / "session.jsonl.zstd", DSH_LINES)
    return codex, pi, dsh


def main() -> int:
    plugin = load_plugin()
    base = Path(tempfile.mkdtemp(prefix="savvy-agents-"))
    cache_dir = base / "state"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "savvy-progress.agents.json"
    # Keep the real ~/.hermes untouched.
    setattr(plugin, "_others_path", lambda: cache_file)

    os.environ.pop("SAVVY_AGENTS_OFF", None)
    codex, pi, dsh = build_tree(base)
    os.environ["SAVVY_CODEX_DIR"] = str(codex)
    os.environ["SAVVY_PI_DIR"] = str(pi)
    os.environ["SAVVY_DSH_DIR"] = str(dsh)

    def reset() -> None:
        plugin._others_cache["ts"] = 0.0
        plugin._others_cache["value"] = None
        if cache_file.exists():
            cache_file.unlink()

    # 1. Each scanner reads its own format and totals.
    reset()
    now = time.time()
    data = plugin._scan_others(now)
    agents = {agent["name"]: agent for agent in data["agents"]}

    check("scan returns the three agents", sorted(agents) == ["codex", "dsh", "pi"], str(sorted(agents)))

    codex_entry = agents["codex"]
    check("codex tokens are the last cumulative token_count total",
          codex_entry["tokens"] == CODEX_TOKENS, f"{codex_entry['tokens']}")
    check("codex cached input is not double counted",
          codex_entry["input"] == 2000 and codex_entry["cache_read"] == 1000,
          f"in={codex_entry['input']} cached={codex_entry['cache_read']}")
    check("codex model name is read from the rollout", codex_entry["models"][0]["model"] == "gpt-6.1-terra",
          json.dumps(codex_entry["models"][:1]))
    check("an unpriced codex model reports $n/a, not $0",
          codex_entry["cost_usd"] == 0.0 and codex_entry["cost_source"] == "none",
          f"{codex_entry['cost_usd']} {codex_entry['cost_source']}")
    check("an unpriced codex model raises the unpriced flag", codex_entry["unpriced"] is True)

    pi_entry = agents["pi"]
    check("pi tokens sum every assistant usage row", pi_entry["tokens"] == PI_TOKENS,
          f"{pi_entry['tokens']} != {PI_TOKENS}")
    check("pi keeps the provider bill and the table estimate apart",
          abs(pi_entry["cost_usd"] - 0.55) < 1e-9, f"{pi_entry['cost_usd']}")
    check("pi mixed cost is reported as an estimate", pi_entry["cost_source"] == "table",
          pi_entry["cost_source"])
    check("pi local model marks the agent unpriced", pi_entry["unpriced"] is True)
    pi_models = {model["model"]: model for model in pi_entry["models"]}
    check("provider-priced model keeps source=provider",
          pi_models["paid-model"]["cost_source"] == "provider", json.dumps(pi_models["paid-model"]))
    check("table-priced model keeps list price",
          abs(pi_models["deepseek-v4.1-flash"]["cost_usd"] - 0.30) < 1e-9,
          f"{pi_models['deepseek-v4.1-flash']['cost_usd']}")
    check("a truncated line does not drop the rest of the file", len(pi_models) == 3,
          str(sorted(pi_models)))

    dsh_entry = agents["dsh"]
    check("dsh tokens come from the zstd usage chunks", dsh_entry["tokens"] == DSH_TOKENS,
          f"{dsh_entry['tokens']}")
    check("dsh reads the model out of the message source",
          dsh_entry["models"][0]["model"] == "deepseek-v4.1-flash", json.dumps(dsh_entry["models"][:1]))
    check("dsh cost is a table estimate", abs(dsh_entry["cost_usd"] - 0.30) < 1e-9
          and dsh_entry["cost_source"] == "table", f"{dsh_entry['cost_usd']} {dsh_entry['cost_source']}")

    # 2. The combined total is the sum of the three agents.
    check("combined tokens are the sum of every agent",
          data["tokens"] == CODEX_TOKENS + PI_TOKENS + DSH_TOKENS,
          f"{data['tokens']} != {CODEX_TOKENS + PI_TOKENS + DSH_TOKENS}")
    check("combined cost is the priced part only", abs(data["cost_usd"] - 0.85) < 1e-9,
          f"{data['cost_usd']}")
    check("combined total marks the unpriced part", data["unpriced"] is True)
    check("combined source is the weakest source", data["cost_source"] == "table", data["cost_source"])

    # 3. The disk cache: written once, then reused without touching the trees again.
    reset()
    first = plugin._others_totals(now, block=True)
    check("cache file is written", cache_file.exists())
    check("cache file is mode 0600", oct(cache_file.stat().st_mode)[-3:] == "600",
          oct(cache_file.stat().st_mode)[-3:])
    calls: list[str] = []
    real_scan = plugin._scan_others
    plugin._scan_others = lambda ts: calls.append("scan") or real_scan(ts)  # type: ignore[assignment]
    plugin._others_cache["ts"] = 0.0  # force the lookup path, not the memory hit
    plugin._others_cache["value"] = None
    second = plugin._others_totals(now + 1.0, block=True)
    check("a fresh cache avoids a second scan", calls == [], str(calls))
    check("cache round-trip keeps the totals", second["tokens"] == first["tokens"],
          f"{second['tokens']} vs {first['tokens']}")
    plugin._scan_others = real_scan  # type: ignore[assignment]

    # 3b. The hook path must never block: a cold call returns at once and fills later.
    reset()
    started = time.time()
    cold = plugin._others_totals(now + 120.0)
    elapsed = time.time() - started
    check("a cold non-blocking call returns at once", elapsed < 0.05, f"{elapsed:.3f}s")
    check("a cold non-blocking call reports zeros, not a fake total",
          cold["tokens"] == 0, f"{cold['tokens']}")
    for _ in range(400):
        if plugin._others_cache.get("value") is not None:
            break
        time.sleep(0.02)
    check("the background scan fills the cache", plugin._others_cache.get("value") is not None)
    warm = plugin._others_totals(now + 121.0)
    check("the next hook sees the refreshed totals", warm["tokens"] == data["tokens"],
          f"{warm['tokens']}")

    # 4. Stale cache rescans; a broken cache file is ignored, never fatal.
    reset()
    cache_file.write_text("{\"agents\": [", encoding="utf-8")
    broken = plugin._others_totals(now, block=True)
    check("a corrupt cache file falls back to a live scan",
          broken["tokens"] == data["tokens"], f"{broken['tokens']}")

    reset()
    cache_file.write_text(json.dumps({"ts": now - 600, "agents": [], "tokens": 1}), encoding="utf-8")
    stale = plugin._others_totals(now, block=True)
    check("a stale cache file is rescanned", stale["tokens"] == data["tokens"], f"{stale['tokens']}")

    # 5. Missing trees are zeros, not errors, and the scan never raises.
    os.environ["SAVVY_CODEX_DIR"] = str(base / "does-not-exist")
    reset()
    missing = plugin._scan_others(now)
    missing_codex = next(a for a in missing["agents"] if a["name"] == "codex")
    check("a missing tree reports available=false", missing_codex["available"] is False)
    check("a missing tree contributes no tokens", missing_codex["tokens"] == 0)
    check("the other agents still report", missing["tokens"] == PI_TOKENS + DSH_TOKENS,
          f"{missing['tokens']}")
    os.environ["SAVVY_CODEX_DIR"] = str(codex)

    # 6. The off switch zeroes everything without touching a transcript.
    os.environ["SAVVY_AGENTS_OFF"] = "1"
    reset()
    off = plugin._others_totals(now, block=True)
    check("SAVVY_AGENTS_OFF zeroes the totals", off["tokens"] == 0 and off["cost_usd"] == 0.0,
          f"{off['tokens']} {off['cost_usd']}")
    check("SAVVY_AGENTS_OFF keeps the three agent slots",
          [a["name"] for a in off["agents"]] == ["codex", "pi", "dsh"])
    os.environ.pop("SAVVY_AGENTS_OFF", None)
    reset()

    # 7. The card block: 42 cells, every agent, a total, and no fake row for a dead tree.
    live = plugin._scan_others(now)
    payload = {
        "others": live["agents"],
        "others_tokens": live["tokens"],
        "others_cost_usd": live["cost_usd"],
        "others_source": live["cost_source"],
        "others_unpriced": live["unpriced"],
    }
    lines = plugin._others_lines(payload)
    check("card block is not empty", bool(lines), f"{len(lines)} lines")
    check("every card line stays under CARD_W",
          all(len(line) <= plugin.CARD_W for line in lines),
          f"max={max(len(line) for line in lines)}")
    text = "\n".join(lines)
    for name in ("codex", "pi", "dsh", "total"):
        check(f"card block prints {name}", name in text, text.replace("\n", " | ")[:160])
    check("card block marks the unpriced part", "?" in text, text.replace("\n", " | ")[:160])

    os.environ["SAVVY_PI_DIR"] = str(base / "gone")
    reset()
    dead = plugin._scan_others(now)
    dead_lines = plugin._others_lines({
        "others": dead["agents"], "others_tokens": dead["tokens"],
        "others_cost_usd": dead["cost_usd"], "others_source": dead["cost_source"],
        "others_unpriced": dead["unpriced"]})
    check("a dead tree is skipped, not printed as zero",
          not any(line.strip().startswith("pi") for line in dead_lines),
          "\n".join(dead_lines)[:160])
    os.environ["SAVVY_PI_DIR"] = str(pi)

    none_lines = plugin._others_lines({"others": []})
    check("no agents at all prints nothing", none_lines == [], str(none_lines))

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
