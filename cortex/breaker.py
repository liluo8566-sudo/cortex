"""Circuit breaker ("main switch") — cli side.

ONE persistent file decides whether cortex AUTONOMOUS activity may run:

    <marrow config dir>/breaker.json
        {"scope": "all"|"cli"|"tg", "reason": "auto_fuse"|"manual", "ts": iso}
    file absent  = breaker clear
    unreadable   = treated as clear (logged), never wedges the shell

Sibling tally, appended by BOTH shells:

    <marrow config dir>/fuse_events.json
        {"events": [{"ts": iso, "shell": "cli"|"tg"}, ...]}
    pruned to [cortex.breaker].window_hours on every write

The JSON file IS the cross-repo protocol: the tg bridge (synapse) ships its own
independent copy of this logic (synapse_core/breaker.py). Nothing is imported
across repos — only the schema is shared, documented in both MAPs.

Thresholds live in marrow's config.toml under [cortex.breaker], read directly
here so the same number is never duplicated into cortex.toml.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

BREAKER_FILE = "breaker.json"
FUSE_FILE = "fuse_events.json"

SCOPE_ALL = "all"
SHELLS = ("cli", "tg")
REASON_AUTO = "auto_fuse"
REASON_MANUAL = "manual"

DEFAULTS = {
    "enabled": True,
    "fuse_threshold": 2,
    "window_hours": 24,
    "trip_message": (
        "Circuit breaker tripped: fuse #{count} within {hours}h. Cortex "
        "autonomous activity paused ({scope}). Clear with ct-duty cli|tg|all."
    ),
}


# --- paths ---------------------------------------------------------------

def breaker_path(config_dir: Path | str) -> Path:
    return Path(config_dir).expanduser() / BREAKER_FILE


def fuse_path(config_dir: Path | str) -> Path:
    return Path(config_dir).expanduser() / FUSE_FILE


def settings(config_dir: Path | str) -> dict:
    """[cortex.breaker] from marrow's config.toml, layered over DEFAULTS.
    Missing file / section / key -> defaults. Never raises."""
    out = dict(DEFAULTS)
    p = Path(config_dir).expanduser() / "config.toml"
    try:
        if p.exists():
            with p.open("rb") as f:
                data = tomllib.load(f)
            section = ((data.get("cortex") or {}).get("breaker") or {})
            if isinstance(section, dict):
                for k in DEFAULTS:
                    if k in section:
                        out[k] = section[k]
    except (OSError, ValueError, TypeError) as e:
        logger.warning("breaker settings read failed (%s) — using defaults", e)
    return out


# --- io ------------------------------------------------------------------

@contextlib.contextmanager
def _flock(p: Path):
    """Advisory lock on a `.lock` sibling around read-modify-write. Best effort:
    a lock we cannot take must never wedge the daemon."""
    lp = p.with_suffix(".lock")
    fd = None
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError as e:
        logger.warning("breaker lock failed (%s) — proceeding unlocked", e)
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
            logger.warning("breaker file %s has unexpected shape — ignoring", p.name)
    except (OSError, ValueError) as e:
        logger.warning("breaker file %s unreadable (%s) — treated as clear", p.name, e)
    return default


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# --- state ---------------------------------------------------------------

def read(config_dir: Path | str) -> dict | None:
    """Current breaker state, or None when clear. A corrupt/odd file reads as
    clear (logged) — a broken breaker must never silently freeze the shell."""
    d = _read_json(breaker_path(config_dir), {})
    scope = str(d.get("scope") or "").strip().lower()
    if not scope:
        return None
    return {"scope": scope,
            "reason": str(d.get("reason") or REASON_MANUAL),
            "ts": str(d.get("ts") or "")}


def covers(config_dir: Path | str, shell: str) -> bool:
    """Is `shell` held down right now — by the manual/fuse breaker scope OR by
    the duty rotation hold? The two files are independent: duty never writes
    breaker.json, so enforcement takes their union."""
    target = str(shell).strip().lower()
    state = read(config_dir)
    if state is not None and state["scope"] in (SCOPE_ALL, target):
        return True
    from cortex import duty
    return duty.covers(config_dir, target)


def _union(current: str | None, scope: str) -> str:
    """Widest hold of the two: a second single-shell pause never releases the
    shell the first one holds."""
    if current is None or current == scope:
        return scope
    return SCOPE_ALL


def trip(config_dir: Path | str, scope: str = SCOPE_ALL,
         reason: str = REASON_MANUAL, *, now: datetime | None = None,
         merge: bool = False) -> dict:
    """Write the breaker. Last writer wins by default — the auto fuse trips
    scope "all", which already covers everything standing.

    `merge=True` (manual pause) unions the new scope with the standing one
    instead: pausing tg while cli is held holds BOTH, and pausing one shell
    while scope is "all" leaves "all" alone. `reason` is still the new writer's
    — a merged manual pause reads as manual."""
    p = breaker_path(config_dir)
    with _flock(p):
        target = str(scope).strip().lower() or SCOPE_ALL
        if merge:
            current = read(config_dir)
            target = _union(current["scope"] if current else None, target)
        state = {"scope": target,
                 "reason": reason,
                 "ts": (now or datetime.now().astimezone()).isoformat()}
        _write_json(p, state)
    return state


def clear(config_dir: Path | str, shell: str | None = None) -> bool:
    """Release the breaker. `shell=None` removes the record entirely (all
    shells). A `shell` releases only that half, mirroring `trip`'s per-shell
    scope: scope "all" NARROWS to the other shell (reason/ts preserved — it is
    the same hold, just smaller), scope == shell removes the record, any other
    scope is a no-op. True when something was released."""
    p = breaker_path(config_dir)
    target = str(shell).strip().lower() if shell else None
    with _flock(p):
        state = read(config_dir)
        if target is None:
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)
            return state is not None
        if state is None or target not in SHELLS:
            return False
        if state["scope"] == target:
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)
            return True
        if state["scope"] == SCOPE_ALL:
            _write_json(p, {**state, "scope": next(s for s in SHELLS if s != target)})
            return True
        return False


# --- fuse tally ----------------------------------------------------------

def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def _prune(events: list, cutoff: datetime) -> list:
    kept = []
    for e in events:
        if isinstance(e, str):
            e = {"ts": e, "shell": ""}
        if not isinstance(e, dict):
            continue
        dt = _parse(e.get("ts"))
        if dt is not None and dt >= cutoff:
            kept.append({"ts": dt.isoformat(), "shell": str(e.get("shell") or "")})
    return kept


def record_fuse(config_dir: Path | str, shell: str, *,
                now: datetime | None = None) -> int:
    """Append one fuse event for `shell`, prune the rolling window, return the
    surviving count ACROSS ALL SHELLS (both write this same file)."""
    cfg = settings(config_dir)
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(hours=float(cfg["window_hours"]))
    p = fuse_path(config_dir)
    with _flock(p):
        data = _read_json(p, {})
        events = data.get("events")
        events = _prune(events if isinstance(events, list) else [], cutoff)
        events.append({"ts": now.isoformat(), "shell": str(shell)})
        _write_json(p, {"events": events})
    return len(events)


def record_fuse_and_maybe_trip(config_dir: Path | str, shell: str, *,
                               now: datetime | None = None) -> tuple[int, dict | None]:
    """Record a fuse, then trip the breaker (scope=all, reason=auto_fuse) when
    the rolling count reaches the threshold and [cortex.breaker].enabled.

    Returns (count, tripped_state_or_None). `enabled = false` still tallies —
    the count is the useful signal — it just never trips."""
    cfg = settings(config_dir)
    count = record_fuse(config_dir, shell, now=now)
    if not bool(cfg["enabled"]):
        return count, None
    try:
        threshold = int(cfg["fuse_threshold"])
    except (TypeError, ValueError):
        threshold = int(DEFAULTS["fuse_threshold"])
    if threshold <= 0 or count < threshold:
        return count, None
    return count, trip(config_dir, SCOPE_ALL, REASON_AUTO, now=now)


# --- cfg-level conveniences ----------------------------------------------
# Every cortex call site goes through these so the config-dir derivation lives
# in exactly one place.

def _dir(cfg: dict) -> Path:
    from cortex import config
    return config.marrow_config_dir(cfg)


def holds(cfg: dict, shell: str = "cli") -> bool:
    """True when the breaker covers `shell` — no autonomous wake / spawn / reap."""
    return covers(_dir(cfg), shell)


def state(cfg: dict) -> dict | None:
    return read(_dir(cfg))


def pause(cfg: dict, scope: str = SCOPE_ALL) -> dict:
    return trip(_dir(cfg), scope, REASON_MANUAL, merge=True)


def release(cfg: dict, shell: str | None = None) -> bool:
    return clear(_dir(cfg), shell)


def held_line(cfg: dict, what: str) -> str:
    """One-line log/report string for a refused autonomous action."""
    st = state(cfg)
    reason = st["reason"] if st else "?"
    scope = st["scope"] if st else "?"
    return f"breaker held ({scope}/{reason}): {what}"


def trip_message(config_dir: Path | str, count: int, scope: str = SCOPE_ALL) -> str:
    cfg = settings(config_dir)
    try:
        return str(cfg["trip_message"]).format(
            count=count, hours=cfg["window_hours"], scope=scope)
    except (KeyError, IndexError, ValueError):
        return str(cfg["trip_message"])
