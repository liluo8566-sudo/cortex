"""cli wake daemon: the timing loop for this shell, hosted on synapse_core's
Scheduler instead of a launchd cron tick.

Two logical deadlines, one per scheduler shell key (the scheduler holds ONE
deadline per key, and firing consumes it):

  <shell>.reconcile — fixed cadence, self-rearming. Runs the ledger reconcile
      (cortex.reconcile): DND hold, manual adopt, dead+due fire,
      accidental-close resume, watchdog heal, silence backup, stale-suspect
      debounce.
  <shell>            — business: the armed alarm (next_wake_at) when there is
      one, else the silence-round due. NEVER min()'d: the idle cycle does not
      tick while an alarm stands, and the idle window is a MINIMUM interval
      (never fire early, never re-fire on an interrupted delivery). ALWAYS
      armed (safety horizon when nothing is pending) because Scheduler.kick()
      no-ops on a key with no entry, and the lie_down socket kick sends this
      key. A kick just fires the callback early; it recomputes from state.

Every callback re-arms its key on entry AND in a finally, so neither a consumed
entry nor a raised exception can leave the daemon alive-but-unarmed.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from synapse_core.scheduler import Scheduler

from cortex import (
    breaker, config, db, occupancy, reconcile, transcript, wake_state,
)

RECONCILE_SUFFIX = ".reconcile"


def _dcfg(cfg: dict) -> dict:
    return cfg.get("daemon") or {}


def _age_min(ts_iso) -> float:
    try:
        ts = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 1e9
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def silence_due_in(cfg: dict, st: dict) -> float | None:
    """Seconds until the awake free-round backup is due (0.0 = now), or None when
    the shell is not awake (or an alarm owns the deadline). Mirrors
    watchdog.silence_action's own gate: the cycle elapses silent_max_min after the
    last real user message (awake_since when the user never spoke this wake) AND
    at least silent_max_min after the last injection. A pending kick round —
    an explicit external request, not idle — is due immediately."""
    if not st.get("awake"):
        return None
    if st.get("kick_round"):
        return 0.0
    if st.get("next_wake_at"):
        return None  # mutual exclusion: an armed alarm owns the deadline, idle
        #              does not tick underneath it
    silent_max = float(cfg["wake"].get("watchdog", {}).get("silent_max_min", 20))
    silent_min = wake_state.silence_basis_min(cfg, transcript.user_silent_min(cfg))
    wait = silent_max - silent_min
    last = st.get("tuck_pending")
    if last:
        wait = max(wait, silent_max - _age_min(last))
    return max(0.0, wait) * 60.0


def business_reason(cfg: dict, st: dict, now: datetime) -> str | None:
    """What (if anything) the business deadline owes right now.

      awake      -> "silence" once the free-round cycle has elapsed, else None
                    (the awake gate: never emit a wake signal while awake).
      asleep     -> "next_wake_at" when the durable ledger is due;
                    "kick" when kick reasons are queued and no ledger is armed
                    (note.py consumes the reasons on the wake it triggers).
    """
    if st.get("awake"):
        due_in = silence_due_in(cfg, st)
        return "silence" if due_in is not None and due_in <= 0 else None
    due = reconcile._parse_local(wake_state.get_next_wake_at(cfg), cfg)
    if due is not None:
        return "next_wake_at" if now >= due else None
    return "kick" if st.get("kick_reasons") else None


class WakeDaemon:
    def __init__(self, cfg: dict, *, scheduler: Scheduler | None = None,
                 clock=None) -> None:
        d = _dcfg(cfg)
        self.cfg = cfg
        self.shell = str(d.get("shell") or "cli")
        self.reconcile_shell = self.shell + RECONCILE_SUFFIX
        self.reconcile_interval = float(d.get("reconcile_interval_sec", 60))
        self.horizon = float(d.get("safety_horizon_sec", 300))
        self.retry = float(d.get("retry_interval_sec", 60))
        self._clock = clock or time.time
        self.scheduler = scheduler or Scheduler(
            socket_path=config.daemon_socket_path(cfg),
            safety_tick=self.reconcile_interval,
            clock=self._clock,
        )
        self._work = asyncio.Lock()

    # --- arming ---------------------------------------------------------

    def arm(self) -> None:
        self._reset_overdue_silence()
        now = self._clock()
        self.scheduler.schedule(self.reconcile_shell,
                                now + self.reconcile_interval, self._on_reconcile)
        self.scheduler.schedule(self.shell, self._next_business_at(now),
                                self._on_business)

    def _reset_overdue_silence(self) -> None:
        """Starting up must never itself deliver a free round. The idle window is
        a MINIMUM interval, so a cycle that expired while the daemon was down owes
        nothing — re-arm it from now and fire only after a full fresh window. A
        pending kick round is an explicit external request and is left alone."""
        try:
            st = wake_state.load(self.cfg)
            if not st.get("awake") or st.get("kick_round"):
                return
            due_in = silence_due_in(self.cfg, st)
            if due_in is not None and due_in <= 0:
                wake_state.stamp_silence_basis(self.cfg)
        except Exception:  # noqa: BLE001 — a state read must never block startup
            return

    def _arm_reconcile(self) -> None:
        self.scheduler.schedule(self.reconcile_shell,
                                self._clock() + self.reconcile_interval,
                                self._on_reconcile)

    def _arm_business(self, at: float) -> None:
        self.scheduler.schedule(self.shell, at, self._on_business)

    def _next_business_at(self, now: float) -> float:
        """The pending business deadline; safety horizon when idle. An armed alarm
        IS the deadline — never min()'d with the idle cycle, which does not tick
        while an alarm stands. A target already in the past means the fire was
        held (breaker / gated / failed) — retry on the retry interval rather than
        spinning."""
        try:
            st = wake_state.load(self.cfg)
            due = reconcile._parse_local(
                wake_state.get_next_wake_at(self.cfg), self.cfg)
            if due is not None:
                target = due.timestamp()
            elif st.get("awake"):
                due_in = silence_due_in(self.cfg, st)
                target = now + due_in if due_in is not None else now + self.horizon
            else:
                target = now + self.horizon
        except Exception:  # noqa: BLE001 — state read must never unarm the daemon
            target = now + self.horizon
        return target if target > now else now + self.retry

    # --- callbacks ------------------------------------------------------

    async def _on_reconcile(self, shell: str) -> None:
        self._arm_reconcile()  # keep the key armed while the body runs
        try:
            async with self._work:
                self._emit(await asyncio.to_thread(self.reconcile_once))
        finally:
            self._arm_reconcile()

    async def _on_business(self, shell: str) -> None:
        self._arm_business(self._clock() + self.horizon)
        try:
            async with self._work:
                self._emit(await asyncio.to_thread(self.business_once))
        finally:
            self._arm_business(self._next_business_at(self._clock()))

    def _emit(self, line: str | None) -> None:
        if line:
            print(f"{db.utcnow_iso()} {line}", flush=True)

    # --- work (blocking; runs in a worker thread) ------------------------

    def reconcile_once(self) -> str:
        """Ledger reconcile: the durable alarm ledger is the only wake source
        (the old trigger decision engine is retired)."""
        cfg = self.cfg
        if not config.shell_enabled(cfg, self.shell):
            return f"{self.shell} shell off (marrow [cortex].shells): reconcile skipped"
        conn = db.connect(cfg)
        try:
            st = wake_state.load(cfg)
            snap_gen = st.get("gen") if isinstance(st.get("gen"), int) else None
            if breaker.holds(cfg, self.shell):
                return breaker.held_line(cfg, "reconcile + reaps + injections held")
            rc = reconcile._reconcile(conn, cfg, st, occupancy._now(cfg))
            if rc is not None:
                return rc
            if st.get("awake"):
                return reconcile._handle_awake(conn, cfg, st, snap_gen=snap_gen)
            return "reconcile: nothing due"
        finally:
            conn.close()

    def business_once(self) -> str:
        cfg = self.cfg
        if not config.shell_enabled(cfg, self.shell):
            return f"{self.shell} shell off (marrow [cortex].shells): business skipped"
        if breaker.holds(cfg, self.shell):
            # Held BEFORE _fire_wake consumes the alarm, so next_wake_at stands
            # and fires on the first pass after the breaker clears.
            return breaker.held_line(cfg, "business deadline held")
        st = wake_state.load(cfg)
        now = occupancy._now(cfg)
        reason = business_reason(cfg, st, now)
        if reason is None:
            return "business: nothing due"
        if reason == "silence":
            from cortex.watchdog import silence_action
            action = silence_action(cfg, transcript.user_silent_min(cfg) or 0.0)
            return f"business silence: {action or 'held'}"
        return self._fire_wake(cfg, reason, now)

    def _fire_wake(self, cfg: dict, reason: str, now: datetime) -> str:
        """Synthesized decision -> the standard wake pipeline (ctl precedent).
        ANY outcome other than a live window — mode="failed", dry_run, or a
        raised exception — books the next wake again, so the alarm consumed at
        fire time is never silently lost.

        Ledger-before-delivery: the alarm is consumed the moment this fire is
        decided, not after set_awake has verified the bell landed. Delivery can be
        interrupted (esc) or missed, and an un-consumed alarm is still due — the
        next deadline would re-fire it seconds later. One alarm = one fire; the
        next one is armed by the re-arm below."""
        from cortex.wake import run_wake
        if reason == "next_wake_at":
            wake_state.clear_next_wake_at(cfg)
        decision = {"wake": True, "reasons": [], "gated_by": [],
                    "wake_reasons": reason,
                    "explanation": f"{now.strftime('%H:%M')} daemon wake: {reason}"}
        conn = db.connect(cfg)
        try:
            if bool(cfg["pacemaker"].get("dry_run", True)):
                self._rearm_next_wake(conn, cfg)
                return f"business wake ({reason}) -> dry_run, next wake re-armed only"
            try:
                mode = run_wake(conn, cfg, decision, now=now).get("mode")
            except Exception as e:  # noqa: BLE001 — a crashed round must still re-arm
                traceback.print_exc()
                mode = f"error:{type(e).__name__}"
            if mode != "window":
                self._rearm_next_wake(conn, cfg)
            return f"business wake ({reason}) -> mode={mode}"
        finally:
            conn.close()

    @staticmethod
    def _rearm_next_wake(conn, cfg: dict) -> None:
        """Book the default next wake and write it to the durable ledger."""
        next_at = occupancy.lie_down(conn, cfg)
        wake_state.set_next_wake_at(
            cfg, next_at.isoformat() if next_at else None)


# --- process entry ------------------------------------------------------

def lock_path(cfg: dict) -> Path:
    raw = str(_dcfg(cfg).get("lock_path") or "").strip()
    return Path(raw).expanduser() if raw else config.state_dir(cfg) / "daemon.lock"


@contextlib.contextmanager
def singleton(cfg: dict):
    """Yield True when this process owns the daemon lock, False when another
    instance already holds it (the caller then exits cleanly)."""
    p = lock_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


async def _run(cfg: dict) -> int:
    daemon = WakeDaemon(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon.scheduler.stop)
    daemon.arm()
    await daemon.scheduler.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    shell = str(_dcfg(cfg).get("shell") or "cli")
    if not bool(_dcfg(cfg).get("enabled", True)):
        print("daemon disabled ([daemon].enabled)", flush=True)
        return 0
    if not config.shell_enabled(cfg, shell):
        print(f"{shell} shell off (marrow [cortex].shells): daemon not started", flush=True)
        return 0
    with singleton(cfg) as owned:
        if not owned:
            print("daemon already running (lock held)", flush=True)
            return 0
        return asyncio.run(_run(cfg))


if __name__ == "__main__":
    sys.exit(main())
