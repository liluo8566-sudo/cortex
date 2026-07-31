"""cortex.ctl — manual control CLI. Thin wrappers over the same wake/lie_down/
ledger paths the wake daemon uses, so a human can drive the resident window by
hand without racing the reconcile.

  wake            release the circuit breaker for --shell (cli|tg|all,
                  default all), then wake THAT shell now: cli through the
                  standard run_wake pipeline (alive resident -> ear signal;
                  dead -> rotated?fresh:resume), tg by booking a due-now
                  next_wake_at in its ledger and kicking its host socket
  sleep           awake resident -> inject a lie_down instruction; else
                  (dead, or alive-but-dormant) set the ledger directly
  pause           throw the circuit breaker: stop cortex autonomous activity
                  (auto wake / spawn / fed round) for all shells, or one shell
                  with --shell. Scopes MERGE (cli then tg -> all). Persistent
                  across restarts.
  resume          release the breaker without waking — all shells, or one
                  shell with --shell (scope "all" narrows to the other shell).
                  A cli release with an empty ledger books a due-now alarm so
                  the next reconcile pulls the shell up.
  duty            rotate duty to cli|tg|off|all: clear the breaker, then hold
                  the off-duty shell and wake the on-duty one
  status          print the breaker + duty + ledger state

Each subcommand prints one human-readable result line.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from cortex import (breaker, config, db, duty, occupancy, shell_ledger,
                    wake_state, window)

TG_SHELL = "tg"


def _now(cfg: dict) -> datetime:
    return datetime.now(config.get_tz(cfg))


def cmd_wake(cfg: dict, shell: str = breaker.SCOPE_ALL) -> str:
    """Release the breaker for `shell` and wake it NOW. Default "all" clears
    the whole file (both shells, auto or manual) and kicks both — no silent
    bypass: the state file never disagrees with what is actually running."""
    target = (shell or breaker.SCOPE_ALL).strip().lower()
    released = breaker.release(
        cfg, None if target == breaker.SCOPE_ALL else target)
    prefix = "breaker cleared; " if released else ""
    parts = []
    # tg first: its kick is a socket write, while the cli pipeline can spend
    # seconds in AppleScript before returning.
    if target in (breaker.SCOPE_ALL, TG_SHELL):
        parts.append(_wake_tg(cfg))
    if target in (breaker.SCOPE_ALL, "cli"):
        parts.append(_wake_cli(cfg))
    return prefix + " | ".join(parts)


def _kick_shell_host(cfg: dict, shell: str) -> bool:
    """Poke a shell host's scheduler over its unix socket (the same one-line
    wire format synapse_core.scheduler.send_kick / Scheduler._on_conn speak, as
    used for the cli daemon in cortex.kick). Best effort: a host that is down
    just reads its ledger on the next recompute tick."""
    import asyncio

    from synapse_core.scheduler import send_kick

    try:
        p = config.shell_socket_path(cfg, shell)
    except ValueError:
        return False
    if p is None:
        return False
    try:
        asyncio.run(send_kick(p, shell))
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _wake_tg(cfg: dict, rotate: bool = False) -> str:
    """Book a due-now wake in the tg ledger, then kick its host. The ledger
    write is the durable half — the host claims it whenever it next reads
    (kick, safety tick, or bridge start), so the kick itself is best effort.
    `rotate` rides along in the same booking: the host ends its window and
    respawns instead of feeding the round into the old session."""
    booking = {"next_wake_at": _now(cfg).isoformat()}
    if rotate:
        booking["rotate_pending"] = True
    shell_ledger.write(config.shell_state_dir(cfg), TG_SHELL, booking)
    if _kick_shell_host(cfg, TG_SHELL):
        return "wake tg: round due now, host kicked"
    return "wake tg: round due now, host unreachable — fires on its next pass"


def _wake_cli(cfg: dict) -> str:
    from cortex.wake import run_wake, _window_alive
    # Already-on-duty guard (singleton invariant): a resident window that is both
    # alive AND awake is already on duty — re-driving run_wake would re-set_awake
    # and spawn a second watchdog. The live session already has the human's
    # attention; refuse rather than double-activate. (Alive-but-dormant still
    # wakes: that is the intended ear path below.)
    if _window_alive(cfg) and wake_state.is_awake(cfg):
        return "wake cli: already awake on duty -> no-op (one resident)"
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
        return f"wake cli: {rotated} (mode={result.get('mode')})"
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


def cmd_pause(cfg: dict, shell: str | None = None) -> str:
    """Throw the breaker. Default scope "all" (both shells); --shell cli|tg
    holds one, MERGING with whatever already stands (cli then tg -> all), so a
    second pause never releases the shell the first one holds. Persistent: only
    ctl wake / ctl resume / ct-duty releases it."""
    scope = (shell or breaker.SCOPE_ALL).strip().lower()
    state = breaker.pause(cfg, scope)
    # Silent: a manual pause is a deliberate human action already known to the
    # caller — no tg receipt. Only an auto trip (watchdog fuse) announces on
    # tg and writes an alert row (see breaker.trip_message / watchdog._fuse).
    extra = ""
    # Put the live cli window down through the SAME proxy path the watchdog fuse
    # uses (lie_down -> clears awake, kills the watchdog). book_alarm=False: a
    # pause is a pure stop, it books NO next wake — only a manual release
    # resumes, and that fires a round immediately.
    if scope in (breaker.SCOPE_ALL, "cli") and wake_state.load(cfg).get("awake"):
        from cortex import lie_down as lie_down_mod
        try:
            lie_down_mod.lie_down(cfg, force_slept="ct-pause", book_alarm=False)
            extra = "; live cli window put down"
        except Exception as e:  # noqa: BLE001 — the breaker stands regardless
            extra = f"; live cli window still up ({e})"
    return (f"pause: breaker ON scope={state['scope']} — cortex autonomous "
            f"activity held until ct-duty{extra}")


def _book_cli_alarm(cfg: dict) -> bool:
    """A pause puts the cli window down with NO alarm booked, so a bare
    release would leave the daemon nothing to fire (business_reason returns
    None while asleep with an empty ledger and no kick reasons) — the shell
    would sleep forever. Book one due now instead: the next reconcile pulls it
    up, and resume still means "no immediate kick". Nothing to do when the
    shell is awake or an alarm already stands."""
    d = wake_state.load(cfg)
    if d.get("awake") or d.get("next_wake_at"):
        return False
    wake_state.set_next_wake_at(cfg, _now(cfg).isoformat())
    return True


def cmd_resume(cfg: dict, shell: str | None = None) -> str:
    """Release the breaker without waking. Default clears every shell; --shell
    releases one half (scope "all" narrows to the other shell). tg needs no
    booking of its own — its host falls back to the idle window."""
    released = breaker.release(cfg, shell)
    st = breaker.state(cfg)
    if not released:
        if shell and st is not None:
            return (f"resume: breaker holds scope={st['scope']} only — "
                    f"{shell} was not held")
        return "resume: breaker already clear — nothing held"
    tail = ("; cli alarm booked now"
            if shell in (None, "cli") and _book_cli_alarm(cfg) else "")
    if st is not None:
        return (f"resume: breaker OFF for {shell} — "
                f"still ON scope={st['scope']}{tail}")
    return ("resume: breaker OFF — overdue ledger alarms fire on the next "
            f"reconcile{tail}")


def cmd_duty(cfg: dict, mode: str) -> tuple[str, int]:
    """Rotate duty and act on it now. Validation lives here — duty.write takes
    any string, so an unchecked mode would land as a no-hold state.

    The breaker is cleared mid-transition (apply's after_hold): an explicit duty
    command is the human saying which shell runs, and that outranks a standing
    fuse trip. Clearing it before the new hold is written would leave an instant
    with both enforcement files clear; clearing it after the kicks would let the
    woken shell meet a standing breaker and skip its round. This is the only
    place duty code touches breaker.json."""
    target = str(mode or "").strip().lower()
    if target not in duty.MODES:
        return (f"duty: unknown mode {mode!r} — choose "
                f"{'|'.join(duty.MODES)}", 1)
    cleared: list[bool] = []
    from cortex import wake_source
    r = duty.apply(cfg, target, source=wake_source.KIND_CTL,
                   after_hold=lambda: cleared.append(breaker.release(cfg, None)))
    prefix = "breaker cleared; " if any(cleared) else ""
    woken = ", ".join(f"{s} fresh" if s in r["fresh"] else s
                      for s in r["woken"]) or "-"
    tail = "; live cli window put down" if r["put_down"] else ""
    return (f"{prefix}duty: mode={r['mode']} hold={r['hold'] or '-'} "
            f"woken={woken}{tail}", 0)


def cmd_status(cfg: dict) -> str:
    st = breaker.state(cfg)
    if st is None:
        line = "breaker: clear"
    else:
        line = (f"breaker: ON scope={st['scope']} reason={st['reason']} "
                f"since={st['ts']}")
    duty_state = duty.read(config.marrow_config_dir(cfg))
    d = wake_state.load(cfg)
    return (f"{line} | duty: mode={duty_state['mode']} "
            f"hold={duty_state['hold'] or '-'} "
            f"| awake={bool(d.get('awake'))} "
            f"next_wake_at={d.get('next_wake_at') or '-'} "
            f"rotated={bool(d.get('rotated'))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cortex.ctl", description="Manual cortex control")
    sub = parser.add_subparsers(dest="cmd", required=True)
    wp = sub.add_parser("wake", help="breaker OFF + wake that shell right now")
    wp.add_argument("--shell", default=breaker.SCOPE_ALL,
                    choices=["cli", "tg", breaker.SCOPE_ALL],
                    help="which shell to release and kick (default: all)")
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
    dp = sub.add_parser("duty", help="rotate which shell is on duty")
    dp.add_argument("mode", metavar="{" + ",".join(duty.MODES) + "}",
                    help="cli/tg = that shell only, off = neither, all = both")
    sub.add_parser("status", help="breaker + duty + ledger state")
    args = parser.parse_args(argv)

    cfg = config.load()
    if args.cmd == "duty":
        line, code = cmd_duty(cfg, args.mode)
        print(line)
        return code
    if args.cmd == "wake":
        line = cmd_wake(cfg, args.shell)
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
