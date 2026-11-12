#!/usr/bin/env python3
"""e2e_probe.py — load the plugin through Hermes' OWN plugin loader.

Extra proof on top of test_plugin.py: a throwaway HERMES_HOME gets a
plugins/filetree symlink to this stage dir, then hermes_cli.plugins discovers
and loads it and fires the real post_tool_call dispatch.

Run: python3 tests/filetree-rail/e2e_probe.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "filetree-rail"
REPO = Path(os.environ.get("HERMES_SRC", str(Path.home() / ".hermes" / "hermes-agent")))
if REPO.is_dir() and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="home-e2e-", dir=PLUGIN_DIR))
    plugins_dir = home / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "filetree").symlink_to(PLUGIN_DIR, target_is_directory=True)
    # User plugins are opt-in: plugins.enabled must name the plugin.
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - filetree-rail\n", encoding="utf-8")
    os.environ["HERMES_HOME"] = str(home)

    try:
        from hermes_cli import plugins

        plugins.discover_plugins(force=True)

        manager = plugins.get_plugin_manager()
        loaded = {key: entry for key, entry in manager._plugins.items()}
        entry = loaded.get("filetree-rail") or loaded.get("filetree")

        print(f"plugin keys loaded: {sorted(loaded)}")
        if entry is None:
            print("FAIL: filetree-rail not loaded")
            return 1
        print(f"manifest: name={entry.manifest.name} version={entry.manifest.version} "
              f"provides_hooks={entry.manifest.provides_hooks}")
        print(f"load error: {entry.error!r}")

        print(f"has_hook('post_tool_call') = {plugins.has_hook('post_tool_call')}")

        plugins.invoke_hook("post_tool_call", tool_name="read_file",
                            args={"path": str(Path.home() / "e2e" / "read.py")}, result="{}", status="ok")
        plugins.invoke_hook("post_tool_call", tool_name="patch",
                            args={"path": str(Path.home() / "e2e" / "read.py")}, result="{}", status="ok")
        plugins.invoke_hook("post_tool_call", tool_name="terminal",
                            args={"command": "ls"}, result="{}", status="ok")

        state_file = home / "state" / "filetree.json"
        time.sleep(0.7)  # the plugin throttles disk writes to 2/s; let the deferred write land
        state = json.loads(state_file.read_text(encoding="utf-8"))
        print(f"state written: {state_file}")
        print(f"counts: {state['counts']}  files: {len(state['files'])}  events: {len(state['events'])}")
        print(f"files[0]: {state['files'][0]}")
        print("events:", [(event['tool'], event['op']) for event in state['events']])

        assert state["counts"] == {"read": 1, "write": 0, "edit": 1}, state["counts"]
        assert [event["tool"] for event in state["events"]] == ["read_file", "patch"]
        print("E2E ok: real loader registered the hook, real dispatch wrote the state file")
        return 0
    finally:
        os.environ.pop("HERMES_HOME", None)
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
