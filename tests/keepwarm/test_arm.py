#!/usr/bin/env python3
"""Offline test: the arm file, the scriptable door into the keepwarm loop.

A slash command is not the only way to start warming: a script writes
``state/keepwarm-arm.json`` and the next API hook consumes it. This suite drives the whole
path against a local fake provider, so no key and no network are involved. Exits 0 on success.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "keepwarm"
FAKE_KEY = "sk-test-arm-0001"

failures: list[str] = []
pings: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        failures.append(name)


def load_plugin():
    spec = importlib.util.spec_from_file_location("keepwarm_plugin_arm", PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeProvider(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence the test server
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        pings.append(json.loads(self.rfile.read(length).decode("utf-8")))
        raw = json.dumps({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 1,
                      "prompt_tokens_details": {"cached_tokens": 480}},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    plugin = load_plugin()

    server = http.server.HTTPServer(("127.0.0.1", 0), FakeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    home = Path(tempfile.mkdtemp(prefix="keepwarm-arm-"))
    os.environ["HERMES_HOME"] = str(home)
    (home / "state").mkdir(parents=True, exist_ok=True)

    capture = {
        "ts": time.time(), "session_id": "local-arm", "model": "deepseek-v4.1-flash",
        "provider": "custom", "base_url": f"http://127.0.0.1:{port}/v1",
        "api_mode": "chat_completions",
        "body": {"method": "POST", "body": {
            "model": "deepseek-v4.1-flash",
            "messages": [{"role": "user", "content": "arm test"}],
            "tools": [{"type": "function", "function": {"name": "t"}}],
        }},
    }
    (home / "state" / "keepwarm-request.json").write_text(json.dumps(capture))
    plugin.resolve_api_key = lambda base: ("TEST_KEY", FAKE_KEY)  # type: ignore[assignment]

    try:
        # 1. Nothing pending: the hook is a no-op.
        check("no arm file: nothing happens", plugin.poll_arm() == "", plugin.poll_arm())

        # 2. Bad verbs are refused at the writer.
        check("arm rejects an unknown verb", plugin.arm("frobnicate") is False)
        check("a refused verb writes nothing", not plugin.arm_path().exists())

        # 3. Start: the next poll starts the loop and consumes the file.
        check("arm start writes the file", plugin.arm("start", 60) is True)
        check("arm file is mode 0600", (plugin.arm_path().stat().st_mode & 0o777) == 0o600,
              oct(plugin.arm_path().stat().st_mode & 0o777))
        outcome = plugin.poll_arm()
        check("poll starts the loop", outcome == "started", outcome)
        check("arm file consumed", not plugin.arm_path().exists())
        check("loop reports running", plugin.is_running() is True)

        deadline = time.time() + 10
        while not pings and time.time() < deadline:
            time.sleep(0.1)
        check("the loop fired a real ping", len(pings) >= 1, str(len(pings)))
        if pings:
            body = pings[0]
            check("ping forced max_tokens=1", body.get("max_tokens") == 1, str(body.get("max_tokens")))
            check("ping was not streamed", body.get("stream") is False)

        status = plugin.read_status()
        check("status file says running", status.get("running") is True, str(status)[:120])
        check("status counts the ping", int(status.get("pings") or 0) >= 1, str(status.get("pings")))

        # 4. A second start request is harmless.
        plugin.arm("start", 60)
        check("second start reports already running", plugin.poll_arm() == "already")

        # 5. Stop through the same door.
        check("arm stop writes the file", plugin.arm("stop") is True)
        check("poll stops the loop", plugin.poll_arm() == "stopped")
        check("loop reports stopped", plugin.is_running() is False)
        check("status file says stopped", plugin.read_status().get("running") is False)

        # 6. A stale request is consumed and ignored, so it cannot resurrect the loop later.
        plugin._write_json(plugin.arm_path(), {"action": "start", "interval_s": 60,
                                               "ts": time.time() - plugin.ARM_TTL_S - 60})
        check("stale arm is refused", plugin.poll_arm() == "stale")
        check("stale arm left the loop stopped", plugin.is_running() is False)
        check("stale arm file consumed", not plugin.arm_path().exists())

        # 7. Start with no capture is refused with a reason, not a crash.
        (home / "state" / "keepwarm-request.json").unlink()
        plugin.arm("start", 60)
        check("no capture: refused", plugin.poll_arm() == "no-capture")
        check("no capture: loop stays stopped", plugin.is_running() is False)

        # 8. A malformed arm file is consumed and ignored.
        (home / "state" / "keepwarm-arm.json").write_text("{not json")
        # A file with no usable ts reads as stale: consumed, no action.
        check("malformed arm: ignored", plugin.poll_arm() in ("", "stale"), "unexpected outcome")
        check("malformed arm: consumed", not plugin.arm_path().exists())
    finally:
        try:
            plugin.stop_loop("test cleanup")
        except Exception:
            pass
        server.shutdown()
        os.environ.pop("HERMES_HOME", None)

    print()
    if failures:
        print(f"test_arm: {len(failures)} FAILED")
        return 1
    print("test_arm: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
