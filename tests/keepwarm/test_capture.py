#!/usr/bin/env python3
"""Offline test: pre_api_request capture. Exits 0 on success."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "keepwarm"
FAKE_KEY = "sk-test-KEY-abc123-do-not-store"

failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        failures.append(name)


def load_plugin():
    spec = importlib.util.spec_from_file_location("keepwarm_plugin_capture", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    def __init__(self):
        self.hooks = {}
        self.commands = {}

    def register_hook(self, name, callback, **kwargs):
        self.hooks.setdefault(name, []).append(callback)
        return object()

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler
        return object()


def synthetic_payload() -> dict:
    return {
        "method": "POST",
        "body": {
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "messages": [
                {"role": "system", "content": "You are a careful engineer. " * 12},
                {"role": "user", "content": "Explain the prompt cache."},
                {"role": "assistant", "content": "A cache hit needs an identical prefix."},
                {"role": "user", "content": "Thanks. Now the numbers."},
            ],
            "tools": [{"type": "function", "function": {"name": "terminal", "description": "run"}}],
            "temperature": 0.3,
            "max_tokens": 8192,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home
        os.environ["OPENAI_API_KEY"] = FAKE_KEY
        os.environ.pop("HERMES_CUSTOM_API_KEY", None)

        plugin = load_plugin()
        ctx = FakeCtx()
        plugin.register(ctx)

        check("hooks registered", set(ctx.hooks) == {"pre_api_request", "post_api_request"},
              str(sorted(ctx.hooks)))
        check("commands registered", set(ctx.commands) == {"keepwarm", "keepwarm-stop"},
              str(sorted(ctx.commands)))

        payload = synthetic_payload()
        original = copy.deepcopy(payload)
        callback = ctx.hooks["pre_api_request"][0]
        result = callback(
            task_id="task-1",
            turn_id="turn-1",
            api_request_id="req-1",
            session_id="sess-1",
            user_message="hello",
            conversation_history=[],
            platform="cli",
            model="deepseek-ai/DeepSeek-V4.1-Flash-1017",
            provider="custom",
            base_url="https://api.river.ai/v1",
            api_mode="chat_completions",
            api_call_count=1,
            retry_count=0,
            request_messages=[],
            system_prompt="",
            message_count=4,
            tool_count=1,
            approx_input_tokens=1200,
            request_char_count=4800,
            max_tokens=8192,
            started_at=0.0,
            middleware_trace=[],
            request=payload,
        )

        check("callback returns None", result is None, repr(result))
        check("request not mutated", payload == original)

        path = Path(home) / "state" / "keepwarm-request.json"
        check("capture file exists", path.is_file(), str(path))
        mode = stat.S_IMODE(path.stat().st_mode)
        check("capture mode 0600", mode == 0o600, oct(mode))

        record = json.loads(path.read_text(encoding="utf-8"))
        stored = record.get("body")
        check("body identical", stored == original)
        check("body byte identical",
              json.dumps(stored, sort_keys=True) == json.dumps(original, sort_keys=True))
        check("metadata kept",
              record.get("session_id") == "sess-1" and record.get("model") == payload["body"]["model"]
              and record.get("base_url") == "https://api.river.ai/v1")

        raw = path.read_text(encoding="utf-8")
        check("no key in capture", FAKE_KEY not in raw)

        # Throttle: a second fire within 5 s must not rewrite the file.
        first_ts = record.get("ts")
        callback(request=synthetic_payload(), session_id="sess-2",
                 model="m", provider="custom", base_url="https://api.river.ai/v1",
                 api_mode="chat_completions")
        record2 = json.loads(path.read_text(encoding="utf-8"))
        check("throttled", record2.get("ts") == first_ts and record2.get("session_id") == "sess-1")

        # Nothing else in the state dir may contain the key.
        leaked = [p.name for p in (Path(home) / "state").iterdir()
                  if p.is_file() and FAKE_KEY in p.read_text(encoding="utf-8", errors="replace")]
        check("no key anywhere in state", leaked == [], str(leaked))

    print("test_capture:", "FAIL" if failures else "ALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
