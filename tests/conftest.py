from __future__ import annotations

import subprocess
import sqlite3
from pathlib import Path

import pytest

from cortex import config, db, wake_state

# Binaries that would touch the real machine (spawn iTerm, type into a window,
# steal focus, play a sound, probe processes). No test may reach these: a test
# that hits one un-mocked is an isolation bug, not a pass.
_BLOCKED_BINS = {
    "osascript", "afplay", "claude", "pgrep", "lsof", "ps", "security",
}


# Detached background modules a test must never really launch (they sleep +
# run a tick / poll loop, leaking a live process past the test).
_BLOCKED_MODULES = {"cortex.watchdog", "cortex.daemon"}


def _guarded(orig, name):
    def wrapper(cmd, *a, **kw):
        first = None
        if isinstance(cmd, (list, tuple)) and cmd:
            first = str(cmd[0]).rsplit("/", 1)[-1]
        elif isinstance(cmd, str):
            first = cmd.strip().split()[0].rsplit("/", 1)[-1] if cmd.strip() else None
        if first in _BLOCKED_BINS:
            raise AssertionError(
                f"test isolation: real subprocess.{name}({first!r}) blocked — "
                f"mock this call (spawn/osascript/afplay/discovery) in the test")
        if isinstance(cmd, (list, tuple)) and "-m" in cmd:
            mods = {str(x) for x in cmd}
            if mods & _BLOCKED_MODULES:
                raise AssertionError(
                    f"test isolation: real subprocess.{name} spawning a cortex "
                    f"background module blocked — stub cortex.watchdog/daemon "
                    f"Popen in the test")
        return orig(cmd, *a, **kw)
    return wrapper


@pytest.fixture(autouse=True)
def _block_real_processes(monkeypatch):
    """Fail loudly if any test reaches a machine-touching binary through an
    un-mocked subprocess call — never open a real window / play a sound / spawn
    claude during the suite. Tests that mock subprocess.run|Popen locally
    override this guard for their own scope."""
    monkeypatch.setattr(subprocess, "run", _guarded(subprocess.run, "run"))
    monkeypatch.setattr(subprocess, "Popen", _guarded(subprocess.Popen, "Popen"))


# Real live-runtime dir (~/.config/marrow/): the machine's actual cortex state,
# audit, wake_signal and note files. Tonight's incident: the pytest suite wrote
# test notes/audit lines INTO these live files (a cfg with empty [paths] falls
# back to DEFAULT_CORTEX_HOME), delivering a test-rendered note to the real
# window. A test must NEVER resolve a state/log path under this dir — always
# tmp_path.
_LIVE_CONFIG_DIR = (Path.home() / ".config" / "marrow").resolve()

# Path builders whose default fallback lands under the live dir. Wrapped so any
# resolution that would touch real runtime state fails the test loudly instead of
# corrupting the live window.
_GUARDED_PATH_FUNCS = [
    (config, "cortex_home"),
    (config, "state_dir"),
    (config, "wake_audit_log_path"),
    (config, "handoff_path"),
    (config, "wake_timing_log_path"),
    (config, "marrow_config_dir"),
    (wake_state, "wake_state_path"),
    (wake_state, "wakeup_note_path"),
    (wake_state, "watchdog_pidfile_path"),
]


def _under_live_dir(p) -> bool:
    try:
        rp = Path(p).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return rp == _LIVE_CONFIG_DIR or _LIVE_CONFIG_DIR in rp.parents


@pytest.fixture(autouse=True)
def _no_live_config_writes(monkeypatch):
    """Hard wall: any test that resolves a cortex state/log/note path under the
    real ~/.config/marrow/ fails loudly (same incident class as marrow's live-DB
    barrier). Every test must supply tmp_path-based [paths]; a bare/empty cfg that
    falls back to the live default is an isolation bug, not a pass."""
    def _guard(orig, name):
        def wrapper(cfg, *a, **kw):
            out = orig(cfg, *a, **kw)
            if _under_live_dir(out):
                raise AssertionError(
                    f"test isolation: {name}() resolved to the LIVE dir {out!r} — "
                    f"set cfg['paths'] (cortex_home / wake_state_file / …) to "
                    f"tmp_path so no test touches real ~/.config/marrow/ runtime")
            return out
        return wrapper
    for mod, name in _GUARDED_PATH_FUNCS:
        monkeypatch.setattr(mod, name, _guard(getattr(mod, name), name))


@pytest.fixture
def marrow_conn(tmp_path):
    conn = db.connect_path(tmp_path / "marrow.db")
    yield conn
    conn.close()


@pytest.fixture
def base_cfg(tmp_path):
    return {
        "core": {"timezone": "Australia/Melbourne"},
        "paths": {
            "marrow_db": str(tmp_path / "marrow.db"),
            "knowledgec_db": "",
            "geofence_file": "",
            "health_export": "",
        },
        "knowledgec": {"stream_name": "/app/usage", "categories": {"default": "uncategorized"}},
        "geofence": {"enabled": False},
        "health": {"enabled": False},
    }


def make_knowledgec_fixture(path, rows):
    """rows: list of (bundle_id, start_coredata, end_coredata) seconds since
    2001-01-01 UTC, matching real ZOBJECT column semantics."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ZOBJECT (ZSTREAMNAME TEXT, ZVALUESTRING TEXT, ZSTARTDATE REAL, ZENDDATE REAL)"
    )
    conn.executemany(
        "INSERT INTO ZOBJECT (ZSTREAMNAME, ZVALUESTRING, ZSTARTDATE, ZENDDATE) VALUES ('/app/usage', ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
