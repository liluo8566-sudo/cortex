"""Wake state + token occupancy: the durable ct_pacemaker_state row and the
"Cortex Today" token accounting.

Sole owner of the single-row JSON state (id=1): the persisted WakeState
dataclass, the side-channel window_tokens occupancy, the lie-down timestamp,
and the activation wake-log row. No decision logic lives here — callers
(lie_down, wake, watchdog, note, ctl) read/write through these APIs.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from cortex import config as _config, db


@dataclass(frozen=True)
class PacemakerState:
    last_wake_at: datetime | None = None
    # C-wm timing: lie-down = wake finished; the next wake is scheduled from here.
    last_lie_down_at: datetime | None = None
    # Cortex session resume (C3). Only the wake caller (cortex.wake)
    # reads/writes this.
    cortex_session_id: str | None = None


# --------------------------------------------------------------------------
# datetime helpers
# --------------------------------------------------------------------------

def _now(cfg: dict) -> datetime:
    return datetime.now(_config.get_tz(cfg))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def parse_due_at(value: str | None, tz: tzinfo) -> datetime | None:
    """Parse a self-schedule due_at. Accepts tz-aware ISO and offset-free (naive)
    ISO; naive is interpreted as local wall time in `tz` (DST-correct). The
    convention is offset-free local — no hardcoded UTC offset (breaks under DST)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


# --------------------------------------------------------------------------
# state persistence (ct_pacemaker_state, single row id=1)
# --------------------------------------------------------------------------

def _state_to_json(state: PacemakerState, base: dict | None = None) -> str:
    obj = dict(base or {})  # preserve side-channel keys (window_tokens)
    # Drop legacy keys carried in from an old row (retired engines).
    for dead in ("desire", "expect_reply", "cortex_session_date",
                 "night_cap_key", "night_wake_count", "next_floor_due_at"):
        obj.pop(dead, None)
    obj.update({
        "last_wake_at": _iso(state.last_wake_at),
        "last_lie_down_at": _iso(state.last_lie_down_at),
        "cortex_session_id": state.cortex_session_id,
    })
    return json.dumps(obj)


def _state_from_json(text: str) -> PacemakerState:
    # Tolerant load: legacy keys from retired engines are simply ignored.
    o = json.loads(text)
    return PacemakerState(
        last_wake_at=_parse_dt(o.get("last_wake_at")),
        last_lie_down_at=_parse_dt(o.get("last_lie_down_at")),
        cortex_session_id=o.get("cortex_session_id"),
    )


def load_state(conn: sqlite3.Connection) -> PacemakerState:
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
    return _state_from_json(row["state"]) if row else PacemakerState()


def store_window_tokens(conn: sqlite3.Connection, tokens: int | None) -> None:
    """Stash the live window occupancy (statusline total: input + cache_read +
    cache_creation + output) on the ct_pacemaker_state JSON (window_tokens_hint
    reads it back). Merged into the raw JSON (not the dataclass) so it survives
    independently of tick saves."""
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
    try:
        obj = json.loads(row["state"]) if row else {}
    except (ValueError, TypeError):
        obj = {}
    obj["window_tokens"] = int(tokens) if tokens else None
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
        (json.dumps(obj), db.utcnow_iso()),
    )
    conn.commit()


def _raw_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
    try:
        return json.loads(row["state"]) if row else {}
    except (ValueError, TypeError):
        return {}


def window_tokens_hint(conn: sqlite3.Connection) -> int:
    """Live window occupancy published on the ct_pacemaker_state JSON
    (store_window_tokens). 0 if absent/unparseable. This is the current window's
    contribution to Cortex Today (the last finished-window run has already lain
    down; the live window's growth is only visible here, fresher than its last
    ct_wake_log row)."""
    val = _raw_state(conn).get("window_tokens")
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def save_state(conn: sqlite3.Connection, state: PacemakerState) -> None:
    base = _raw_state(conn)  # keep side-channel keys (window_tokens)
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
        (_state_to_json(state, base), db.utcnow_iso()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# token occupancy (Cortex Today)
# --------------------------------------------------------------------------

def _finished_window_finals(conn: sqlite3.Connection, now: datetime) -> int:
    """Cortex Today, finished part = SUM over today's finished windows of each
    window's FINAL context occupancy (ct_wake_log.tokens, recorded by lie_down).

    Occupancy grows monotonically within a window (each lie_down of the same
    window is >= the last); a fresh/respawned or resumed window restarts lower,
    so a drop vs the previous row marks a new window. Walking today's tokens
    rows in ts order, each monotonic run is one window and its LAST value is
    that window's final. The trailing run is the CURRENT window — excluded here
    (its live occupancy is added on top via window_tokens_hint) so it is counted
    once, from the fresher live figure, not double-counted.

    Agent/subagent tokens never appear in occupancy by construction, so they are
    excluded automatically. ts is stored UTC ISO; filter from local midnight
    (converted to UTC) then confirm the local date so the day resets at local
    midnight."""
    tz = now.tzinfo
    start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        ZoneInfo("UTC")).isoformat()
    try:
        rows = conn.execute(
            "SELECT ts, tokens FROM ct_wake_log "
            "WHERE tokens IS NOT NULL AND ts >= ? ORDER BY ts ASC",
            (start_utc,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    today = now.date()
    occ: list[int] = []
    for row in rows:
        try:
            if _parse_dt(row["ts"]).astimezone(tz).date() == today:
                occ.append(int(row["tokens"]))
        except (TypeError, ValueError, AttributeError):
            continue
    total = 0
    prev = None
    for i, val in enumerate(occ):
        # A drop from the previous row closes a window: the previous value was
        # its final. The very last run (current window) is never closed here.
        if prev is not None and val < prev:
            total += prev
        prev = val
    return total


# --------------------------------------------------------------------------
# wake log + next-wake scheduling
# --------------------------------------------------------------------------

def log_activation_wake_row(conn: sqlite3.Connection, now: datetime,
                            reasons: str, shell: str = "cli") -> int | None:
    """Insert one wake=1 activation row for a wake that no scheduled decision
    row already covers (user/ctl/reconcile/rotate wakes). `reasons` tags the
    origin (e.g. 'user', 'ctl', 'reconcile', 'rotate') so the wakeup note's
    "Last wake" segment sees every real wake, while force_slept-based auto-rate
    stats stay unaffected (this row's force_slept is NULL until lie_down sets
    it). `shell` stamps which shell the wake belongs to — every caller here is
    the cli window. Returns the new row id, or None on any error (best-effort —
    a failed log must never block the wake)."""
    try:
        cur = conn.execute(
            "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons, shell) "
            "VALUES (?, 1, 0, ?, ?)",
            (now.astimezone(ZoneInfo("UTC")).isoformat(), reasons, shell),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.Error:
        return None


def schedule_next_wake(now: datetime, config: dict,
                       minutes: float | None = None) -> datetime:
    """The next wake due time from `now`. `minutes` = an explicit choice
    (already clamped by the caller); None = [wake].default_sleep_min. Callers
    pass lie-down time as `now` on the wake path (C-wm: the clock runs from
    lie-down, not wake)."""
    if minutes is None:
        minutes = config.get("wake", {}).get("default_sleep_min", 20)
    return now + timedelta(minutes=minutes)


def lie_down(conn: sqlite3.Connection, cfg: dict, now: datetime | None = None,
             minutes: float | None = None) -> datetime:
    """Mark wake end (C-wm) and pick the next internal wake. `minutes` = an
    explicit choice (pre-clamped by the caller to [0, next_wake_max] via
    clamp_next_wake_minutes, not re-clamped here); None = [wake].default_sleep_min.
    The clock restarts from lie-down. Called when a wake finishes — including on
    wake failure, so a crashed wake can't wedge it.
    Returns the next-wake datetime (local tz)."""
    now = now or _now(cfg)

    next_at = schedule_next_wake(now, cfg, minutes)
    save_state(conn, dataclasses.replace(load_state(conn), last_lie_down_at=now))
    return next_at
