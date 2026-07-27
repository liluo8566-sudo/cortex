"""python -m cortex.install — register/remove the cortex launchd jobs.

Mirrors marrow's plist install (gui/<uid> domain, template token resolve).
Two jobs: the collect tick (StartInterval from tick.collect_interval_sec, so
cadence stays config-driven) and the always-on wake daemon (KeepAlive). Cortex
owns only its own jobs; hooks/MCP belong to marrow.

_RETIRED holds labels this repo used to install (the pacemaker cron). They are
booted out on both install and remove, but their resolved plist file is left in
place on disk — it is the rollback path, and deleting it would strand it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cortex import config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEPLOY_DIR = _REPO_ROOT / "deploy"
_VENV_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python"
_CONFIG_DIR = Path.home() / ".config" / "marrow"
_LOG_DIR = _CONFIG_DIR / "logs"
_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
_PATH_ENV = (
    f"{Path.home() / '.local' / 'bin'}:"
    f"{_REPO_ROOT / '.venv' / 'bin'}:"
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
)

_PLISTS: list[tuple[str, str]] = [
    ("ct-collect-tick.plist", "com.cortex.collect-tick"),
    ("ct-wake-daemon.plist", "com.cortex.wake-daemon"),
]

# Retired labels: unloaded, never file-deleted (T11 P4 — rollback insurance).
_RETIRED: list[str] = ["com.cortex.pacemaker-tick"]


def venv_python() -> Path:
    """The repo venv interpreter — single source for the __VENV_PYTHON__ token
    in the launchd plists."""
    return _VENV_PYTHON


def _launchctl(*args: str) -> tuple[int, str]:
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _uid() -> str:
    return subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()


def _resolve(text: str, cfg: dict) -> str:
    tick = cfg["tick"]
    return (
        text
        .replace("__VENV_PYTHON__", str(_VENV_PYTHON))
        .replace("__PROJECT_DIR__", str(_REPO_ROOT))
        .replace("__LOG_DIR__", str(_LOG_DIR))
        .replace("__PATH_ENV__", _PATH_ENV)
        .replace("__COLLECT_INTERVAL_SEC__", str(int(tick["collect_interval_sec"])))
    )


def _bootout_retired(domain: str) -> None:
    """Unload every retired job. The resolved plist file stays on disk (manual
    rollback: `launchctl bootstrap <domain> <file>`)."""
    for label in _RETIRED:
        tgt = _LAUNCH_AGENTS / f"{label}.plist"
        if tgt.exists():
            _launchctl("bootout", domain, str(tgt))
            print(f"  ok {label} unloaded (plist left in place)")


def install_plists() -> bool:
    cfg = config.load()
    _LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{_uid()}"
    _bootout_retired(domain)
    errors = 0
    for fname, label in _PLISTS:
        src = _DEPLOY_DIR / fname
        tgt = _LAUNCH_AGENTS / f"{label}.plist"
        tgt.write_text(_resolve(src.read_text(), cfg))
        _launchctl("bootout", domain, str(tgt))  # tolerated
        rc, msg = _launchctl("bootstrap", domain, str(tgt))
        if rc == 0:
            print(f"  ok {label} loaded")
        else:
            print(f"  fail {label}: {msg}", file=sys.stderr)
            errors += 1
    return errors == 0


def remove_plists() -> None:
    domain = f"gui/{_uid()}"
    _bootout_retired(domain)
    for _fname, label in _PLISTS:
        tgt = _LAUNCH_AGENTS / f"{label}.plist"
        if not tgt.exists():
            continue
        _launchctl("bootout", domain, str(tgt))
        tgt.unlink(missing_ok=True)
        print(f"  ok removed {label}")


def main(argv: list[str]) -> int:
    if argv and argv[0] == "remove":
        remove_plists()
        return 0
    return 0 if install_plists() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
