"""Persistent window/wake runtime state (JSON file, sibling of affect_flag /
self_schedule). Holds the resident iTerm session id, the awake marker
(awake_since + wake_log row id + transcript hint) and the rotate guard. Kept
out of the pure PacemakerState so the decision core stays I/O-free; all paths
resolve from config (OSS-overridable via [paths]).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from cortex import config

_AWAKE_KEYS = ("awake", "awake_since", "wake_log_id", "transcript",
               "user_replied_this_wake", "tuck_pending", "kick_round")

_LOCK_TIMEOUT_SEC = 5.0


class StateValidationError(Exception):
    """Fail-closed sentinel: a strict-lock section could not acquire the lock,
    the state file was unreadable/malformed, or a captured (gen, state_id) token
    no longer matches the live state. Every deferred actor treats it as "abort
    the pending side effect silently" — correctness never depends on the lock
    succeeding, only that a doubtful mutation is dropped."""


def wake_state_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("wake_state_file") or ""
    return Path(raw).expanduser() if raw else config.state_dir(cfg) / "wake_state.json"


def wakeup_note_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("wakeup_note_file") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "wakeup_note.md"


def free_round_note_path(cfg: dict) -> Path:
    """Staging file for the INVISIBLE free-round payload: cortex writes the
    rendered note (and any claimed ct notes) here right before typing the short
    ⏳ marker line, and the marrow UserPromptSubmit hook reads + consumes it on
    the marker turn. Same bell->note pattern, so the window only ever shows the
    marker. Must match marrow [cortex].free_round_note_file."""
    raw = cfg["paths"].get("free_round_note_file") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "free_round_note.md"


def watchdog_pidfile_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("watchdog_pidfile") or ""
    return Path(raw).expanduser() if raw else config.state_dir(cfg) / "watchdog.pid"


def spawn_lock_path(cfg: dict) -> Path:
    """Exclusive flock file serialising EVERY window-spawn entrant (daemon
    tick reconcile, ctl wake's no-resident branch, rotate succession) — see
    wake._spawn_serialized. Default: <cortex_home>/state/spawn.lock."""
    raw = cfg["paths"].get("spawn_lock_file") or ""
    return Path(raw).expanduser() if raw else config.state_dir(cfg) / "spawn.lock"


def lock_path(cfg: dict) -> Path:
    """Sibling .lock file guarding load-modify-write. Shared byte-for-byte with
    the marrow hook side so cross-process updates never lose each other.
    COUPLED: base = [paths].wake_state_file / [paths].cortex_home. Marrow's side
    (cortex_bridge._wake_state_lock via _cortex_wake_state_path) resolves from
    marrow [cortex].wake_state_file / [cortex].home — override one without the
    other and the two lock files split (silent lost update)."""
    return wake_state_path(cfg).with_suffix(".lock")


def _alert_lock_giveup(cfg: dict, detail: str) -> None:
    """Surface a lock give-up: one marrow `alerts` row (same table watchdog's
    breaker trip writes) plus an audit line. Both give-ups were silent before —
    an advisory _flock proceeding unlocked, and a strict section failing closed
    with the caller swallowing it. Throttled to one row per
    [wake].lock_alert_throttle_min via a stamp file, so a stuck lock cannot
    spam. Best-effort: never raises, never blocks the caller."""
    try:
        stamp = config.state_dir(cfg) / "flock_alert.stamp"
        throttle = float(cfg.get("wake", {}).get("lock_alert_throttle_min", 60))
        now = datetime.now(timezone.utc)
        try:
            last = datetime.fromisoformat(stamp.read_text().strip())
            if (now - last).total_seconds() < throttle * 60:
                return
        except (OSError, ValueError):
            pass
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(now.isoformat())
        wake_audit(cfg, "lock_giveup", detail, str(lock_path(cfg)))
        from cortex import db
        conn = db.connect(cfg)
        try:
            conn.execute(
                "INSERT INTO alerts (severity, type, message, source)"
                " VALUES (?, ?, ?, ?)",
                ("warn", "cortex_lock_giveup", detail, "cortex.wake_state"))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — table/db may be absent; audit already tried
        pass


@contextlib.contextmanager
def _flock(cfg: dict):
    """Blocking exclusive flock on the sibling .lock file (short timeout via a
    non-blocking retry loop). Best-effort: if the lock cannot be acquired the
    write still proceeds (an unlocked write is the pre-existing behaviour), so a
    lock-dir hiccup never wedges a wake — but the give-up raises an alert."""
    lp = lock_path(cfg)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        _alert_lock_giveup(cfg, f"wake_state lock open failed ({e}) — writing unlocked")
        yield
        return
    deadline = _mono() + _LOCK_TIMEOUT_SEC
    got = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if _mono() >= deadline:
                    break
                _sleep(0.02)
        if not got:
            _alert_lock_giveup(
                cfg, "wake_state lock timeout — writing unlocked "
                     "(concurrent update may be lost)")
        yield
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


@contextlib.contextmanager
def _strict_flock(cfg: dict):
    """Fail-closed exclusive flock: unlike _flock (advisory, proceeds unlocked on
    timeout), this RAISES StateValidationError if the lock cannot be created or
    acquired within the timeout. Used for every consequential cancellation-epoch
    check + mutation, so a lock hiccup drops the doubtful side effect instead of
    racing an unlocked write."""
    lp = lock_path(cfg)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        _alert_lock_giveup(cfg, f"wake_state strict lock open failed ({e}) "
                                f"— side effect dropped")
        raise StateValidationError(f"lock open failed: {e}") from e
    deadline = _mono() + _LOCK_TIMEOUT_SEC
    got = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if _mono() >= deadline:
                    _alert_lock_giveup(
                        cfg, "wake_state strict lock timeout — side effect "
                             "dropped (fail-closed)")
                    raise StateValidationError("lock acquire timeout")
                _sleep(0.02)
        yield
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _mono() -> float:
    import time
    return time.monotonic()


def _sleep(sec: float) -> None:
    import time
    time.sleep(sec)


def _load_strict(cfg: dict) -> dict:
    """Read the state file, RAISING StateValidationError on any read/parse
    failure (unlike load() which returns {}). Caller must hold _strict_flock."""
    p = wake_state_path(cfg)
    try:
        if not p.exists():
            return {}
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise StateValidationError(f"state unreadable/malformed: {e}") from e


def _ensure_epoch(d: dict) -> bool:
    """Initialise gen (0) + a random state_id on first touch. Returns True when a
    field was added (caller must persist). state_id defends the delete/recreate
    ABA: a fresh file re-seeds a different id, so a token captured against the
    old file never validates against the new one."""
    changed = False
    if not isinstance(d.get("gen"), int):
        d["gen"] = 0
        changed = True
    if not d.get("state_id"):
        d["state_id"] = secrets.token_hex(8)
        changed = True
    return changed


def current_epoch(cfg: dict) -> tuple[int, str]:
    """Capture the live (gen, state_id) token under the STRICT lock — a deferred
    actor's birth token. Raises StateValidationError on lock/parse failure so a
    doubtful capture never yields a token that would spuriously validate later."""
    with _strict_flock(cfg):
        d = _load_strict(cfg)
        if _ensure_epoch(d):
            _save(cfg, d)
        return int(d["gen"]), str(d["state_id"])


def _token_current(d: dict, token: tuple[int, str] | None) -> bool:
    """True when a captured (gen, state_id) still matches the loaded state. A
    None token = legacy/no-token = always current (backward tolerance)."""
    if token is None:
        return True
    gen, state_id = token
    return isinstance(d.get("gen"), int) and d.get("gen") == gen \
        and str(d.get("state_id") or "") == str(state_id)


def token_current(cfg: dict, token: tuple[int, str] | None) -> bool:
    """Read-only epoch check under the STRICT lock: True if `token` still matches
    the live (gen, state_id). Raises StateValidationError on lock/parse failure
    (fail closed) so a deferred actor drops the side effect rather than proceed on
    a doubtful read. token=None -> True (legacy/no token)."""
    with _strict_flock(cfg):
        d = _load_strict(cfg)
        _ensure_epoch(d)
        return _token_current(d, token)


def conditional_mutate(cfg: dict, token: tuple[int, str] | None, mutate):
    """Run `mutate(d)` and persist ONLY if `token` still matches the live epoch,
    all under the STRICT lock. `mutate` edits the dict in place; its return value
    is passed back to the caller. Raises StateValidationError on lock/parse
    failure OR token mismatch (fail closed) so the deferred side effect is
    dropped. token=None skips the check (unconditional, still strict-locked)."""
    with _strict_flock(cfg):
        d = _load_strict(cfg)
        _ensure_epoch(d)
        if not _token_current(d, token):
            raise StateValidationError("epoch token stale")
        result = mutate(d)
        _save(cfg, d)
        return result


def bump_gen(cfg: dict) -> tuple[int, str]:
    """Increment gen under the strict lock and return the NEW (gen, state_id).
    The one primitive behind every cancellation epoch: a bump invalidates every
    token captured against the old gen. Callers that also mutate state should use
    the higher-level helpers (claim_lie_down, set_awake, wait, ...) which bump +
    mutate atomically in one locked section."""
    with _strict_flock(cfg):
        d = _load_strict(cfg)
        _ensure_epoch(d)
        d["gen"] = int(d["gen"]) + 1
        _save(cfg, d)
        return int(d["gen"]), str(d["state_id"])


def wake_audit(cfg: dict, action: str, reason: str = "", detail: str = "") -> None:
    """Append one tab-separated audit line (ISO-ts, action, reason, detail) to
    the config-routed wake-audit log. Byte-shared with marrow's _wake_audit.
    Best-effort — never raises."""
    try:
        path = config.wake_audit_log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        line = "\t".join((ts, action, str(reason).replace("\t", " "),
                          str(detail).replace("\t", " ")))
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load(cfg: dict) -> dict:
    p = wake_state_path(cfg)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except (OSError, ValueError):
        pass
    return {}


# Legacy keys from older schema versions, dropped on the next _save so state
# files converge (nothing reads these anymore — verified in both repos).
_DEAD_KEYS = ("rotated_at",)


def _save(cfg: dict, data: dict) -> None:
    """Atomic whole-file write: temp file in the same dir + os.replace so a
    reader never sees a half-written file. Callers hold _flock for the
    read-modify-write; _save alone is atomic but not serialised."""
    p = wake_state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    for k in _DEAD_KEYS:
        data.pop(k, None)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def update(cfg: dict, **kv) -> dict:
    with _flock(cfg):
        d = load(cfg)
        d.update(kv)
        _save(cfg, d)
        return d


def get_session_id(cfg: dict) -> str | None:
    return load(cfg).get("session_id")


def set_session_id(cfg: dict, sid: str) -> None:
    update(cfg, session_id=sid)


def get_cortex_claude_sid(cfg: dict) -> str | None:
    """Claude session UUID of the cli shell's cortex window (`cortex_claude_sid`,
    stamped by set_awake at registration). NOT `session_id` — that key is the
    iTerm session id."""
    sid = load(cfg).get("cortex_claude_sid")
    return str(sid).strip() or None if sid else None


def shell_claude_sid(cfg: dict, shell: str | None = None) -> str | None:
    """Claude session UUID of the cortex session running in `shell`.

    cli  -> this file's `cortex_claude_sid`.
    else -> `<shell_state_dir>/<shell>.json` key `session_id`, the ledger that
            shell's host (e.g. the synapse tg bridge) writes; there `session_id`
            IS the claude sid (cortex's own file uses that key for iTerm).
    None when the file/key is missing — callers must degrade, never guess."""
    shell = (shell or "cli").strip().lower()
    if shell == "cli":
        return get_cortex_claude_sid(cfg)
    p = config.shell_state_dir(cfg) / f"{shell}.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sid = d.get("session_id") if isinstance(d, dict) else None
    return str(sid).strip() or None if sid else None


def is_awake(cfg: dict) -> bool:
    return bool(load(cfg).get("awake"))


def set_awake(cfg: dict, wake_log_id: int | None, transcript: str | None,
              expected_gen: int | None = None, bump: bool = True,
              expected_token: tuple[int, str] | None = None,
              session_id: str | None = None,
              cortex_claude_sid: str | None = None) -> tuple[int, str] | None:
    """Activate a wake (asleep -> awake). BUMPS gen by default (a fresh wake is a
    new epoch that invalidates the sleeping window's alarm token). Returns the new
    (gen, state_id) on success, None if the conditional flip lost.

    Two conditional forms (codex adversarial-review Fix 4):
      expected_token=(gen, state_id) -- the FULL token, validated via
        _token_current (gen AND state_id). Use this for any spawn-path caller: a
        gen-only check tolerates the delete/recreate ABA (wake_state.json wiped
        and recreated back to the SAME gen with a NEW state_id passes a gen-only
        compare, letting a stale actor overwrite the recreated state -- marrow's
        receipt consumer already validates both fields, so a gen-only cortex
        check disagreed with marrow). Prefer this over expected_gen.
      expected_gen=<int> -- LEGACY gen-only check, kept only for the ear path's
        pre-existing call shape (not itself part of this fix; still gen-only by
        design there). Superseded by expected_token when both are given.

    session_id, when given, is committed in the SAME atomic section as the awake
    flip (Fix 2): the spawn path no longer persists the new resident session id
    separately before this CAS is known to succeed, so a stale/superseded spawn
    can never leave its session id recorded as the resident's while a newer
    epoch's spawn (or the prior resident) is what's actually live. None leaves
    session_id untouched (the ear/rearm callers, which never spawn a new window).

    next_wake_at is the durable ledger: a successful wake means it fired, so it is
    cleared here (re-armed by the next lie_down) in the same atomic section so an
    awake window never carries a stale scheduled time. Audited (`set_awake`,
    old->new gen) whenever it actually bumps."""
    try:
        with _strict_flock(cfg):
            d = _load_strict(cfg)
            _ensure_epoch(d)
            if expected_token is not None and not _token_current(d, expected_token):
                return None
            if expected_token is None and expected_gen is not None \
                    and int(d["gen"]) != int(expected_gen):
                return None
            old_gen = int(d["gen"])
            if bump:
                d["gen"] = old_gen + 1
            new_gen = d["gen"]
            d.update(awake=True, next_wake_at=None,
                     awake_since=datetime.now(timezone.utc).isoformat(),
                     wake_log_id=wake_log_id, transcript=transcript,
                     user_replied_this_wake=False, tuck_pending=None)
            if session_id is not None:
                d["session_id"] = session_id
            if cortex_claude_sid is not None:
                d["cortex_claude_sid"] = cortex_claude_sid
            _save(cfg, d)
            result = int(d["gen"]), str(d["state_id"])
    except StateValidationError:
        return None
    if bump:
        wake_audit(cfg, "set_awake", f"gen {old_gen}->{new_gen}", "")
    return result


def clear_awake(cfg: dict) -> None:
    """Clear the awake marker AND bump gen (a successful sleep is a new epoch —
    any alarm token from the just-ended wake is invalidated). Strict-locked.
    Audited (`clear_awake`, old->new gen)."""
    try:
        with _strict_flock(cfg):
            d = _load_strict(cfg)
            _ensure_epoch(d)
            old_gen = int(d["gen"])
            d["gen"] = old_gen + 1
            new_gen = d["gen"]
            for k in _AWAKE_KEYS:
                d.pop(k, None)
            _save(cfg, d)
    except StateValidationError:
        return
    wake_audit(cfg, "clear_awake", f"gen {old_gen}->{new_gen}", "")


def claim_lie_down(cfg: dict, force_slept: str | None = None) -> dict | None:
    """Atomic read-and-clear of the awake marker under the STRICT wake_state lock,
    so exactly one lie_down proceeds when the watchdog (60s poll) and the tick
    awake-branch both fire silence_action in the same window. On the winning claim
    (was awake -> now cleared) BUMPS gen — every deferred alarm from the ending
    wake is now stale, and the returned token is the NEW epoch the lie_down body
    carries through its late side effects. Returns the pre-clear snapshot PLUS a
    `claim_token` (gen, state_id) to the single winner; None to any later caller
    (already cleared / lock lost -> no-op, no bump). Writes a `lie_down_claim`
    audit line (old->new gen)."""
    try:
        with _strict_flock(cfg):
            d = _load_strict(cfg)
            _ensure_epoch(d)
            if not d.get("awake"):
                return None
            snapshot = dict(d)
            old_gen = int(d["gen"])
            d["gen"] = old_gen + 1
            new_gen = d["gen"]
            for k in _AWAKE_KEYS:
                d.pop(k, None)
            _save(cfg, d)
            snapshot["claim_token"] = (new_gen, str(d["state_id"]))
    except StateValidationError:
        return None
    wake_audit(cfg, "lie_down_claim", f"gen {old_gen}->{new_gen}",
               f"force_slept={force_slept}")
    return snapshot


def user_replied_this_wake(cfg: dict) -> bool:
    """True once a real user message landed in the current wake (set by the
    marrow UserPromptSubmit hook). Selects which timestamp source the unified
    silence_action idle bar times from (user message vs awake_since)."""
    return bool(load(cfg).get("user_replied_this_wake"))


def _age_min_or_none(raw) -> float | None:
    """Minutes since an ISO timestamp held in the state, or None when the field
    is absent/unparseable. Naive timestamps read as UTC (the writers' format)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def awake_since_min(cfg: dict) -> float | None:
    """Minutes elapsed since this wake began (awake_since), or None when not
    awake / unparseable. When the user never spoke this wake, silence_action
    times the same idle bar from HERE instead of a user-message ts that may
    never exist."""
    return _age_min_or_none(load(cfg).get("awake_since"))


def last_user_msg_min(cfg: dict) -> float | None:
    """Minutes since the last real user message, as stamped into the state by
    the marrow UserPromptSubmit hook (last_user_msg_ts). Written synchronously
    with the prompt, unlike the transcript read which only sees the message once
    claude has flushed it to the jsonl."""
    return _age_min_or_none(load(cfg).get("last_user_msg_ts"))


def silence_basis_min(cfg: dict, transcript_min: float | None) -> float:
    """The ONE silence basis for the free-round cycle, shared by the watchdog
    poll and the daemon business deadline.

    `transcript_min` is the transcript-derived value (transcript.user_silent_min)
    and it LAGS: the marrow UserPromptSubmit hook stamps last_user_msg_ts and
    drops tuck_pending (the cycle's only other gate) in one write, before the
    message reaches the jsonl. A transcript-only basis therefore still reports
    the PREVIOUS message's age for seconds after a user arrival, with the gate
    already gone -> a free round fires immediately on top of the user's message.
    Take the newest (smallest) of the two sources.

    No user message this wake -> awake_since, so the same bar still elapses.
    Nothing known -> 0.0 (hold), the pre-existing unreadable-transcript
    behaviour: the window is a MINIMUM, never fire on an unknown basis."""
    d = load(cfg)
    if not d.get("user_replied_this_wake"):
        elapsed = _age_min_or_none(d.get("awake_since"))
        if elapsed is not None:
            return elapsed
    known = [v for v in (transcript_min, _age_min_or_none(d.get("last_user_msg_ts")))
             if v is not None]
    return min(known) if known else 0.0


def stamp_silence_basis(cfg: dict) -> bool:
    """Re-arm the free-round cycle from NOW without delivering anything (stamp
    the tuck_pending last-injection marker). Used when a deadline is found
    already overdue at daemon start: the window is a minimum interval, so an
    expiry that elapsed while nothing was running must restart the cycle rather
    than fire on the spot. Awake-only. Returns True on stamp."""
    with _flock(cfg):
        d = load(cfg)
        if not d.get("awake"):
            return False
        d["tuck_pending"] = datetime.now(timezone.utc).isoformat()
        _save(cfg, d)
        return True


def mark_kick_round(cfg: dict) -> bool:
    """External-wake carrier primitive (kick.py replacement for the retired
    wait-expiry ride): stamp kick_round=True under the strict lock so the next
    silence_action poll (watchdog / tick awake gate) treats the silence timer as
    immediately elapsed and injects a free-round note NOW, regardless of
    silent_min. Only stamps while awake with no kick_round already pending
    (idempotent — a second kick before the first is consumed is a no-op).
    Returns True on a fresh stamp, False otherwise (not awake / already
    pending / lock failure)."""
    try:
        with _strict_flock(cfg):
            d = _load_strict(cfg)
            _ensure_epoch(d)
            if not d.get("awake"):
                return False
            if d.get("kick_round"):
                return False
            d["kick_round"] = True
            _save(cfg, d)
            return True
    except StateValidationError:
        return False


def take_kick_round(cfg: dict) -> bool:
    """Consume the kick_round marker (read-and-clear, advisory lock). True if it
    was pending. silence_action calls this once it has decided to act on it, so
    the carrier fires exactly once per kick."""
    with _flock(cfg):
        d = load(cfg)
        val = bool(d.pop("kick_round", None))
        if val:
            _save(cfg, d)
        return val


def peek_kick_round(cfg: dict) -> bool:
    """Non-destructive read of the kick_round marker."""
    return bool(load(cfg).get("kick_round"))


def clear_kick_round(cfg: dict) -> None:
    """Drop the kick_round marker without consuming it as a fire (e.g. an
    interrupt kick replacing a still-pending carrier). Best-effort no-op when
    unset."""
    with _flock(cfg):
        d = load(cfg)
        if d.pop("kick_round", None) is not None:
            _save(cfg, d)


def set_next_wake_at(cfg: dict, iso_local: str | None) -> None:
    """Persist the scheduled next-wake instant (local ISO) as the durable ledger.
    The scheduled time must never live only in a process's args: a
    compact/kill loses those, but this survives so the tick reconcile can fire an
    overdue wake. None clears it (e.g. paused, or no schedule)."""
    if iso_local is None:
        with _flock(cfg):
            d = load(cfg)
            if d.pop("next_wake_at", None) is not None:
                _save(cfg, d)
        return
    update(cfg, next_wake_at=iso_local)


def get_next_wake_at(cfg: dict) -> str | None:
    """The recorded next-wake instant (local ISO) or None."""
    v = load(cfg).get("next_wake_at")
    return str(v) if v else None


def clear_next_wake_at(cfg: dict) -> None:
    set_next_wake_at(cfg, None)


# The old per-shell `paused` DND flag lived here. It is gone: the circuit
# breaker (cortex.breaker, <marrow config dir>/breaker.json) is now the single
# truth for "cortex autonomous activity is held", shared with the tg shell and
# surviving restarts. Readers call breaker.holds(cfg, "cli").


def set_rotated(cfg: dict) -> None:
    """Rotate flag: lie_down sets it when the window grew past the rotate line so
    the NEXT wake plans a fresh spawn instead of resuming the oversized session.
    Nothing is killed — the predecessor window stays open for the user to
    close, and retired_sid keeps every resume path off that session."""
    update(cfg, rotated=True)


def peek_rotated(cfg: dict) -> bool:
    """Non-destructive read of the rotate flag. Used to CLASSIFY a wake as fresh
    without consuming the one-shot flag: the flag must survive until the fresh
    successor is verified live, so a failed spawn keeps retry ownership (consuming
    it during classification, before the spawn succeeded, let a failed spawn drop
    the flag -> the retiring conversation got reactivated on the next wake, Fix 1).
    Consume with take_rotated only AFTER the successor is confirmed."""
    return bool(load(cfg).get("rotated"))


def take_rotated(cfg: dict) -> bool:
    """Consume the rotate flag (read-and-clear). True = last lie_down asked the
    next wake to respawn the window fresh. Called only AFTER a fresh successor is
    verified live (Fix 1) so a spawn failure never strands the retired window."""
    with _flock(cfg):
        d = load(cfg)
        val = bool(d.pop("rotated", False))
        if val:
            _save(cfg, d)
        return val


def set_retired_sid(cfg: dict, transcript_path: str | None) -> None:
    """Durably record the claude session UUID (the transcript jsonl stem, same
    convention as window.claude_session_id) that was just retired by a
    rotate — a per-session fact, unlike the one-shot `rotated` flag. Every
    resume path must check its resume target against this before resuming: a
    rotated session handed off and must NEVER be resumed again, even after
    `rotated` itself has already been consumed by an unrelated wake and the
    (also one-shot) `transcript` hint still happens to point at it."""
    sid = Path(str(transcript_path)).stem if transcript_path else None
    update(cfg, retired_sid=sid)


def get_retired_sid(cfg: dict) -> str | None:
    return load(cfg).get("retired_sid")
