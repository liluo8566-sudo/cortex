from __future__ import annotations

import pytest

from cortex import config, watchdog, wake, wake_state
import cortex.lie_down as lie_down_mod


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    c["paths"]["handoff_file"] = str(tmp_path / "handoff.md")
    # Parent of marrow_db = the shared config dir (breaker.json / fuse_events.json).
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c.setdefault("wake", {}).setdefault("watchdog", {})["fuse_handoff_grace_sec"] = 1.0
    return c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Fuse polls in a bounded wall-clock loop; skip the real sleeps so tests are
    # fast (the deadline still bounds iterations via time.time()).
    monkeypatch.setattr(watchdog.time, "sleep", lambda *_a, **_k: None)


def _stub_window(monkeypatch, calls):
    monkeypatch.setattr(watchdog.window, "send_esc", lambda cfg: calls.append("esc"))
    # FUSE now delivers only the ⚙️ [FUSE] marker covertly (bell/typed); the body
    # is injected marrow-side. Stub the covert delivery, capturing the marker.
    monkeypatch.setattr(watchdog.window, "deliver_covert_marker",
                        lambda cfg, line: calls.append(("marker", line)) or "bell")
    monkeypatch.setattr(watchdog, "_verify_esc_or_hard_interrupt",
                        lambda cfg, grace, trig: None)


def test_fuse_session_lies_down_itself_no_force(cfg, monkeypatch):
    calls = []
    _stub_window(monkeypatch, calls)
    forced = []
    monkeypatch.setattr(lie_down_mod, "lie_down",
                        lambda cfg, force_slept=None: forced.append(force_slept))
    wake_state.update(cfg, awake=True)

    real_load = wake_state.load
    seen = {"n": 0}

    def fake_load(c):
        seen["n"] += 1
        d = real_load(c)
        if seen["n"] >= 2:  # session lay down after the first poll
            d = dict(d)
            d.pop("awake", None)
        return d

    monkeypatch.setattr(watchdog.wake_state, "load", fake_load)
    watchdog._fuse(cfg, grace=0.0)
    assert "esc" in calls
    assert any(c[0] == "marker" and "[FUSE]" in c[1] for c in calls
               if isinstance(c, tuple))  # only the marker delivered, not the body
    assert forced == []  # session lay down itself -> no proxy lie_down


def test_fuse_timeout_no_handoff_forces_with_marker(cfg, monkeypatch):
    calls = []
    _stub_window(monkeypatch, calls)
    forced = []
    monkeypatch.setattr(lie_down_mod, "lie_down",
                        lambda cfg, force_slept=None: forced.append(force_slept))
    wake_state.update(cfg, awake=True)  # stays awake through grace
    watchdog._fuse(cfg, grace=0.0)
    assert len(forced) == 1
    assert forced[0] == "fuse"  # no handoff -> force_slept marker fires


def test_fuse_timeout_with_handoff_forces_without_marker(cfg, monkeypatch):
    calls = []
    _stub_window(monkeypatch, calls)
    forced = []

    def fake_deliver(c, line):
        # Simulate the session writing its handoff (but never lying down).
        config.handoff_path(c).write_text("did stuff", encoding="utf-8")
        calls.append(("marker", line))
        return "bell"

    monkeypatch.setattr(watchdog.window, "deliver_covert_marker", fake_deliver)
    monkeypatch.setattr(lie_down_mod, "lie_down",
                        lambda cfg, force_slept=None: forced.append(force_slept))
    wake_state.update(cfg, awake=True)
    watchdog._fuse(cfg, grace=0.0)
    assert len(forced) == 1
    assert forced[0] is None  # handoff written -> clean proxy, no catchup marker


def test_run_dead_window_retires_no_proxy_sleep(cfg, monkeypatch):
    # An accidentally-closed window: ledger still awake, but the window is dead.
    # The watchdog must retire immediately WITHOUT firing any proxy lie_down
    # (silence_action / _fuse), so reconcile's rescue branch owns the revival.
    wake_state.update(cfg, awake=True)
    monkeypatch.setattr(watchdog.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)

    forced = []
    monkeypatch.setattr(lie_down_mod, "lie_down",
                        lambda cfg, force_slept=None: forced.append(force_slept))
    # If the guard let control reach fuse/silence, these would run; make them
    # loud so a regression is obvious.
    monkeypatch.setattr(watchdog, "_fuse",
                        lambda *a, **k: pytest.fail("fuse ran on dead window"))
    monkeypatch.setattr(watchdog, "silence_action",
                        lambda *a, **k: pytest.fail("silence ran on dead window"))

    rc = watchdog.run(cfg)
    assert rc == 0
    assert forced == []  # no proxy lie_down for a dead window


def test_run_alive_window_reaches_idle_gate(cfg, monkeypatch):
    # An alive window keeps current behaviour: the poll proceeds to the idle gate.
    wake_state.update(cfg, awake=True)
    monkeypatch.setattr(watchdog.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(watchdog.transcript, "user_silent_min", lambda c: 0.0)
    monkeypatch.setattr(watchdog.transcript, "window_tokens", lambda c: 0)

    class _FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(watchdog.db, "connect", lambda c: _FakeConn())
    monkeypatch.setattr(watchdog.occupancy, "store_window_tokens",
                        lambda conn, tokens: None)

    reached = {"silence": False}

    def fake_silence(cfg, silent_min, **kw):
        reached["silence"] = True
        wake_state.update(cfg, awake=False)  # end the loop cleanly
        return "test-stop"

    monkeypatch.setattr(watchdog, "silence_action", fake_silence)
    rc = watchdog.run(cfg)
    assert rc == 0
    assert reached["silence"] is True  # alive -> idle gate ran (unchanged path)
