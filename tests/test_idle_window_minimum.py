"""The idle window is a MINIMUM interval: never fire early, never re-fire on an
interrupted delivery.

Covers the four rules:
  1. an armed alarm (next_wake_at) owns the deadline — the idle cycle does not
     tick underneath it (daemon + watchdog),
  2. a user message resets the silence basis (the marrow hook stamps
     last_user_msg_ts synchronously; the transcript read lags it, and the same
     hook write drops tuck_pending — the cycle's only other gate),
  3. the fire is accounted BEFORE delivery, so an esc-interrupted / failed
     injection is still consumed — no re-fire inside the window,
  4. a deadline already overdue at daemon start re-arms from now instead of
     delivering on the spot.

No real window/process: window.inject_prompt and the watchdog spawn are stubbed.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from cortex import config, daemon, db, wake_state, watchdog, window


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    c["paths"]["ny_db_pages"] = str(tmp_path / "ny")
    c["paths"]["wake_timing_log"] = str(home / "wake_timing.log")
    c["paths"]["handoff_file"] = str(home / "handoff.md")
    c["daemon"]["socket_path"] = str(tmp_path / "d.sock")
    return c


@pytest.fixture
def typed(monkeypatch):
    """Every keystroke the free-round delivery would type into the window."""
    lines: list[str] = []
    monkeypatch.setattr(window, "inject_prompt",
                        lambda c, text: lines.append(text) or True)
    return lines


@pytest.fixture
def awake(cfg, monkeypatch):
    monkeypatch.setattr("cortex.watchdog.spawn", lambda c: None)
    monkeypatch.setattr("cortex.lie_down._notify_daemon", lambda *a, **k: None)
    conn = db.connect(cfg)
    conn.close()
    wake_state.set_awake(cfg, None, None)
    return cfg


def _silent_max(cfg) -> float:
    return float(cfg["wake"].get("watchdog", {}).get("silent_max_min", 20))


def _minutes_ago(mins: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=mins)).isoformat()


class FakeScheduler:
    def __init__(self):
        self.table = {}

    def schedule(self, shell, at, callback):
        self.table[shell] = (at, callback)

    def cancel(self, shell):
        self.table.pop(shell, None)


# --- 1. an armed alarm suppresses the idle cycle ---------------------------

def test_alarm_pending_suppresses_the_silence_fire(awake, typed):
    """Alarm armed while awake -> the idle cycle holds even though the silence
    bar has long elapsed. Mutual exclusion, not min()."""
    cfg = awake
    wake_state.update(cfg, user_replied_this_wake=True,
                      last_user_msg_ts=_minutes_ago(90))
    due = datetime.now(config.get_tz(cfg)) + timedelta(minutes=30)
    wake_state.set_next_wake_at(cfg, due.isoformat())

    st = wake_state.load(cfg)
    assert daemon.silence_due_in(cfg, st) is None
    assert daemon.business_reason(cfg, st, datetime.now(config.get_tz(cfg))) is None
    assert watchdog.silence_action(cfg, silent_min=90.0) is None
    assert typed == []


def test_business_deadline_is_the_alarm_not_the_idle_cycle(awake):
    """The business key is armed on the alarm instant, never on an earlier idle
    deadline."""
    cfg = awake
    wake_state.update(cfg, user_replied_this_wake=True,
                      last_user_msg_ts=_minutes_ago(90))
    due = datetime.now(config.get_tz(cfg)) + timedelta(minutes=30)
    wake_state.set_next_wake_at(cfg, due.isoformat())
    d = daemon.WakeDaemon(cfg, scheduler=FakeScheduler(), clock=time.time)
    assert d._next_business_at(time.time()) == pytest.approx(due.timestamp(), abs=1)


def test_kick_still_fires_with_an_alarm_armed(awake, typed):
    """An external kick is an explicit request, not idle — it is not what the
    alarm excludes."""
    cfg = awake
    wake_state.set_next_wake_at(
        cfg, (datetime.now(config.get_tz(cfg))
              + timedelta(minutes=30)).isoformat())
    assert wake_state.mark_kick_round(cfg) is True
    assert daemon.silence_due_in(cfg, wake_state.load(cfg)) == 0.0


# --- 2. a user message resets the silence basis ----------------------------

def test_basis_prefers_the_hook_stamp_over_the_lagging_transcript(awake):
    """The live re-fire: the marrow hook stamps last_user_msg_ts and drops
    tuck_pending in one write, seconds before the message reaches the jsonl.
    A transcript-only basis still reads 40min silence with no gate left."""
    cfg = awake
    wake_state.update(cfg, user_replied_this_wake=True,
                      last_user_msg_ts=_minutes_ago(0.1))
    assert wake_state.silence_basis_min(cfg, 40.0) == pytest.approx(0.1, abs=0.2)


def test_user_message_restarts_the_full_window(awake, typed):
    """Replay of the marrow hook write (next_wake_at + tuck_pending dropped,
    user_replied + last_user_msg_ts stamped): the next fire is a FULL window
    away, not immediate."""
    cfg = awake
    wake_state.update(cfg, user_replied_this_wake=True,
                      last_user_msg_ts=_minutes_ago(40),
                      tuck_pending=_minutes_ago(40))
    wake_state.set_next_wake_at(cfg, _minutes_ago(5))
    # --- the marrow hook write, byte-for-byte (marrow edits this file directly,
    # its venv cannot import cortex) ---
    with wake_state._flock(cfg):
        d = wake_state.load(cfg)
        d.pop("tuck_pending", None)
        d.pop("next_wake_at", None)
        d["user_replied_this_wake"] = True
        d["last_user_msg_ts"] = datetime.now(timezone.utc).isoformat()
        wake_state._save(cfg, d)

    assert wake_state.get_next_wake_at(cfg) is None      # alarm cancelled
    assert watchdog.silence_action(cfg, silent_min=40.0) is None  # basis reset
    assert typed == []
    assert daemon.silence_due_in(cfg, wake_state.load(cfg)) == pytest.approx(
        _silent_max(cfg) * 60, abs=5)


# --- 3. the fire is accounted before delivery ------------------------------

def test_interrupted_delivery_is_still_consumed(awake, monkeypatch):
    """esc / no resident window -> the injection fails, but the round is already
    accounted: no retry, no re-fire inside the window."""
    cfg = awake
    wake_state.update(cfg, user_replied_this_wake=True,
                      last_user_msg_ts=_minutes_ago(40))
    monkeypatch.setattr(window, "inject_prompt",
                        lambda c, text: (_ for _ in ()).throw(
                            window.WindowError("esc interrupted")))

    assert watchdog.silence_action(cfg, silent_min=40.0) == "free-round appended"
    stamped = wake_state.load(cfg).get("tuck_pending")
    assert stamped is not None  # ledger advanced despite the failed delivery

    # Same poll conditions a second later -> held, and the stamp is untouched.
    assert watchdog.silence_action(cfg, silent_min=41.0) is None
    assert wake_state.load(cfg).get("tuck_pending") == stamped
    assert daemon.silence_due_in(cfg, wake_state.load(cfg)) == pytest.approx(
        _silent_max(cfg) * 60, abs=5)


def test_alarm_is_consumed_before_the_wake_is_delivered(cfg, monkeypatch):
    """A due alarm is cleared the moment the fire is decided, so an interrupted
    delivery cannot re-fire on the next deadline. A raising delivery then lands
    on the re-arm path (a fresh alarm, not the old due one) instead of losing
    the alarm entirely."""
    import cortex.wake as wake_mod
    seen = {}

    def fake_run_wake(conn, c, decision, now=None, **kw):
        seen["ledger_at_delivery"] = wake_state.get_next_wake_at(c)
        raise RuntimeError("esc: delivery interrupted")

    cfg["pacemaker"]["dry_run"] = False
    monkeypatch.setattr(wake_mod, "run_wake", fake_run_wake)
    tz = config.get_tz(cfg)
    wake_state.set_next_wake_at(cfg, (datetime.now(tz)
                                      - timedelta(minutes=1)).isoformat())
    d = daemon.WakeDaemon(cfg, scheduler=FakeScheduler(), clock=time.time)
    d.business_once()
    assert seen["ledger_at_delivery"] is None       # cleared BEFORE delivery
    rearmed = datetime.fromisoformat(wake_state.get_next_wake_at(cfg))
    assert rearmed > datetime.now(tz)               # future, so no re-fire now


# --- 4. overdue at startup re-arms, never delivers -------------------------

def test_startup_with_an_overdue_window_does_not_fire(awake, typed):
    cfg = awake
    wake_state.update(cfg, awake_since=_minutes_ago(120))  # never spoke, overdue
    d = daemon.WakeDaemon(cfg, scheduler=FakeScheduler(), clock=time.time)
    assert daemon.silence_due_in(cfg, wake_state.load(cfg)) == 0.0  # due pre-arm

    d.arm()

    assert typed == []                                   # nothing delivered
    assert d.business_once() == "business: nothing due"
    assert typed == []
    assert daemon.silence_due_in(cfg, wake_state.load(cfg)) == pytest.approx(
        _silent_max(cfg) * 60, abs=5)                    # full fresh window
    assert d.scheduler.table["cli"][0] == pytest.approx(
        time.time() + _silent_max(cfg) * 60, abs=5)


def test_startup_keeps_a_pending_kick(awake):
    """A queued external kick is an explicit request — startup re-arming must
    not swallow it."""
    cfg = awake
    wake_state.update(cfg, awake_since=_minutes_ago(120))
    wake_state.mark_kick_round(cfg)
    d = daemon.WakeDaemon(cfg, scheduler=FakeScheduler(), clock=time.time)
    d.arm()
    assert wake_state.peek_kick_round(cfg) is True
    assert daemon.silence_due_in(cfg, wake_state.load(cfg)) == 0.0
