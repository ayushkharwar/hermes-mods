#!/usr/bin/env python3
"""test_plugin.py — verification for the filetree-rail Hermes plugin.

No Hermes process, no gateway, no network. The hook contract is exercised
through a fake ``ctx`` and the real ``post_tool_call`` callback.

Run: python3 tests/filetree-rail/test_plugin.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "filetree-rail"
PLUGIN_FILE = PLUGIN_DIR / "__init__.py"
REPO = Path(os.environ.get("HERMES_SRC", str(Path.home() / ".hermes" / "hermes-agent")))
STATE_REL = Path("state") / "filetree.json"

if REPO.is_dir() and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))  # optional: real hermes_constants.get_hermes_home

CHECKS = 0


def ok(label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"ok {CHECKS} - {label}")


def load_plugin():
    spec = importlib.util.spec_from_file_location("filetree_rail_plugin", PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    """Captures what register() asks for; nothing else of Hermes is needed."""

    def __init__(self) -> None:
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, object] = {}

    def register_hook(self, name, callback, **_kwargs) -> None:
        self.hooks.setdefault(name, []).append(callback)

    def register_command(self, name, handler, description="", **_kwargs):
        self.commands[name] = handler


def read_json(home: Path) -> dict:
    return json.loads((home / STATE_REL).read_text(encoding="utf-8"))


def fire(hook, tool: str, args, **extra) -> None:
    hook(tool_name=tool, args=args, result="{}", status="ok", **extra)


def main() -> int:
    plugin = load_plugin()
    ok(f"plugin module imported from {PLUGIN_FILE}")

    ctx = FakeContext()
    plugin.register(ctx)

    assert "post_tool_call" in ctx.hooks and len(ctx.hooks["post_tool_call"]) == 1
    hook = ctx.hooks["post_tool_call"][0]
    ok("register(ctx) registered one post_tool_call hook")

    assert "filetree-clear" in ctx.commands
    assert callable(ctx.commands["filetree-clear"])
    ok("register(ctx) registered the /filetree-clear command")

    assert plugin.MAX_FILES == 40 and plugin.MAX_EVENTS == 200
    ok("caps: 40 files, 200 events")

    # ── phase A: temp HERMES_HOME, one real arg shape per mapped tool ───────
    home_a = Path(tempfile.mkdtemp(prefix="home-a-", dir=PLUGIN_DIR))
    home_b = Path(tempfile.mkdtemp(prefix="home-b-", dir=PLUGIN_DIR))
    real_home_env = os.environ.get("HERMES_HOME")

    try:
        os.environ["HERMES_HOME"] = str(home_a)
        ctx.commands["filetree-clear"]("")

        target = "/home/user/projects/app/main.py"
        fire(hook, "read_file", {"path": target}, session_id="20261111_101500_ab12cd")
        fire(hook, "write_file", {"file_path": "/home/user/projects/app/out.txt"})
        fire(hook, "patch", {"path": target, "old_string": "a", "new_string": "b"})
        fire(hook, "terminal", {"command": "ls -la"})
        fire(hook, "read_file", {"path": "."})  # ignored
        fire(hook, "read_file", {})  # ignored, must not raise

        plugin.flush()
        state = read_json(home_a)
        ok(f"JSON written to {home_a / STATE_REL}")

        assert {"ts", "session_id", "counts", "files", "events"} <= set(state)
        ok("contract keys present: ts, session_id, counts, files, events")

        assert state["counts"] == {"read": 1, "write": 1, "edit": 1}, state["counts"]
        ok("counts match the fired calls: read 1, write 1, edit 1")

        paths = [entry["path"] for entry in state["files"]]
        assert paths == [target, "/home/user/projects/app/out.txt"], paths
        assert state["files"][0]["op"] == "edit"
        assert state["files"][0]["reads"] == 1 and state["files"][0]["writes"] == 1
        ok("files newest-first, one entry per path, edit merged into the read entry")

        assert state["session_id"] == "20261111_101500_ab12cd"
        ok("session_id carried from the hook kwargs")

        tools = [event["tool"] for event in state["events"]]
        assert tools == ["read_file", "write_file", "patch"], tools
        ok("terminal and empty args added nothing (3 events only)")

        for entry in state["files"]:
            assert os.path.isabs(entry["path"]), entry
        ok("stored paths are absolute")

        # tilde expansion + relative resolution
        os.environ["HOME"] = str(home_a)  # keep expanduser inside the sandbox
        fire(hook, "read_file", {"path": "~/notes.md"})
        fire(hook, "read_file", {"path": "logs/today.txt"}, cwd=str(home_a))
        plugin.flush()
        state = read_json(home_a)
        stored = {entry["path"] for entry in state["files"]}
        assert str(home_a / "notes.md") in stored, stored
        assert str(home_a / "logs" / "today.txt") in stored, stored
        ok("~ expanded to $HOME, relative path joined to the hook cwd")
        del os.environ["HOME"]

        # hook must never raise
        hook()
        hook(tool_name=None, args=None)
        hook(tool_name="read_file", args="not-a-dict")
        hook(tool_name="patch", args={"path": 42})
        ok("garbage hook payloads do not raise")

        # ── throttle: at most one write per 0.5 s window ───────────────────
        os.environ["HERMES_HOME"] = str(home_b)
        ctx.commands["filetree-clear"]("")
        start = time.time()
        for i in range(20):
            fire(hook, "read_file", {"path": f"/home/user/projects/throttle/t{i}.py"})
        elapsed = time.time() - start
        disk_before = read_json(home_b)
        plugin.flush()
        disk_after = read_json(home_b)
        assert elapsed < plugin.MIN_WRITE_INTERVAL, f"loop too slow to prove throttling: {elapsed:.3f}s"
        assert len(disk_before["files"]) < 20, f"throttle failed: {len(disk_before['files'])} files already on disk"
        assert len(disk_after["files"]) == 20
        ok(f"throttled writes: {len(disk_before['files'])} of 20 on disk mid-burst, 20 after flush")

        state_dir = home_b / "state"
        assert not list(state_dir.glob("*.tmp")), "stray .tmp file left behind"
        ok("atomic write: no .tmp file left after replace")

        # ── caps after 250 synthetic events ───────────────────────────────
        ctx.commands["filetree-clear"]("")
        for i in range(250):
            fire(hook, "read_file", {"path": f"/home/user/projects/synthetic/f{i}.py"})
        plugin.flush()
        capped = read_json(home_b)
        assert len(capped["files"]) == 40, len(capped["files"])
        assert len(capped["events"]) == 200, len(capped["events"])
        assert capped["files"][0]["path"].endswith("f249.py")
        assert capped["events"][-1]["path"].endswith("f249.py")
        assert capped["events"][0]["path"].endswith("f50.py")
        ok("after 250 events: files capped at 40 (newest first), events at 200 (newest last)")

        # ── phase B: the real default home (~/.hermes), for the rail ───────
        os.environ.pop("HERMES_HOME", None)
        if real_home_env is not None:
            os.environ["HERMES_HOME"] = real_home_env
            os.environ.pop("HERMES_HOME", None)
        ctx.commands["filetree-clear"]("")

        for tool, arg in (
            ("read_file", {"path": str(ROOT / "docs" / "filetree-rail-WORKORDER.md")}),
            ("read_file", {"path": str(PLUGIN_FILE)}),
            ("patch", {"path": str(ROOT / "tui-widgets" / "filetree.mjs")}),
            ("write_file", {"file_path": str(ROOT / "docs" / "filetree-rail-WORKORDER.md")}),
            ("search_files", {"path": str(ROOT / "plugins"), "pattern": "filetree"}),
        ):
            fire(hook, tool, arg, session_id="20261111_101500_ab12cd")
        plugin.flush()

        real_state = read_json(Path(os.path.expanduser("~/.hermes")))
        ok(f"real state written to {Path(os.path.expanduser('~/.hermes')) / STATE_REL}")
    finally:
        shutil.rmtree(home_a, ignore_errors=True)
        shutil.rmtree(home_b, ignore_errors=True)

    print(f"\n{CHECKS} checks passed, 0 failed")

    trimmed = {
        "ts": round(real_state["ts"], 3),
        "session_id": real_state["session_id"],
        "counts": real_state["counts"],
        "files": [
            {
                "path": Path(entry["path"]).name,
                "op": entry["op"],
                "reads": entry["reads"],
                "writes": entry["writes"],
            }
            for entry in real_state["files"]
        ],
        "events": [{"op": event["op"], "path": Path(event["path"]).name, "tool": event["tool"]}
                   for event in real_state["events"]],
    }
    print("\ntrimmed read-back of ~/.hermes/state/filetree.json:")
    print(json.dumps(trimmed, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
