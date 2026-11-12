#!/usr/bin/env python3
"""Offline test: replay ping against a local fake provider. Exits 0 on success."""

from __future__ import annotations

import copy
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "keepwarm"
FAKE_KEY = "sk-test-KEY-ping-987654"

failures = []
received = {}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        failures.append(name)


def load_plugin():
    spec = importlib.util.spec_from_file_location("keepwarm_plugin_ping", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeProvider(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        received["path"] = self.path
        received["auth"] = self.headers.get("Authorization") or ""
        received["body"] = json.loads(raw.decode("utf-8"))
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "length"}],
            "usage": {
                "prompt_tokens": 900,
                "completion_tokens": 1,
                "total_tokens": 901,
                "prompt_cache_hit_tokens": 820,
                "prompt_tokens_details": {"cached_tokens": 820},
            },
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def captured_body() -> dict:
    return {
        "method": "POST",
        "body": {
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "messages": [
                {"role": "system", "content": "prefix that must stay warm " * 10},
                {"role": "user", "content": "hello"},
            ],
            "tools": [{"type": "function", "function": {"name": "terminal"}}],
            "temperature": 0.3,
            "max_tokens": 8192,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    }


def main() -> int:
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("http_proxy", None)
    os.environ.pop("ALL_PROXY", None)
    os.environ.pop("all_proxy", None)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with tempfile.TemporaryDirectory() as home:
            os.environ["HERMES_HOME"] = home
            os.environ["OPENAI_API_KEY"] = FAKE_KEY

            plugin = load_plugin()
            body = captured_body()
            original = copy.deepcopy(body)
            state = Path(home) / "state"
            state.mkdir(parents=True, exist_ok=True)
            (state / "keepwarm-request.json").write_text(json.dumps({
                "ts": 1.0,
                "session_id": "sess-ping",
                "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                "provider": "custom",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "api_mode": "chat_completions",
                "body": body,
            }), encoding="utf-8")

            result = plugin.ping_and_record(timeout=10)
            check("ping ok", bool(result.get("ok")), json.dumps(result)[:200])
            check("path is chat completions", received.get("path") == "/v1/chat/completions",
                  str(received.get("path")))
            check("auth header sent", received.get("auth") == "Bearer " + FAKE_KEY)

            sent = received.get("body") or {}
            check("max_tokens forced to 1", sent.get("max_tokens") == 1, str(sent.get("max_tokens")))
            check("stream forced false", sent.get("stream") is False, repr(sent.get("stream")))

            expected = copy.deepcopy(original["body"])
            expected["max_tokens"] = 1
            expected["stream"] = False
            # stream_options is invalid with stream=false; the ping drops it.
            expected.pop("stream_options", None)
            check("stream_options dropped", "stream_options" not in sent)
            check("body otherwise identical", sent == expected)
            check("captured file untouched", json.loads(
                (state / "keepwarm-request.json").read_text(encoding="utf-8"))["body"] == original)

            status = json.loads((state / "keepwarm-status.json").read_text(encoding="utf-8"))
            check("status pings", status.get("pings") == 1, str(status.get("pings")))
            check("status ok", status.get("ok") == 1 and status.get("failed") == 0, json.dumps(status))
            check("status usage", (status.get("last_usage") or {}).get("prompt_tokens") == 900
                  and (status.get("last_usage") or {}).get("prompt_cache_hit_tokens") is None
                  and (status.get("last_usage") or {}).get("cache_read_tokens") == 820,
                  json.dumps(status.get("last_usage")))
            check("status cache total", status.get("est_cache_read_tokens") == 820,
                  str(status.get("est_cache_read_tokens")))
            check("status running false", status.get("running") is False, str(status.get("running")))

            leaked = [p.name for p in state.iterdir()
                      if p.is_file() and FAKE_KEY in p.read_text(encoding="utf-8", errors="replace")]
            check("no key in any file", leaked == [], str(leaked))

            card = plugin.card()
            check("card has no json", "{" not in card and "}" not in card)
            check("card lines <= 46", all(len(line) <= 46 for line in card.splitlines()),
                  str(max((len(line) for line in card.splitlines()), default=0)))
            check("card no em dash", "\u2014" not in card)
            print("-- card --")
            print(card)
            print("-- end card --")
    finally:
        server.shutdown()
        server.server_close()

    print("test_ping:", "FAIL" if failures else "ALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
