"""cortex.lie_down — the command cortex runs to end a wake (the watchdog runs
it as proxy). It: clears due self-schedule entries, books the next wake, records
this wake's token spend into ct_wake_log, kills the watchdog, flags a rotate
(next wake respawns a fresh window) when --rotate is passed, then clears the
awake marker. Rotate is an explicit session decision, no auto token judgement.

The interactive window returns control the moment a note is injected, so the
wake is NOT over when the wake runner returns — this command (or a proxy) is
what actually ends a wake. force_slept marks a proxy lie-down (ct-pause/stale/fuse).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
from datetime import datetime, timezone

from cortex import config, db, occupancy, transcript, wake_state


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _clear_due_self_schedule(cfg: dict) -> int:
    """Drop self_schedule entries whose due_at <= now (in-scene => processed).
    Returns count removed. Future entries are kept."""
    p = config.self_schedule_path(cfg)
    try:
        items = json.loads(p.read_text()) if p.exists() else []
    except (OSError, ValueError):
        return 0
    if isinstance(items, dict):  # tolerate a bare dict (single entry, not wrapped in a list)
        items = [items]
    if not isinstance(items, list):
        return 0
    now = _now_utc()
    tz = config.get_tz(cfg)
    kept = []
    for it in items:
        due = it.get("due_at") if isinstance(it, dict) else None
        d = occupancy.parse_due_at(due, tz)  # tz-aware or naive-local (DST-correct)
        if d is not None and d <= now:
            continue
        kept.append(it)
    p.write_text(json.dumps(kept, ensure_ascii=False, indent=2))
    return len(items) - len(kept)


def _kill_watchdog(cfg: dict) -> None:
    p = wake_state.watchdog_pidfile_path(cfg)
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        return
    if pid != os.getpid():  # a proxy lie-down from the watchdog itself skips this
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    p.unlink(missing_ok=True)


def shell_id(cfg: dict) -> str:
    """The shell stamped on this process's ct_wake_log rows (config.shell_id).
    Non-cli shells never reach this module (marrow routes them to their own
    host)."""
    return config.shell_id(cfg)


def _record_tokens(conn, cfg: dict, state: dict, force_slept: str | None) -> int:
    """Record this wake's context occupancy (`tokens` — last assistant usage
    totals) into its ct_wake_log row. Occupancy grows monotonically within a
    window; the daily Cortex-Today metric sums each window's final occupancy
    (occupancy._finished_window_finals) plus the live window, so no per-wake
    net delta is stored. The shell is re-stamped here so the row always names
    the shell that was put down, whoever inserted it. Returns the recorded
    occupancy."""
    tokens = transcript.window_tokens(cfg)
    wid = state.get("wake_log_id")
    if wid:
        try:
            conn.execute(
                "UPDATE ct_wake_log SET tokens=?, force_slept=?, shell=? WHERE id=?",
                (tokens or None, force_slept, shell_id(cfg), wid))
            conn.commit()
        except Exception:  # column race with concurrent migrate; best-effort
            pass
    return tokens


def clamp_next_wake_minutes(minutes: float, config: dict,
                            human_override: bool = False) -> float:
    """Clamp a lie_down(next_wake_min=N) choice to [0, wake.next_wake_max] —
    one merged band for every hour (0 = immediate re-wake, e.g. a rotate
    starting the successor window right away). `human_override` (explicit ctl
    minutes) passes unclamped. Proxy paths pass None and skip this clamp."""
    if human_override:
        return minutes
    wcfg = config.get("wake", {})
    hi = wcfg.get("next_wake_max", 360)
    return max(0, min(hi, minutes))


def lie_down(cfg: dict, force_slept: str | None = None, rotate: bool = False,
             next_wake_min: float | None = None,
             human_override: bool = False, book_alarm: bool = True) -> dict:
    """End the current wake. `next_wake_min` picks the next internal wake: an
    explicit minutes-from-now, clamped to [0, next_wake_max] regardless of hour
    (0 = immediate re-wake) — or None = [wake].default_sleep_min (proxy paths:
    stale, fuse; N is required at the MCP/CLI layer). `rotate` respawns a fresh
    window next wake. `human_override` (explicit ctl minutes) passes
    next_wake_min unclamped. `book_alarm=False` books NO next wake at all
    (ledger cleared) — the ct-pause path: a pause is a pure stop, only a manual
    ct-wake resumes it."""
    if next_wake_min is not None:
        next_wake_min = clamp_next_wake_minutes(
            next_wake_min, cfg, human_override=human_override)
    # Atomic awake claim: the watchdog (60s poll) and the tick awake-branch can
    # both run silence_action in the same window; only the caller that clears the
    # awake marker here proceeds, so the ct_wake_log update + next-wake booking
    # fire once. A later caller (already cleared) no-ops. awake=true callers win as
    # before. The claim BUMPS gen and hands back a claim_token (gen, state_id):
    # every late side effect below re-validates it under the strict lock, so a
    # user message / newer claim landing mid-body cancels this whole lie_down's
    # alarm chain (fail-closed cancellation epoch — BUG A).
    state = wake_state.claim_lie_down(cfg, force_slept=force_slept)
    if state is None:
        return {"skipped": "not awake", "force_slept": force_slept,
                "rotated": False, "next_wake": None}
    token = state.get("claim_token")
    conn = db.connect(cfg)
    try:
        tokens = _record_tokens(conn, cfg, state, force_slept)
        cleared = _clear_due_self_schedule(cfg)
        # A newer epoch (user reset / newer claim) already superseded this claim
        # -> the wake it was ending is now someone else's live wake. Abort every
        # remaining alarm side effect (next-wake booking, watchdog kill, rotate,
        # ledger) so we never re-arm against a stale generation.
        if not _token_ok(cfg, token):
            return {"tokens": tokens, "cleared_due": cleared,
                    "force_slept": force_slept, "rotated": False,
                    "next_wake": None, "superseded": True}
        # Next wake booked from now; drives the next_wake HH:MM the marrow MCP
        # wrapper surfaces to the session. book_alarm=False books none (pause).
        next_at = occupancy.lie_down(conn, cfg, minutes=next_wake_min)
        if not book_alarm:
            next_at = None
        # Publish AFTER that save_state (which drops the key), so the
        # window_tokens_hint sees this wake's window occupancy (statusline
        # total: input + cache_read + cache_creation + output — the same metric
        # `tokens` already computed above for rotate/fuse), not the NET spend.
        occupancy.store_window_tokens(conn, tokens)
        if _token_ok(cfg, token):
            _kill_watchdog(cfg)
        # Rotate is now an explicit session decision (the --rotate flag), not an
        # auto token judgement — set it and the NEXT wake respawns a fresh window
        # (SIGTERM claude + fresh spawn) that reads the handoff. The rotate/retire
        # writes are conditional CHILDREN of the claim gen (they do NOT bump —
        # bumping would self-invalidate this claim's own alarm), so a superseding
        # user reset suppresses them.
        rotated = False
        if rotate:
            try:
                rotated = bool(wake_state.conditional_mutate(
                    cfg, token, _mark_rotated(state.get("transcript"))))
            except wake_state.StateValidationError:
                # Superseded epoch race -> the newer claim owns the window, no
                # rotate. Must not go silent (the caller would else wake back
                # into the same un-rotated window with no trace): same
                # best-effort alerts-row pattern watchdog/wake use.
                detail = ("rotate skipped: superseded epoch, "
                          f"transcript={state.get('transcript')}")
                wake_state.wake_audit(cfg, "rotate_failed", "superseded", detail)
                try:
                    conn.execute(
                        "INSERT INTO alerts (severity, type, message, source)"
                        " VALUES (?, ?, ?, ?)",
                        ("warn", "cortex_rotate_failed", detail, "cortex.lie_down"))
                    conn.commit()
                except Exception:  # noqa: BLE001 - table may be absent; audit already tried
                    pass
        # awake marker already cleared atomically by claim_lie_down at entry.
        # The durable ledger carries the alarm; the daemon kick makes it instant.
        persist_next_wake_at(cfg, next_at, token)
        next_wake = _local_hm(next_at, cfg)
        return {"tokens": tokens, "cleared_due": cleared,
                "force_slept": force_slept, "rotated": rotated,
                "next_wake": next_wake}
    finally:
        conn.close()


def _token_ok(cfg: dict, token) -> bool:
    """True if the claim token still matches the live epoch (no bump / no
    delete-recreate since the claim). Fail-closed: a lock/parse failure reads as
    NOT ok, so a doubtful late side effect is dropped."""
    try:
        return wake_state.token_current(cfg, token)
    except wake_state.StateValidationError:
        return False


def _mark_rotated(transcript_path):
    """Mutator (used under conditional_mutate): set the one-shot rotate flag +
    the durable retired-sid, both children of the claim gen (no bump). retired_sid
    is the belt-and-braces guard the resume paths check so a stale transcript
    pointer never resumes the retired session."""
    from pathlib import Path

    def _m(d: dict):
        d["rotated"] = True
        d["retired_sid"] = Path(str(transcript_path)).stem if transcript_path else None
        return True
    return _m


def persist_next_wake_at(cfg: dict, next_at: datetime | None, token=None) -> bool:
    """Persist the durable next-wake ledger for `next_at` as a CONDITIONAL child
    of the claim `token`, then notify the wake daemon. The ledger is the alarm:
    it is what a restarted daemon reconciles against. None clears it (no alarm).
    Returns False when a newer epoch (user reset / newer claim) already
    superseded this claim — the caller must then drop every remaining alarm side
    effect."""
    iso = _local_iso(next_at, cfg) if next_at is not None else None
    try:
        wake_state.conditional_mutate(cfg, token, _set_ledger(iso))
    except wake_state.StateValidationError:
        return False  # superseded -> newer epoch owns the ledger + alarm
    _notify_daemon(cfg)
    return True


def _notify_daemon(cfg: dict) -> None:
    """Best-effort kick to the wake daemon's scheduler socket so it re-reads the
    ledger immediately instead of waiting for its safety tick. Silent no-op when
    unconfigured or the daemon is down."""
    dcfg = cfg.get("daemon") or {}
    shell = str(dcfg.get("shell") or "cli")
    try:
        path = config.daemon_socket_path(cfg)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(float(dcfg.get("kick_timeout_sec", 1.0)))
            s.connect(str(path))
            s.sendall((shell + "\n").encode("utf-8"))
    except (OSError, ValueError):
        pass


def _set_ledger(iso):
    def _m(d: dict):
        if iso is None:
            d.pop("next_wake_at", None)
        else:
            d["next_wake_at"] = iso
        return True
    return _m


def _local_hm(dt: datetime | None, cfg: dict) -> str | None:
    """Next-wake datetime -> local HH:MM (config tz). None -> None."""
    if dt is None:
        return None
    return dt.astimezone(config.get_tz(cfg)).strftime("%H:%M")


def _local_iso(dt: datetime | None, cfg: dict) -> str | None:
    """Next-wake datetime -> local ISO (config tz) for the durable ledger."""
    if dt is None:
        return None
    return dt.astimezone(config.get_tz(cfg)).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End the current cortex wake")
    parser.add_argument("--force-slept", default=None,
                        help="mark a proxy lie-down (timeout|fuse|stale)")
    parser.add_argument("--rotate", action="store_true",
                        help="respawn a fresh window on the next wake")
    parser.add_argument("--next-wake-min", type=float, required=True,
                        help="minutes until the next internal wake (required, "
                             "clamped to [0, next_wake_max])")
    parser.add_argument("--human-override", action="store_true",
                        help="explicit ctl minutes pass unclamped")
    args = parser.parse_args(argv)
    cfg = config.load()
    result = lie_down(cfg, force_slept=args.force_slept, rotate=args.rotate,
                      next_wake_min=args.next_wake_min,
                      human_override=args.human_override)
    print(json.dumps(result, ensure_ascii=False))  # surface next_wake harmlessly
    return 0


if __name__ == "__main__":
    sys.exit(main())
