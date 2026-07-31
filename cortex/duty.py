"""Duty rotation ("who is on duty") — at most one cortex shell running.

    <marrow config dir>/duty.json
        {"mode": "cli"|"tg"|"off"|"all", "hold": "cli"|"tg"|"all"|null, "ts": iso}
    file absent  = no duty hold
    unreadable   = treated as no hold (logged), never wedges a shell

`mode` is the intent; `hold` is the materialised pause scope — enforcement
reads `hold` only. The file sits beside breaker.json and IS the cross-repo
protocol: the tg bridge (synapse) ships its own independent reader, nothing is
imported across repos.

Duty never writes breaker.json. The effective pause of a shell is the union of
the manual breaker scope and the duty hold (see breaker.covers).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from cortex import breaker, config

logger = logging.getLogger(__name__)

DUTY_FILE = "duty.json"

SHELL_CLI = "cli"
SHELL_TG = "tg"
# tg first everywhere a pair is walked: its kick is a socket write, while the
# cli pipeline can spend seconds in AppleScript before returning.
SHELLS = (SHELL_TG, SHELL_CLI)

MODE_CLI = "cli"
MODE_TG = "tg"
MODE_OFF = "off"
MODE_ALL = "all"
MODES = (MODE_CLI, MODE_TG, MODE_OFF, MODE_ALL)

HOLD_ALL = "all"
HOLDS = ("cli", "tg", HOLD_ALL)

FORCE_SLEPT = "duty"

_HOLD_BY_MODE = {
    MODE_OFF: HOLD_ALL,
    MODE_CLI: MODE_TG,
    MODE_TG: MODE_CLI,
    MODE_ALL: None,
}

ERR_BREAKER_HELD = ("breaker held - transfer never clears it; "
                    "release it with /ct-duty <mode>")


# --- paths ---------------------------------------------------------------

def duty_path(config_dir: Path | str) -> Path:
    """Sibling of breaker.json — the breaker path is the single derivation, so
    relocating one file moves both halves of the protocol together."""
    return breaker.breaker_path(config_dir).with_name(DUTY_FILE)


def hold_for(mode: str) -> str | None:
    """Pause scope a mode materialises: the shell on duty runs, the other is
    held."""
    return _HOLD_BY_MODE.get(str(mode).strip().lower())


# --- io ------------------------------------------------------------------

@contextlib.contextmanager
def _flock(p: Path):
    """Advisory lock on a `.lock` sibling. Best effort: a lock we cannot take
    must never wedge the caller."""
    lp = p.with_suffix(".lock")
    fd = None
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError as e:
        logger.warning("duty lock failed (%s) — proceeding unlocked", e)
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _read_json(p: Path, default):
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, type(default)):
                return data
            logger.warning("duty file %s has unexpected shape — ignoring", p.name)
    except (OSError, ValueError) as e:
        logger.warning("duty file %s unreadable (%s) — treated as clear", p.name, e)
    return default


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# --- state ---------------------------------------------------------------

def read(config_dir: Path | str) -> dict:
    """Current duty state. Each field is validated on its own: `hold` alone
    drives enforcement (the tg bridge reads nothing else), so an unknown mode
    never invalidates a live hold, an unknown hold reads as no hold, and an
    absent or unparseable file reads as mode "all" with no hold — a broken duty
    file must never freeze a shell."""
    d = _read_json(duty_path(config_dir), {})
    mode = str(d.get("mode") or "").strip().lower()
    hold = str(d.get("hold") or "").strip().lower()
    return {"mode": mode if mode in MODES else MODE_ALL,
            "hold": hold if hold in HOLDS else None,
            "ts": str(d.get("ts") or "")}


def write(config_dir: Path | str, mode: str, *,
          now: datetime | None = None) -> dict:
    """Record `mode` with the hold it materialises. Last writer wins — a mode
    describes the whole two-shell world, so there is nothing to merge."""
    with _flock(duty_path(config_dir)):
        return _write_state(config_dir, mode, now=now)


def _write_state(config_dir: Path | str, mode: str, *,
                 now: datetime | None = None) -> dict:
    """Unlocked half of write: the caller already holds the duty lock (flock is
    per-descriptor, so taking it twice in one process would deadlock)."""
    target = str(mode).strip().lower() or MODE_ALL
    state = {"mode": target,
             "hold": hold_for(target),
             "ts": (now or datetime.now().astimezone()).isoformat()}
    _write_json(duty_path(config_dir), state)
    return state


def covers(config_dir: Path | str, shell: str) -> bool:
    """Does the duty hold cover `shell` right now?"""
    hold = read(config_dir)["hold"]
    if not hold:
        return False
    return hold in (HOLD_ALL, str(shell).strip().lower())


def held_shells(hold: str | None) -> frozenset[str]:
    """Shells a hold value covers."""
    if not hold:
        return frozenset()
    if hold == HOLD_ALL:
        return frozenset(SHELLS)
    return frozenset({hold})


# --- transition ----------------------------------------------------------

def apply(cfg: dict, mode: str, *, now: datetime | None = None,
          after_hold=None, source: str | None = None,
          from_shell: str | None = None) -> dict:
    """Move duty to `mode` and act on the change: the new hold lands on disk
    FIRST, then a shell newly under it is put down, and only then is every
    shell the hold leaves free woken — so no instant exists where both shells
    are active. The incoming shell passes the fresh-vs-resume gate ([duty]
    thresholds) on its way up.

    The WHOLE transition runs under the duty lock, put-down and kicks included:
    two opposing transitions racing would otherwise interleave their actions and
    leave both shells running under the last writer's hold. Nothing called from
    inside takes that lock again (the read path never locks), so it cannot
    deadlock.

    `after_hold` is a callback run once the new hold is on disk and before any
    put-down or kick — ctl clears the breaker there, so the two enforcement
    files are never both clear and no kick is delivered under a standing breaker.

    The kick does not depend on the previous state: naming a shell means "that
    one is on duty, wake it NOW", so repeating a mode kicks it again (the cli
    already-awake guard and the idempotent tg booking make that safe). "off"
    holds both and kicks nothing. Callers own mode validation — an unknown mode
    writes through as a no-hold state rather than raising.

    `source` (wake_source.KIND_*) labels the rotation: each woken shell gets the
    matching [duty] line staged before its kick, and its own wakeup note renders
    it on the way in. `from_shell` fills the transfer line's origin. Left unset
    (an auto wake) nothing is staged and the note reads exactly as before."""
    now = now or datetime.now(config.get_tz(cfg))
    config_dir = config.marrow_config_dir(cfg)
    fresh = []
    with _flock(duty_path(config_dir)):
        before = held_shells(read(config_dir)["hold"])
        state = _write_state(config_dir, mode, now=now)
        after = held_shells(state["hold"])
        if after_hold is not None:
            after_hold()

        put_down = SHELL_CLI in (after - before) and _put_down_cli(cfg)
        waking = frozenset(SHELLS) - after
        for shell in SHELLS:
            if shell not in waking:
                continue
            _stage_source(cfg, shell, state["hold"], source, from_shell)
            woke_fresh = (_wake_tg(cfg, now) if shell == SHELL_TG
                          else _wake_cli(cfg, now))
            if woke_fresh:
                fresh.append(shell)
    return {"mode": state["mode"], "hold": state["hold"],
            "put_down": put_down,
            "woken": [s for s in SHELLS if s in waking],
            "fresh": fresh}


def _stage_source(cfg: dict, shell: str, hold: str | None,
                  source: str | None, from_shell: str | None) -> None:
    """Park the rotation's source line for `shell` BEFORE its kick — the tg host
    and the marrow injection hook both render the note as their first act on
    waking, so a line staged afterwards would miss its own round."""
    if not source:
        return
    from cortex import wake_source

    wake_source.stage(cfg, shell, wake_source.render(
        cfg, source, shell=shell, hold=hold, from_shell=from_shell))


def _put_down_cli(cfg: dict) -> bool:
    """End a live cli wake through the same proxy lie_down ct-pause uses (clears
    the awake marker, kills the watchdog, books NO next wake — a hold is a pure
    stop). Silent, and never fatal: the hold stands whether or not the window
    goes down cleanly."""
    from cortex import lie_down as lie_down_mod
    from cortex import wake_state

    if not wake_state.load(cfg).get("awake"):
        return False
    try:
        lie_down_mod.lie_down(cfg, force_slept=FORCE_SLEPT, book_alarm=False)
    except Exception as e:  # noqa: BLE001 — the hold outranks a failed put-down
        logger.warning("duty put-down of the cli window failed (%s)", e)
        return False
    return True


# --- fresh-vs-resume gate ------------------------------------------------

def _thresholds(cfg: dict) -> tuple[int, float]:
    d = cfg.get("duty") or {}
    return (int(d.get("fresh_token_threshold", 80000)),
            float(d.get("fresh_age_hours", 8)))


def _cli_needs_fresh(cfg: dict, now: datetime) -> bool:
    """Is the cli window too full or too stale to be resumed? Token source is
    fixed: the transcript's window occupancy, never a fresh parse. No transcript
    at all is not "old" — the wake pipeline already spawns fresh when there is
    nothing to resume."""
    from cortex import transcript

    tokens, hours = _thresholds(cfg)
    if transcript.window_tokens(cfg) > tokens:
        return True
    m = transcript.mtime(cfg)
    if m is None:
        return False
    return (now.timestamp() - m) > hours * 3600


def _tg_needs_fresh(cfg: dict, now: datetime) -> bool:
    """Same gate on the tg side, read from the ledger the bridge maintains:
    `occupancy` for size, the later of the last user / last note stamp for age.
    An empty ledger reads as neither — a shell that never ran has nothing to
    respawn."""
    from cortex import shell_ledger
    from cortex.occupancy import parse_due_at

    tokens, hours = _thresholds(cfg)
    d = shell_ledger.read(config.shell_state_dir(cfg), SHELL_TG)
    occ = d.get("occupancy")
    if isinstance(occ, int) and not isinstance(occ, bool) and occ > tokens:
        return True
    tz = config.get_tz(cfg)
    stamps = [s for s in (parse_due_at(d.get("last_user_ts"), tz),
                          parse_due_at(d.get("last_note_ts"), tz)) if s]
    if not stamps:
        return False
    return (now - max(stamps)).total_seconds() > hours * 3600


# --- wakes ---------------------------------------------------------------

def _wake_cli(cfg: dict, now: datetime) -> bool:
    """Wake cli through the standard ctl pipeline. Over the gate it goes up as a
    rotate: the flag makes the wake a fresh spawn, and the retired session id
    keeps every resume path off the window being left behind."""
    from cortex import ctl, wake_state

    fresh = _cli_needs_fresh(cfg, now)
    if fresh:
        # Only a real pointer may be recorded. Passing None through would CLEAR
        # a retired_sid a previous rotate had already put there, un-retiring a
        # session every resume path is meant to stay off.
        retiring = wake_state.load(cfg).get("transcript")
        if retiring:
            wake_state.set_retired_sid(cfg, retiring)
        wake_state.set_rotated(cfg)
    ctl._wake_cli(cfg)
    return fresh


def _wake_tg(cfg: dict, now: datetime) -> bool:
    """Wake tg through the ctl plumbing: a due-now booking in its ledger (the
    durable half) plus a best-effort host kick. Over the gate the booking
    carries rotate_pending, so the bridge respawns instead of resuming."""
    from cortex import ctl

    fresh = _tg_needs_fresh(cfg, now)
    ctl._wake_tg(cfg, rotate=fresh)
    return fresh


# --- transfer ------------------------------------------------------------

def other_shell(shell: str) -> str | None:
    """The shell a transfer hands duty to. Two-shell world: the target is
    whichever one the caller is not."""
    s = str(shell).strip().lower()
    if s not in SHELLS:
        return None
    return next(x for x in SHELLS if x != s)


def _retire_caller(cfg: dict, shell: str) -> None:
    """Retire the CALLER's own window as it hands duty away: whenever duty comes
    back to this shell it must spawn fresh, never resume the session being left
    behind. The retirement is recorded on the caller's side only — the incoming
    shell's wake is untouched and still judged by the fresh-vs-resume gate.

    tg rides the ledger flag its host claims on the next kick; cli mirrors the
    rotate writes of _wake_cli. Both must land BEFORE the caller's put-down:
    lie_down's claim drops the `transcript` hint retired_sid is derived from."""
    if shell == SHELL_TG:
        from cortex import shell_ledger

        shell_ledger.write(config.shell_state_dir(cfg), SHELL_TG,
                           {"rotate_pending": True})
        return
    from cortex import wake_state

    retiring = wake_state.load(cfg).get("transcript")
    if retiring:
        wake_state.set_retired_sid(cfg, retiring)
    wake_state.set_rotated(cfg)


def transfer(cfg: dict, shell: str, rotate: bool = False) -> dict:
    """Hand duty from `shell` to the other cortex shell — the on-duty shell's
    own graceful handover (marrow exposes it as the transfer MCP tool).

    Duty moves to mode = the target, so the caller's shell is what the new hold
    covers: it simply goes quiet where it stands, and the fresh-vs-resume gate
    judges its window at its own next takeover. Everything else is apply's
    ordinary transition — no lie_down is issued here.

    `rotate` retires the CALLER's window on the way out (see _retire_caller), so
    its own next takeover spawns fresh whatever the gate would have said. It runs
    as apply's after_hold: under the duty lock, once the new hold is on disk and
    before any put-down or kick.

    A standing breaker.json refuses: a manual/fuse pause outranks a rotation,
    and transfer must never clear it (only an explicit ctl duty does)."""
    target = other_shell(shell)
    if target is None:
        return {"ok": False, "error": f"unknown shell: {shell}"}
    if breaker.read(config.marrow_config_dir(cfg)) is not None:
        return {"ok": False, "error": ERR_BREAKER_HELD}
    caller = str(shell).strip().lower()
    from cortex import wake_source

    out = apply(cfg, target, source=wake_source.KIND_TRANSFER,
                from_shell=caller,
                after_hold=(lambda: _retire_caller(cfg, caller)) if rotate
                else None)
    return {"ok": True, "shell": caller, "target": target,
            "rotated": bool(rotate), **out}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Duty rotation")
    parser.add_argument("--transfer", metavar="SHELL", required=True,
                        help="hand duty from SHELL to the other cortex shell")
    parser.add_argument("--rotate", action="store_true",
                        help="retire the calling shell's window on the way out")
    args = parser.parse_args(argv)
    print(json.dumps(transfer(config.load(), args.transfer, rotate=args.rotate),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
