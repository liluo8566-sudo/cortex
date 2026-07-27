"""The conftest live-config barrier (_no_live_config_writes) must FAIL LOUDLY the
moment any cortex state/log/note path resolves under the real ~/.config/marrow/.
Tonight's incident: the suite wrote test notes/audit into live runtime files. The
barrier is the hard wall; these tests prove it fires (and does not fire on a
tmp-isolated cfg)."""
from __future__ import annotations

import pytest

from cortex import config, wake_state


def _live_fallback_cfg():
    """A cfg with empty [paths] — every path builder falls back to the live
    ~/.config/marrow/ default (the incident-shape config)."""
    return {"core": {"timezone": "Australia/Melbourne"},
            "paths": {}, "wake": {}}


@pytest.mark.parametrize("resolve", [
    lambda c: config.cortex_home(c),
    lambda c: config.state_dir(c),
    lambda c: config.wake_audit_log_path(c),
    lambda c: config.wake_timing_log_path(c),
    lambda c: wake_state.wake_state_path(c),
    lambda c: wake_state.wakeup_note_path(c),
    lambda c: wake_state.watchdog_pidfile_path(c),
])
def test_barrier_blocks_live_path_resolution(resolve):
    c = _live_fallback_cfg()
    with pytest.raises(AssertionError, match="LIVE dir"):
        resolve(c)


def test_barrier_allows_tmp_isolated_paths(tmp_path):
    c = {"core": {"timezone": "Australia/Melbourne"},
         "paths": {"cortex_home": str(tmp_path / "home"),
                   "wake_timing_log": str(tmp_path / "t.log")},
         "wake": {}}
    # None of these raise: all resolve under tmp_path.
    assert str(config.cortex_home(c)).startswith(str(tmp_path))
    assert str(wake_state.wake_state_path(c)).startswith(str(tmp_path))
    assert str(config.wake_timing_log_path(c)).startswith(str(tmp_path))
