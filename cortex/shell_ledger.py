"""Non-cli shell ledger (`<shell_state_dir>/<shell>.json`) — cortex-side writer.

The tg shell host (synapse_tg.shell.ShellHost) takes its deadline from this
file: `next_wake_at` booked here suspends its idle cycle and becomes the round
it fires. Cortex only ever writes the booking; the host owns claiming it.

Protocol copied, never imported (the two repos stay independent — same rule as
breaker.json): flock on a `<shell>.lock` sibling around read-modify-write, tmp +
`os.replace`. Keys written by the other side survive the merge, and vice versa.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def state_path(state_dir: Path | str, shell: str) -> Path:
    return Path(state_dir).expanduser() / f"{shell}.json"


@contextlib.contextmanager
def _flock(p: Path):
    """Advisory lock, best effort: a lock we cannot take must never wedge the
    caller — the write still lands."""
    lp = p.with_suffix(".lock")
    fd = None
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError as e:
        logger.warning("shell ledger lock failed (%s) — proceeding unlocked", e)
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _load(p: Path) -> dict:
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            logger.warning("shell ledger %s has unexpected shape — ignoring", p.name)
    except (OSError, ValueError) as e:
        logger.warning("shell ledger %s unreadable (%s) — starting clean", p.name, e)
    return {}


def read(state_dir: Path | str, shell: str) -> dict:
    p = state_path(state_dir, shell)
    with _flock(p):
        return _load(p)


def write(state_dir: Path | str, shell: str, data: dict) -> Path:
    """Merge `data` into the shell's ledger under the lock; a None value drops
    its key. Keys this side does not know are left untouched."""
    p = state_path(state_dir, shell)
    with _flock(p):
        d = _load(p)
        for k, v in data.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    return p
