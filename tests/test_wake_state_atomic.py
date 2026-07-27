"""wake_state atomicity + lock tests: _save is atomic (temp + os.replace) and the
sibling .lock exists. Also lie_down --next-wake-min is required at the CLI."""
from __future__ import annotations

import pytest

from cortex import config, db, lie_down, wake_state


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




def _alerts_table(cfg):
    conn = db.connect(cfg)
    conn.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY,"
                 " severity TEXT, type TEXT, message TEXT, source TEXT)")
    conn.commit()
    conn.close()


def _alert_rows(cfg):
    conn = db.connect(cfg)
    try:
        return conn.execute(
            "SELECT type, message FROM alerts").fetchall()
    finally:
        conn.close()


def _never_locks(monkeypatch):
    """Every flock attempt fails and the clock jumps past the timeout on the
    first retry -> the give-up path, without the real 5s wait."""
    monkeypatch.setattr(wake_state.fcntl, "flock", _boom)
    ticks = iter(range(0, 10_000_000, 100))
    monkeypatch.setattr(wake_state, "_mono", lambda: next(ticks))


def _boom(*a, **k):
    raise OSError("locked by someone else")


def test_flock_giveup_raises_one_alert(cfg, monkeypatch):
    """A lock give-up used to be completely silent — the advisory _flock just
    wrote unlocked. It now leaves ONE alert row, throttled so a stuck lock
    cannot spam the table."""
    _alerts_table(cfg)
    _never_locks(monkeypatch)

    wake_state.update(cfg, awake=True)          # advisory path, proceeds unlocked
    with pytest.raises(wake_state.StateValidationError):
        wake_state.current_epoch(cfg)           # strict path, fails closed

    rows = _alert_rows(cfg)
    assert len(rows) == 1                       # throttled: second give-up silent
    assert rows[0]["type"] == "cortex_lock_giveup"
    assert wake_state.load(cfg).get("awake") is True  # the write still landed


def test_flock_giveup_alert_rearms_after_throttle(cfg, monkeypatch):
    _alerts_table(cfg)
    _never_locks(monkeypatch)
    cfg["wake"]["lock_alert_throttle_min"] = 0  # no throttle window

    wake_state.update(cfg, awake=True)
    wake_state.update(cfg, awake=False)

    assert len(_alert_rows(cfg)) == 2


def test_lie_down_cli_requires_next_wake_min(cfg, monkeypatch):
    monkeypatch.setenv("CORTEX_CONFIG", "/no/such/file.toml")
    # argparse required=True -> missing --next-wake-min exits non-zero.
    with pytest.raises(SystemExit) as exc:
        lie_down.main([])
    assert exc.value.code != 0
