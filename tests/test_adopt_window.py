"""Auto-adopt: a cortex window the user opened `claude` in herself (in
cortex_home) but never registered must be recorded as the resident by the
reconcile pass instead of firing/spawning a duplicate. No iTerm/ps here — the
AppleScript/ps layer is stubbed at window.find_adoptable_window /
window._list_sessions / window._claude_start_on_tty."""
from __future__ import annotations

from datetime import datetime

import pytest

from cortex import config, reconcile, wake_state, window


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


def _tz(cfg):
    return config.get_tz(cfg)


# --- adoption unit (window.find_adoptable_window) ----------------------------

def test_find_adoptable_newest_wins(cfg, monkeypatch):
    """Multiple interactive claude windows in cortex_home -> the one whose claude
    start time is NEWEST is picked."""
    home = str(config.cortex_home(cfg))
    monkeypatch.setattr(window, "_list_sessions",
                        lambda: [("SID-OLD", "/dev/ttys001"),
                                 ("SID-NEW", "/dev/ttys002")])

    def _start(ttyname, h):
        assert h == home
        return {"ttys001": 100.0, "ttys002": 200.0}.get(ttyname)
    monkeypatch.setattr(window, "_claude_start_on_tty", _start)
    assert window.find_adoptable_window(cfg) == "SID-NEW"


def test_find_adoptable_none_when_no_candidate(cfg, monkeypatch):
    monkeypatch.setattr(window, "_list_sessions",
                        lambda: [("SID-1", "/dev/ttys001")])
    monkeypatch.setattr(window, "_claude_start_on_tty", lambda t, h: None)
    assert window.find_adoptable_window(cfg) is None


def test_find_adoptable_skips_ttyless_session(cfg, monkeypatch):
    """A session with no live tty (empty) is skipped without a ps probe."""
    monkeypatch.setattr(window, "_list_sessions",
                        lambda: [("SID-1", "")])
    probed = {"n": 0}

    def _start(t, h):
        probed["n"] += 1
        return 1.0
    monkeypatch.setattr(window, "_claude_start_on_tty", _start)
    assert window.find_adoptable_window(cfg) is None
    assert probed["n"] == 0  # ttyless never probed


def test_headless_excluded_by_interactive_tty(cfg, monkeypatch):
    """A headless `claude -p` run has no controlling tty, so it never appears in
    _list_sessions' iTerm sessions and _claude_start_on_tty returns None: no
    adoption. Modeled by an empty session list (headless is not an iTerm session)."""
    monkeypatch.setattr(window, "_list_sessions", lambda: [])
    assert window.find_adoptable_window(cfg) is None


# --- tick reconcile adoption (_adopt_manual_window) --------------------------

def _stub_lock(monkeypatch):
    import contextlib
    from cortex import wake

    @contextlib.contextmanager
    def _noop(cfg):
        yield
    monkeypatch.setattr(wake, "_spawn_serialized", _noop)


def test_adopt_happy_path_records_resident(cfg, monkeypatch):
    from cortex import wake
    _stub_lock(monkeypatch)
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    monkeypatch.setattr(window, "find_adoptable_window", lambda c: "SID-MANUAL")
    monkeypatch.setattr(window, "claude_session_id", lambda c: "conv-uuid")
    msg = reconcile._adopt_manual_window(cfg)
    assert msg is not None and "SID-MANUAL" in msg
    st = wake_state.load(cfg)
    assert st.get("session_id") == "SID-MANUAL"
    assert st.get("awake") is True
    assert st.get("transcript", "").endswith("conv-uuid.jsonl")


def test_adopt_no_candidate_returns_none(cfg, monkeypatch):
    from cortex import wake
    _stub_lock(monkeypatch)
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    monkeypatch.setattr(window, "find_adoptable_window", lambda c: None)
    assert reconcile._adopt_manual_window(cfg) is None
    assert wake_state.load(cfg).get("session_id") is None


def test_adopt_alive_under_lock_skips(cfg, monkeypatch):
    """A resident that landed under the lock -> no adoption (never overwrite)."""
    from cortex import wake
    _stub_lock(monkeypatch)
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    called = {"n": 0}
    monkeypatch.setattr(window, "find_adoptable_window",
                        lambda c: called.__setitem__("n", called["n"] + 1) or "X")
    assert reconcile._adopt_manual_window(cfg) is None
    assert called["n"] == 0


def test_adopt_disabled_by_config(cfg, monkeypatch):
    from cortex import wake
    cfg["wake"]["auto_adopt"] = False
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    probed = {"n": 0}
    monkeypatch.setattr(window, "find_adoptable_window",
                        lambda c: probed.__setitem__("n", probed["n"] + 1) or "X")
    assert reconcile._adopt_manual_window(cfg) is None
    assert probed["n"] == 0  # gated before any scan


def test_adopt_cas_loss_records_nothing(cfg, monkeypatch):
    """A newer epoch superseding between token capture and set_awake -> the CAS
    returns None, adoption aborts, no session recorded."""
    from cortex import wake
    _stub_lock(monkeypatch)
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    monkeypatch.setattr(window, "find_adoptable_window", lambda c: "SID-X")
    monkeypatch.setattr(window, "claude_session_id", lambda c: None)
    monkeypatch.setattr(wake_state, "set_awake",
                        lambda *a, **k: None)  # CAS lost
    assert reconcile._adopt_manual_window(cfg) is None
    assert wake_state.load(cfg).get("session_id") is None


def test_reconcile_adopts_before_firing(cfg, monkeypatch):
    """The reconcile path adopts a manual window BEFORE any dead-window fire: an
    overdue ledger + dead recorded session but an adoptable manual window ->
    adopt, no fire."""
    from cortex import wake
    from datetime import timedelta
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    fired = {"n": 0}
    monkeypatch.setattr(reconcile, "_fire_dead_window",
                        lambda conn, c, why: fired.__setitem__("n", fired["n"] + 1))
    monkeypatch.setattr(reconcile, "_adopt_manual_window",
                        lambda c: "adopted manual window SID-M")
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now - timedelta(minutes=5)).isoformat())
    msg = reconcile._reconcile(None, cfg, {}, now)
    assert msg == "adopted manual window SID-M"
    assert fired["n"] == 0  # adopted -> never fired
