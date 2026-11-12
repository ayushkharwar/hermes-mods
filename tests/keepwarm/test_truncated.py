#!/usr/bin/env python3
"""Truncated captures must be refused, never replayed as a preview."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

PLUGIN = pathlib.Path(__file__).resolve().parents[2] / "plugins" / "keepwarm" / "__init__.py"


def load(home: pathlib.Path):
    os.environ["HERMES_HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("keepwarm_trunc_probe", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        (home / "state").mkdir(parents=True)
        mod = load(home)

        # 1. Truncated preview record: refuse, no network call.
        record = {
            "ts": 1762830000.0,
            "base_url": "http://127.0.0.1:9/v1",
            "model": "deepseek-v4.1-flash",
            "body": {"_truncated": True, "original_type": "dict", "preview": '{"meth'},
        }
        mod.capture_path().write_text(json.dumps(record), encoding="utf-8")
        result = mod.ping_once(timeout=1.0)
        check("truncated capture refused", result["ok"] is False, json.dumps(result)[:120])
        check(
            "error names the cap knob",
            "HERMES_PLUGIN_PAYLOAD_MAX_CHARS" in result.get("error", ""),
            result.get("error", "")[:120],
        )

        # 2. Capable record with no messages: also refused.
        record2 = {
            "ts": 1762830000.0,
            "base_url": "http://127.0.0.1:9/v1",
            "model": "deepseek-v4.1-flash",
            "body": {"method": "POST", "body": {"model": "x", "tools": []}},
        }
        mod.capture_path().write_text(json.dumps(record2), encoding="utf-8")
        result2 = mod.ping_once(timeout=1.0)
        check("body without messages refused", result2["ok"] is False, result2.get("error", "")[:80])

        # 3. Missing capture: refused with the documented message.
        mod.capture_path().unlink()
        result3 = mod.ping_once(timeout=1.0)
        check("missing capture refused", result3.get("error") == "no captured request", str(result3.get("error")))

    print()
    if failures:
        print(f"test_truncated: FAIL ({len(failures)} failing)")
        return 1
    print("test_truncated: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
