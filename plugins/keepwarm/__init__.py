"""keepwarm: keep this session's provider prompt cache warm.

How it works
------------
``pre_api_request`` receives the full request payload Hermes is about to POST
(``request`` kwarg). We copy it to ``$HERMES_HOME/state/keepwarm-request.json``
(mode 0600, atomic, at most one write every 5 seconds).

``/keepwarm`` starts one daemon thread. Every interval it re-POSTs that captured
chat body with ``max_tokens=1`` and ``stream=false`` to
``<base_url>/chat/completions``. DeepSeek style prompt caches count a hit when
the prefix is byte-identical, so a replay keeps the prefix alive and cheap.

Rules kept
----------
* Hook callbacks always return ``None``, never raise, never touch the real request.
* The API key is read at ping time and never written to disk, never logged,
  never put into an error string.
* Atomic writes everywhere (tmp file + ``os.replace``), mode 0600.
* Hard limits: 60 pings, 3 hours, 3 failures in a row. On stop we record why.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PLUGIN_NAME = "keepwarm"

DEFAULT_INTERVAL_S = 240
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 1800
CAPTURE_THROTTLE_S = 5.0

MAX_PINGS = 60
MAX_RUNTIME_S = 3 * 3600
MAX_CONSECUTIVE_FAILURES = 3
PING_TIMEOUT_S = 45.0

#: Price used for the running estimate. Only the known model is priced.
PRICED_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
CACHE_READ_USD_PER_M = 0.0024
OUTPUT_USD_PER_M = 0.48

CARD_WIDTH = 46
CARD_LABEL_WIDTH = 14
MAX_TOKENS_FORCED = 1


# --------------------------------------------------------------------------- paths


def hermes_home() -> Path:
    """``$HERMES_HOME``, else ``~/.hermes``. Resolved per call so tests can re-point it."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".hermes"


def state_dir() -> Path:
    return hermes_home() / "state"


def capture_path() -> Path:
    return state_dir() / "keepwarm-request.json"


def status_path() -> Path:
    return state_dir() / "keepwarm-status.json"


def _write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    """Atomic write: tmp file in the same dir, fsync, chmod, replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- capture

_last_capture_ts = 0.0
_capture_lock = threading.Lock()


def pre_api_request(**kwargs):
    """Copy the outgoing request payload to disk. Returns None, never raises."""
    try:
        payload = kwargs.get("request")
        if not isinstance(payload, dict) or not payload:
            return None
        now = time.time()
        global _last_capture_ts
        with _capture_lock:
            if now - _last_capture_ts < CAPTURE_THROTTLE_S:
                return None
            _last_capture_ts = now
        record = {
            "ts": now,
            "session_id": str(kwargs.get("session_id") or ""),
            "model": str(kwargs.get("model") or ""),
            "provider": str(kwargs.get("provider") or ""),
            "base_url": str(kwargs.get("base_url") or ""),
            "api_mode": str(kwargs.get("api_mode") or ""),
            # Unmodified copy of the hook's ``request`` value: {"method": "POST", "body": {...}}
            "body": payload,
        }
        _write_json(capture_path(), record)
    except Exception:
        logger.debug("keepwarm: capture failed", exc_info=True)
    return None


def post_api_request(**kwargs):
    """Observer for real turns. Keeps the last real activity time for the card."""
    global _last_real_turn_ts
    try:
        _last_real_turn_ts = time.time()
    except Exception:
        pass
    return None


_last_real_turn_ts = 0.0


def _chat_body(record: dict) -> dict:
    """Inner OpenAI chat body from a captured record."""
    payload = record.get("body")
    if isinstance(payload, dict) and isinstance(payload.get("body"), dict):
        return copy.deepcopy(payload["body"])
    return copy.deepcopy(payload) if isinstance(payload, dict) else {}


def _truncated_reason(record: dict) -> str:
    """Return a reason string when the capture cannot be replayed faithfully.

    Hermes caps hook payloads (``HERMES_PLUGIN_PAYLOAD_MAX_CHARS``, default
    50000). Over the cap the payload arrives as ``{"_truncated": true,
    "preview": ...}``. Replaying a preview would send a body with no messages
    and warm nothing, so refuse instead of pretending.
    """
    payload = record.get("body")
    if not isinstance(payload, dict):
        return "capture has no body"
    if payload.get("_truncated"):
        return (
            "capture truncated by Hermes (payload over "
            "HERMES_PLUGIN_PAYLOAD_MAX_CHARS). Raise that cap and restart the "
            "gateway."
        )
    body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
    if not isinstance(body, dict):
        return "capture has no body"
    if not body.get("messages"):
        return "capture body has no messages"
    return ""


# --------------------------------------------------------------------------- api key


def _slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(text or "").upper()).strip("_")


def key_env_candidates(base_url: str) -> list:
    """Env var names Hermes may use for a custom endpoint, most specific first.

    Mirrors ``hermes_cli.config.custom_endpoint_key_env``: identity slug upper cased,
    fixed ``HERMES_CUSTOM_`` prefix.
    """
    parsed = urlparse(base_url or "")
    netloc = (parsed.netloc or "").lower()
    host = (parsed.hostname or "").lower()
    names: list = []
    for identity in (netloc, host):
        if identity:
            names.append(f"HERMES_CUSTOM_{_slug(identity)}_API_KEY")
    stem = host[4:] if host.startswith("api.") else host
    if stem:
        names.append(f"{_slug(stem)}_API_KEY")
    names.extend(["HERMES_CUSTOM_API_KEY", "OPENAI_API_KEY"])
    return list(dict.fromkeys(names))


def _read_dotenv(path: Path) -> dict:
    values: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[name] = value
    return values


def resolve_api_key(base_url: str):
    """Return (env_name, value). Empty on no match. Never log the value."""
    names = key_env_candidates(base_url)
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return name, value
    dotenv = _read_dotenv(hermes_home() / ".env")
    for name in names:
        value = (dotenv.get(name) or "").strip()
        if value:
            return name, value
    return "", ""


# --------------------------------------------------------------------------- ping


def _redact(text: str, secret: str) -> str:
    text = str(text or "")
    if secret and secret in text:
        text = text.replace(secret, "[redacted]")
    return text[:200]


def _extract_usage(parsed) -> dict:
    usage = parsed.get("usage") if isinstance(parsed, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("prompt_tokens_details")
    cache = usage.get("prompt_cache_hit_tokens")
    if cache is None:
        cache = usage.get("cached_tokens")
    if cache is None and isinstance(details, dict):
        cache = details.get("cached_tokens")
    if cache is None:
        cache = usage.get("cache_read_input_tokens")
    if cache is None:
        cache = usage.get("cache_read_tokens")

    def _int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "prompt_tokens": _int(usage.get("prompt_tokens")),
        "cache_read_tokens": _int(cache),
        "completion_tokens": _int(usage.get("completion_tokens")),
    }


def _endpoint(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def ping_once(timeout: float = PING_TIMEOUT_S) -> dict:
    """One warm ping. Returns {"ok": bool, "usage": dict, "error": str}."""
    record = _read_json(capture_path())
    if not record:
        return {"ok": False, "usage": {}, "error": "no captured request"}
    blocked = _truncated_reason(record)
    if blocked:
        return {"ok": False, "usage": {}, "error": blocked}
    base_url = str(record.get("base_url") or "")
    if not base_url:
        return {"ok": False, "usage": {}, "error": "capture has no base_url"}
    key_name, key = resolve_api_key(base_url)
    if not key:
        return {"ok": False, "usage": {}, "error": "no api key found"}

    body = _chat_body(record)
    if not body:
        return {"ok": False, "usage": {}, "error": "capture has no chat body"}
    body["max_tokens"] = MAX_TOKENS_FORCED
    body["stream"] = False
    # stream_options is only valid with stream=true; some OpenAI-compatible
    # endpoints reject it outright. It does not affect the cached prefix.
    body.pop("stream_options", None)
    if "max_completion_tokens" in body:
        body["max_completion_tokens"] = MAX_TOKENS_FORCED

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urlrequest.Request(
        _endpoint(base_url),
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        return {"ok": False, "usage": {},
                "error": _redact(f"HTTP {exc.code}: {detail}", key)}
    except Exception as exc:
        return {"ok": False, "usage": {},
                "error": _redact(f"{type(exc).__name__}: {exc}", key)}

    try:
        parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return {"ok": False, "usage": {}, "error": "response was not JSON"}
    if isinstance(parsed, dict) and parsed.get("error"):
        return {"ok": False, "usage": {},
                "error": _redact(f"provider error: {parsed.get('error')}", key)}
    return {"ok": True, "usage": _extract_usage(parsed), "error": ""}


# --------------------------------------------------------------------------- loop state


class _Keepwarm:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.running = False
        self.interval = DEFAULT_INTERVAL_S
        self.started_at = 0.0
        self.pings = 0
        self.ok = 0
        self.failed = 0
        self.consecutive_failures = 0
        self.last_error = ""
        self.last_usage: dict = {}
        self.last_ping_at = 0.0
        self.cache_read_total = 0
        self.output_total = 0
        self.stop_reason = ""

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "started_at": self.started_at,
                "interval_s": self.interval,
                "pings": self.pings,
                "ok": self.ok,
                "failed": self.failed,
                "last_error": self.last_error,
                "last_usage": dict(self.last_usage),
                "est_cache_read_tokens": self.cache_read_total,
                "last_ping_at": self.last_ping_at,
                "stop_reason": self.stop_reason,
            }


_S = _Keepwarm()


def read_status() -> dict:
    return _read_json(status_path())


def _write_status() -> None:
    try:
        _write_json(status_path(), _S.snapshot())
    except Exception:
        logger.debug("keepwarm: status write failed", exc_info=True)


def is_running() -> bool:
    with _S.lock:
        return bool(_S.running and _S.thread is not None and _S.thread.is_alive())


def _finish(reason: str, *, write: bool = True) -> None:
    """Mark the loop stopped and record why."""
    with _S.lock:
        if not _S.running:
            return
        _S.running = False
        _S.stop_reason = reason
        _S.last_error = _S.last_error or ""
    logger.info("keepwarm: stopped: %s", reason)
    if write:
        _write_status()


def _record(result: dict) -> None:
    with _S.lock:
        _S.pings += 1
        _S.last_ping_at = time.time()
        if result.get("ok"):
            usage = result.get("usage") or {}
            _S.ok += 1
            _S.consecutive_failures = 0
            _S.last_usage = dict(usage)
            _S.last_error = ""
            _S.cache_read_total += int(usage.get("cache_read_tokens") or 0)
            _S.output_total += int(usage.get("completion_tokens") or 0)
        else:
            _S.failed += 1
            _S.consecutive_failures += 1
            _S.last_error = str(result.get("error") or "unknown error")[:300]
    _write_status()


def ping_and_record(timeout: float = PING_TIMEOUT_S) -> dict:
    """One ping plus status bookkeeping. Safe to call without the loop."""
    result = ping_once(timeout=timeout)
    _record(result)
    return result


def _loop(interval_s: float) -> None:
    deadline = time.monotonic() + MAX_RUNTIME_S
    stop_event = _S.stop_event
    if stop_event is None:
        return
    while not stop_event.is_set():
        ping_and_record()
        if stop_event.is_set():
            return
        with _S.lock:
            pings = _S.pings
            failures = _S.consecutive_failures
        if failures >= MAX_CONSECUTIVE_FAILURES:
            _finish(f"{failures} failures in a row")
            return
        if pings >= MAX_PINGS:
            _finish(f"ping limit reached ({MAX_PINGS})")
            return
        if time.monotonic() > deadline:
            _finish("time limit reached (3 h)")
            return
        if stop_event.wait(interval_s):
            return


def start_loop(interval_s: float = DEFAULT_INTERVAL_S) -> bool:
    """Start the worker. False when one is already running."""
    with _S.lock:
        if _S.thread is not None and _S.thread.is_alive():
            return False
        _S.interval = int(clamp_interval(interval_s))
        _S.stop_event = threading.Event()
        _S.running = True
        _S.started_at = time.time()
        _S.pings = _S.ok = _S.failed = 0
        _S.consecutive_failures = 0
        _S.last_error = ""
        _S.last_usage = {}
        _S.last_ping_at = 0.0
        _S.cache_read_total = 0
        _S.output_total = 0
        _S.stop_reason = ""
        thread = threading.Thread(
            target=_loop, args=(float(_S.interval),),
            name="keepwarm", daemon=True,
        )
        _S.thread = thread
    _write_status()
    thread.start()
    return True


def stop_loop(reason: str = "stopped by user", join_s: float = 3.0) -> bool:
    """Stop the worker and wait briefly. True when no thread is left alive."""
    with _S.lock:
        thread = _S.thread
        event = _S.stop_event
        was_running = _S.running
    if event is not None:
        event.set()
        if thread is not None:
            thread.join(timeout=join_s)
    alive = bool(thread is not None and thread.is_alive())
    with _S.lock:
        if not alive:
            _S.running = False
            _S.thread = None
            _S.stop_reason = reason if was_running else (_S.stop_reason or reason)
    _write_status()
    return not alive


# --------------------------------------------------------------------------- card


def clamp_interval(value) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, seconds))


def _row(label: str, value: str) -> str:
    """One card line, label column, value aligned. Never wider than CARD_WIDTH."""
    room = CARD_WIDTH - CARD_LABEL_WIDTH
    return (f"{label:<{CARD_LABEL_WIDTH}}{value}"[:CARD_WIDTH]).rstrip()


def _clock(epoch: float) -> str:
    if not epoch:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def card() -> str:
    """Status card. Aligned labels, thousands separators, no raw JSON, <= 46 cols."""
    snap = _S.snapshot()
    running = is_running()
    usage = snap.get("last_usage") or {}
    cache_read = int(snap.get("est_cache_read_tokens") or 0)
    output = int(_S.output_total or 0)
    model = str(_read_json(capture_path()).get("model") or "")
    priced = bool(model) and model == PRICED_MODEL
    cost = 0.0
    if priced:
        cost = (cache_read / 1_000_000.0) * CACHE_READ_USD_PER_M
        cost += (output / 1_000_000.0) * OUTPUT_USD_PER_M
    pings = int(snap.get("pings") or 0)
    lines = [
        _row("Keepwarm", "running" if running else "stopped"),
        _row("Interval", f"{int(snap.get('interval_s') or DEFAULT_INTERVAL_S)} s"),
        _row("Pings", f"{pings:,} / {MAX_PINGS:,}"),
        _row("OK / failed", f"{int(snap.get('ok') or 0):,} / {int(snap.get('failed') or 0):,}"),
        _row("Cache reads", f"{cache_read:,} tok"),
        _row("Cost (est)", f"${cost:.4f}" if priced else "not priced"),
        _row("Prompt tok", f"{int(usage.get('prompt_tokens') or 0):,}"),
        _row("Last ping", _clock(float(snap.get("last_ping_at") or 0))),
        _row("Started", _clock(float(snap.get("started_at") or 0))),
    ]
    if running and snap.get("started_at"):
        elapsed = time.time() - float(snap["started_at"])
        lines.append(_row("Age", _duration(elapsed)))
        lines.append(_row("Limits", "60 pings or 3 h"))
    if snap.get("last_error"):
        lines.append(_row("Last error", _one_line(snap["last_error"])))
    if not running and snap.get("stop_reason"):
        lines.append(_row("Stopped", _one_line(snap["stop_reason"])))
    return "\n".join(lines)


def _one_line(text: str, limit: int = CARD_WIDTH) -> str:
    return " ".join(str(text or "").split())[:limit]


# --------------------------------------------------------------------------- commands


def cmd_keepwarm(raw_args: str = "") -> str:
    try:
        return _cmd_keepwarm(raw_args)
    except Exception:
        logger.debug("keepwarm: command failed", exc_info=True)
        return "keepwarm: internal error, see the log."


def _cmd_keepwarm(raw_args: str) -> str:
    argument = (raw_args or "").strip()
    interval = DEFAULT_INTERVAL_S
    if argument:
        first = argument.split()[0]
        if not re.fullmatch(r"\d+(\.\d+)?", first):
            return "Use /keepwarm or /keepwarm 120."
        interval = clamp_interval(first)

    if not capture_path().exists():
        return "Send a normal message first. No capture yet."

    if is_running():
        return card()
    start_loop(interval)
    return card()


def cmd_keepwarm_stop(raw_args: str = "") -> str:
    try:
        if not capture_path().exists() and not status_path().exists():
            return "Nothing to stop. Keepwarm never started."
        stop_loop("stopped by user")
        if not is_running():
            snap = _S.snapshot()
            if snap.get("pings"):
                return card()
            return "Keepwarm stopped. It had not pinged yet."
        return "Keepwarm could not stop the worker."
    except Exception:
        logger.debug("keepwarm: stop command failed", exc_info=True)
        return "keepwarm: internal error, see the log."


# --------------------------------------------------------------------------- register


def register(ctx) -> None:
    ctx.register_hook("pre_api_request", pre_api_request)
    ctx.register_hook("post_api_request", post_api_request)
    ctx.register_command(
        "keepwarm", cmd_keepwarm,
        description="Keep the prompt cache warm by replaying the last request",
        args_hint="[seconds]",
    )
    ctx.register_command(
        "keepwarm-stop", cmd_keepwarm_stop,
        description="Stop the keepwarm loop",
    )
