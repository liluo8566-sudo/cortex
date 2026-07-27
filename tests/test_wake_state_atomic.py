"""wake_state atomicity + lock tests: _save is atomic (temp + os.replace) and the
sibling .lock exists. Also lie_down --next-wake-min is required at the CLI."""
from __future__ import annotations

import pytest

from cortex import config, lie_down, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    return c


def test_save_is_atomic_no_tmp_left(cfg):
    wake_state.update(cfg, awake=True)
    p = wake_state.wake_state_path(cfg)
    assert p.exists()
    # No stray temp files from the atomic replace.
    leftovers = list(p.parent.glob("*.tmp.*"))
    assert leftovers == []


def test_lock_file_path_is_sibling(cfg):
    lp = wake_state.lock_path(cfg)
    assert lp == wake_state.wake_state_path(cfg).with_suffix(".lock")


def test_mark_kick_round_once_then_take(cfg):
    """External-wake carrier primitive: marks only while awake, idempotent (a
    second mark before consumption is a no-op), and take_kick_round consumes it
    exactly once."""
    wake_state.set_awake(cfg, 1, None)
    assert wake_state.mark_kick_round(cfg) is True
    assert wake_state.mark_kick_round(cfg) is False  # already pending -> no-op
    assert wake_state.peek_kick_round(cfg) is True
    assert wake_state.take_kick_round(cfg) is True
    assert wake_state.peek_kick_round(cfg) is False
    assert wake_state.take_kick_round(cfg) is False  # already consumed


def test_mark_kick_round_noop_when_asleep(cfg):
    wake_state.update(cfg, awake=None)
    assert wake_state.mark_kick_round(cfg) is False
    assert wake_state.peek_kick_round(cfg) is False




def test_lie_down_cli_requires_next_wake_min(cfg, monkeypatch):
    monkeypatch.setenv("CORTEX_CONFIG", "/no/such/file.toml")
    # argparse required=True -> missing --next-wake-min exits non-zero.
    with pytest.raises(SystemExit) as exc:
        lie_down.main([])
    assert exc.value.code != 0
