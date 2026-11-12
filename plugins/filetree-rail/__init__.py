"""filetree-rail plugin: records which files the agent touched.

Writes one JSON snapshot to ``$HERMES_HOME/state/filetree.json``; the TUI widget
``filetree.mjs`` polls that file once a second and paints a 40-column rail.

Design rules:
- The hook never raises. Every body sits in ``try/except`` and logs at debug.
- The in-memory state is authoritative. Disk writes are throttled to at most
  2 per second (``MIN_WRITE_INTERVAL``) and written atomically (tmp + replace).
- Caps: 40 distinct files (newest first), 200 events (append-only ring).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FILES = 40
MAX_EVENTS = 200
MIN_WRITE_INTERVAL = 0.5  # seconds between disk writes, so max 2 per second

#: tool name -> recorded op. ``terminal`` deliberately absent (nothing to log).
_OPS = {
    "read_file": "read",
    "write_file": "write",
    "patch": "edit",
    "search_files": "search",
}

#: Ops that raise a counter in ``counts`` (the widget header shows r/w/e).
_COUNTED = ("read", "write", "edit")

_PATH_KEYS = ("path", "file_path")

_lock = threading.RLock()
_state = {
    "ts": 0.0,
    "session_id": None,
    "counts": {"read": 0, "write": 0, "edit": 0},
    "files": [],
    "events": [],
}
_last_write = 0.0
_pending_timer = None


# ── paths ────────────────────────────────────────────────────────────────────


def _hermes_home() -> Path:
    """Active Hermes home: the runtime resolver when importable, else $HERMES_HOME."""
    try:  # inside a Hermes process this resolves profiles correctly
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME", "").strip()
        return Path(home).expanduser() if home else Path.home() / ".hermes"


def state_path() -> Path:
    return _hermes_home() / "state" / "filetree.json"


def _normalise(raw, cwd=None):
    """Absolute path for a tool argument, or None when there is nothing to log."""
    if not isinstance(raw, str):
        return None
    path = raw.strip()
    if not path or path in {".", "./", "~"}:
        return None
    path = os.path.expanduser(path)  # ~ -> $HOME, never stored unexpanded
    if os.path.isabs(path):
        path = os.path.normpath(path)
    elif isinstance(cwd, str) and cwd.strip():
        base = os.path.expanduser(cwd.strip())
        path = os.path.normpath(os.path.join(base, path))
    # Relative with no cwd: keep it exactly as given.
    return None if path in {"", "."} else path


def _arg_path(args):
    if not isinstance(args, dict):
        return None
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# ── state ───────────────────────────────────────────────────────────────────


def _record(op: str, path: str, tool: str, session_id) -> None:
    now = time.time()
    with _lock:
        _state["ts"] = now
        if isinstance(session_id, str) and session_id:
            _state["session_id"] = session_id
        if op in _COUNTED:
            _state["counts"][op] += 1

        files = _state["files"]
        entry = None
        for index, item in enumerate(files):
            if item.get("path") == path:
                entry = files.pop(index)
                break
        if entry is None:
            entry = {"path": path, "op": op, "reads": 0, "writes": 0, "ts": now}
        entry["op"] = op
        if op == "read":
            entry["reads"] = int(entry.get("reads", 0)) + 1
        elif op in ("write", "edit"):
            entry["writes"] = int(entry.get("writes", 0)) + 1
        entry["ts"] = now
        files.insert(0, entry)  # most recent first
        del files[MAX_FILES:]

        events = _state["events"]
        events.append({"ts": now, "op": op, "path": path, "tool": tool})
        if len(events) > MAX_EVENTS:
            del events[: len(events) - MAX_EVENTS]
    _schedule_write()


def _snapshot() -> dict:
    return {
        "ts": _state["ts"],
        "session_id": _state["session_id"],
        "counts": dict(_state["counts"]),
        "files": [dict(item) for item in _state["files"]],
        "events": [dict(item) for item in _state["events"]],
    }


def _write_now() -> None:
    global _last_write
    try:
        target = state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(_snapshot(), ensure_ascii=False)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, target)  # atomic: readers never see a half file
        _last_write = time.time()
    except Exception:
        logger.debug("filetree-rail: state write failed", exc_info=True)


def _deferred_write() -> None:
    global _pending_timer
    with _lock:
        _pending_timer = None
        _write_now()


def _schedule_write() -> None:
    """Write now when the throttle window is open, else arm one deferred write."""
    global _pending_timer
    with _lock:
        delay = MIN_WRITE_INTERVAL - (time.time() - _last_write)
        if delay <= 0:
            _write_now()
        elif _pending_timer is None:
            timer = threading.Timer(delay, _deferred_write)
            timer.daemon = True
            _pending_timer = timer
            timer.start()


def flush() -> None:
    """Write the in-memory state to disk now (tests and the clear command)."""
    global _pending_timer
    with _lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
            _pending_timer = None
        _write_now()


# ── hooks ───────────────────────────────────────────────────────────────────


def on_post_tool_call(**kwargs) -> None:
    """post_tool_call observer: record the file operations of one tool call."""
    try:
        tool = kwargs.get("tool_name")
        tool_name = tool if isinstance(tool, str) else ""
        op = _OPS.get(tool_name)
        if op is None:
            return
        raw = _arg_path(kwargs.get("args"))
        path = _normalise(raw, kwargs.get("cwd"))
        if not path:
            return
        _record(op, path, tool_name, kwargs.get("session_id"))
    except Exception:
        logger.debug("filetree-rail: post_tool_call failed", exc_info=True)


def clear(_raw_args: str = "") -> str:
    """``/filetree-clear``: reset the log."""
    with _lock:
        _state["ts"] = time.time()
        _state["session_id"] = None
        _state["counts"] = {"read": 0, "write": 0, "edit": 0}
        _state["files"] = []
        _state["events"] = []
    flush()
    return "filetree rail: cleared"


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", on_post_tool_call)
    try:
        ctx.register_command(
            "filetree-clear",
            clear,
            "Reset the filetree rail activity log",
        )
    except Exception:
        logger.debug("filetree-rail: slash command not registered", exc_info=True)
