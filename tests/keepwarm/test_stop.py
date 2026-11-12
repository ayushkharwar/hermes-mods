#!/usr/bin/env python3
"""Offline test: loop start/stop lifecycle. Exits 0 on success."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "keepwarm"
FAKE_KEY = "sk-test-KEY-stop-135790"

failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        failures.append(name)


def load_plugin():
    spec = importlib.util.spec_from_file_location("keepwarm_plugin_stop", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home
        os.environ["OPENAI_API_KEY"] = FAKE_KEY

        plugin = load_plugin()
        check("clamp low", plugin.clamp_interval(30) == 60)
        check("clamp high", plugin.clamp_interval(5000) == 1800)
        check("default interval", plugin.clamp_interval(240) == 240)
        check("default constant", plugin.DEFAULT_INTERVAL_S == 240)

        state = Path(home) / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "keepwarm-request.json").write_text(json.dumps({
            "ts": 1.0,
            "session_id": "sess-stop",
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "provider": "custom",
            # Closed port: the first ping fails fast, the loop then sleeps.
            "base_url": "http://127.0.0.1:9/v1",
            "api_mode": "chat_completions",
            "body": {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                     "max_tokens": 4096, "stream": True},
        }), encoding="utf-8")

        started = plugin.start_loop(1)
        check("loop started", started is True)
        check("running flag", plugin.is_running())
        thread = plugin._S.thread
        check("thread alive", thread is not None and thread.is_alive())

        status = json.loads((state / "keepwarm-status.json").read_text(encoding="utf-8"))
        check("status running true", status.get("running") is True)
        check("interval clamped to 60", status.get("interval_s") == 60, str(status.get("interval_s")))

        time.sleep(0.5)
        check("second start refused", plugin.start_loop(120) is False)

        began = time.monotonic()
        stopped = plugin.stop_loop("stopped by user")
        elapsed = time.monotonic() - began
        check("stop returned true", stopped is True)
        check("thread exited under 3 s", elapsed < 3.0, f"{elapsed:.2f}s")
        check("thread not alive", not (thread and thread.is_alive()))
        check("running flag false", plugin.is_running() is False)

        status = json.loads((state / "keepwarm-status.json").read_text(encoding="utf-8"))
        check("status running false", status.get("running") is False)
        check("status stop reason", status.get("stop_reason") == "stopped by user",
              str(status.get("stop_reason")))

        card = plugin.card()
        check("card shows stopped", "stopped" in card.splitlines()[0], card.splitlines()[0])
        check("card lines <= 46", all(len(line) <= 46 for line in card.splitlines()))
        check("no key in status", FAKE_KEY not in (state / "keepwarm-status.json").read_text(
            encoding="utf-8"))

    print("test_stop:", "FAIL" if failures else "ALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
