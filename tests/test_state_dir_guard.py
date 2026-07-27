"""Guard: every runtime file resolver (json/log/lock/pid) must land under
the state/ subdirectory. Prevents future files from accidentally landing in
cortex root."""
from __future__ import annotations

import pytest

from cortex import config, wake_state

_RUNTIME_EXTENSIONS = {".json", ".log", ".lock", ".pid"}


def _runtime_resolvers():
    """Collect (name, callable) for every path resolver returning a runtime
    file (extension in _RUNTIME_EXTENSIONS). Excludes cortex_home/state_dir
    (directories), wishlist/handoff/wakeup_note (md files), and timing log
    (lives under ~/.config/marrow/logs/, not cortex/)."""
    pairs = []
    for name in ("affect_flag_path", "self_schedule_path", "wake_audit_log_path"):
        pairs.append((f"config.{name}", getattr(config, name)))
    for name in ("wake_state_path", "watchdog_pidfile_path"):
        pairs.append((f"wake_state.{name}", getattr(wake_state, name)))
    return pairs


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    return c


@pytest.mark.parametrize("name,resolver", _runtime_resolvers(),
                         ids=[n for n, _ in _runtime_resolvers()])
def test_runtime_files_land_in_state_dir(cfg, name, resolver):
    p = resolver(cfg)
    assert p.suffix in _RUNTIME_EXTENSIONS, (
        f"{name} returns {p} with unexpected extension {p.suffix}")
    assert p.parent.name == "state", (
        f"{name} resolved to {p} — expected parent dir 'state', "
        f"got '{p.parent.name}'")
