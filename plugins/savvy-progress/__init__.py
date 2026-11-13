"""savvy-progress — session token and cost snapshot producer.

Hermes plugin (data producer) for the ``savvy-progress.mjs`` TUI widget. On every
``post_tool_call`` it reads the session's usage totals from ``$HERMES_HOME/state.db``
(read-only) and writes one small JSON snapshot to ``$HERMES_HOME/state/savvy-progress.json``
(atomic ``.tmp`` + ``os.replace``). The widget polls that file once per second. The same
snapshot backs the ``/svprogress`` slash command, which prints an aligned text card
(``render_card``) instead of raw JSON.

Per-session continuity: the snapshot file is shared, so the session that fired last owns
it. Turn history therefore lives in a second file,
``$HERMES_HOME/state/savvy-progress.sessions.json`` (atomic write, mode 0600), keyed by
session_id. Each entry keeps its own ``turn``, ``turns`` and the usage ``totals`` it last
recorded. The hook diffs the new totals of the firing session against that session's own
stored totals, so a parallel session cannot reset the counter. Caps: 8 most recently
touched sessions, 24 turns each, entries untouched for 6 hours are dropped. The sessions
file is internal state; the snapshot keeps its own 17 keys.

Primary / delegate split (the card follows the user's own session):
  * a live ``local-`` parentless session that fires a hook is PINNED to
    ``$HERMES_HOME/state/savvy-progress.primary.json`` before any usage lookup, so a
    brand-new chat owns the card from its first API call, before its first usage row
    exists. A delegate child never pins.
  * resolution order: (a) the pinned session, while its ``sessions`` row still exists,
    is still ``created_source = 'tui'``, the pin is under 12 hours old and the session
    did not end over 30 minutes ago; (b) else the newest parentless ``local-`` session
    ranked by ``created_source`` first (``tui`` beats ``desktop`` beats ``cli`` beats
    anything else), then by ``MAX(started_at, last_activity_at, max usage last_seen)``;
    (c) else the newest session of any kind (a parented row never qualifies). Without a
    ``sessions`` table the newest ``last_seen`` in ``session_model_usage`` decides, id
    prefix first.
  * ``created_source`` separates the user's own chat (``tui``) from spawned workers
    (``cli`` tmux sessions, ``cron``, ``oneshot``), so a fresh worker can never take the
    card. The pin is only written for a parentless ``tui`` session.
  * a primary with no usage rows yet reports zeros for its own id, never the numbers of
    the session that wrote the snapshot last.
  * delegates = every session whose ``parent_session_id`` chains up to the primary,
    at most 3 levels deep, cycle-safe, active within the same 6-hour window. Their
    token and cost columns are summed beside the primary, never instead of it.
    The walk starts at the primary and only descends, so a sibling's child, a session
    under another parent, a cron row, a oneshot row, or a row whose parent is missing
    from the table is never counted. Every chat therefore counts only what it spawned.
  * every session that fires a hook also gets its own file,
    ``$HERMES_HOME/state/savvy-progress.sessions/<session_id>.json`` (same 19 keys, its
    own numbers and its own scoped delegates). The directory keeps the 16 most recently
    written files. The widget reads only ``savvy-progress.json``, so it needs no change.
  * the id-prefix rule stays as the last resort for old databases without a usable
    ``sessions`` table.
  * the snapshot keeps its 14 original keys and adds ``primary_session``,
    ``delegates`` and ``total_cost_usd`` (17 keys).

Rules kept from the data contract:
  * at most 24 recent per-turn deltas, oldest first,
  * ``cache_hit_pct`` = cache_read / (input + cache_read) * 100,
  * one write per second maximum, no write when nothing changed,
  * every failure is swallowed and logged at debug level (a hook must never raise).

Schema note: in this Hermes build the cost columns are ``NOT NULL DEFAULT 0``, so
``COALESCE(actual_cost_usd, estimated_cost_usd)`` can never fall through. The provider
lane used here (``custom`` / ``api.river.ai``) reports 0 for both, with
``cost_status = 'unknown'``, so the snapshot would print $0 forever. Cost therefore
resolves per row in this order:

  1. ``actual_cost_usd`` when > 0      -> ``cost_source = "provider"``
  2. ``estimated_cost_usd`` when > 0   -> ``cost_source = "provider"``
  3. tokens x ``PRICE_TABLE``          -> ``cost_source = "table"``
  4. no price match (or no tokens)     -> ``cost_usd = 0.0``, ``cost_source = "none"``

A session that mixes sources reports "provider" when any row carried a real cost,
because that part of the total is not an estimate. "table" means every priced dollar
is an estimate. "none" means the number is not a cost at all, so the widget prints n/a.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:  # POSIX only; a host without fcntl keeps the threading lock as its only guard.
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SNAPSHOT_NAME = "savvy-progress.json"
SESSIONS_NAME = "savvy-progress.sessions.json"
PIN_NAME = "savvy-progress.primary.json"
MAX_TURNS = 24
MAX_SESSIONS = 8
MAX_SESSION_TURNS = 24
SESSION_TTL_S = 6 * 3600.0
MIN_WRITE_INTERVAL_S = 1.0
DB_TIMEOUT_S = 1.0
# The "today" row sums every session started inside this window, so parallel chats and
# cron runs cannot hide their spend behind the one session the card describes.
DAY_WINDOW_S = 24 * 3600.0
# A burn rate divided by a two-minute session is noise, not a rate. Floor the divisor.
BURN_FLOOR_S = 300.0
# Day spend scans several tables; the card refreshes on every tool call. Cache the result
# for a few seconds so a long chat does not re-scan the DB on every hook.
DAY_CACHE_S = 5.0
# Hard cap on usage rows summed for the day row: a runaway DB cannot stall a tool call.
DAY_ROW_LIMIT = 50_000
# Seconds to wait for the cross-process file lock before skipping this write.
WRITE_LOCK_TIMEOUT_S = 1.0
# Optional price overrides, merged over PRICE_TABLE on every mtime change.
PRICE_OVERRIDE_NAME = "savvy-progress.prices.json"

# --- primary / delegate split ---------------------------------------------------
# The card describes the user's own interactive session. Everything below it in the
# parent chain is a delegate and is reported as an extra cost, never as the card.
PRIMARY_PREFIX = "local-"
DELEGATE_WINDOW_S = 6 * 3600.0
MAX_DELEGATE_DEPTH = 3
# ``sessions.created_source`` says who started a session. The card belongs to the user's
# own chat (``tui``), never to a spawned worker (``cli`` tmux, ``cron``, ``oneshot``).
# Lower rank wins; an unknown or NULL source ranks last.
PRIMARY_SOURCE = "tui"
SOURCE_RANK: Dict[str, int] = {"tui": 0, "desktop": 1, "cli": 2}
SOURCE_RANK_OTHER = 3
# A cron or oneshot row is scheduled work, not a task this chat spawned. Never a delegate.
DELEGATE_SOURCE_BLOCK = {"cron", "oneshot"}
# Per-session view files: one per firing session, 16 most recent kept.
SESSION_DIR_NAME = "savvy-progress.sessions"
MAX_SESSION_FILES = 16
# A pin outlives one turn, not one day: after 12 h the time-ordered rule takes over.
PIN_TTL_S = 12 * 3600.0
# A pinned session that ended more than 30 min ago must not hold the card.
PIN_ENDED_STALE_S = 30 * 60.0

# --- context bar -----------------------------------------------------------------
# context_tokens is the biggest prompt this session has sent: one model row's
# (input + cache read + cache write) divided by that row's api_call_count.
DEFAULT_CONTEXT_WINDOW = 1_000_000
_context_cache: Dict[str, Any] = {}
_price_cache: Dict[str, Any] = {"key": None, "table": None}
_day_cache: Dict[str, Any] = {"key": None, "value": None}

DELEGATE_DEFAULTS: Dict[str, Any] = {
    "count": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read": 0,
    "cost_usd": 0.0,
    "cost_source": "none",
}

# --- /svprogress card geometry -------------------------------------------------
# Every printed line is kept under CARD_W cells. The label column is fixed, every
# number is right-aligned, and the cost field has a constant width, so rows never jump.
CARD_W = 42
LABEL_W = 14
NUM_W = 12
TURN_NUM_W = 3
TURN_IN_W = 8
TURN_OUT_W = 6
TURN_COST_W = 8
RULE = "─"
STALE_S = 300.0
NO_DATA = "savvy-progress: no data yet. Run a tool."
UNAVAILABLE = "savvy-progress: snapshot unavailable."

_lock = threading.Lock()
_last_write = 0.0

# Fallback prices, USD per 1,000,000 tokens: (input, output, cache_read).
# Keys are matched as case-insensitive substrings of the model name; when several keys
# match, the longest one wins, so a fully qualified id can be priced more precisely than
# a short alias. Add one line per new model. Rates are list prices; update them rather
# than guessing, and keep them 3-tuples so the estimate stays auditable.
# cache_write_tokens is not billed here: the seeded table carries no cache-write rate,
# so charging one would be an invented number.
PRICE_TABLE: Dict[str, Tuple[float, float, float]] = {
    "deepseek-v4.1-flash": (0.30, 1.20, 0.006),
    "deepseek-ai/deepseek-v4.1-flash-1017": (0.30, 1.20, 0.006),
}

_PRICE_KEYS = tuple(sorted(PRICE_TABLE, key=len, reverse=True))

_ROWS_SQL = """
SELECT model,
       api_call_count,
       input_tokens,
       output_tokens,
       cache_read_tokens,
       cache_write_tokens,
       actual_cost_usd,
       estimated_cost_usd
FROM session_model_usage
WHERE session_id = ?
"""

_MODEL_SQL = """
SELECT model, billing_provider
FROM session_model_usage
WHERE session_id = ?
ORDER BY api_call_count DESC, last_seen DESC
LIMIT 1
"""

# Session tree, one query. ``last_activity_at`` and ``started_at`` are the authority; the
# per-session usage rows are the fallback for rows that never got the timestamps filled in.
# ``created_source`` decides who the session belongs to (``tui`` = the user's own chat).
_SESSIONS_SQL = """
SELECT s.id,
       s.parent_session_id,
       s.created_source,
       s.model,
       s.input_tokens,
       s.output_tokens,
       s.cache_read_tokens,
       s.cache_write_tokens,
       s.estimated_cost_usd,
       s.actual_cost_usd,
       s.ended_at,
       CASE WHEN COALESCE(s.started_at, 0) = 0
                 AND COALESCE(s.last_activity_at, 0) = 0
                 AND COALESCE(u.seen, 0) = 0
            THEN NULL
            ELSE MAX(COALESCE(s.started_at, 0),
                     COALESCE(s.last_activity_at, 0),
                     COALESCE(u.seen, 0))
       END AS seen
FROM sessions s
LEFT JOIN (
    SELECT session_id, MAX(last_seen) AS seen
    FROM session_model_usage
    GROUP BY session_id
) u ON u.session_id = s.id
"""

# One row per firing session: does the pin belong to it? Parentless and started by the TUI.
_PIN_ELIGIBLE_SQL = """
SELECT id
FROM sessions
WHERE id = ? AND parent_session_id IS NULL AND created_source = ?
"""

_LATEST_ANY_SQL = """
SELECT session_id, MAX(last_seen) AS seen
FROM session_model_usage
WHERE last_seen IS NOT NULL
GROUP BY session_id
ORDER BY seen DESC
LIMIT 1
"""

_LATEST_PRIMARY_SQL = """
SELECT session_id, MAX(last_seen) AS seen
FROM session_model_usage
WHERE last_seen IS NOT NULL AND session_id LIKE 'local-%'
GROUP BY session_id
ORDER BY seen DESC
LIMIT 1
"""

_DELEGATE_USAGE_SQL = """
SELECT session_id
FROM session_model_usage
WHERE last_seen IS NOT NULL
  AND session_id NOT LIKE 'local-%'
GROUP BY session_id
HAVING MAX(last_seen) >= ?
"""

_CONTEXT_SQL = """
SELECT MAX((input_tokens + cache_read_tokens + cache_write_tokens) /
           CASE WHEN api_call_count > 0 THEN api_call_count ELSE 1 END)
FROM session_model_usage
WHERE session_id = ?
"""

# Day spend: every usage row whose session started inside the window. A session with a
# null start falls back to its last activity, so an old chat that kept running still counts.
_DAY_SQL = f"""
SELECT u.model,
       u.input_tokens,
       u.output_tokens,
       u.cache_read_tokens,
       u.cache_write_tokens,
       u.actual_cost_usd,
       u.estimated_cost_usd,
       u.session_id
FROM session_model_usage u
JOIN sessions s ON s.id = u.session_id
WHERE COALESCE(NULLIF(s.started_at, 0), NULLIF(s.last_activity_at, 0), 0) >= ?
  AND COALESCE(NULLIF(s.started_at, 0), NULLIF(s.last_activity_at, 0), 0) < ?
LIMIT {DAY_ROW_LIMIT}
"""


def _hermes_home() -> Path:
    """Profile-aware home. Falls back to the env var, then to ``~/.hermes``."""
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415 — optional dependency

        return Path(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        return Path(home)


def _snapshot_path() -> Path:
    return _hermes_home() / "state" / SNAPSHOT_NAME


def _sessions_path() -> Path:
    return _hermes_home() / "state" / SESSIONS_NAME


def _pin_path() -> Path:
    return _hermes_home() / "state" / PIN_NAME


def _session_dir() -> Path:
    return _hermes_home() / "state" / SESSION_DIR_NAME


def _db_path() -> Path:
    return _hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=DB_TIMEOUT_S)
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """A read-only connection that always closes.

    ``with sqlite3.connect(...)`` commits on exit but does NOT close the handle, so a card
    that refreshes on every tool call leaks one file descriptor per query. This wrapper
    closes it, which is what keeps a long session flat.
    """
    conn = _connect()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover — already closed
            pass


def _safe_rows(sql: str, params: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
    """Rows for a read-only query, or ``[]``.

    Every DB read goes through here: a missing DB, a locked DB, a corrupt file and a
    schema change all reduce to "no rows", so the card degrades to zeros instead of
    killing the tool hook that fired it.
    """
    try:
        with _db() as conn:
            return conn.execute(sql, params).fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.debug("savvy-progress: query failed (%s): %s", sql.split()[0:2], exc)
        return []
    except Exception as exc:  # noqa: BLE001 — a read must never break a turn
        logger.debug("savvy-progress: unexpected read failure: %s", exc)
        return []


def _safe_row(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Tuple[Any, ...]]:
    rows = _safe_rows(sql, params)
    return rows[0] if rows else None


def _match_price(model: str) -> Optional[Tuple[float, float, float]]:
    """Price tuple for a model name, or None.

    Case-insensitive substring match, longest key first. An operator override outranks a
    baked row: if someone writes a rate for an alias, that rate applies to every model id
    containing the alias, because the override is the more specific statement of intent.
    """
    name = (model or "").lower()
    overrides = _override_table()
    for key in sorted(overrides, key=len, reverse=True):
        if key in name:
            return overrides[key]
    table = _price_table()
    for key in sorted(table, key=len, reverse=True):
        if key in name:
            return table[key]
    return None


def _load_price_overrides() -> Dict[str, Tuple[float, float, float]]:
    """``state/savvy-progress.prices.json`` as a price table. Missing or bad rows ignored.

    Format: ``{"model-alias": [input, output, cache_read]}`` in USD per 1M tokens. This
    lets a rate change or a new model land without editing the plugin, which is the
    difference between a personal script and something installable.
    """
    path = _hermes_home() / "state" / PRICE_OVERRIDE_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.debug("savvy-progress: price overrides unreadable: %s", exc)
        return {}

    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.debug("savvy-progress: price overrides are not JSON: %s", exc)
        return {}

    if not isinstance(data, dict):
        return {}

    table: Dict[str, Tuple[float, float, float]] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(value, dict):  # tolerate a named-field form
            value = [value.get("input"), value.get("output"), value.get("cache_read")]
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            logger.debug("savvy-progress: price row %r ignored, needs 3 numbers", key)
            continue
        try:
            numbers = tuple(max(0.0, _finite(part)) for part in value)
        except (TypeError, ValueError):
            logger.debug("savvy-progress: price row %r ignored, not numeric", key)
            continue
        table[key.strip().lower()] = numbers  # type: ignore[assignment]
    return table


def _override_table() -> Dict[str, Tuple[float, float, float]]:
    """Override rows, cached until the file's mtime changes. A read per render is waste."""
    path = _hermes_home() / "state" / PRICE_OVERRIDE_NAME
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    if _price_cache.get("override_key") == stamp and isinstance(_price_cache.get("overrides"), dict):
        return _price_cache["overrides"]  # type: ignore[return-value]
    table = _load_price_overrides()
    _price_cache["override_key"] = stamp
    _price_cache["overrides"] = table
    return table


def _price_table() -> Dict[str, Tuple[float, float, float]]:
    """``PRICE_TABLE`` merged with the override file, cached until either one changes."""
    path = _hermes_home() / "state" / PRICE_OVERRIDE_NAME
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (stamp, path.exists())
    cached = _price_cache.get("key")
    if cached == key and isinstance(_price_cache.get("table"), dict):
        return _price_cache["table"]  # type: ignore[return-value]

    table = dict(PRICE_TABLE)
    table.update(_load_price_overrides())
    _price_cache["key"] = key
    _price_cache["table"] = table
    return table


def _resolve_cost(
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_read: int,
    actual: Any,
    estimated: Any,
) -> Tuple[float, str]:
    """``(cost_usd, cost_source)`` for one usage row.

    Order: provider actual, provider estimate, then the local price table. A row with no
    provider cost and no matching price yields ``(0.0, "none")`` instead of a silent $0.
    """
    for raw in (actual, estimated):
        try:
            value = float(raw or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            return value, "provider"

    price = _match_price(model)
    if price is None:
        return 0.0, "none"

    price_in, price_out, price_cache = price
    cost = (
        (tokens_in / 1_000_000.0) * price_in
        + (tokens_out / 1_000_000.0) * price_out
        + (cache_read / 1_000_000.0) * price_cache
    )
    return cost, "table"


def _read_totals(session_id: str) -> Optional[Dict[str, Any]]:
    """Summed usage for one session, or None when the session is unknown."""
    rows = _safe_rows(_ROWS_SQL, (session_id,))
    if not rows:
        return None

    calls = tokens_in = tokens_out = cache_read = cache_write = 0
    cost_total = 0.0
    sources = set()

    for model, row_calls, r_in, r_out, r_cache, r_write, actual, estimated in rows:
        r_in = int(r_in or 0)
        r_out = int(r_out or 0)
        r_cache = int(r_cache or 0)
        calls += int(row_calls or 0)
        tokens_in += r_in
        tokens_out += r_out
        cache_read += r_cache
        cache_write += int(r_write or 0)

        row_cost, row_source = _resolve_cost(str(model or ""), r_in, r_out, r_cache, actual, estimated)
        cost_total += row_cost
        sources.add(row_source)

    if "provider" in sources:
        cost_source = "provider"
    elif "table" in sources:
        cost_source = "table"
    else:
        cost_source = "none"

    return {
        "api_calls": calls,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": round(cost_total, 6),
        "cost_source": cost_source,
    }


def _zero_totals() -> Dict[str, Any]:
    """Zeros for a session that exists but has no usage row yet.

    A brand-new chat is exactly that for a few seconds. Reporting its own zeros keeps the
    card honest; reporting the previous chat's numbers is the bug this exists to stop.
    """
    return {
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost_usd": 0.0,
        "cost_source": "none",
    }


def _read_model(session_id: str) -> Tuple[str, str]:
    """The busiest model row for the session: (model, provider)."""
    row = _safe_row(_MODEL_SQL, (session_id,))
    if not row:
        return "unknown", ""
    model = str(row[0] or "unknown")
    provider = str(row[1] or "").split(":")[0].strip()
    return model, provider


def _read_context_tokens(session_id: str) -> int:
    """Current prompt size of the session, in tokens.

    ``input_tokens``, ``cache_read_tokens`` and ``cache_write_tokens`` are lifetime sums in
    this schema, so their plain sum is the session's whole throughput, not its prompt: on a
    long session it reads ~12x the 1M window. Dividing that sum by the row's
    ``api_call_count`` gives the average prompt per call, and the max over rows is the
    biggest prompt the session has sent. That is the number the TUI status bar tracks.
    """
    row = _safe_row(_CONTEXT_SQL, (session_id,))
    try:
        return max(0, int(row[0] or 0)) if row else 0
    except (TypeError, ValueError):
        return 0


def _finite(value: Any, default: float = 0.0) -> float:
    """A finite float, or ``default``.

    ``json.dumps`` writes ``Infinity`` and ``NaN`` as bare literals, which are not JSON:
    the TUI's ``JSON.parse`` throws and the whole card falls back to zeros. One bad cost
    float must never blank the card, so every float leaves here finite.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int_value(value: Any, default: int = 0) -> int:
    """An int, or ``default``. A hostile payload can carry a list where a count belongs."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _read_session_start(session_id: str) -> float:
    """``started_at`` for one session, or 0.0. Never raises."""
    row = _safe_row("SELECT started_at FROM sessions WHERE id = ?", (session_id,))
    try:
        return max(0.0, float(row[0] or 0.0)) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def _burn_rate(cost_usd: float, started_at: float, now: float) -> Tuple[float, float]:
    """``(cost_per_hour, elapsed_s)`` for the session. Elapsed floors at BURN_FLOOR_S.

    A session three minutes old produces a rate that is technically true and useless, so
    a short session reports the rate for its first five minutes instead of a spike.
    """
    try:
        elapsed = float(now) - float(started_at)
    except (TypeError, ValueError):
        elapsed = 0.0
    if elapsed <= 0.0 or float(started_at) <= 0.0:
        # A zero start means the row has no clock, not that the chat began at the epoch.
        return 0.0, 0.0
    hours = max(elapsed, BURN_FLOOR_S) / 3600.0
    try:
        rate = _finite(cost_usd) / hours
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0, elapsed
    # Two decimals is what the card shows, and it keeps the payload stable for minutes:
    # a payload that changes every second rewrites and fsyncs the file on every tool call.
    return round(_finite(rate), 2), elapsed


def _cache_saved_usd(model: str, cache_read: int) -> Tuple[float, str]:
    """Dollars a cache hit avoided: cached tokens billed at the cache rate, not input.

    ``(0.0, "none")`` when the model has no price row, so the card prints n/a instead of
    inventing a saving.
    """
    price = _match_price(model)
    if price is None:
        return 0.0, "none"
    price_in, _price_out, price_cache = price
    try:
        tokens = max(0, int(cache_read or 0))
    except (TypeError, ValueError):
        tokens = 0
    saved = (tokens / 1_000_000.0) * max(0.0, price_in - price_cache)
    return round(saved, 6), "table"


def _day_bounds(now: float) -> Tuple[float, float]:
    """Local calendar day ``[00:00, next 00:00)`` as epoch seconds.

    The "today" row answers a question the user can check against their own clock: spend
    since midnight. A rolling 24 h window showed yesterday evening's chats as today's, which
    reads as a miscount. DST is handled by asking ``mktime`` to normalise the next day.
    """
    lt = time.localtime(now)
    year, month, day = lt.tm_year, lt.tm_mon, lt.tm_mday
    start = time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
    end = time.mktime((year, month, day + 1, 0, 0, 0, 0, 0, -1))
    if end <= start:  # a broken TZ database: fall back to 24 h from midnight
        end = start + 86400.0
    return start, end


def _day_spend(now: float) -> Dict[str, Any]:
    """Every session started today, priced together.

    The card follows one session; this is the only row that can see the cron runs, the
    delegate children and the other terminal. Returns ``cost_usd``, ``cost_source`` and
    the number of distinct sessions, zeros included, so a quiet day reads $0, not n/a.
    """
    start, end = _day_bounds(now)
    bucket = int(now // DAY_CACHE_S)
    try:
        db_stamp = _db_path().stat()
        key = (bucket, round(start, 3), round(end, 3), db_stamp.st_mtime_ns, db_stamp.st_size)
    except OSError:
        key = (bucket, round(start, 3), round(end, 3), 0, 0)
    if _day_cache.get("key") == key and isinstance(_day_cache.get("value"), dict):
        return dict(_day_cache["value"])  # type: ignore[arg-type]

    rows = _safe_rows(_DAY_SQL, (start, end))
    if len(rows) >= DAY_ROW_LIMIT:
        logger.debug("savvy-progress: day spend hit the %d row cap", DAY_ROW_LIMIT)

    total = 0.0
    sources = set()
    sessions = set()
    day_in = day_out = day_cache = day_cache_write = 0
    for model, r_in, r_out, r_cache, r_cache_write, actual, estimated, session_id in rows:
        # Tokens are counted raw, the same way the provider bills them: a cached prompt is
        # still tokens through the model, just cheaper ones.
        day_in += max(0, int(r_in or 0))
        day_out += max(0, int(r_out or 0))
        day_cache += max(0, int(r_cache or 0))
        day_cache_write += max(0, int(r_cache_write or 0))
        row_cost, row_source = _resolve_cost(
            str(model or ""),
            int(r_in or 0),
            int(r_out or 0),
            int(r_cache or 0),
            actual,
            estimated,
        )
        total += row_cost
        sources.add(row_source)
        sessions.add(str(session_id or ""))

    if "provider" in sources:
        source = "provider"
    elif "table" in sources:
        source = "table"
    else:
        source = "none"
    result = {
        "cost_usd": round(_finite(total), 6),
        "cost_source": source,
        "sessions": len(sessions),
        # Every token the day pushed through a model: fresh input, cached input, cache
        # writes and output, summed. The card shows the total; the parts are kept for tests.
        "input_tokens": day_in,
        "output_tokens": day_out,
        "cache_read": day_cache,
        "cache_write": day_cache_write,
        "tokens": day_in + day_out + day_cache + day_cache_write,
    }
    _day_cache["key"] = key
    _day_cache["value"] = dict(result)
    return dict(result)


def _context_window() -> int:
    """``model.context_length`` from ``config.yaml``. Never raises, never returns 0."""
    path = _hermes_home() / "config.yaml"
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return DEFAULT_CONTEXT_WINDOW

    if _context_cache.get("key") == key and _context_cache.get("value"):
        return int(_context_cache["value"])

    window = DEFAULT_CONTEXT_WINDOW
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""

    if text:
        parsed = None
        try:
            import yaml  # noqa: PLC0415 — optional, the regex below is the fallback

            data = yaml.safe_load(text)
            if isinstance(data, dict):
                model_block = data.get("model")
                if isinstance(model_block, dict):
                    parsed = model_block.get("context_length")
        except Exception as exc:
            logger.debug("savvy-progress: yaml parse failed, using the regex: %s", exc)

        try:
            candidate = int(parsed) if parsed is not None else 0
        except (TypeError, ValueError):
            candidate = 0

        if candidate <= 0:
            match = re.search(r"^\s*context_length:\s*(\d+)\s*$", text, re.MULTILINE)
            if match:
                try:
                    candidate = int(match.group(1))
                except ValueError:
                    candidate = 0
        if candidate > 0:
            window = candidate

    _context_cache["key"] = key
    _context_cache["value"] = window
    return window


def _read_session_table() -> Optional[Dict[str, Dict[str, Any]]]:
    """Every row of ``sessions`` keyed by id, or None when the table is unusable.

    One query per snapshot: the tree is small (a few hundred rows) and walking it in
    Python keeps the depth cap and the cycle guard obvious.
    """
    try:
        with _db() as conn:
            cur = conn.execute(_SESSIONS_SQL)
            names = [col[0] for col in cur.description or ()]
            raw = cur.fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.debug("savvy-progress: sessions table unusable: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — the card must never break a turn
        logger.debug("savvy-progress: unexpected sessions read failure: %s", exc)
        return None

    table: Dict[str, Dict[str, Any]] = {}
    for raw_row in raw:
        rec = dict(zip(names, raw_row))
        sid = rec.get("id")
        if sid is None:
            continue
        try:
            rec["seen"] = float(rec["seen"]) if rec["seen"] is not None else None
        except (TypeError, ValueError):
            rec["seen"] = None
        try:
            rec["ended_at"] = float(rec["ended_at"]) if rec["ended_at"] is not None else None
        except (TypeError, ValueError):
            rec["ended_at"] = None
        rec["parent"] = rec.get("parent_session_id") or None
        rec["source"] = str(rec.get("created_source") or "").strip().lower()
        table[str(sid)] = rec
    return table


def _source_rank(rec: Optional[Dict[str, Any]]) -> int:
    """Rank of a session's ``created_source``: tui 0, desktop 1, cli 2, anything else 3."""
    if not rec:
        return SOURCE_RANK_OTHER
    return SOURCE_RANK.get(str(rec.get("source") or ""), SOURCE_RANK_OTHER)


# --- primary pin ------------------------------------------------------------------
# A brand-new chat has a ``sessions`` row seconds before its first usage row, so the
# usage-ordered rule cannot see it. The session that fires a hook is therefore pinned to
# one small file, written before any usage lookup, and read first when the card is built.


def _read_pin() -> Tuple[Optional[str], float]:
    """``(session_id, ts)`` of the pin, or ``(None, 0.0)`` when absent or unreadable."""
    try:
        with open(_pin_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, 0.0
    except Exception as exc:
        logger.debug("savvy-progress: unreadable pin ignored: %s", exc)
        return None, 0.0

    if not isinstance(data, dict):
        return None, 0.0
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None, 0.0
    try:
        ts = float(data.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return sid, ts


def _write_pin(session_id: str, ts: Optional[float] = None) -> bool:
    """Atomic write, mode 0600: the file names one session, so keep it private."""
    sid = str(session_id or "")
    if not sid:
        return False
    payload = {"session_id": sid, "ts": round(time.time() if ts is None else float(ts), 3)}
    return _write_json_atomic(_pin_path(), payload)


def _drop_pin() -> None:
    """Remove the pin. Never raises."""
    try:
        _pin_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("savvy-progress: pin not removed: %s", exc)


def _pin_session(session_id: str) -> bool:
    """Pin a parentless ``tui`` session. Called by both hooks before any usage read.

    A delegate child, a ``cli`` worker, a cron or a oneshot session returns False and
    leaves the pin alone, so a spawned worker can never take the card.
    """
    sid = str(session_id or "")
    if not sid.startswith(PRIMARY_PREFIX):
        return False
    row = _safe_row(_PIN_ELIGIBLE_SQL, (sid, PRIMARY_SOURCE))
    if row is None:
        return False
    try:
        wrote = _write_pin(sid)
    except Exception as exc:
        logger.debug("savvy-progress: pin write failed: %s", exc)
        return False
    return bool(wrote)


def _pinned_session(
    table: Dict[str, Dict[str, Any]],
    now: float,
) -> Optional[str]:
    """The pinned session, while it is still a live parentless ``tui`` session."""
    sid, ts = _read_pin()
    if not sid or not sid.startswith(PRIMARY_PREFIX):
        return None
    rec = table.get(sid)
    if rec is None:
        return None                      # the row is gone: the pin is dead
    if rec["parent"] is not None:
        return None                      # never a delegate
    if _source_rank(rec) != SOURCE_RANK[PRIMARY_SOURCE]:
        return None                      # never a spawned worker
    if now - ts > PIN_TTL_S:
        return None                      # older than 12 h
    ended = rec.get("ended_at")
    if ended is not None and now - ended > PIN_ENDED_STALE_S:
        return None                      # ended over 30 min ago
    return sid


def _primary_from_table(table: Dict[str, Dict[str, Any]], now: float) -> Optional[str]:
    """Newest parentless ``local-`` session by source rank, else the newest of any kind.

    ``created_source`` ranks first: the user's own ``tui`` chat beats a ``desktop`` one,
    which beats a spawned ``cli`` worker. Only then does the clock decide, so a tmux
    worker that started a second ago cannot take the card from the chat it was spawned by.
    """
    best_local: Optional[Dict[str, Any]] = None
    best_key: Optional[Tuple[int, float]] = None
    for rec in table.values():
        if rec["parent"] is not None or not str(rec["id"]).startswith(PRIMARY_PREFIX):
            continue
        if rec["seen"] is None:
            continue
        key = (_source_rank(rec), -float(rec["seen"]))
        if best_key is None or key < best_key:
            best_local = rec
            best_key = key

    if best_local is not None:
        return str(best_local["id"])

    # Last resort inside the table: the newest session of any kind. A parented row is a
    # delegate and never becomes the card.
    timed = [
        rec for rec in table.values()
        if rec["seen"] is not None and rec["parent"] is None
    ]
    if not timed:
        return None
    newest = max(timed, key=lambda rec: rec["seen"])
    return str(newest["id"])


def _primary_from_usage(now: float) -> Optional[str]:
    """Last resort for databases without a usable ``sessions`` table: id prefix only."""
    try:
        with _db() as conn:
            local = conn.execute(_LATEST_PRIMARY_SQL).fetchone()
            if local is not None:
                try:
                    seen = float(local[1])
                except (TypeError, ValueError):
                    seen = 0.0
                if now - seen <= DELEGATE_WINDOW_S:
                    return str(local[0])
            any_row = conn.execute(_LATEST_ANY_SQL).fetchone()
    except sqlite3.Error as exc:
        logger.debug("savvy-progress: usage table unusable: %s", exc)
        return None
    return str(any_row[0]) if any_row is not None else None


def _primary_session_id(
    now: Optional[float] = None,
    table: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[str]:
    """The session the card describes: the pin first, then the table, then usage rows."""
    now = time.time() if now is None else now
    rows = _read_session_table() if table is None else table
    if rows:
        pinned = _pinned_session(rows, now)
        if pinned:
            return pinned
        picked = _primary_from_table(rows, now)
        if picked:
            return picked
    return _primary_from_usage(now)


def _combine_sources(sources: List[str]) -> str:
    """provider beats table beats none: a real bill anywhere makes the total real."""
    known = {src for src in sources if src in ("provider", "table", "none")}
    if "provider" in known:
        return "provider"
    if "table" in known:
        return "table"
    return "none"


def _delegate_ids(
    primary_id: str,
    table: Dict[str, Dict[str, Any]],
    now: float,
) -> List[str]:
    """Primary's children plus their descendants, depth capped, cycle-safe, still warm.

    The walk descends from ``primary_id`` only, so a session counts when its
    ``parent_session_id`` chain reaches the primary. A sibling's child, a session under a
    different parent and a row whose parent is missing from the table are all unreachable
    and therefore excluded. Cron and oneshot rows are blocked by source.
    """
    if not primary_id:
        return []

    children: Dict[str, List[str]] = {}
    for sid, rec in table.items():
        parent = rec["parent"]
        if parent:
            children.setdefault(str(parent), []).append(sid)

    found: List[str] = []
    visited = {primary_id}
    frontier = [primary_id]
    for _depth in range(MAX_DELEGATE_DEPTH):
        nxt: List[str] = []
        for pid in frontier:
            for cid in children.get(pid, []):
                if cid in visited:
                    continue
                visited.add(cid)
                rec = table.get(cid) or {}
                if str(rec.get("source") or "") in DELEGATE_SOURCE_BLOCK:
                    continue          # scheduled work, not a task this chat spawned
                found.append(cid)
                nxt.append(cid)
        frontier = nxt
        if not frontier:
            break

    warm: List[str] = []
    for sid in found:
        seen = table[sid]["seen"]
        if seen is not None and now - float(seen) <= DELEGATE_WINDOW_S:
            warm.append(sid)
    return warm


def _delegate_totals(
    primary_id: str,
    table: Optional[Dict[str, Dict[str, Any]]],
    now: float,
) -> Dict[str, Any]:
    """Summed usage of every delegate under the primary, from the per-session columns.

    Cost resolves per row with the same order as the main path: provider actual, provider
    estimate, then ``PRICE_TABLE``. A session whose model has no price adds 0 and pulls
    the source down to ``none`` unless some other row was really billed.
    """
    totals: Dict[str, Any] = dict(DELEGATE_DEFAULTS)
    sources: List[str] = []

    if table:
        for sid in _delegate_ids(primary_id, table, now):
            rec = table[sid]
            tokens_in = int(rec.get("input_tokens") or 0)
            tokens_out = int(rec.get("output_tokens") or 0)
            cache_read = int(rec.get("cache_read_tokens") or 0)
            totals["count"] += 1
            totals["input_tokens"] += tokens_in
            totals["output_tokens"] += tokens_out
            totals["cache_read"] += cache_read
            cost, source = _resolve_cost(
                str(rec.get("model") or ""),
                tokens_in,
                tokens_out,
                cache_read,
                rec.get("actual_cost_usd"),
                rec.get("estimated_cost_usd"),
            )
            totals["cost_usd"] += cost
            sources.append(source)
    else:
        # Old database without a sessions table: keep the id-prefix rule as a last resort.
        try:
            with _db() as conn:
                rows = conn.execute(_DELEGATE_USAGE_SQL, (now - DELEGATE_WINDOW_S,)).fetchall()
        except (sqlite3.Error, OSError) as exc:
            logger.debug("savvy-progress: delegate scan failed: %s", exc)
            rows = []
        except Exception as exc:  # noqa: BLE001 — the card must never break a turn
            logger.debug("savvy-progress: unexpected delegate scan failure: %s", exc)
            rows = []
        for (sid,) in rows:
            if str(sid) == primary_id:
                continue  # the primary is never its own delegate
            usage = _read_totals(str(sid))
            if usage is None:
                continue
            totals["count"] += 1
            totals["input_tokens"] += int(usage["input_tokens"])
            totals["output_tokens"] += int(usage["output_tokens"])
            totals["cache_read"] += int(usage["cache_read"])
            totals["cost_usd"] += float(usage["cost_usd"])
            sources.append(str(usage.get("cost_source") or "none"))

    totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
    totals["cost_source"] = _combine_sources(sources)
    return totals


def _read_prev(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("savvy-progress: unreadable snapshot ignored: %s", exc)
        return None


def _write_snapshot(path: Path, payload: Dict[str, Any]) -> bool:
    """Atomic private write of one JSON object. Returns False instead of raising."""
    return _write_json_atomic(path, payload)


@contextmanager
def _write_lock(path: Path) -> Iterator[bool]:
    """Best-effort cross-process lock for one target file.

    Yields ``True`` when the lock was taken. A second Hermes process (a oneshot run, a
    cron worker) can fire the same hook, and two writers interleaving a read-modify-write
    would drop a session's turn history. The lock is advisory and bounded: on timeout the
    caller skips this write and keeps the previous file, which is always valid.
    """
    if fcntl is None:
        yield True
        return

    lock_path = path.parent / (path.name + ".lock")
    handle = None
    taken = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        deadline = time.time() + WRITE_LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                taken = True
                break
            except OSError:
                if time.time() >= deadline:
                    logger.debug("savvy-progress: write lock busy for %s, skipping", path.name)
                    break
                time.sleep(0.01)
        yield taken
    except OSError as exc:
        logger.debug("savvy-progress: lock unavailable for %s: %s", path.name, exc)
        yield False
    finally:
        if handle is not None:
            try:
                if taken:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(handle)
            except OSError:
                pass


def _write_json_atomic(path: Path, data: Any) -> bool:
    """Write ``data`` to ``path`` atomically and privately. Never raises.

    Layers, outermost first:
      * cross-process lock, so two Hermes processes cannot interleave one file
      * ``.tmp`` beside the target, so ``os.replace`` stays atomic on one filesystem
      * mode 0600 set before the rename, so the visible file is never world-readable
      * ``fsync`` before the rename, so a crash cannot expose a zero-byte file
      * ``allow_nan=False``, so no ``Infinity``/``NaN`` literal ever lands on disk

    A failure leaves the previous file untouched, which the widget keeps polling.
    """
    tmp = None
    try:
        with _write_lock(path) as taken:
            if not taken:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            # mkstemp creates a brand-new file with O_EXCL and mode 0600. A fixed ".tmp"
            # name let a planted symlink redirect a write outside the state directory.
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".",
                                            suffix=".tmp")
            tmp = Path(tmp_name)
            try:
                try:
                    text = json.dumps(data, indent=2, allow_nan=False)
                except (TypeError, ValueError):
                    # A non-finite or exotic object slipped through: write the safe form
                    # instead of corrupting the file the TUI parses strictly.
                    text = json.dumps(_json_safe(data), indent=2, allow_nan=False)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, 0o600)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            os.replace(tmp, path)
            return True
    except Exception as exc:  # noqa: BLE001 — a write must never break a turn
        logger.debug("savvy-progress: write failed for %s: %s", path.name, exc)
        try:
            if tmp is not None:
                os.unlink(tmp)
        except OSError:
            pass
        return False


def _json_safe(value: Any) -> Any:
    """Deep copy with every non-finite float replaced by 0.0. Never raises."""
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _session_file_path(session_id: str) -> Path:
    """``.../savvy-progress.sessions/<safe session id>.json``. The id is sanitised."""
    name = _SAFE_ID.sub("_", str(session_id or ""))[:200] or "unknown"
    return _session_dir() / f"{name}.json"


def _prune_session_files(directory: Path, keep: int = MAX_SESSION_FILES) -> int:
    """Delete all but the ``keep`` most recently written per-session files. Returns the count."""
    try:
        files = [p for p in directory.iterdir() if p.suffix == ".json" and p.is_file()]
    except OSError as exc:
        logger.debug("savvy-progress: session dir unreadable: %s", exc)
        return 0
    if len(files) <= keep:
        return 0
    try:
        files.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    except OSError as exc:
        logger.debug("savvy-progress: session file stat failed: %s", exc)
        return 0
    removed = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError as exc:
            logger.debug("savvy-progress: session file not removed: %s", exc)
    return removed


def _publish_session_file(session_id: str, payload: Dict[str, Any]) -> bool:
    """Write the firing session's own view when it changed, then cap the directory."""
    path = _session_file_path(session_id)
    if path.exists() and _unchanged(_read_prev(path), payload):
        return False
    _write_snapshot(path, payload)
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.debug("savvy-progress: session file mode not set: %s", exc)
    _prune_session_files(path.parent)
    return True


def _read_sessions(path: Optional[Path] = None) -> Dict[str, Any]:
    """The per-session history store. Missing or broken file reads as empty."""
    try:
        with open(path if path is not None else _sessions_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("savvy-progress: unreadable session history ignored: %s", exc)
        return {}

    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return {str(sid): entry for sid, entry in sessions.items() if isinstance(entry, dict)}


def _write_sessions(path: Path, sessions: Dict[str, Any]) -> bool:
    """Atomic, mode-0600 write of the session history. Returns False instead of raising."""
    return _write_json_atomic(path, {"sessions": sessions})


def _prune_sessions(sessions: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Drop entries untouched for longer than the TTL, keep the 8 most recent, 24 turns each."""
    live: List[Tuple[str, Dict[str, Any], float]] = []
    for sid, entry in sessions.items():
        try:
            ts = float(entry.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if now - ts <= SESSION_TTL_S:
            live.append((sid, entry, ts))

    live.sort(key=lambda row: row[2], reverse=True)
    kept: Dict[str, Any] = {}
    for sid, entry, _ts in live[:MAX_SESSIONS]:
        turns = [t for t in (entry.get("turns") or []) if isinstance(t, dict)][-MAX_SESSION_TURNS:]
        kept[sid] = {**entry, "turns": turns}
    return kept


def _entry_totals(totals: Dict[str, Any]) -> Dict[str, Any]:
    """The usage baseline an entry diffs against on the next call."""
    return {
        "api_calls": int(totals["api_calls"]),
        "in": int(totals["input_tokens"]),
        "out": int(totals["output_tokens"]),
        "cache": int(totals["cache_read"]),
        "cache_write": int(totals["cache_write"]),
        "cost": round(float(totals["cost_usd"]), 6),
    }


def _entry_from_snapshot(prev: Dict[str, Any]) -> Dict[str, Any]:
    """Seed a missing entry from a snapshot that already described this session.

    Only used when upgrading from a build without the sessions file, so the counter
    does not jump back to 0 on the first call after the upgrade.
    """
    totals = {
        "api_calls": int(prev.get("api_calls") or 0),
        "in": int(prev.get("input_tokens") or 0),
        "out": int(prev.get("output_tokens") or 0),
        "cache": int(prev.get("cache_read") or 0),
        "cache_write": int(prev.get("cache_write") or 0),
        "cost": float(prev.get("cost_usd") or 0.0),
    }
    return {
        "turn": int(prev.get("turn") or 0),
        "turns": [t for t in (prev.get("turns") or []) if isinstance(t, dict)][-MAX_SESSION_TURNS:],
        "totals": totals,
        "ts": round(time.time(), 3),
    }


def _delta_from_entry(entry: Dict[str, Any], totals: Dict[str, Any]) -> Dict[str, Any]:
    """Per-turn difference against THIS session's stored baseline, never another one's."""
    prev = entry.get("totals") or {}

    def diff(key: str, prev_key: str) -> int:
        return max(0, int(totals[key]) - int(prev.get(prev_key) or 0))

    cost = round(float(totals["cost_usd"]) - float(prev.get("cost") or 0.0), 6)
    return {
        "in": diff("input_tokens", "in"),
        "out": diff("output_tokens", "out"),
        "cache": diff("cache_read", "cache"),
        "cost": max(0.0, cost),
    }


def _cache_hit_pct(cache_read: int, tokens_in: int) -> float:
    denom = tokens_in + cache_read
    if denom <= 0:
        return 0.0
    return round(cache_read / denom * 100.0, 1)


def _delta(prev: Dict[str, Any], totals: Dict[str, Any]) -> Dict[str, Any]:
    def diff(key: str, prev_key: str) -> int:
        return max(0, int(totals[key]) - int(prev.get(prev_key) or 0))

    cost = round(float(totals["cost_usd"]) - float(prev.get("cost_usd") or 0.0), 6)
    return {
        "in": diff("input_tokens", "input_tokens"),
        "out": diff("output_tokens", "output_tokens"),
        "cache": diff("cache_read", "cache_read"),
        "cost": max(0.0, cost),
    }


# --- other-agent spend (codex / pi / dsh) -----------------------------------------
# Hermes' own tables only see Hermes. Spend in a sibling agent sits in that agent's own
# transcript, so this section reads the three local ones directly:
#
#   codex  ~/.codex/sessions/**/*.jsonl             payload.info.total_token_usage (cumulative)
#   pi     ~/.pi/agent/sessions/**/*.jsonl          message.usage per response, plus its cost block
#   dsh    ~/.dsh/sessions/**/session.jsonl.zstd    assistant/chunk usage inside a zstd frame
#
# Token rule: every token the agent pushed through a model, so fresh input + cache read +
# output. A cache read is billed input, not a free token.
# Cost rule: the agent's own reported cost when it reports one (pi does), else
# ``PRICE_TABLE``, else unknown. Unknown never becomes ``$0.00``: it prints ``$n/a`` and
# raises ``unpriced``, so an estimate can never be mistaken for a measurement.
#
# A full scan costs a few hundred milliseconds, so it runs at most once per
# ``AGENTS_CACHE_S`` and the result is kept in memory and on disk. Any failure keeps the
# previous numbers: a broken sibling transcript must never break the card.
AGENTS_CACHE_NAME = "savvy-progress.agents.json"
AGENTS_CACHE_S = 60.0
AGENTS_MAX_FILES = 4000
AGENT_MODEL_ROWS = 3
AGENTS_OFF_ENV = "SAVVY_AGENTS_OFF"
# Bounds. A transcript is untrusted input: another agent wrote it, and a corrupt or hostile
# one must not be able to stall a tool call or exhaust this process's memory.
AGENT_MAX_FILE_BYTES = 64 * 1024 * 1024      # skip a file bigger than this outright
AGENT_MAX_LINE_BYTES = 8 * 1024 * 1024       # one JSONL line; longer is corrupt
AGENT_MAX_DECOMPRESSED = 256 * 1024 * 1024   # dsh cap; a zstd bomb stops here
AGENTS_SCAN_BUDGET_S = 20.0                  # whole-scan wall clock
AGENT_MAX_TOKENS = 10 ** 12                  # a single count above this is invalid
AGENT_PAYLOAD_MODELS = 8                     # models published into the widget snapshot
# (agent name, env override for its sessions root, default path under $HOME, file glob)
AGENT_SOURCES: Tuple[Tuple[str, str, str, str], ...] = (
    ("codex", "SAVVY_CODEX_DIR", ".codex/sessions", "*.jsonl"),
    ("pi", "SAVVY_PI_DIR", ".pi/agent/sessions", "*.jsonl"),
    ("dsh", "SAVVY_DSH_DIR", ".dsh/sessions", "session.jsonl.zstd"),
)
_MODEL_KEY_RE = re.compile(r'"model"\s*:\s*"([^"\\]{1,80})"')
_ZSTD_BIN = shutil.which("zstd")
_others_cache: Dict[str, Any] = {"ts": 0.0, "value": None}
_others_lock = threading.Lock()
_others_refreshing = False

# --- row toggles ------------------------------------------------------------------
# Every row the card can draw, keyed by the name the widget uses. A hidden row is not
# drawn at all: no blank line, no zero, no header. The list lives in
# ``state/savvy-progress.rows.json`` so one edit or one ``/svprogress hide`` keeps working
# across restarts, and the payload carries it to the widget.
ROWS_NAME = "savvy-progress.rows.json"
ROW_KEYS: Tuple[str, ...] = (
    "model", "api", "tokens", "cache", "ctx", "cost", "burn", "saved", "today", "daytok",
    "codex", "pi", "dsh", "others", "agents",
)
# Spoken aliases: the slash command accepts the label the user reads on the card.
ROW_ALIASES: Dict[str, str] = {
    "in": "tokens",
    "out": "tokens",
    "input": "tokens",
    "output": "tokens",
    "tokens/day": "daytok",
    "tok/day": "daytok",
    "day": "today",
    "context": "ctx",
    "delegates": "agents",
    "delegate": "agents",
    "other": "others",
    "side": "others",
    "ext": "others",
    "cache_saved": "saved",
    "calls": "api",
    "api calls": "api",
    "burn/hour": "burn",
}
_rows_cache: Dict[str, Any] = {"key": None, "hidden": ()}


def _rows_path() -> Path:
    return _hermes_home() / "state" / ROWS_NAME


def _normalize_row(name: Any) -> Optional[str]:
    """One spoken or written row name to its row key, or None when it is not a row."""
    text = str(name or "").strip().lower().lstrip("-")
    if not text:
        return None
    text = ROW_ALIASES.get(text, text)
    return text if text in ROW_KEYS else None


def _rows_stamp() -> Any:
    try:
        stat = _rows_path().stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _hidden_rows() -> List[str]:
    """Hidden row keys, in ROW_KEYS order. Missing or unreadable config hides nothing."""
    stamp = _rows_stamp()
    if _rows_cache.get("key") == stamp:
        return list(_rows_cache.get("hidden") or ())

    hidden: set = set()
    if stamp is not None:
        try:
            raw = json.loads(_rows_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = None
        if isinstance(raw, dict):
            listed = raw.get("hidden")
            if isinstance(listed, list):
                for name in listed:
                    key = _normalize_row(name)
                    if key:
                        hidden.add(key)
            elif isinstance(listed, str):
                key = _normalize_row(listed)
                if key:
                    hidden.add(key)

    ordered = [key for key in ROW_KEYS if key in hidden]
    _rows_cache["key"] = stamp
    _rows_cache["hidden"] = tuple(ordered)
    return ordered


def _write_hidden_rows(hidden: List[str]) -> bool:
    """Persist the hidden set. Returns False when the file could not be written."""
    ordered = [key for key in ROW_KEYS if key in set(hidden)]
    ok = _write_json_atomic(_rows_path(), {"hidden": ordered})
    _rows_cache["key"] = None  # force a re-read against the new stamp
    return ok



def _toggle_rows(names: List[str], hide: bool) -> Optional[str]:
    """``/svprogress hide|show <names>``. Returns a message, or None on a bad name.

    The read-modify-write runs under its own lock, so two processes hiding different rows
    cannot drop each other's change. A failed write is reported, never claimed as done.
    """
    targets = []
    for name in names:
        key = _normalize_row(name)
        if key is None:
            return f"savvy-progress: unknown row '{name}'. Rows: " + ", ".join(ROW_KEYS)
        targets.append(key)

    path = _rows_path()
    with _write_lock(path.parent / (path.name + ".update")) as taken:
        if not taken:
            return "savvy-progress: rows file is busy, try again"
        _rows_cache["key"] = None
        current = set(_hidden_rows())
        if hide:
            current.update(targets)
        else:
            current.difference_update(targets)
        written = _write_hidden_rows(list(current))

    if not written:
        return "savvy-progress: rows not saved (state directory read-only?)"

    ordered = [key for key in ROW_KEYS if key in current]
    _republish(hidden_rows=ordered)
    state = "hidden" if hide else "shown"
    return f"savvy-progress: {state} {', '.join(targets)}"


def _rows_summary() -> str:
    hidden = set(_hidden_rows())
    shown = [key for key in ROW_KEYS if key not in hidden]
    parts = [f"savvy-progress rows  shown: {', '.join(shown) or 'none'}"]
    if hidden:
        parts.append(f"hidden: {', '.join(k for k in ROW_KEYS if k in hidden)}")
    parts.append("toggle: /svprogress hide <row>…  |  show <row>…  |  reset")
    return "\n  ".join(parts)


def _blank_agent(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "tokens": 0,
        "input": 0,
        "cache_read": 0,
        "output": 0,
        "cost_usd": 0.0,
        "cost_source": "none",
        "unpriced": False,
        "sessions": 0,
        "models": [],
        "models_total": 0,
        "available": False,
        "truncated": False,
    }


def _agent_model() -> Dict[str, Any]:
    return {"tokens": 0, "input": 0, "cache_read": 0, "output": 0,
            "cost_usd": 0.0, "cost_source": "none", "calls": 0, "unpriced": False}


def _agent_cost(model: str, fresh_in: int, cache_read: int, tokens_out: int,
                reported: float = 0.0) -> Tuple[float, str]:
    """``(cost_usd, cost_source)`` for one agent model.

    ``reported`` wins when the agent billed something itself. Otherwise the shared price
    table is used. A cache-write token has no rate in that table, so callers pass it as
    fresh input: the estimate stays conservative and, unlike an invented rate, checkable.
    """
    if reported > 0.0:
        # pi computes this from a rate table. It is an estimate, not a bill, so it keeps
        # its own source and is drawn with the estimate mark.
        return reported, "agent"

    price = _match_price(model) if model else None
    if price is None:
        return 0.0, "none"

    price_in, price_out, price_cache = price
    cost = (
        (fresh_in / 1_000_000.0) * price_in
        + (cache_read / 1_000_000.0) * price_cache
        + (tokens_out / 1_000_000.0) * price_out
    )
    return cost, "table"


def _combine_cost_sources(sources: Any) -> str:
    """One source for a mixed set of rows. Any estimate makes the total an estimate.

    ``provider`` stays reserved for a number the provider actually billed. ``table`` and
    ``agent`` both mean "computed from rates", so a mixed total is an estimate.
    """
    seen = {str(s) for s in sources if str(s) in ("provider", "table", "agent")}
    if "table" in seen:
        return "table"
    if "agent" in seen:
        return "agent"
    if "provider" in seen:
        return "provider"
    return "none"


def _iter_lines(path: Path) -> Iterator[str]:
    """Lines of a transcript. An oversized line is corrupt or hostile, so it is skipped."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            if len(line) > AGENT_MAX_LINE_BYTES:
                continue
            yield line


def _add_codex_usage(models: Dict[str, Dict[str, Any]], model: str,
                      usage: Dict[str, Any]) -> bool:
    """Add one response of codex usage to ``model``. False when the row is not plausible."""
    gross = _int_value(usage.get("input_tokens"))
    cached = _int_value(usage.get("cached_input_tokens"))
    tokens_out = _int_value(usage.get("output_tokens"))
    total = _int_value(usage.get("total_tokens"))

    if min(gross, cached, tokens_out, total) < 0:
        return False
    if max(gross, cached, tokens_out, total) > AGENT_MAX_TOKENS:
        return False

    cached = min(gross, cached)
    fresh_in = gross - cached
    cost, source = _agent_cost(model, fresh_in, cached, tokens_out)

    row = models.setdefault(model or "unknown", _agent_model())
    row["tokens"] += total or (gross + tokens_out)
    row["input"] += fresh_in
    row["cache_read"] += cached
    row["output"] += tokens_out
    row["cost_usd"] += cost
    row["calls"] += 1
    row["cost_source"] = _combine_cost_sources([row["cost_source"], source])
    row["unpriced"] = bool(row["unpriced"]) or source == "none"
    return True


def _sum_models(models: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """One tally from a ``{model: row}`` map: the totals for that file."""
    tally = _agent_model()
    for row in models.values():
        for key in ("tokens", "input", "cache_read", "output", "calls"):
            tally[key] += row[key]
        tally["cost_usd"] += row["cost_usd"]
        tally["cost_source"] = _combine_cost_sources([tally["cost_source"], row["cost_source"]])
        tally["unpriced"] = bool(tally["unpriced"]) or bool(row["unpriced"])
    return tally


def _scan_codex_file(path: Path) -> Optional[Dict[str, Any]]:
    """One codex rollout, attributed per response.

    ``token_count`` events carry ``last_token_usage``: the usage of that one response, with
    the model active at that moment. Summing those keeps a model switch from moving earlier
    spend onto the last model. A rollout without the delta (an old build) falls back to the
    cumulative total on the last model.
    """
    models: Dict[str, Dict[str, Any]] = {}
    current = ""
    cumulative: Optional[Dict[str, Any]] = None
    deltas = False

    try:
        for line in _iter_lines(path):
            if '"token_count"' in line:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                info = payload.get("info") if isinstance(payload, dict) else None
                if not isinstance(info, dict):
                    continue
                total = info.get("total_token_usage")
                if isinstance(total, dict):
                    cumulative = total
                last = info.get("last_token_usage")
                if isinstance(last, dict) and _add_codex_usage(models, current, last):
                    deltas = True
            elif '"model"' in line:
                match = _MODEL_KEY_RE.search(line)
                if match:
                    current = match.group(1).strip()
    except OSError:
        return None

    if not deltas:
        if not isinstance(cumulative, dict):
            return None
        if not _add_codex_usage(models, current, cumulative):
            return None

    if not models:
        return None
    return {"tally": _sum_models(models), "models": models}


def _scan_pi_file(path: Path) -> Optional[Dict[str, Any]]:
    """One pi session log. Every assistant message carries its own usage and cost."""
    models: Dict[str, Dict[str, Any]] = {}
    try:
        for line in _iter_lines(path):
            if '"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(message, dict) or not isinstance(usage, dict):
                continue

            outer = entry if isinstance(entry, dict) else {}
            model = str(message.get("model") or outer.get("model") or "unknown")
            fresh_in = _int_value(usage.get("input"))
            cache_read = _int_value(usage.get("cacheRead"))
            cache_write = _int_value(usage.get("cacheWrite"))
            tokens_out = _int_value(usage.get("output"))

            if min(fresh_in, cache_read, cache_write, tokens_out) < 0:
                continue
            if max(fresh_in, cache_read, cache_write, tokens_out) > AGENT_MAX_TOKENS:
                continue

            # No cache-write rate exists in the table, so a write is priced as input.
            fresh_in += cache_write
            reported = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
            cost, source = _agent_cost(model, fresh_in, cache_read, tokens_out,
                                       _finite(reported.get("total")))

            row = models.setdefault(model, _agent_model())
            row["tokens"] += fresh_in + cache_read + tokens_out
            row["input"] += fresh_in
            row["cache_read"] += cache_read
            row["output"] += tokens_out
            row["cost_usd"] += cost
            row["calls"] += 1
            row["cost_source"] = _combine_cost_sources([row["cost_source"], source])
            row["unpriced"] = bool(row["unpriced"]) or source == "none"
    except OSError:
        return None

    if not models:
        return None
    return {"tally": _sum_models(models), "models": models}


def _scan_dsh_file(path: Path) -> Optional[Dict[str, Any]]:
    """One dsh session, decompressed with the ``zstd`` binary.

    The usage chunk carries input, output and cache-read tokens but no cost, so dsh cost is
    always a table estimate. Output is read with a hard byte ceiling: a compression bomb
    stops at the cap instead of filling memory. No ``zstd`` binary means no dsh numbers.
    """
    if not _ZSTD_BIN:
        return None

    try:
        with subprocess.Popen([_ZSTD_BIN, "-dc", str(path)], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            raw = proc.stdout.read(AGENT_MAX_DECOMPRESSED + 1) if proc.stdout else b""
            proc.kill()
    except (OSError, subprocess.SubprocessError):
        return None
    if len(raw) > AGENT_MAX_DECOMPRESSED:
        return None

    models: Dict[str, Dict[str, Any]] = {}
    model = ""
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        if '"source"' in line and '"model"' in line:
            match = _MODEL_KEY_RE.search(line)
            if match:
                model = match.group(1).strip()
            continue
        if '"usage"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        chunk = (event.get("data") or {}).get("chunk") if isinstance(event, dict) else None
        if not isinstance(chunk, dict) or chunk.get("type") != "usage":
            continue
        usage = chunk.get("usage") or {}
        if not isinstance(usage, dict):
            continue

        fresh_in = _int_value(usage.get("inputTokens"))
        cache_read = _int_value(usage.get("cacheReadTokens"))
        tokens_out = _int_value(usage.get("outputTokens"))
        total = _int_value(usage.get("totalTokens"))

        if min(fresh_in, cache_read, tokens_out, total) < 0:
            continue
        if max(fresh_in, cache_read, tokens_out, total) > AGENT_MAX_TOKENS:
            continue

        cost, source = _agent_cost(model, fresh_in, cache_read, tokens_out)

        row = models.setdefault(model or "unknown", _agent_model())
        row["tokens"] += total or (fresh_in + cache_read + tokens_out)
        row["input"] += fresh_in
        row["cache_read"] += cache_read
        row["output"] += tokens_out
        row["cost_usd"] += cost
        row["calls"] += 1
        row["cost_source"] = _combine_cost_sources([row["cost_source"], source])
        row["unpriced"] = bool(row["unpriced"]) or source == "none"

    if not models:
        return None
    return {"tally": _sum_models(models), "models": models}


_SCANNERS = {"codex": _scan_codex_file, "pi": _scan_pi_file, "dsh": _scan_dsh_file}


def _agent_root(env_name: str, rel: str) -> Path:
    override = os.environ.get(env_name, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(os.path.expanduser("~")) / rel


def _agent_files(root: Path, glob: str) -> Tuple[List[Path], bool]:
    """Newest first, regular files only, capped. Returns ``(files, truncated)``.

    A symlink, FIFO or device under a session root is skipped: a FIFO would block the scan
    forever and a symlink can point at anything this user can read. Hard links to the same
    inode are counted once, so a linked transcript cannot double the spend.
    """
    if not root.is_dir():
        return [], False

    found: List[Tuple[float, Path]] = []
    seen: set = set()
    try:
        for path in root.rglob(glob):
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > AGENT_MAX_FILE_BYTES:
                continue
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            found.append((info.st_mtime, path))
    except OSError:
        return [], False

    found.sort(key=lambda item: item[0], reverse=True)
    truncated = len(found) > AGENTS_MAX_FILES
    return [path for _mtime, path in found[:AGENTS_MAX_FILES]], truncated


def _scan_agent(name: str, env_name: str, rel: str, glob: str,
                deadline: Optional[float] = None,
                previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Totals for one agent. Never raises: a bad tree yields an empty, honest entry.

    ``deadline`` bounds the whole scan. When it passes, the agent is marked truncated and
    the numbers cover only the files read so far. ``previous`` keeps the last good numbers
    when a tree that read fine reads empty now, so a transient permission error cannot wipe
    the card.
    """
    entry = _blank_agent(name)
    scanner = _SCANNERS.get(name)
    files, truncated = _agent_files(_agent_root(env_name, rel), glob)
    entry["truncated"] = truncated
    if not scanner or not files or (name == "dsh" and not _ZSTD_BIN):
        return entry

    entry["available"] = True
    models: Dict[str, Dict[str, Any]] = {}
    for path in files:
        if deadline is not None and time.time() > deadline:
            entry["truncated"] = True
            break
        try:
            parsed = scanner(path)
        except Exception as exc:  # noqa: BLE001 — one bad transcript is not the whole agent
            logger.debug("savvy-progress: %s file skipped (%s): %s", name, path, exc)
            continue
        if not parsed:
            continue
        tally = parsed["tally"]
        entry["sessions"] += 1
        for key in ("tokens", "input", "cache_read", "output"):
            entry[key] += tally[key]
        entry["cost_usd"] += tally["cost_usd"]
        for model, row in parsed["models"].items():
            merged = models.setdefault(model, _agent_model())
            for key in ("tokens", "input", "cache_read", "output", "calls"):
                merged[key] += row[key]
            merged["cost_usd"] += row["cost_usd"]
            merged["cost_source"] = _combine_cost_sources(
                [merged["cost_source"], row["cost_source"]]
            )
            merged["unpriced"] = bool(merged["unpriced"]) or bool(row["unpriced"])

    if not models and not entry["sessions"] and isinstance(previous, dict) and previous.get("tokens"):
        # Same tree, previously readable, nothing readable now: keep the last good numbers.
        kept = dict(previous)
        kept["truncated"] = True
        return kept

    entry["cost_usd"] = round(entry["cost_usd"], 6)
    entry["unpriced"] = any(bool(row["unpriced"]) for row in models.values())
    entry["cost_source"] = _combine_cost_sources(
        row["cost_source"] for row in models.values()
    )
    ordered = sorted(models.items(), key=lambda kv: -kv[1]["tokens"])
    entry["models_total"] = len(ordered)
    # The widget snapshot carries only the models it can show. Full detail stays in the
    # agents cache, so a 40-model history cannot bloat every poll.
    entry["models"] = [
        {"model": model, **row} for model, row in ordered[:AGENT_PAYLOAD_MODELS]
    ]
    return entry


def _others_payload(agents: List[Dict[str, Any]], now: float) -> Dict[str, Any]:
    total_cost = round(sum(_finite(a.get("cost_usd")) for a in agents), 6)
    return {
        "ts": round(now, 3),
        "agents": agents,
        "tokens": sum(max(0, _int_value(a.get("tokens"))) for a in agents),
        "cost_usd": total_cost,
        "cost_source": _combine_cost_sources(a.get("cost_source") for a in agents),
        "unpriced": any(bool(a.get("unpriced")) for a in agents),
    }


def _scan_others(now: float) -> Dict[str, Any]:
    deadline = now + AGENTS_SCAN_BUDGET_S
    previous = {}
    cached = _others_cache.get("value")
    if isinstance(cached, dict) and isinstance(cached.get("agents"), list):
        previous = {a.get("name"): a for a in cached["agents"] if isinstance(a, dict)}
    return _others_payload(
        [_scan_agent(name, env_name, rel, glob, deadline=deadline,
                     previous=previous.get(name))
         for name, env_name, rel, glob in AGENT_SOURCES],
        now,
    )


def _others_path() -> Path:
    return _hermes_home() / "state" / AGENTS_CACHE_NAME


_OTHERS_FIELDS = ("ts", "agents", "tokens", "cost_usd", "cost_source", "unpriced")


def _valid_others_payload(value: Any) -> bool:
    """True when ``value`` has every field the snapshot and the card read from it.

    A cache with a missing key used to pass the age check and then abort snapshot
    construction, which silently froze the whole card. Validate the shape instead.
    """
    if not isinstance(value, dict):
        return False
    if any(field not in value for field in _OTHERS_FIELDS):
        return False
    if not isinstance(value["agents"], list):
        return False
    tokens = value["tokens"]
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        return False
    if not isinstance(value["cost_usd"], (int, float)) or isinstance(value["cost_usd"], bool):
        return False
    if value["cost_source"] not in ("provider", "table", "agent", "none"):
        return False
    if not isinstance(value["unpriced"], bool):
        return False
    return True


def _read_others_cache(now: float) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(_others_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not _valid_others_payload(raw):
        return None
    # The payload's own timestamp decides freshness. Re-stamping on load let a 118-second
    # old cache pass a 60-second window.
    age = now - _finite(raw.get("ts"))
    if not (0.0 <= age < AGENTS_CACHE_S):
        return None
    return raw


def _others_totals(now: Optional[float] = None, block: bool = False) -> Dict[str, Any]:
    """Cross-agent totals.

    ``block=False`` is the hook path: it never waits for a scan, returns the last known
    numbers and lets one background thread refresh them. ``block=True`` is the slash path,
    where the user waits for a printout. Both paths share one flight guard, so a command
    cannot start a second scan beside the running one.

    ``SAVVY_AGENTS_OFF=1`` disables the scan entirely.
    """
    global _others_refreshing

    now = time.time() if now is None else now
    off = os.environ.get(AGENTS_OFF_ENV, "").strip().lower()
    if off not in ("", "0", "false", "no"):
        return _others_payload([_blank_agent(name) for name, *_rest in AGENT_SOURCES], now)

    cached = _others_cache.get("value")
    if isinstance(cached, dict):
        age = now - _finite(cached.get("ts"))
        if 0.0 <= age < AGENTS_CACHE_S:
            return cached

    value = _read_others_cache(now)
    if value is not None:
        _others_cache["value"] = value
        return value

    if block:
        with _others_lock:
            waiting = _others_refreshing
            if not waiting:
                _others_refreshing = True
        if waiting:
            deadline = time.time() + AGENTS_SCAN_BUDGET_S
            while time.time() < deadline:
                with _others_lock:
                    if not _others_refreshing:
                        break
                time.sleep(0.02)
            return _stale_others(now)
        try:
            scanned = _refresh_others(now)
        finally:
            with _others_lock:
                _others_refreshing = False
        return scanned if scanned is not None else _stale_others(now)

    with _others_lock:
        if _others_refreshing:
            return _stale_others(now)
        _others_refreshing = True

    thread = threading.Thread(target=_background_scan, daemon=True,
                              name="savvy-progress-agents")
    thread.start()
    return _stale_others(now)


def _stale_others(now: float) -> Dict[str, Any]:
    """The last known cross-agent numbers, or zeros when nothing has been read yet."""
    cached = _others_cache.get("value")
    if isinstance(cached, dict):
        return cached
    return _others_payload([_blank_agent(name) for name, *_rest in AGENT_SOURCES], now)


_last_payload: Optional[Dict[str, Any]] = None


def _republish(**changes: Any) -> bool:
    """Rewrite the snapshot with a few replaced fields, so the widget sees them at once.

    The card keeps its own ``ts``: this is a content refresh, not a new usage sample, and a
    fresh timestamp over an old session would hide staleness behind live sibling numbers.
    """
    payload = _last_payload
    if not isinstance(payload, dict) or not payload:
        return False
    merged = dict(payload)
    merged.update(changes)
    return _write_json_atomic(_snapshot_path(), merged)


def _republish_others(value: Dict[str, Any]) -> bool:
    """Push one finished scan into the snapshot without waiting for another hook."""
    return _republish(
        others=value.get("agents") or [],
        others_tokens=_int_value(value.get("tokens")),
        others_cost_usd=_finite(value.get("cost_usd")),
        others_source=value.get("cost_source") or "none",
        others_unpriced=bool(value.get("unpriced")),
        others_ts=_finite(value.get("ts")),
    )


def _refresh_others(now: float) -> Optional[Dict[str, Any]]:
    """Scan, cache and persist. Returns the payload, or None when the scan itself failed."""
    try:
        value = _scan_others(now)
    except Exception as exc:  # noqa: BLE001 — a hook must never break the turn
        logger.debug("savvy-progress: cross-agent scan failed: %s", exc)
        return None

    _others_cache["ts"] = now
    _others_cache["value"] = value
    try:
        _write_json_atomic(_others_path(), value)
    except Exception as exc:  # noqa: BLE001 — a cache write is best effort
        logger.debug("savvy-progress: cross-agent cache not written: %s", exc)
    return value


def _background_scan() -> None:
    global _others_refreshing

    try:
        value = _refresh_others(time.time())
        if isinstance(value, dict):
            _republish_others(value)
    finally:
        with _others_lock:
            _others_refreshing = False


def _build_snapshot(
    session_id: str,
    prev: Optional[Dict[str, Any]],
    entry: Optional[Dict[str, Any]],
    sessions: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
    table: Optional[Dict[str, Dict[str, Any]]] = None,
    self_view: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(snapshot payload, updated session entry)`` for the firing session.

    ``prev`` is only used to seed a missing entry on the first run after an upgrade.
    ``entry`` is the firing session's own history, so the turn count never depends on
    which session happened to write the shared snapshot last.

    The payload describes the PRIMARY session, not the firing one: a delegate that
    runs cannot take the model, tokens, cache, cost or turns fields. Its own history
    entry is still recorded, so the per-session file keeps its old semantics.

    ``self_view`` builds the payload for the firing session itself, with delegates scoped
    to it. That is what the per-session file holds, so each chat counts only its own work.
    """
    now = time.time() if now is None else now
    if table is None:
        table = _read_session_table()
    rows = table or {}

    totals = _read_totals(session_id)
    if totals is None:
        # A brand-new session has its ``sessions`` row seconds before the first usage row,
        # so a missing usage row is "zero for this session", never "use the last session".
        if session_id not in rows:
            return None, None
        totals = _zero_totals()

    if entry is None and prev and prev.get("session_id") == session_id:
        entry = _entry_from_snapshot(prev)

    turns: List[Dict[str, Any]] = []
    turn = 0
    if entry:
        turn = int(entry.get("turn") or 0)
        turns = [t for t in (entry.get("turns") or []) if isinstance(t, dict)][-MAX_TURNS:]
        step = _delta_from_entry(entry, totals)
        if any(step[k] for k in ("in", "out", "cache", "cost")):
            turn += 1
            step["i"] = turn
            turns = (turns + [step])[-MAX_TURNS:]

    primary_id = session_id if self_view else (_primary_session_id(now, table) or session_id)

    if primary_id == session_id:
        primary = totals
        primary_turn = turn
        primary_turns = turns
    else:
        primary = _read_totals(primary_id)
        if primary is None and primary_id not in rows:
            primary_id = session_id
            primary = totals
            primary_turn = turn
            primary_turns = turns
        else:
            if primary is None:
                # The card's own session has no usage row yet: zeros for it, and none of
                # the firing session's numbers.
                primary = _zero_totals()
            stored = (sessions or {}).get(primary_id) or {}
            primary_turn = int(stored.get("turn") or 0)
            primary_turns = [
                t for t in (stored.get("turns") or []) if isinstance(t, dict)
            ][-MAX_TURNS:]

    model, provider = _read_model(primary_id)
    if model == "unknown":
        # No usage row yet: the session row still names the model.
        candidate = str((rows.get(primary_id) or {}).get("model") or "").strip()
        if candidate:
            model = candidate
    delegates = _delegate_totals(primary_id, table, now)
    total_sources = [primary.get("cost_source") or "none"]
    if delegates["count"]:
        total_sources.append(delegates["cost_source"])
    total_cost = round(float(primary["cost_usd"]) + float(delegates["cost_usd"]), 6)

    cost_source = _combine_sources(total_sources)
    day_start, day_end = _day_bounds(now)
    day = _day_spend(now)
    burn_usd_per_hour, _elapsed_s = _burn_rate(total_cost, _read_session_start(primary_id), now)
    cache_saved, cache_saved_source = _cache_saved_usd(model, primary["cache_read"])
    # Cross-agent spend is cached for a minute; the call is cheap on every other hook.
    others = _others_totals(now)

    payload = {
        "ts": round(now, 3),
        "session_id": primary_id,
        "model": model,
        "provider": provider,
        "turn": primary_turn,
        "api_calls": primary["api_calls"],
        "input_tokens": primary["input_tokens"],
        "output_tokens": primary["output_tokens"],
        "cache_read": primary["cache_read"],
        "cache_write": primary["cache_write"],
        "cache_hit_pct": _cache_hit_pct(primary["cache_read"], primary["input_tokens"]),
        "cost_usd": primary["cost_usd"],
        # Describes the cost the card shows. With no delegates that is the primary's own
        # source, so a single-session run is byte-identical to the old contract.
        "cost_source": cost_source,
        "turns": primary_turns,
        "primary_session": primary_id,
        "delegates": delegates,
        "total_cost_usd": _finite(total_cost),
        "context_tokens": _read_context_tokens(primary_id),
        "context_window": _context_window(),
        # Burn is the total cost over the session's own wall clock. It follows cost_source:
        # a provider number burns a real rate, a table number burns an estimate.
        "burn_usd_per_hour": _finite(burn_usd_per_hour),
        "burn_source": cost_source,
        # Saved is always a table number: it compares the cache rate against list input
        # price, which no provider reports.
        "cache_saved_usd": _finite(cache_saved),
        "cache_saved_source": cache_saved_source,
        # Day spend sees the sessions the card cannot: cron, delegates, other terminals.
        "day_cost_usd": _finite(day["cost_usd"]),
        "day_source": day["cost_source"],
        "day_sessions": day["sessions"],
        "day_window_s": round(day_end - day_start, 1),
        # Day token total: input + cache read + cache write + output, all sessions, all models.
        "day_tokens": day["tokens"],
        "day_input_tokens": day["input_tokens"],
        "day_output_tokens": day["output_tokens"],
        "day_cache_read": day["cache_read"],
        "day_cache_write": day["cache_write"],
        # Spend outside Hermes: codex, pi and dsh read from their own transcripts. Zeros
        # when a tree is missing, never an error, and `others_unpriced` marks a total that
        # leaves an unpriced model out of its cost.
        "others": others["agents"],
        "others_tokens": others["tokens"],
        "others_cost_usd": others["cost_usd"],
        "others_source": others["cost_source"],
        "others_unpriced": others["unpriced"],
        "others_ts": others["ts"],
        # Rows the user switched off. The widget drops them; the card skips their lines.
        "hidden_rows": _hidden_rows(),
    }
    global _last_payload
    _last_payload = payload

    new_entry = {
        "turn": turn,
        "turns": turns,
        "totals": _entry_totals(totals),
        "ts": payload["ts"],
    }
    return payload, new_entry


def _unchanged(prev: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> bool:
    if not prev:
        return False
    # ``ts`` and ``others_ts`` are freshness stamps, not content: a scan timestamp that
    # moves on its own must not rewrite the file the widget polls.
    keys = [k for k in payload if k not in ("ts", "others_ts")]
    return all(prev.get(k) == payload.get(k) for k in keys)


def _entry_same(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> bool:
    """True when a session entry would not change apart from its timestamp."""
    if not isinstance(old, dict):
        return False
    return (
        int(old.get("turn") or 0) == int(new.get("turn") or 0)
        and (old.get("turns") or []) == new.get("turns")
        and (old.get("totals") or {}) == new.get("totals")
    )


def snapshot(session_id: str) -> Optional[Dict[str, Any]]:
    """Write the snapshot when it changed and the throttle allows it. Returns the payload.

    The snapshot always describes the session that fired last. Turn history is read from
    and written back to the per-session file, so two sessions cannot clobber each other.
    """
    global _last_write

    if not session_id:
        return None

    with _lock:
        now = time.time()
        if now - _last_write < MIN_WRITE_INTERVAL_S:
            return None

        try:
            return _snapshot_locked(session_id, now)
        except Exception as exc:  # noqa: BLE001 — a hook must never break the turn
            logger.debug("savvy-progress: snapshot aborted, previous file kept: %s", exc)
            return None


def _snapshot_locked(session_id: str, now: float) -> Optional[Dict[str, Any]]:
    """The body of :func:`snapshot`, under the write lock. May raise; the caller guards."""
    global _last_write

    path = _snapshot_path()
    sessions_path = _sessions_path()
    prev = _read_prev(path)
    stored = _read_sessions(sessions_path)
    sessions = _prune_sessions(stored, now)
    table = _read_session_table()

    payload, entry = _build_snapshot(
        session_id, prev, sessions.get(session_id), sessions, now, table
    )
    if payload is None or entry is None:
        return None

    sessions[session_id] = entry
    sessions = _prune_sessions(sessions, now)

    # The firing session's own view: its numbers and only the delegates it spawned.
    # Every session gets one, so two chats never share a counter.
    try:
        own_payload, _own_entry = _build_snapshot(
            session_id, None, entry, sessions, now, table, self_view=True
        )
        if own_payload is not None:
            _publish_session_file(session_id, own_payload)
    except Exception as exc:
        logger.debug("savvy-progress: per-session view failed: %s", exc)

    snapshot_changed = not (_unchanged(prev, payload) and path.exists())
    sessions_changed = not _entry_same(stored.get(session_id), entry) or not sessions_path.exists()

    if not snapshot_changed and not sessions_changed:
        _last_write = now
        return payload

    # A failed write keeps the previous file: the widget keeps polling a valid card,
    # and the next hook retries. Never let an EACCES or a full disk surface as a turn error.
    if sessions_changed and not _write_sessions(sessions_path, sessions):
        logger.debug("savvy-progress: session history write skipped, previous file kept")
    if snapshot_changed and not _write_snapshot(path, payload):
        logger.debug("savvy-progress: snapshot write skipped, previous file kept")
    _last_write = now
    return payload


def _on_post_tool_call(**kwargs: Any) -> None:
    """``post_tool_call`` observer. Never raises."""
    try:
        session_id = str(kwargs.get("session_id") or "")
        # Pin before any usage read: a brand-new chat owns the card from its first tool.
        _pin_session(session_id)
        snapshot(session_id)
    except Exception as exc:  # pragma: no cover — fail-open by contract
        logger.debug("savvy-progress: snapshot failed: %s", exc)


def _on_post_api_request(**kwargs: Any) -> None:
    """``post_api_request`` observer: keeps the panel live during thinking and streaming.

    Only ``session_id`` is read (turn_response_intake.py:81). Totals still come from the DB,
    never from the hook payload.

    Two steps, in this order:
      1. pin the firing session when it is a parentless ``tui`` session. This runs before
         any usage lookup, so the very first API call of a new chat makes that chat the
         card, even though its usage row lands seconds later.
      2. skip a totals regression, so a zero-token row still being filled in can never
         overwrite real data. A session with no usage row is not a regression: the
         snapshot writes its own zeros.

    Never raises, holds no lock of its own, and stays inside ``snapshot``'s 1 s throttle.
    """
    try:
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return

        _pin_session(session_id)

        baseline = (_read_sessions(_sessions_path()).get(session_id) or {}).get("totals") or {}
        if baseline:
            totals = _read_totals(session_id)
            if totals is not None:
                tokens = int(totals["input_tokens"]) + int(totals["output_tokens"])
                was = int(baseline.get("in") or 0) + int(baseline.get("out") or 0)
                if int(totals["api_calls"]) < int(baseline.get("api_calls") or 0) or tokens < was:
                    return

        snapshot(session_id)
    except Exception as exc:  # pragma: no cover — fail-open by contract
        logger.debug("savvy-progress: post_api_request snapshot failed: %s", exc)


def _thousands(value: Any) -> str:
    """12345 -> '12,345'. Never raises on odd payloads."""
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError, OverflowError):
        return "0"


def _cost_text(cost: Any, source: str) -> Tuple[str, str]:
    """``(value, suffix)`` for the cost field.

    provider -> '$0.0355' with no suffix, table -> '~$0.0355' plus 'list price',
    none -> '$n/a' with no suffix.
    """
    try:
        amount = max(0.0, float(cost or 0.0))
    except (TypeError, ValueError):
        amount = 0.0

    if source == "none":
        return "$n/a", ""
    body = f"${amount:.4f}"
    if source == "table":
        return f"~{body}", "list price"
    if source == "agent":
        # The agent computed this from its own rate table. That is an estimate, not a bill.
        return f"~{body}", "agent estimate"
    return body, ""


def _rate_text(cost: Any, source: str) -> str:
    """``$0.55/h`` for the burn row. ``table`` marks an estimate, ``none`` prints n/a."""
    if source == "none":
        return "$n/a/h"
    try:
        amount = max(0.0, float(cost or 0.0))
    except (TypeError, ValueError):
        amount = 0.0
    body = f"${amount:.2f}/h"
    return f"~{body}" if source in ("table", "agent") else body


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{int(max(0, seconds))}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


def _header(model: Any, provider: Any) -> str:
    """One line: PROGRESS, the model tail, the provider. Always under CARD_W."""
    tail = (str(model or "unknown").split("/") or ["unknown"])[-1] or "unknown"
    prov = str(provider or "").strip()
    room = CARD_W - len("PROGRESS  ") - (len(prov) + 2 if prov else 0)
    if len(tail) > room:
        tail = tail[: max(3, room - 1)] + "…"
    line = "PROGRESS  " + tail + (f"  {prov}" if prov else "")
    return line[:CARD_W]


def _row(label: str, value: str, suffix: str = "") -> str:
    return f"  {label.ljust(LABEL_W)}{value.rjust(NUM_W)}{suffix}"


def _turn_row(turn: Dict[str, Any], source: str) -> str:
    index = _thousands(turn.get("i")) if turn.get("i") is not None else "0"
    cost_value, _ = _cost_text(turn.get("cost"), source)
    return (
        f"  {index:>{TURN_NUM_W}}"
        f"  in {_thousands(turn.get('in')):>{TURN_IN_W}}"
        f"  out {_thousands(turn.get('out')):>{TURN_OUT_W}}"
        f"  {cost_value:>{TURN_COST_W}}"
    )


def _plus_text(cost: Any, source: str) -> str:
    """Delegate cost as an added amount: '+$0.0412', '+~$0.0412', '+$n/a'."""
    value, _suffix = _cost_text(cost, source)
    return "+" + value


def _compact_tokens(value: Any) -> str:
    """Short token count for the context row: '165.9k', '1.0M'. Never raises."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:.1f}k"
    return str(int(amount))


def _agent_cell(value: Any, width: int) -> str:
    """One fixed card cell: a string cut to ``width`` with a single-cell ellipsis.

    Control characters are removed first. A model name comes from another agent's
    transcript, so a newline in it could otherwise forge an extra line on the card.
    """
    body = "".join(ch for ch in str(value if value is not None else "")
                   if ch >= " " and ch != "\x7f")
    if len(body) > width:
        return body[: max(1, width - 1)] + "…"
    return body.ljust(width)


def _agent_cost_cell(cost: Any, source: str, unpriced: bool) -> str:
    """``$n/a``, ``$1.5040``, ``~$1.5040``; a ``?`` marks a priced-total that leaves an
    unpriced model out. ``$n/a?`` would say the same thing twice, so it never appears."""
    value, _suffix = _cost_text(cost, source)
    if unpriced and source != "none":
        return value + "?"
    return value


def _agent_row(label: str, model: Any, tokens: Any, cost: Any, source: str,
               unpriced: bool) -> str:
    """One 42-cell row of the other-agents block, aligned with the rest of the card.

    ``tokens`` and ``cost`` may be ``None`` for a spacer row: the cells stay blank so the
    columns do not move.
    """
    token_cell = "" if tokens is None else _compact_tokens(tokens).rjust(6)
    cost_cell = "" if cost is None else _agent_cost_cell(cost, source, unpriced).rjust(10)
    return (
        f"  {_agent_cell(label, 8)}"
        f"{_agent_cell(model, 16)}"
        f"{token_cell:<6}"
        f"{cost_cell:<10}"
    )[:CARD_W]


def _agent_model_tail(model: Any) -> str:
    """Last path segment of a model id: ``deepseek-ai/DeepSeek-V4.1-Flash`` reads as
    ``DeepSeek-V4.1-Flash``. A long tail is cut in the row, not here."""
    text = str(model or "?").strip()
    return text.split("/")[-1] or text


def _others_lines(payload: Dict[str, Any], hidden: Any = None) -> List[str]:
    """The cross-agent block: one row per agent, its top models, then the combined total.

    An agent with no readable transcript is skipped, so a tree that does not exist on this
    machine cannot print a fake zero. ``?`` after a cost means at least one of its models
    has no price in the table, and that part is left out of the money.
    """
    entries = payload.get("others")
    entries = entries if isinstance(entries, list) else []
    if not entries:
        return []

    skip = {str(key) for key in (hidden or ())}
    if "others" in skip:
        return []

    lines: List[str] = []
    contributors = 0
    reachable = 0
    for entry in entries:
        if isinstance(entry, dict) and entry.get("available"):
            contributors += 1
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("available"):
            continue
        if str(entry.get("name") or "") in skip:
            continue
        reachable += 1
        source = str(entry.get("cost_source") or "none")
        if source not in ("provider", "table", "none"):
            source = "none"
        unpriced = bool(entry.get("unpriced"))
        models = [m for m in (entry.get("models") or []) if isinstance(m, dict)]
        lines.append(
            _agent_row(str(entry.get("name") or "?"),
                       f"{_int_value(entry.get('models_total')) or len(models)} "
                       f"model{'s' if (_int_value(entry.get('models_total')) or len(models)) != 1 else ''}",
                       entry.get("tokens"), entry.get("cost_usd"), source, unpriced)
        )

        for model in models[:AGENT_MODEL_ROWS]:
            model_source = str(model.get("cost_source") or "none")
            if model_source not in ("provider", "table", "none"):
                model_source = "none"
            lines.append(
                _agent_row("", "· " + _agent_model_tail(model.get("model")), model.get("tokens"),
                           model.get("cost_usd"), model_source,
                           model_source == "none")
            )
        extra = len(models) - AGENT_MODEL_ROWS
        if extra > 0:
            lines.append(_agent_row("", f"· +{extra} more", None, None, "none", False))

    if not reachable:
        # Nothing to say: a header over "no transcripts" only pushes the card down.
        return []

    source = str(payload.get("others_source") or "none")
    if source not in ("provider", "table", "none"):
        source = "none"
    lines.append(
        # The amount sums every available agent, so the label counts every contributor,
        # not only the rows still visible.
        _agent_row(f"total {contributors}", "all models", payload.get("others_tokens"),
                   payload.get("others_cost_usd"), source, bool(payload.get("others_unpriced")))
    )
    return lines


def render_card(payload: Dict[str, Any]) -> str:
    """The /svprogress card: plain rows, aligned values, no JSON.

    The rows and the numbers belong to the primary session. Delegate sessions are added
    on top: their summed cost sits on its own ``agents`` row, and the cost row shows the
    total so the two numbers can never be confused.
    """
    source = str(payload.get("cost_source") or "none")
    if source not in ("provider", "table", "none"):
        source = "none"

    delegates = payload.get("delegates")
    delegates = delegates if isinstance(delegates, dict) else {}
    delegate_count = max(0, _int_value(delegates.get("count")))
    delegate_source = str(delegates.get("cost_source") or "none")
    if delegate_source not in ("provider", "table", "none"):
        delegate_source = "none"

    total = payload.get("total_cost_usd")
    if not isinstance(total, (int, float)):
        total = payload.get("cost_usd")

    tokens_in = payload.get("input_tokens") or 0
    cache_read = payload.get("cache_read") or 0
    try:
        hit = float(payload.get("cache_hit_pct") or 0.0)
    except (TypeError, ValueError):
        hit = 0.0
    if not hit:
        hit = _cache_hit_pct(_int_value(cache_read), _int_value(tokens_in))

    cost_value, cost_suffix = _cost_text(total, source)
    suffix = f"   {cost_suffix}" if cost_suffix else ""
    rule = RULE * (CARD_W - 2)

    saved_source = str(payload.get("cache_saved_source") or "none")
    if saved_source not in ("provider", "table", "none"):
        saved_source = "none"
    saved_value, saved_suffix = _cost_text(payload.get("cache_saved_usd"), saved_source)

    day_source = str(payload.get("day_source") or "none")
    if day_source not in ("provider", "table", "none"):
        day_source = "none"
    day_value, _day_suffix = _cost_text(payload.get("day_cost_usd"), day_source)
    day_sessions = max(0, _int_value(payload.get("day_sessions")))
    day_tail = f"   {day_sessions} chat{'s' if day_sessions != 1 else ''}"

    primary_id = str(payload.get("primary_session") or payload.get("session_id") or "")
    tail = primary_id[-12:] if primary_id else "unknown"
    primary_line = (
        f"  {'primary'.ljust(LABEL_W)}{tail}"
        f"  {delegate_count} agent{'s' if delegate_count != 1 else ''}"
    )

    if delegate_count:
        agents_row = _row("agents", _thousands(delegate_count),
                          f"   {_plus_text(delegates.get('cost_usd'), delegate_source)}")
    else:
        agents_row = _row("agents", "none")

    hidden = set()
    for name in (payload.get("hidden_rows") or []):
        key = _normalize_row(name)
        if key:
            hidden.add(key)

    others_block = _others_lines(payload, hidden)

    ctx_tokens = max(0, _int_value(payload.get("context_tokens")))
    ctx_window = _int_value(payload.get("context_window"))
    if ctx_window <= 0:
        ctx_window = DEFAULT_CONTEXT_WINDOW
    ctx_pct = min(100.0, ctx_tokens / ctx_window * 100.0)
    context_row = _row(
        "context",
        f"{_compact_tokens(ctx_tokens)}/{_compact_tokens(ctx_window)}",
        f"   {ctx_pct:4.1f}%",
    )

    def shown(*keys: str) -> bool:
        """True when at least one of ``keys`` is still on. Hidden rows draw nothing."""
        return not any(key in hidden for key in keys)

    body: List[str] = [primary_line]
    if shown("api"):
        body.append(_row("api calls", _thousands(payload.get("api_calls"))))
    if shown("tokens"):
        # The cache gauge lives inside the input row, so `cache` controls only that marker.
        marker = f"   ◐ {f'{hit:.1f}%'.rjust(5)}" if shown("cache") else ""
        body.extend([
            _row("input", _thousands(tokens_in), marker),
            _row("output", _thousands(payload.get("output_tokens"))),
            _row("cache read", _thousands(cache_read)),
        ])
    if shown("ctx"):
        body.append(context_row)
    if shown("cost"):
        body.append(_row("cost", cost_value, suffix))
    if shown("burn"):
        body.append(_row("burn/hour", _rate_text(payload.get("burn_usd_per_hour"), source)))
    if shown("saved"):
        body.append(_row("cache saved", saved_value, f"   {saved_suffix}" if saved_suffix else ""))
    if shown("today"):
        body.append(_row("today", day_value, day_tail))
    if shown("daytok"):
        body.append(_row("tokens/day", _thousands(payload.get("day_tokens"))))
    if shown("agents"):
        body.append(agents_row)
    if others_block:
        body.extend(others_block)

    header_model = payload.get("model") if shown("model") else ""
    lines = [_header(header_model, payload.get("provider")), rule]
    lines.extend(body)
    lines.append(rule)

    turns_raw = payload.get("turns")
    turns = (
        [t for t in turns_raw if isinstance(t, dict)][-5:]
        if isinstance(turns_raw, (list, tuple))
        else []
    )
    if turns:
        lines.append(f"  ▸ last {len(turns)} turns")
        lines.extend(_turn_row(turn, source) for turn in turns)
    else:
        lines.append("  ▸ no turns yet")

    try:
        age = time.time() - float(payload.get("ts") or 0.0)
    except (TypeError, ValueError):
        age = 0.0
    if age > STALE_S:
        lines.append(f"  last update {_age_text(age)} ago")

    return "\n".join(line[:CARD_W].rstrip() for line in lines)


def _cmd_progress(raw_args: str = "", *args: Any, **kwargs: Any) -> str:
    """``/svprogress`` handler: print the session card, never JSON, never a traceback.

    Subcommands, one per row shown above:
      ``/svprogress hide burn codex``   stop drawing those rows
      ``/svprogress show burn``         draw them again
      ``/svprogress reset``             draw every row
      ``/svprogress rows``              print which rows are shown or hidden
    """
    try:
        words = str(raw_args or "").replace(",", " ").split()
        verb = words[0].lower() if words else ""
        names = words[1:]

        if verb in ("hide", "off", "mute") and names:
            return _toggle_rows(names, True)
        if verb in ("show", "on", "unmute") and names:
            return _toggle_rows(names, False)
        if verb in ("reset", "all", "clear"):
            _write_hidden_rows([])
            return "savvy-progress: every row shown"
        if verb in ("rows", "list"):
            return _rows_summary()
        if verb in ("hide", "off", "mute", "show", "on", "unmute"):
            return _rows_summary()
    except Exception as exc:
        logger.debug("savvy-progress: row toggle failed: %s", exc)
        return UNAVAILABLE

    try:
        # The user is waiting for a printout, so this is the one place a cold scan is
        # allowed to block. Every later hook reads the warm cache.
        _others_totals(block=True)
        payload = snapshot(_primary_session_id() or "")
        if not isinstance(payload, dict):
            payload = _read_prev(_snapshot_path())
        if not isinstance(payload, dict):
            return NO_DATA
        payload = dict(payload)
        payload["hidden_rows"] = _hidden_rows()
        return render_card(payload)
    except Exception as exc:
        logger.debug("savvy-progress: /svprogress failed: %s", exc)
        return UNAVAILABLE


def _latest_session_id() -> Optional[str]:
    """Kept for callers of the old name. Now the primary session, not the newest row."""
    return _primary_session_id()


def register(ctx: Any) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    # Second trigger, same writer: an API response refreshes the card even when no tool ran.
    try:
        ctx.register_hook("post_api_request", _on_post_api_request)
    except Exception as exc:
        logger.debug("savvy-progress: post_api_request hook not registered: %s", exc)
    try:
        ctx.register_command(
            "svprogress",
            _cmd_progress,
            description="Print the savvy-progress session snapshot",
        )
    except Exception as exc:
        logger.debug("savvy-progress: command not registered: %s", exc)


