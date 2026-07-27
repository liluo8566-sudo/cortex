"""Ledger reconcile: the decision body the wake daemon runs every cadence tick.

Relocated verbatim from the retired pacemaker_tick entry point (T11 P4) —
DND hold, manual adopt, dead+due fire, accidental-close resume, watchdog heal,
silence backup, stale-suspect debounce. No process entry point lives here; the
daemon owns the loop (cortex/daemon.py).
"""
from __future__ import annotations

import sys
import time

from cortex import config, db, occupancy, transcript, wake_state

# A suspect marker older than this (minutes) is treated as absent even if its
# gen still matches -- caps how long a single stale osascript hiccup can keep
# counting toward the confirm threshold.
_SUSPECT_TTL_MIN = 10


def _handle_awake(conn, cfg: dict, st: dict, snap_gen: int | None = None) -> str:
    """A wake is in progress -> the awake gate: NEVER emit a wake signal while
    awake (the alarm stops once up). Instead run the silence check as a watchdog
    backup, so a dead/rebooted watchdog is not a blind spot. Falls back to the
    stale reap only when the silence check held (cycle not yet elapsed) yet the
    transcript is long idle.

    `snap_gen` = the gen captured in the reconcile's opening snapshot. Before any
    consequential reap, re-validate it against the live epoch: a lie_down / user
    reset since the snapshot means the awake this pass saw is stale (BUG B) —
    hold rather than act on a superseded snapshot."""
    from cortex.watchdog import silence_action
    if not _snapshot_awake_current(cfg, snap_gen):
        return "awake gate: snapshot superseded (gen moved) -> hold"
    # Watchdog-liveness heal (permanent-residency invariant): an awake window
    # must always have a live watchdog (per-wake poll + fuse). If the recorded
    # watchdog pid is dead (crash / reboot), respawn one now — this pass is the
    # backup, but the watchdog owns exact-time fuse + 60s silence polling.
    # Idempotent via watchdog.spawn's own singleton guard (a live pid = no-op).
    from cortex.wake import _window_alive
    if _window_alive(cfg):
        from cortex import watchdog
        if not watchdog._pid_alive(watchdog._recorded_watchdog_pid(cfg)):
            watchdog.spawn(cfg)
    mt = transcript.mtime(cfg)
    # Silence source for the awake gate = minutes since the last REAL user
    # message (assistant / system / ear injections don't reset it). None = 0.0 =
    # hold, matching watchdog.run. `idle` (file mtime) still drives the stale-reap
    # below (window liveness, not user silence); 1e9 when the transcript is gone.
    idle = (time.time() - mt) / 60.0 if mt else 1e9
    action = silence_action(cfg, transcript.user_silent_min(cfg) or 0.0)
    if action and not wake_state.load(cfg).get("awake"):
        return f"awake gate: {action} (idle {idle:.0f}min)"
    stale_min = float(cfg["wake"].get("stale", {}).get("threshold_min", 15))
    if idle >= stale_min:
        # Alive-but-quiet is normal (user reading/typing): transcript mtime is
        # not a liveness signal. Only reap when the resident window is actually
        # gone. Live-but-silent windows are handled by the silence tier above.
        from cortex.wake import _window_alive
        if _window_alive(cfg):
            if wake_state.load(cfg).get("stale_suspect"):
                wake_state.update(cfg, stale_suspect=None)
            return f"stale hold: window alive (idle {idle:.0f}min)"
        # Re-validate the snapshot epoch right before the reap: a user reset /
        # lie_down since the snapshot must cancel this stale-reap (fail closed).
        if not _snapshot_awake_current(cfg, snap_gen):
            return "stale hold: snapshot superseded (gen moved)"
        # Debounce: a single dead verdict can be a transient osascript hiccup.
        # Require `confirm_ticks` CONSECUTIVE dead verdicts (same snapshot gen,
        # within the suspect TTL) before reaping.
        confirm = int(cfg["wake"].get("stale", {}).get("confirm_ticks", 2))
        n = _stale_suspect_count(cfg, snap_gen) + 1
        if n < confirm:
            wake_state.update(cfg, stale_suspect={
                "ts": db.utcnow_iso(), "gen": snap_gen, "count": n})
            return (f"stale suspect: window dead ({n}/{confirm}) "
                    "-> hold for confirmation")
        wake_state.update(cfg, stale_suspect=None)
        from cortex import lie_down as lie_down_mod
        r = lie_down_mod.lie_down(cfg, force_slept="stale")
        sys.stderr.write(
            f"[cortex] STALE WAKE reaped: idle={idle:.1f}min tokens={r['tokens']}\n")
        return f"stale wake reaped (idle {idle:.0f}min) -> proxy lie_down"
    if action:
        return f"awake gate: {action} (idle {idle:.0f}min)"
    return f"wake in progress (idle {idle:.0f}min) -> tick skipped"


def _snapshot_awake_current(cfg: dict, snap_gen: int | None) -> bool:
    """True if the opening snapshot is still authoritative: the live epoch gen
    has not moved since the snapshot. snap_gen=None (legacy state with no gen)
    -> True (no epoch to compare, behave as before). Fail closed: a lock/parse
    failure reads as NOT current, so a doubtful reap is held."""
    if snap_gen is None:
        return True
    try:
        gen, _sid = wake_state.current_epoch(cfg)
    except wake_state.StateValidationError:
        return False
    return gen == snap_gen


def _stale_suspect_count(cfg: dict, snap_gen: int | None) -> int:
    """Prior consecutive dead-verdict count from the persisted marker, or 0 if
    absent/malformed/gen-mismatched/expired. A marker whose gen no longer
    matches the current snapshot's gen is a stale carry-over from a superseded
    epoch (user message / lie_down bumps gen) -> treated as absent (fresh
    first strike), not accumulated."""
    from datetime import datetime, timezone
    marker = wake_state.load(cfg).get("stale_suspect")
    if not isinstance(marker, dict):
        return 0
    if marker.get("gen") != snap_gen:
        return 0
    ts_raw = marker.get("ts")
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 0
    if age_min > _SUSPECT_TTL_MIN:
        return 0
    try:
        return int(marker.get("count", 0))
    except (TypeError, ValueError):
        return 0


def _parse_local(iso: str | None, cfg: dict):
    from datetime import datetime
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    tz = config.get_tz(cfg)
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def _fire_dead_window(conn, cfg: dict, why: str) -> str:
    """A dead resident window whose ledger is due (or an accidental close) needs
    firing NOW. Reuse the tested wake path: run_wake's _window_wake_plan reads the
    rotate flag itself — rotated -> fresh spawn (handoff), else -> resume the
    recorded session. dry_run short-circuits to a log-only re-arm.

    Every branch here handled the due ledger entry -> it must be consumed
    (cleared or replaced with the freshly booked next wake), else the stale
    next_wake_at stays due and reconcile re-fires it again every cadence."""
    from cortex.wake import run_wake
    now = occupancy._now(cfg)
    if bool(cfg["pacemaker"].get("dry_run", True)):
        _rearm_next_wake(conn, cfg)
        return f"reconcile ({why}) -> dry_run, next wake re-armed only"
    decision = {"wake": True, "reasons": [], "gated_by": [],
                "wake_reasons": "reconcile",
                "explanation": f"{now.strftime('%H:%M')} reconcile: {why}"}
    result = run_wake(conn, cfg, decision, now=now)
    if result.get("mode") != "window":
        _rearm_next_wake(conn, cfg)
    return f"reconcile ({why}) -> wake fired (mode={result.get('mode')})"


def _rearm_next_wake(conn, cfg: dict) -> None:
    """Book the default next wake and write it to the durable ledger — the
    consumed alarm is replaced, never silently lost."""
    next_at = occupancy.lie_down(conn, cfg)
    wake_state.set_next_wake_at(cfg, next_at.isoformat() if next_at else None)


def _adopt_manual_window(cfg: dict) -> str | None:
    """Auto-adopt a cortex window the user opened `claude` in herself (in
    cortex_home) but never registered — so this pass treats it as the live
    resident instead of firing/spawning a duplicate. Runs INSIDE the shared spawn
    lock (wake._spawn_serialized) so it never races an actual spawn. Config-gated
    ([wake].auto_adopt, default on).

    Re-check liveness under the lock first (a spawn may have landed a resident
    between the caller's check and the lock). Then scan iTerm for an adoptable
    window (window.find_adoptable_window: interactive `claude` in cortex_home,
    newest start wins; headless `claude -p` excluded by construction). Adopt via
    the SAME atomic CAS the spawn path uses (wake_state.set_awake with the live
    epoch token, bump=False, session_id + claude transcript sid committed
    together) so a concurrent lie_down/reset cannot be overwritten. Returns a log
    line on adoption, else None (no candidate / adoption CAS lost / disabled)."""
    from cortex import wake, window
    if not bool(cfg["wake"].get("auto_adopt", True)):
        return None
    with wake._spawn_serialized(cfg):
        if wake._window_alive(cfg):
            return None  # a resident landed under the lock -> nothing to adopt
        sid = window.find_adoptable_window(cfg)
        if not sid:
            return None
        claude_sid = window.claude_session_id(cfg)
        transcript_path = None
        if claude_sid:
            transcript_path = str(transcript.transcript_dir(cfg) / f"{claude_sid}.jsonl")
        try:
            token = wake_state.current_epoch(cfg)
        except wake_state.StateValidationError:
            return None
        new_epoch = wake_state.set_awake(
            cfg, None, transcript_path, expected_token=token, bump=False,
            session_id=sid)
        if new_epoch is None:
            return None  # a newer epoch superseded between capture and commit
        wake_state.wake_audit(cfg, "adopt_manual_window", sid,
                              f"claude_sid={claude_sid}")
        return f"adopted manual window {sid} (claude_sid={claude_sid})"


def _reconcile(conn, cfg: dict, st: dict, now) -> str | None:
    """Ledger reconcile (runs every cadence pass). Returns a log line when it
    acts / short-circuits the rest of the pass, else None (let the normal flow
    proceed). HARD RULE: an ALIVE recorded session is never touched here.

      - breaker held                             -> hold everything.
      - window ALIVE                             -> None (normal flow / awake gate).
      - window dead + next_wake_at in the past   -> fire now (rotated?fresh:resume).
      - window dead + awake + no next_wake_at    -> accidental close -> resume now.
      - window dead + ASLEEP + next_wake_at in the future -> SILENT resume
        (reopen the same conversation, no bell, no awake flip, no ledger
        change), then hold: the cadence still fires the wake at due time.
      - window dead + next_wake_at in the future -> hold (the cadence catches it
        at due time; the ledger is the source of truth, a re-arm would only
        duplicate the same fire). This hold is authoritative: it short-circuits
        the caller so no other wake path can fire early while a future ledger
        alarm exists (e.g. right after `ctl sleep --min 30`)."""
    from cortex.wake import _window_alive

    from cortex import breaker
    if breaker.holds(cfg, config.shell_id(cfg)):
        return breaker.held_line(cfg, "reconcile + reaps + injections held")
    if _window_alive(cfg):
        return None  # alive -> never touch; normal flow handles it
    # Before ANY dead-window fire/spawn: adopt a window the user opened herself.
    # A hit records it as the resident under the spawn lock -> treat as alive
    # this pass (no fire, no spawn), so she never re-registers her window.
    adopted = _adopt_manual_window(cfg)
    if adopted is not None:
        return adopted
    due = _parse_local(wake_state.get_next_wake_at(cfg), cfg)
    if due is not None and now >= due:
        return _fire_dead_window(conn, cfg, "ledger due, window dead")
    if st.get("awake") and due is None and wake_state.get_session_id(cfg):
        # An awake session whose window was closed with no scheduled wake: resume
        # immediately (1h prompt-cache tier — resume within ~5 min keeps it hot).
        return _fire_dead_window(conn, cfg, "accidental close of awake window")
    if due is not None:
        # Dead window, ledger not yet due. The ledger is authoritative — no wake
        # fires early — but the window itself is reopened SILENTLY (resume, no
        # bell, no awake flip, no ledger change) so the shell keeps sleeping in
        # a live window instead of a closed one. Awake+dead+future-ledger is NOT
        # this case and still just holds.
        if not st.get("awake"):
            from cortex.wake import resume_asleep
            resumed = resume_asleep(cfg)
            if resumed:
                return f"{resumed}; next wake {due.strftime('%H:%M')}"
        return f"ledger hold: next wake {due.strftime('%H:%M')}, window dead"
    return None
