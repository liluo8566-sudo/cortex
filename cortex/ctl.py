"""cortex.ctl — manual control CLI. Thin wrappers over the same wake/lie_down/
ledger paths the wake daemon uses, so a human can drive the resident window by
hand without racing the reconcile.

  wake            clear the circuit breaker, then wake immediately via the
                  standard run_wake pipeline (alive resident -> ear signal;
                  dead -> rotated?fresh:resume)
  sleep           awake resident -> inject a lie_down instruction; else
                  (dead, or alive-but-dormant) set the ledger directly
  pause           throw the circuit breaker: stop cortex autonomous activity
                  (auto wake / spawn / fed round) for all shells, or one shell
                  with --shell. Persistent across restarts.
  resume          release the breaker without waking — all shells, or one
                  shell with --shell (scope "all" narrows to the other shell)
  status          print the breaker + ledger state

Each subcommand prints one human-readable result line.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from cortex import breaker, config, db, occupancy, wake_state, window


def _now(cfg: dict) -> datetime:
    return datetime.now(config.get_tz(cfg))


def cmd_wake(cfg: dict) -> str:
    from cortex.wake import run_wake, _window_alive
    # A human explicitly waking wants activity back — clear the WHOLE breaker
    # first (both shells, auto or manual). No silent bypass: the state file
    # never disagrees with what is actually running.
    released = breaker.release(cfg)
    prefix = "breaker cleared; " if released else ""
    # Already-on-duty guard (singleton invariant): a resident window that is both
    # alive AND awake is already on duty — re-driving run_wake would re-set_awake
    # and spawn a second watchdog. The live session already has the human's
    # attention; refuse rather than double-activate. (Alive-but-dormant still
    # wakes: that is the intended ear path below.)
    if _window_alive(cfg) and wake_state.is_awake(cfg):
        return f"{prefix}wake: already awake on duty -> no-op (one resident)"
    # Always drive the standard wake pipeline (run_wake -> _window_wake_plan
    # + _window_wake), including the alive-resident ear path: it renders a
    # fresh note, sets the awake marker and starts the watchdog, and alerts +
    # gives up the round on any AppleScript failure. Do not re-implement any
    # of that here — a hand-rolled signal-only path would skip set_awake and
    # the watchdog, letting the next tick double-wake and the eventual
    # lie_down hit claim_lie_down's "not awake" no-op.
    conn = db.connect(cfg)
    try:
        now = _now(cfg)
        decision = {"wake": True, "reasons": [], "gated_by": [],
                    "wake_reasons": "ctl",
                    "explanation": f"{now.strftime('%H:%M')} manual ctl wake"}
        result = run_wake(conn, cfg, decision, now=now)
        if result.get("mode") != "window":
            next_at = occupancy.lie_down(conn, cfg)
            wake_state.set_next_wake_at(
                cfg, next_at.isoformat() if next_at else None)
        rotated = "fresh" if wake_state.load(cfg).get("rotated") else "resume/spawn"
        return f"{prefix}wake: {rotated} (mode={result.get('mode')})"
    finally:
        conn.close()


def _resolve_minutes(cfg: dict, until: str | None, minutes: float | None) -> float:
    if until:
        hh, mm = until.split(":")
        now = _now(cfg)
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return max(1.0, (target - now).total_seconds() / 60.0)
    return float(minutes) if minutes is not None else 30.0


def cmd_sleep(cfg: dict, until: str | None, minutes: float | None, rotate: bool) -> str:
    mins = _resolve_minutes(cfg, until, minutes)
    # Gate on the awake marker, not window liveness: a resident window is
    # commonly alive-but-dormant (asleep, no wake in progress). Injecting a
    # lie_down prompt then hits claim_lie_down's "not awake" no-op and the
    # requested minutes/rotate are silently dropped.
    if wake_state.load(cfg).get("awake"):
        # Covert delivery: only the "⚙️ [CTL] mins=N rotate=B" marker line reaches
        # the window (typed directly — Monitor retired, T11 P3).
        # The full sleep instruction body is injected invisibly by the marrow hook
        # ([cortex].ctl_sleep_text), rendered from the mins/rotate args this line
        # carries — she never SEES the instruction, only the short marker.
        marker = str(cfg["wake"].get("ctl_sleep_marker") or "⚙️ [CTL]").strip()
        # human=true: an explicit ctl minutes choice, so the rendered lie_down
        # passes it unclamped (marrow ctl_sleep_text -> lie_down human_override).
        marker_line = (f"{marker} mins={int(mins)} "
                       f"rotate={'true' if rotate else 'false'} human=true")
        rung = window.deliver_covert_marker(cfg, marker_line)
        if rung != "none":
            return (f"sleep: instruction delivered ({rung}) "
                    f"(next_wake_min={int(mins)}, rotate={rotate})")
        return "sleep: no resident window to inject into"
    # Not awake (dead window, or alive-but-dormant): set the ledger directly
    # so the next reconcile/tick fires it.
    due = _now(cfg) + timedelta(minutes=mins)
    wake_state.set_next_wake_at(cfg, due.isoformat())
    if rotate:
        wake_state.set_rotated(cfg)
    return f"sleep: ledger set for {due.strftime('%H:%M')} (rotate={rotate})"


def _receipt(cfg: dict, message: str) -> None:
    """Best-effort user-facing receipt for a manual breaker change: one pending
    outbox note the tg bridge delivers. NO alert row — a manual pause is not an
    incident (only an auto trip writes one)."""
    try:
        conn = db.connect(cfg)
    except Exception:  # noqa: BLE001
        return
    try:
        conn.execute(
            "INSERT INTO outbox (from_sid, from_channel, target, body)"
            " VALUES (?, ?, ?, ?)",
            (None, "cortex", "tg", message),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — a missing receipt must not fail the pause
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def cmd_pause(cfg: dict, shell: str | None = None) -> str:
    """Throw the breaker. Default scope "all" (both shells); --shell cli|tg
    holds one. Persistent: only ct-wake / ctl resume releases it."""
    scope = (shell or breaker.SCOPE_ALL).strip().lower()
    state = breaker.pause(cfg, scope)
    # Silent: a manual pause is a deliberate human action already known to the
    # caller — no tg receipt. Only an auto trip (watchdog fuse) announces on
    # tg and writes an alert row (see breaker.trip_message / watchdog._fuse).
    extra = ""
    # Put the live cli window down through the SAME proxy path the watchdog fuse
    # uses (lie_down -> clears awake, kills the watchdog). book_alarm=False: a
    # pause is a pure stop, it books NO next wake — only a manual ct-wake
    # resumes, and that fires a round immediately.
    if scope in (breaker.SCOPE_ALL, "cli") and wake_state.load(cfg).get("awake"):
        from cortex import lie_down as lie_down_mod
        try:
            lie_down_mod.lie_down(cfg, force_slept="ct-pause", book_alarm=False)
            extra = "; live cli window put down"
        except Exception as e:  # noqa: BLE001 — the breaker stands regardless
            extra = f"; live cli window still up ({e})"
    return (f"pause: breaker ON scope={state['scope']} — cortex autonomous "
            f"activity held until ct-wake{extra}")


def cmd_resume(cfg: dict, shell: str | None = None) -> str:
    """Release the breaker without waking. Default clears every shell; --shell
    releases one half (scope "all" narrows to the other shell)."""
    released = breaker.release(cfg, shell)
    st = breaker.state(cfg)
    if not released:
        if shell and st is not None:
            return (f"resume: breaker holds scope={st['scope']} only — "
                    f"{shell} was not held")
        return "resume: breaker already clear — nothing held"
    # The receipt lands on tg, so only announce a clear that actually frees tg.
    if shell is None or shell == "tg":
        settings = breaker.settings(config.marrow_config_dir(cfg))
        _receipt(cfg, str(settings["clear_message"]))
    if st is not None:
        return (f"resume: breaker OFF for {shell} — still ON scope={st['scope']}")
    return "resume: breaker OFF — overdue ledger alarms fire on the next reconcile"


def cmd_status(cfg: dict) -> str:
    st = breaker.state(cfg)
    if st is None:
        line = "breaker: clear"
    else:
        line = (f"breaker: ON scope={st['scope']} reason={st['reason']} "
                f"since={st['ts']}")
    d = wake_state.load(cfg)
    return (f"{line} | awake={bool(d.get('awake'))} "
            f"next_wake_at={d.get('next_wake_at') or '-'} "
            f"rotated={bool(d.get('rotated'))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cortex.ctl", description="Manual cortex control")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wake", help="wake the resident window now (ear signal / resume / fresh)")
    sp = sub.add_parser("sleep", help="lie the live window down, or set the ledger")
    sp.add_argument("--until", default=None, help="wake at HH:MM (local)")
    sp.add_argument("--min", dest="minutes", type=float, default=None,
                    help="minutes until next wake")
    sp.add_argument("--rotate", action="store_true", help="respawn fresh next wake")
    pp = sub.add_parser("pause", help="breaker ON — stop cortex autonomous activity")
    pp.add_argument("--shell", default=None, choices=["cli", "tg"],
                    help="hold ONE shell only (default: all)")
    rp = sub.add_parser("resume", help="breaker OFF (without waking)")
    rp.add_argument("--shell", default=None, choices=["cli", "tg"],
                    help="release ONE shell only (default: all)")
    sub.add_parser("status", help="breaker + ledger state")
    args = parser.parse_args(argv)

    cfg = config.load()
    if args.cmd == "wake":
        line = cmd_wake(cfg)
    elif args.cmd == "sleep":
        line = cmd_sleep(cfg, args.until, args.minutes, args.rotate)
    elif args.cmd == "pause":
        line = cmd_pause(cfg, args.shell)
    elif args.cmd == "status":
        line = cmd_status(cfg)
    else:
        line = cmd_resume(cfg, args.shell)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
