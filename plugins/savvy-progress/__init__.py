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
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    return sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=DB_TIMEOUT_S)


def _match_price(model: str) -> Optional[Tuple[float, float, float]]:
    """Price tuple for a model name, or None. Case-insensitive, longest key wins."""
    name = (model or "").lower()
    for key in _PRICE_KEYS:
        if key in name:
            return PRICE_TABLE[key]
    return None


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
    with _connect() as conn:
        rows = conn.execute(_ROWS_SQL, (session_id,)).fetchall()
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
    with _connect() as conn:
        row = conn.execute(_MODEL_SQL, (session_id,)).fetchone()
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
    try:
        with _connect() as conn:
            row = conn.execute(_CONTEXT_SQL, (session_id,)).fetchone()
    except sqlite3.Error as exc:
        logger.debug("savvy-progress: context query failed: %s", exc)
        return 0
    try:
        return max(0, int(row[0] or 0)) if row else 0
    except (TypeError, ValueError):
        return 0


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
        with _connect() as conn:
            cur = conn.execute(_SESSIONS_SQL)
            names = [col[0] for col in cur.description or ()]
            raw = cur.fetchall()
    except sqlite3.Error as exc:
        logger.debug("savvy-progress: sessions table unusable: %s", exc)
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


def _write_pin(session_id: str, ts: Optional[float] = None) -> None:
    """Atomic write, mode 0600: the file names one session, so keep it private."""
    sid = str(session_id or "")
    if not sid:
        return
    path = _pin_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    payload = {"session_id": sid, "ts": round(time.time() if ts is None else float(ts), 3)}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


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
    try:
        with _connect() as conn:
            row = conn.execute(_PIN_ELIGIBLE_SQL, (sid, PRIMARY_SOURCE)).fetchone()
    except sqlite3.Error as exc:
        logger.debug("savvy-progress: pin eligibility query failed: %s", exc)
        return False
    if row is None:
        return False
    try:
        _write_pin(sid)
    except Exception as exc:
        logger.debug("savvy-progress: pin write failed: %s", exc)
        return False
    return True


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
        with _connect() as conn:
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
            with _connect() as conn:
                rows = conn.execute(_DELEGATE_USAGE_SQL, (now - DELEGATE_WINDOW_S,)).fetchall()
        except sqlite3.Error as exc:
            logger.debug("savvy-progress: delegate scan failed: %s", exc)
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


def _write_snapshot(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


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


def _write_sessions(path: Path, sessions: Dict[str, Any]) -> None:
    """Atomic write, mode 0600: the file holds session ids, so keep it private."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"sessions": sessions}, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


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
        "cost_source": _combine_sources(total_sources),
        "turns": primary_turns,
        "primary_session": primary_id,
        "delegates": delegates,
        "total_cost_usd": total_cost,
        "context_tokens": _read_context_tokens(primary_id),
        "context_window": _context_window(),
    }
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
    keys = [k for k in payload if k != "ts"]
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

        if sessions_changed:
            _write_sessions(sessions_path, sessions)
        if snapshot_changed:
            _write_snapshot(path, payload)
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
    except (TypeError, ValueError):
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
    return body, ""


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
    try:
        delegate_count = int(delegates.get("count") or 0)
    except (TypeError, ValueError):
        delegate_count = 0
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
        hit = _cache_hit_pct(int(cache_read), int(tokens_in))

    cost_value, cost_suffix = _cost_text(total, source)
    suffix = f"   {cost_suffix}" if cost_suffix else ""
    rule = RULE * (CARD_W - 2)

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

    try:
        ctx_tokens = max(0, int(payload.get("context_tokens") or 0))
    except (TypeError, ValueError):
        ctx_tokens = 0
    try:
        ctx_window = int(payload.get("context_window") or 0)
    except (TypeError, ValueError):
        ctx_window = 0
    if ctx_window <= 0:
        ctx_window = DEFAULT_CONTEXT_WINDOW
    ctx_pct = min(100.0, ctx_tokens / ctx_window * 100.0)
    context_row = _row(
        "context",
        f"{_compact_tokens(ctx_tokens)}/{_compact_tokens(ctx_window)}",
        f"   {ctx_pct:4.1f}%",
    )

    lines = [
        _header(payload.get("model"), payload.get("provider")),
        rule,
        primary_line,
        _row("api calls", _thousands(payload.get("api_calls"))),
        _row("input", _thousands(tokens_in), f"   ◐ {f'{hit:.1f}%'.rjust(5)}"),
        _row("output", _thousands(payload.get("output_tokens"))),
        _row("cache read", _thousands(cache_read)),
        context_row,
        _row("cost", cost_value, suffix),
        agents_row,
        rule,
    ]

    turns = [t for t in (payload.get("turns") or []) if isinstance(t, dict)][-5:]
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
    """``/svprogress`` handler: print the session card, never JSON, never a traceback."""
    try:
        payload = snapshot(_primary_session_id() or "")
        if not isinstance(payload, dict):
            payload = _read_prev(_snapshot_path())
        if not isinstance(payload, dict):
            return NO_DATA
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
