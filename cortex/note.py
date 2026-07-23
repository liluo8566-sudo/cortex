"""Wakeup note: the床头字条 (bedside note) handed to a cortex session on wake.

gather() is the I/O layer — DB reads plus a best-effort macOS frontmost-app
probe. Every external source is wrapped in try/except so a failure omits its
line rather than crashing the wake. render() is pure — no I/O, no DB — so it
can be unit-tested with synthetic data.

Layout: a header block (Now / Plan Used / Active), then `---`-separated blocks
for pending self-schedule and Replay. The handoff injects at
SessionStart (marrow), not here. Cal/Rem lines retired (global inject pending).
The old "Wake:" reason line is gone — reasons carry no signal (desire engine
retired, wander-only).
"""
from __future__ import annotations

import json
import re
import subprocess
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cortex import config
from cortex.pacemaker.integration import parse_due_at
from cortex.pacemaker.integration import _today_tokens as _integration_today_tokens

# ct_rate_limit is a flat kv table (key, value, updated_at) the marrow-side
# collector owns (usage_snapshot). Keys read here: five_hour_pct /
# five_hour_reset_at, seven_day_pct / seven_day_reset_at, today_net_tokens.
# Any missing key -> that segment is omitted.
_FIVE_HOUR = ("five_hour_pct", "five_hour_reset_at")
_SEVEN_DAY = ("seven_day_pct", "seven_day_reset_at")
_TODAY_NET_KEY = "today_net_tokens"

# A wake row younger than this many seconds is treated as *this* wake (the tick
# logs it before the note is assembled), so "Last wake" reports the one before.
_CURRENT_WAKE_EPSILON_S = 90


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))


def _parse_utc(ts_iso: str) -> datetime:
    return datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))


def _local_hm(ts_iso: str, cfg: dict) -> str:
    return _parse_utc(ts_iso).astimezone(_tz(cfg)).strftime("%H:%M")


def _note_cfg(cfg: dict) -> dict:
    return cfg.get("note", {}) or {}


# Channels whose turns are cortex self-talk (wake monologues), excluded from
# Replay so the wakeup note does not replay itself back into its own context.
_DEFAULT_REPLAY_EXCLUDE_CHANNELS = ("ct",)


def _replay_exclude_channels(cfg: dict) -> tuple[str, ...]:
    raw = _note_cfg(cfg).get("replay_exclude_channels")
    if raw is None:
        return _DEFAULT_REPLAY_EXCLUDE_CHANNELS
    return tuple(str(c) for c in raw if str(c).strip())


# --------------------------------------------------------------------------- #
# DB-sourced facts
# --------------------------------------------------------------------------- #

def _last_wake(conn: sqlite3.Connection, now: datetime) -> dict | None:
    """Previous wake=1 row's age + force_slept marker. Skips a row logged for
    the current wake (younger than the epsilon)."""
    try:
        rows = conn.execute(
            "SELECT ts, force_slept FROM ct_wake_log WHERE wake = 1 "
            "ORDER BY ts DESC LIMIT 3"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        try:
            age = now - _parse_utc(row["ts"])
        except (TypeError, ValueError):
            continue
        if age.total_seconds() < _CURRENT_WAKE_EPSILON_S:
            continue
        return {
            "minutes_ago": int(age.total_seconds() // 60),
            "force_slept": row["force_slept"],
            "ts": row["ts"],
        }
    return None


def _handoff_after(cfg: dict, prev_ts: str | None) -> bool:
    """True if the handoff file is non-empty and was modified after `prev_ts`
    (the prior wake=1 row's ISO-UTC ts). Uses the DB row ts as the stable
    reference (wake_state.awake_since is cleared by lie_down / rewritten by
    external resets, so it is not reliable here)."""
    if not prev_ts:
        return False
    try:
        prev_epoch = _parse_utc(prev_ts).timestamp()
    except (TypeError, ValueError):
        return False
    from cortex import config as _config
    handoff = _config.handoff_path(cfg)
    try:
        if not handoff.exists() or handoff.stat().st_mtime <= prev_epoch:
            return False
        return bool(handoff.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _today_tokens(conn: sqlite3.Connection, now: datetime) -> int:
    """Cortex Today = sum of today's finished-window final occupancies + the
    current live window occupancy. Delegates to the daily-budget gate's helper
    (pacemaker.integration._today_tokens) so the note line and the gate always
    show the same figure (parity by construction, not by two copies)."""
    return _integration_today_tokens(conn, now)


def _rate_limit_kv(conn: sqlite3.Connection) -> dict:
    try:
        rows = conn.execute("SELECT key, value FROM ct_rate_limit").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["key"]: row["value"] for row in rows}


def _window_tokens(conn: sqlite3.Connection) -> int | None:
    """Window occupancy hint (statusline total: input + cache_read +
    cache_creation + output) published by lie_down / watchdog into
    ct_pacemaker_state JSON under 'window_tokens'. Absent -> None (segment
    omitted). Rendered as the Budget 'Net Session Token: Xk' segment."""
    try:
        row = conn.execute(
            "SELECT state FROM ct_pacemaker_state WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        state = json.loads(row["state"])
    except (ValueError, TypeError):
        return None
    val = state.get("window_tokens")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# Media / bridge-marker shaper — deliberate copy of marrow strip_media_markers
# (marrow/marrow/transcript.py:102; marrow not importable from the cortex env).
# Keep byte-identical with that pairing. Strips wx [time:]/[sticker:] markers and
# <image|file|gif path="..."/> tags so replayed rows read like plain dialogue.
_TIME_PREFIX_RE = re.compile(r"^\[time:[^\]]+\]\s*")
_STICKER_LINE_RE = re.compile(r"^\[sticker:[^\]\n]*\]\n?", re.M)
_MEDIA_TAG_RE = re.compile(r'\s*<(?:image|file|gif)\s+path="[^"]*?"[^>]*>\s*')


def _strip_markers(text: str) -> str:
    if not text:
        return ""
    text = _TIME_PREFIX_RE.sub("", text)
    text = _STICKER_LINE_RE.sub("", text)
    return _MEDIA_TAG_RE.sub(" ", text).strip()


def _replay(conn: sqlite3.Connection, cfg: dict, limit: int, per_chars: int,
            since_ts: str | None = None) -> tuple[list[dict], str | None]:
    """Fetch the replay events AND the exact cutoff of the RENDERED subset in a
    single read. Returns (events, cutoff_ts).

    events: last `limit` real user/assistant events (cross-session,
    chronological), each tagged [channel HH:mm] + role marker (N=user,
    Y=assistant) and capped at per_chars. Excludes role='tl' and cortex
    self-talk channels.

    cutoff_ts: the max raw timestamp of the events actually returned, i.e. the
    cutoff of exactly what was rendered — never a re-query of the newest event
    overall. When more new rows exist than `limit`, the query keeps only the
    newest `limit` (ORDER BY id DESC LIMIT), so cutoff is the newest of that
    rendered subset; older-but-still-new overflow rows below the limit are NOT
    covered by this cutoff and remain replayable on the next round (the caller
    that seeds/advances the baseline uses this cutoff, so overflow is never
    skipped). When no eligible events are returned, cutoff_ts is None — the
    caller keeps the prior baseline (no advance, no rewind).

    `since_ts` (diff mode, D6): only events with timestamp > since_ts — a
    free-round tuck-in replays what happened since the wake's last rendered
    note, not the whole wake. None = full replay (epoch zero / wake's initial
    note).

    Replay is meant to show the real user<->assistant exchange context; cortex's
    own wake monologues (channel='ct') are excluded so the note does not replay
    itself. The excluded channel set is config-driven (note.replay_exclude_channels)."""
    if limit <= 0:
        return [], None
    exclude = _replay_exclude_channels(cfg)
    placeholders = ",".join("?" for _ in exclude) if exclude else ""
    where_channel = (
        f" AND COALESCE(channel,'') NOT IN ({placeholders})" if exclude else "")
    where_since = " AND timestamp > ?" if since_ts else ""
    params = (*exclude, *((since_ts,) if since_ts else ()), limit)
    try:
        rows = conn.execute(
            "SELECT role, content, timestamp, channel FROM events "
            "WHERE role IN ('user', 'assistant')" + where_channel + where_since
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return [], None
    events = []
    cutoff_ts: str | None = None
    for row in reversed(rows):  # chronological
        ts = row["timestamp"]
        content = _strip_markers(row["content"])
        if not content:
            continue
        # Cutoff tracks the max ts of the RENDERED subset only. Rows are the
        # newest `limit` (ORDER BY id DESC LIMIT), reversed to chronological, so
        # the last kept row carries the newest ts. Guard on value in case of
        # non-monotonic ts across the window.
        if ts and (cutoff_ts is None or ts > cutoff_ts):
            cutoff_ts = ts
        events.append({
            "channel": row["channel"] or "?",
            "hm": _local_hm(ts, cfg) if ts else "??:??",
            "role": "N" if row["role"] == "user" else "Y",
            "content": _truncate(content, per_chars),
        })
    return events, cutoff_ts


def _replay_events(conn: sqlite3.Connection, cfg: dict, limit: int, per_chars: int,
                   since_ts: str | None = None) -> list[dict]:
    """Thin wrapper: the rendered replay events only (drops the cutoff). See
    `_replay` for the full contract."""
    return _replay(conn, cfg, limit, per_chars, since_ts)[0]


_OMITTED = object()


def seed_baseline(conn: sqlite3.Connection, cfg: dict,
                  cutoff_ts=_OMITTED) -> None:
    """Seed the diff-mode replay baseline (wake_state.last_note_ts) so the FIRST
    free-round tuck-in diffs from the wake-open moment, not epoch zero (D6:
    baseline = the wake's initial note). Called once per wake AFTER set_awake
    (which resets last_note_ts=None).

    `cutoff_ts` (P2-A): the replay cutoff captured when the wake's initial note
    was assembled. Semantics by value:
      - a truthy ts  -> seed that ts verbatim (the baseline must be EXACTLY the
        cutoff of what was rendered, never a later re-query that could race in an
        event the note never showed and drop it from the first free-round).
      - explicit None -> the assembled note had ZERO eligible replay events; seed
        NOTHING, keep the baseline as-is (#2). run_wake always passes the note's
        captured cutoff, so None here is a valid empty note, NOT an omitted arg.
      - OMITTED (arg not passed) -> legacy / test callers with no captured cutoff;
        fall back to a fresh _latest_replay_ts query.

    No-op when there is nothing to seed. Never raises — a failed seed just falls
    back to full replay on the first free-round."""
    try:
        from cortex import wake_state
        if cutoff_ts is _OMITTED:
            latest_ts = _latest_replay_ts(conn, cfg)
        else:
            latest_ts = cutoff_ts
        if latest_ts:
            wake_state.set_last_note_ts(cfg, latest_ts)
    except Exception:
        pass


def _latest_replay_ts(conn: sqlite3.Connection, cfg: dict) -> str | None:
    """ISO timestamp of the most recent non-ct user/assistant event, or None."""
    exclude = _replay_exclude_channels(cfg)
    ph = ",".join("?" for _ in exclude) if exclude else ""
    where_ch = f" AND COALESCE(channel,'') NOT IN ({ph})" if exclude else ""
    try:
        row = conn.execute(
            "SELECT MAX(timestamp) as ts FROM events "
            "WHERE role IN ('user', 'assistant')" + where_ch,
            (*exclude,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["ts"] if row and row["ts"] else None


# --------------------------------------------------------------------------- #
# External best-effort facts (cadence CLI, osascript, handoff file)
# --------------------------------------------------------------------------- #

def _frontmost_app() -> str | None:
    """macOS frontmost application name. Locked screen / login window / any
    failure -> None (line omitted)."""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application '
             'process whose frontmost is true'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = out.stdout.strip()
    if out.returncode != 0 or not name or name in ("loginwindow",):
        return None
    return name


def _pending(cfg: dict, now: datetime) -> list[dict]:
    """self_schedule.json entries due within pending_window_min from now.
    due_at may be tz-aware or offset-free local ISO (parse_due_at handles
    both); a garbage/unparseable entry is skipped, never crashes the note."""
    path = config.self_schedule_path(cfg)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    window = _note_cfg(cfg).get("pending_window_min", 15)
    horizon = now + timedelta(minutes=window)
    tz = _tz(cfg)
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            due = parse_due_at(item.get("due_at"), tz)
            if due is None or due > horizon:
                continue
            out.append({
                "hm": due.astimezone(tz).strftime("%H:%M"),
                "intent": item.get("intent", ""),
            })
        except (ValueError, TypeError):
            continue
    return out


# --------------------------------------------------------------------------- #
# gather / render
# --------------------------------------------------------------------------- #

def _safe(fn, *args, default=None):
    """Run one gather section; any exception -> default (section omitted).
    Belt-and-braces on top of each helper's own try/except: no data failure
    may crash note assembly (module design: best-effort throughout)."""
    try:
        return fn(*args)
    except Exception:
        return default


def gather(
    conn: sqlite3.Connection,
    cfg: dict,
    now: datetime,
    decision: dict | None = None,
    *,
    fresh: bool = False,
    wake_kind: str | None = None,
    died_no_handoff: bool = False,
    window_sid: str | None = None,
    advance_baseline: bool = False,
) -> dict:
    """Assemble the wakeup note data dict. conn must use sqlite3.Row factory.
    `fresh`/`wake_kind` are accepted for caller compatibility; the handoff
    now injects at SessionStart, not here. `died_no_handoff` = the prior window
    crashed without a handoff (respawn catchup line).

    `window_sid` (caller-supplied) overrides the wake_state transcript for the
    Window line — the caller's own transcript stem is correct even after a
    rotation, whereas wake_state.transcript was cleared at lie_down and is only
    re-set after this note is written. awake_since still comes from wake_state.

    `advance_baseline` (default False): move the diff-mode replay baseline
    (wake_state.last_note_ts) to the newest eligible event. ONLY the free-round
    tuck-in path passes True — each free-round consumes the events it showed so
    the next one diffs from here. Every render-only path (marrow render_module /
    --print-note / any SessionStart re-render) MUST leave this False, else a
    passive re-render advances the baseline and the next real free-round silently
    drops replay events."""
    ncfg = _note_cfg(cfg)

    from cortex import wake_state

    kv = _safe(_rate_limit_kv, conn, default={})
    budget = _safe(_build_budget, conn, cfg, now, kv, ncfg)
    last_wake = _safe(_last_wake, conn, now)
    # Catchup suppression: the prior window may have been reaped (force_slept set)
    # yet still wrote its handoff before dying. If the handoff was touched after
    # that prior wake row's ts and is non-empty, there is nothing to backfill ->
    # skip the catchup line and its 30-40k token re-read.
    catchup_handoff_written = bool(
        last_wake and _handoff_after(cfg, last_wake.get("ts")))

    ws = {}
    try:
        ws = wake_state.load(cfg)
    except Exception:
        pass
    awake_since_hm = None
    if not window_sid:
        transcript_raw = ws.get("transcript")
        if transcript_raw:
            try:
                from pathlib import Path
                window_sid = Path(str(transcript_raw)).stem[:8]
            except Exception:
                pass
    since_raw = ws.get("awake_since")
    if since_raw:
        try:
            since_dt = _parse_utc(since_raw)
            awake_since_hm = since_dt.astimezone(_tz(cfg)).strftime("%H:%M")
        except (TypeError, ValueError):
            pass
    # Diff mode (D6): replay only events newer than the last rendered note this
    # wake (last_note_ts). Absent (wake's initial note, or wake_state load
    # failed) -> full replay, same as before this refactor.
    note_since_ts = ws.get("last_note_ts")
    replay, rendered_cutoff = _safe(
        _replay, conn, cfg,
        ncfg.get("replay_events", 4),
        ncfg.get("replay_event_chars", 300),
        note_since_ts,
        default=([], None),
    )
    replay_stale = False
    if last_wake:
        # Use the exact prior-wake ts (last_wake["ts"]), never the floored
        # minutes_ago reconstruction: int(seconds // 60) truncates, so
        # now - timedelta(minutes=minutes_ago) can land up to 59s AFTER the
        # real wake, moving the stale boundary forward and wrongly staling an
        # event that arrived just after the real wake.
        try:
            last_wake_dt = _parse_utc(last_wake["ts"])
        except (TypeError, ValueError, KeyError):
            last_wake_dt = None
        if last_wake_dt is not None:
            if not replay:
                # No new eligible events this render. If the newest event overall
                # predates this wake, mark the replay stale ("no new messages") — a
                # cheap read used only for the human-facing line, never for cutoff.
                latest_ts = _safe(_latest_replay_ts, conn, cfg)
                if latest_ts:
                    try:
                        replay_stale = _parse_utc(latest_ts) < last_wake_dt
                    except (TypeError, ValueError):
                        pass
            elif note_since_ts is None and rendered_cutoff:
                # Initial-wake FULL replay (no diff baseline yet): _replay applied
                # no since filter, so it can return events that all PREDATE this
                # wake. Their newest (rendered_cutoff) older than the prior wake =
                # nothing fresh -> "no new messages", not a fake-fresh "### Replay"
                # of an old conversation. Reuses rendered_cutoff (no extra query).
                try:
                    if _parse_utc(rendered_cutoff) < last_wake_dt:
                        replay_stale = True
                except (TypeError, ValueError):
                    pass
    # The replay cutoff this render actually used: the max ts of the RENDERED
    # subset (rendered_cutoff), or the diff baseline it started from when nothing
    # new was rendered. Derived from the same read as `replay` — never a separate
    # re-query, which could race in an event this note never showed and then drop
    # it from the next round (P2-A / P2-B / #1). With more new rows than the render
    # limit, rendered_cutoff is the newest of the rendered subset only, so overflow
    # rows below the limit stay > baseline and replay next round (never skipped).
    replay_cutoff_ts = rendered_cutoff or note_since_ts
    # Advance the diff-mode baseline to the cutoff of what was rendered, so the
    # NEXT free-round tuck-in diffs from here. Monotonic: only moves forward.
    # Gated on advance_baseline: render-only callers never write it.
    if advance_baseline and rendered_cutoff and (
            not note_since_ts or rendered_cutoff > note_since_ts):
        _safe(wake_state.set_last_note_ts, cfg, rendered_cutoff)

    kick = None
    try:
        from cortex.kick import read_signal
        kick = read_signal(cfg)
    except Exception:
        pass

    return {
        "replay_cutoff_ts": replay_cutoff_ts,
        "last_wake": last_wake,
        "budget": budget,
        "active_app": _safe(_frontmost_app),
        "pending": _safe(_pending, cfg, now, default=[]),
        "died_no_handoff": died_no_handoff,
        "replay": replay,
        "replay_stale": replay_stale,
        "window_sid": window_sid,
        "awake_since_hm": awake_since_hm,
        "catchup_handoff_written": catchup_handoff_written,
        "kick": kick,
    }


def _build_budget(conn, cfg, now, kv, ncfg) -> dict:
    five = kv.get(_FIVE_HOUR[0])
    seven = kv.get(_SEVEN_DAY[0])
    five_reset = kv.get(_FIVE_HOUR[1])
    seven_reset = kv.get(_SEVEN_DAY[1])
    return {
        "five_h_pct": _as_float(five),
        "five_h_reset": _local_hm(five_reset, cfg) if five_reset else None,
        "seven_d_pct": _as_float(seven),
        "seven_d_countdown": _countdown(seven_reset, now) if seven_reset else None,
        "window_tokens": _window_tokens(conn),
        "today_tokens": _today_tokens(conn, now),
        "daily_budget": int(ncfg.get("daily_budget", 1_000_000)),
    }


def _countdown(reset_iso: str, now: datetime) -> str | None:
    """Remaining time until an ISO reset moment as a compact `1d2h`/`5h`/`12m`
    string. Past/unparseable -> None (segment omitted)."""
    try:
        delta = _parse_utc(reset_iso) - now.astimezone(ZoneInfo("UTC"))
    except (TypeError, ValueError):
        return None
    secs = int(delta.total_seconds())
    if secs <= 0:
        return None
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def _as_float(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def render(cfg: dict, now: datetime, data: dict) -> str:
    """Pure assembly: data dict -> wakeup note text. No DB / no I/O.

    Layout (plan §一): a header block (Now / Plan Used / Active), then
    `---`-separated blocks for pending self-schedule and Replay, then a final
    turn-end reminder line (note.turn_end_text, every render; "" omits it).
    Handoff no longer lives here — it is injected at SessionStart on a
    fresh window."""
    header: list[str] = []

    now_seg = f"Now: {now.strftime('%H:%M %a')}"
    last = data.get("last_wake")
    if last:
        seg = f"Last wake: {last['minutes_ago']}min ago"
        # "auto" = routine proxy sleep on the silence path -> render neutrally,
        # never as a force incident. Only real force incidents get the tag.
        if last.get("force_slept") and last.get("force_slept") != "auto":
            seg += " (force-slept mid-task)"
        now_seg += f" | {seg}"
    header.append(now_seg)

    budget_line = _render_budget(data.get("budget"))
    if budget_line:
        header.append(budget_line)

    app = data.get("active_app")
    if app:
        header.append(f"Active (Mac): {app}")

    w_sid = data.get("window_sid")
    w_since = data.get("awake_since_hm")
    if w_sid or w_since:
        parts = []
        if w_since:
            parts.append(f"since {w_since}")
        if w_sid:
            parts.append(f"SID {w_sid}")
        header.append("Window: " + " | ".join(parts))

    # Prior window was force-slept without writing its handoff -> tell this
    # window to backfill from DB events (recall/tl), never from raw jsonl.
    # "auto" (routine silence sleep) is not an incident -> no catchup line.
    # If the handoff was written after the prior wake (catchup_handoff_written),
    # there is nothing to backfill -> skip the catchup + its costly re-read.
    if (last and last.get("force_slept") and last.get("force_slept") != "auto"
            and not data.get("catchup_handoff_written")):
        catchup = _note_cfg(cfg).get("force_slept_catchup_text", "")
        if catchup:
            header.append(catchup)

    # Prior window DIED (crash/manual close) mid-wake without a handoff -> the
    # fresh respawn recovers from its transcript.
    if data.get("died_no_handoff"):
        catchup = _note_cfg(cfg).get("died_no_handoff_catchup_text", "")
        if catchup:
            header.append(catchup)

    blocks: list[str] = ["\n".join(header)]

    kick = data.get("kick")
    if kick:
        kind = kick.get("kind", "?")
        if kind == "reply":
            reply_text = kick.get("text", "")
            seg = "**Kick: reply received on TG/WX** — check outbox for fired watches."
            if reply_text:
                seg += f"\nHer reply: {reply_text}"
            blocks.append(seg)
        elif kind == "timeout":
            blocks.append("**Kick: watch_reply timeout** — she hasn't replied; check outbox for fired watches.")
        elif kind == "morning":
            blocks.append("**Kick: morning flag-pull** — night mode clearing, resume day cadence.")
        else:
            blocks.append(f"**Kick: {kind}** — check outbox.")

    pending = data.get("pending") or []
    if pending:
        segs = [f"due {p['hm']} {p['intent']}".rstrip() for p in pending]
        blocks.append("Pending self-schedule: " + " · ".join(segs))

    replay = data.get("replay") or []
    if data.get("replay_stale"):
        blocks.append("No new messages since last wake.")
    elif replay:
        rlines = ["### Replay"]
        for ev in replay:
            role = ev.get("role", "")
            content = " ".join((ev.get("content") or "").split())
            rlines.append(f"[{ev['channel']} {ev['hm']}] {role}: {content}")
        blocks.append("\n".join(rlines))

    note_text = "\n\n---\n\n".join(blocks)
    turn_end = _note_cfg(cfg).get("turn_end_text", "")
    if turn_end:
        note_text += "\n\n" + turn_end

    title = _note_cfg(cfg).get("title", "")
    if title:
        note_text = title + "\n\n" + note_text
    return note_text


def _render_budget(budget: dict | None) -> str | None:
    """Plan Used line — shows utilization (USED %, statusline口径), pipe-joined:
    `Plan Used: 5h 5% (04:50) | 7d 50% (1d2h) | Cortex Today 250k/1M 25% |
    Net Session Token: 50k`. Net Session Token is window occupancy (statusline
    total), not net spend — label kept for cross-system consistency with
    marrow's threshold line. Any missing datum drops just its segment."""
    if not budget:
        return None
    parts = []
    five = budget.get("five_h_pct")
    if five is not None:
        seg = f"5h {five:.0f}%"
        if budget.get("five_h_reset"):
            seg += f" ({budget['five_h_reset']})"
        parts.append(seg)
    seven = budget.get("seven_d_pct")
    if seven is not None:
        seg = f"7d {seven:.0f}%"
        if budget.get("seven_d_countdown"):
            seg += f" ({budget['seven_d_countdown']})"
        parts.append(seg)
    daily = int(budget.get("daily_budget", 1_000_000))
    today = int(budget.get("today_tokens", 0))
    pct = (today / daily * 100) if daily else 0
    parts.append(f"Cortex Today {today // 1000}k/{_fmt_budget(daily)} {pct:.0f}%")
    window = budget.get("window_tokens")
    if window is not None:
        parts.append(f"Net Session Token: {window // 1000}k")
    return "Plan Used: " + " | ".join(parts) if parts else None


def _fmt_budget(n: int) -> str:
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    return f"{n // 1000}k"


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


if __name__ == "__main__":  # pragma: no cover - eyeball a real note
    from cortex import db

    _cfg = config.load()
    _conn = db.connect(_cfg)
    _now = datetime.now(_tz(_cfg))
    _data = gather(_conn, _cfg, _now, fresh=True, wake_kind="rotate")
    print(render(_cfg, _now, _data))
    _conn.close()
