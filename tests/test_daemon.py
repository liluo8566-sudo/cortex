"""cortex.daemon — scheduler-hosted cli timing loop.

Covers: both deadlines armed at start, re-arm after a firing AND after a raising
callback (the alive-but-unarmed trap), kick delivery over a real tmp-path unix
socket in lie_down's wire format, business reason precedence (awake gate /
ledger / kick reasons / silence), next-deadline computation with the anti-spin
retry floor, reconcile delegation to cortex.reconcile, the
singleton lock and the [daemon] config section surviving config.load.

No real windows/processes/sockets outside tmp_path: run_wake, silence_action and
the reconcile internals are all stubbed.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex import config, daemon, lie_down, reconcile, wake_state


@pytest.fixture
def short_dir():
    """pytest's tmp_path is far too long for an AF_UNIX address (104 bytes) —
    socket tests need a short one."""
    d = Path(tempfile.mkdtemp(prefix="ctd", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


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


class FakeScheduler:
    def __init__(self):
        self.table = {}

    def schedule(self, shell, at, callback):
        self.table[shell] = (at, callback)

    def cancel(self, shell):
        self.table.pop(shell, None)


@pytest.fixture
def clock():
    return {"now": 1_000_000.0}


def _daemon(cfg, clock, scheduler=None):
    return daemon.WakeDaemon(cfg, scheduler=scheduler or FakeScheduler(),
                             clock=lambda: clock["now"])


# --- arming -------------------------------------------------------------

def test_arm_holds_both_deadlines(cfg, clock):
    d = _daemon(cfg, clock)
    d.arm()
    assert set(d.scheduler.table) == {"cli", "cli.reconcile"}
    assert d.scheduler.table["cli.reconcile"][0] == clock["now"] + 60


def test_business_always_armed_when_idle(cfg, clock):
    """kick() no-ops on a shell with no entry -> the business key must carry an
    entry even when nothing is pending (safety horizon)."""
    d = _daemon(cfg, clock)
    d.arm()
    assert d.scheduler.table["cli"][0] == clock["now"] + d.horizon


def test_reconcile_rearms_after_firing(cfg, clock, monkeypatch):
    d = _daemon(cfg, clock)
    d.arm()
    monkeypatch.setattr(d, "reconcile_once", lambda: "ok")
    d.scheduler.table.pop("cli.reconcile")  # scheduler consumes before callback
    asyncio.run(d._on_reconcile("cli.reconcile"))
    assert d.scheduler.table["cli.reconcile"][0] == clock["now"] + 60


def test_reconcile_rearms_when_callback_raises(cfg, clock, monkeypatch):
    def boom():
        raise RuntimeError("reconcile blew up")

    d = _daemon(cfg, clock)
    d.arm()
    monkeypatch.setattr(d, "reconcile_once", boom)
    d.scheduler.table.pop("cli.reconcile")
    with pytest.raises(RuntimeError):
        asyncio.run(d._on_reconcile("cli.reconcile"))
    assert "cli.reconcile" in d.scheduler.table  # never alive-but-unarmed


def test_business_rearms_when_callback_raises(cfg, clock, monkeypatch):
    def boom():
        raise RuntimeError("business blew up")

    d = _daemon(cfg, clock)
    d.arm()
    monkeypatch.setattr(d, "business_once", boom)
    d.scheduler.table.pop("cli")
    with pytest.raises(RuntimeError):
        asyncio.run(d._on_business("cli"))
    assert "cli" in d.scheduler.table


# --- next business deadline --------------------------------------------

def _tz(cfg):
    return config.get_tz(cfg)


def test_next_business_at_tracks_the_ledger(cfg, clock):
    d = _daemon(cfg, clock)
    due = datetime.now(_tz(cfg)) + timedelta(minutes=17)
    wake_state.set_next_wake_at(cfg, due.isoformat())
    at = d._next_business_at(time.time())
    assert abs(at - due.timestamp()) < 1.0


def test_next_business_at_retries_a_past_ledger(cfg, clock):
    """A due-but-unfired ledger (breaker / gated) re-arms on the retry interval,
    never at a past timestamp (that would hot-spin the loop)."""
    d = _daemon(cfg, clock)
    past = datetime.now(_tz(cfg)) - timedelta(minutes=5)
    wake_state.set_next_wake_at(cfg, past.isoformat())
    now = time.time()
    assert d._next_business_at(now) == pytest.approx(now + d.retry)


def test_next_business_at_idle_uses_horizon(cfg, clock):
    d = _daemon(cfg, clock)
    now = time.time()
    assert d._next_business_at(now) == pytest.approx(now + d.horizon)


# --- business reasons ---------------------------------------------------

def test_reason_ledger_due(cfg):
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now - timedelta(minutes=1)).isoformat())
    st = wake_state.load(cfg)
    assert daemon.business_reason(cfg, st, now) == "next_wake_at"


def test_reason_future_ledger_holds(cfg):
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now + timedelta(minutes=30)).isoformat())
    st = wake_state.load(cfg)
    assert daemon.business_reason(cfg, st, now) is None


def test_reason_kick_when_reasons_queued_and_no_ledger(cfg):
    wake_state.update(cfg, kick_reasons=["Msg #1 replied"])
    st = wake_state.load(cfg)
    assert daemon.business_reason(cfg, st, datetime.now(_tz(cfg))) == "kick"


def test_awake_never_wakes_and_kick_round_is_silence(cfg):
    wake_state.set_awake(cfg, 1, None)
    wake_state.set_next_wake_at(cfg, datetime.now(_tz(cfg)).isoformat())
    st = wake_state.load(cfg)
    assert daemon.business_reason(cfg, st, datetime.now(_tz(cfg))) is None
    wake_state.mark_kick_round(cfg)
    st = wake_state.load(cfg)
    assert daemon.business_reason(cfg, st, datetime.now(_tz(cfg))) == "silence"


def test_silence_due_in_counts_down_from_awake_since(cfg):
    wake_state.set_awake(cfg, 1, None)
    st = wake_state.load(cfg)
    silent_max = float(cfg["wake"].get("watchdog", {}).get("silent_max_min", 20))
    assert daemon.silence_due_in(cfg, st) == pytest.approx(silent_max * 60, abs=5)
    assert daemon.silence_due_in(cfg, {}) is None


# --- fire path ----------------------------------------------------------

def test_business_fires_wake_and_redraws_ledger_on_non_window(cfg, clock, monkeypatch):
    import cortex.wake as wake_mod
    seen = {}

    def fake_run_wake(conn, c, decision, now=None, **kw):
        seen["decision"] = decision
        return {"mode": "failed"}

    monkeypatch.setattr(wake_mod, "run_wake", fake_run_wake)
    wake_state.set_next_wake_at(
        cfg, (datetime.now(_tz(cfg)) - timedelta(minutes=1)).isoformat())
    d = _daemon(cfg, clock)
    line = d.business_once()
    assert seen["decision"]["wake_reasons"] == "next_wake_at"
    assert "mode=failed" in line
    # the round ended without a window -> a fresh floor is on the ledger again
    assert wake_state.get_next_wake_at(cfg) is not None


def test_business_breaker_holds(cfg, clock, monkeypatch):
    import cortex.wake as wake_mod
    from cortex import breaker
    monkeypatch.setattr(wake_mod, "run_wake",
                        lambda *a, **k: pytest.fail("no wake while held"))
    breaker.pause(cfg)
    overdue = (datetime.now(_tz(cfg)) - timedelta(minutes=1)).isoformat()
    wake_state.set_next_wake_at(cfg, overdue)
    assert "breaker held" in _daemon(cfg, clock).business_once()
    # The alarm is NOT consumed by the hold — it fires once the breaker clears.
    assert wake_state.get_next_wake_at(cfg) == overdue


def test_business_silence_calls_silence_action(cfg, clock, monkeypatch):
    import cortex.watchdog as watchdog_mod
    calls = []
    monkeypatch.setattr(watchdog_mod, "silence_action",
                        lambda c, s, **kw: calls.append(s) or "free round appended")
    wake_state.set_awake(cfg, 1, None)
    wake_state.mark_kick_round(cfg)
    assert "free round appended" in _daemon(cfg, clock).business_once()
    assert calls


# --- reconcile ----------------------------------------------------------

def test_reconcile_delegates_to_reconcile_module(cfg, clock, monkeypatch):
    monkeypatch.setattr(reconcile, "_reconcile",
                        lambda conn, c, st, now: "ledger hold: next wake 09:00")
    assert _daemon(cfg, clock).reconcile_once().startswith("ledger hold")


def test_reconcile_awake_gate_gets_snapshot_gen(cfg, clock, monkeypatch):
    monkeypatch.setattr(reconcile, "_reconcile", lambda *a, **k: None)
    seen = {}

    def fake_awake(conn, c, st, snap_gen=None):
        seen["gen"] = snap_gen
        return "wake in progress"

    monkeypatch.setattr(reconcile, "_handle_awake", fake_awake)
    wake_state.set_awake(cfg, 1, None)
    assert _daemon(cfg, clock).reconcile_once() == "wake in progress"
    assert seen["gen"] == wake_state.load(cfg)["gen"]


def test_reconcile_breaker_holds_everything(cfg, clock, monkeypatch):
    from cortex import breaker
    monkeypatch.setattr(reconcile, "_reconcile",
                        lambda *a, **k: pytest.fail("no reconcile while held"))
    breaker.pause(cfg)
    assert "breaker held" in _daemon(cfg, clock).reconcile_once()


def test_shell_off_skips(cfg, clock):
    marrow_dir = Path(cfg["paths"]["marrow_db"]).parent
    (marrow_dir / "config.toml").write_text('[cortex]\nshells = ["tg"]\n')
    d = _daemon(cfg, clock)
    assert "shell off" in d.reconcile_once()
    assert "shell off" in d.business_once()


# --- kick over the real socket -------------------------------------------

def test_lie_down_kick_fires_the_business_deadline(cfg, short_dir, monkeypatch):
    """End-to-end wire check: lie_down._notify_daemon's "cli\\n" line must hit the
    business entry (a kick on a shell with no entry is a silent no-op)."""
    cfg["daemon"]["socket_path"] = str(short_dir / "d.sock")
    cfg["daemon"]["reconcile_interval_sec"] = 3600  # nothing else may fire
    fired = asyncio.Event()

    async def scenario():
        d = daemon.WakeDaemon(cfg)
        monkeypatch.setattr(d, "business_once",
                            lambda: fired.set() or "business: nothing due")
        d.arm()
        task = asyncio.create_task(d.scheduler.run())
        for _ in range(100):  # wait for the socket to be listening
            if config.daemon_socket_path(cfg).exists():
                break
            await asyncio.sleep(0.01)
        await asyncio.to_thread(lie_down._notify_daemon, cfg)
        try:
            await asyncio.wait_for(fired.wait(), timeout=3)
        finally:
            d.scheduler.stop()
            await asyncio.wait_for(task, timeout=3)
        assert "cli" in d.scheduler._table  # re-armed after the kick fired it

    asyncio.run(scenario())
    assert fired.is_set()


# --- process guards ------------------------------------------------------

def test_singleton_second_instance_declines(cfg):
    with daemon.singleton(cfg) as first:
        assert first is True
        with daemon.singleton(cfg) as second:
            assert second is False


def test_socket_path_length_guard(cfg, tmp_path):
    cfg["daemon"]["socket_path"] = str(tmp_path / ("x" * 120) / "d.sock")
    with pytest.raises(ValueError):
        config.daemon_socket_path(cfg)


def test_daemon_section_survives_config_load(tmp_path):
    """config.load drops sections outside the whitelist — [daemon] must be in it."""
    p = tmp_path / "c.toml"
    p.write_text("[daemon]\nreconcile_interval_sec = 5\n", encoding="utf-8")
    loaded = config.load(path=p)
    assert loaded["daemon"]["reconcile_interval_sec"] == 5
    assert loaded["daemon"]["shell"] == "cli"  # defaults merged, not replaced


def test_default_socket_path_lands_in_the_state_dir(cfg, short_dir):
    cfg["daemon"]["socket_path"] = ""
    cfg["paths"]["cortex_home"] = str(short_dir)
    p = config.daemon_socket_path(cfg)
    assert p == short_dir / "state" / "cortex-daemon.sock"
    assert len(str(p).encode()) < 104


def test_utc_helper_handles_naive_and_garbage():
    assert daemon._age_min("not-a-date") > 1e8
    ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert daemon._age_min(ts) < 1.0
