#!/usr/bin/env python3
"""Offline test: the body ladder that keeps a warm ping alive on a strict relay.

A provider that rejects the captured tool array (24 schemas came back HTTP 400 on a real
relay) must still get a warm ping: the plugin retries without tools, which keeps the
conversation prefix identical and the cache hit intact. Exits 0 on success.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "keepwarm"
FAKE_KEY = "sk-test-fallback-0001"

failures: list[str] = []
requests: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        failures.append(name)


def load_plugin(name: str = "keepwarm_plugin_fallback"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StrictProvider(http.server.BaseHTTPRequestHandler):
    """400 on any body that carries a tool array, 200 otherwise."""

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        requests.append(body)
        if body.get("tools"):
            payload = {"error": {"message": "[code=upstream_error] The request could not be completed.",
                                 "type": "upstream_error", "code": "upstream_error"}}
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        payload = {
            "id": "chatcmpl-strict",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "length"}],
            "usage": {
                "prompt_tokens": 95170,
                "completion_tokens": 16,
                "prompt_tokens_details": {"cached_tokens": 95104, "cached_read_tokens": 95104},
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class DeadProvider(http.server.BaseHTTPRequestHandler):
    """400 on every body."""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        requests.append({"dead": True})
        raw = json.dumps({"error": {"message": "nope"}}).encode("utf-8")
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(handler) -> tuple[http.server.HTTPServer, int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def main() -> int:
    plugin = load_plugin()

    # 1. The ladder itself: full, tool-free, tool-free without reasoning.
    record = {
        "base_url": "http://127.0.0.1:1/v1",
        "model": "deepseek-v4.1-flash",
        "body": {"method": "POST", "body": {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
            "tools": [{"type": "function", "function": {"name": f"tool{i}"}} for i in range(24)],
        }},
    }
    variants = plugin._body_variants(record)
    modes = [mode for mode, _ in variants]
    check("ladder has three modes", modes == ["full", "no-tools", "no-tools-no-reasoning"], str(modes))
    check("full body keeps tools", bool(variants[0][1].get("tools")))
    check("tool-free body drops tools and tool_choice", "tools" not in variants[1][1] and "tool_choice" not in variants[1][1])
    check("tool-free body drops parallel_tool_calls", "parallel_tool_calls" not in variants[1][1])
    check("leanest body drops reasoning_effort", "reasoning_effort" not in variants[2][1])
    check("every variant forces max_tokens=1", all(b.get("max_tokens") == 1 for _, b in variants))
    check("every variant forces stream off", all(b.get("stream") is False for _, b in variants))
    check("messages survive every variant", all(b.get("messages") for _, b in variants))

    # 2. Strict relay: the first request fails, the second succeeds, mode is reported.
    server, port = serve(StrictProvider)
    try:
        capture = {
            "ts": 1.0, "session_id": "local-fallback", "model": "deepseek-v4.1-flash",
            "provider": "custom", "base_url": f"http://127.0.0.1:{port}/v1",
            "api_mode": "chat_completions",
            "body": {"method": "POST", "body": {
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": "high",
                "tools": [{"type": "function", "function": {"name": f"t{i}"}} for i in range(24)],
            }},
        }
        plugin.capture_path = lambda: Path("/tmp/keepwarm-fallback-capture.json")  # type: ignore[assignment]
        Path("/tmp/keepwarm-fallback-capture.json").write_text(json.dumps(capture))
        plugin.resolve_api_key = lambda base: ("TEST_KEY", FAKE_KEY)  # type: ignore[assignment]

        requests.clear()
        result = plugin.ping_once(timeout=10)
        check("strict relay: ping succeeds", result["ok"] is True, str(result)[:160])
        check("strict relay: mode is no-tools", result.get("mode") == "no-tools", str(result.get("mode")))
        check("strict relay: cache read parsed", result["usage"].get("cache_read_tokens") == 95104, str(result["usage"]))
        check("strict relay: two requests made", len(requests) == 2, str(len(requests)))
        check("strict relay: first request kept tools", bool(requests[0].get("tools")))
        check("strict relay: second request dropped tools", "tools" not in requests[1])
        check("strict relay: prompt is the same conversation", requests[0]["messages"] == requests[1]["messages"])
        check("strict relay: key never in the result", FAKE_KEY not in json.dumps(result))
    finally:
        server.shutdown()

    # 3. Dead relay: every variant 400s, the error names the last attempt, no crash.
    server2, port2 = serve(DeadProvider)
    try:
        capture["base_url"] = f"http://127.0.0.1:{port2}/v1"
        Path("/tmp/keepwarm-fallback-capture.json").write_text(json.dumps(capture))
        requests.clear()
        result = plugin.ping_once(timeout=10)
        check("dead relay: reports failure", result["ok"] is False, str(result)[:160])
        check("dead relay: three attempts", len(requests) == 3, str(len(requests)))
        check("dead relay: error names the mode", "no-tools-no-reasoning" in str(result.get("error")), str(result.get("error"))[:120])
        check("dead relay: key never in the error", FAKE_KEY not in json.dumps(result))
    finally:
        server2.shutdown()

    # 4. Pricing covers the alias the config actually uses.
    check("priced: alias matches", plugin._is_priced("deepseek-v4.1-flash"))
    check("priced: fully qualified matches", plugin._is_priced("deepseek-ai/DeepSeek-V4.1-Flash-1017"))
    check("priced: other model is not priced", not plugin._is_priced("gpt-5.2"))
    check("priced: empty model is not priced", not plugin._is_priced(""))

    # 5. A capture without tools never builds the leaner variants.
    flat = {"body": {"method": "POST", "body": {"model": "m", "messages": [{"role": "user", "content": "x"}]}}}
    check("no tools in capture: one variant", [m for m, _ in plugin._body_variants(flat)] == ["full"])

    print()
    if failures:
        print(f"test_fallback: {len(failures)} FAILED")
        return 1
    print("test_fallback: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
