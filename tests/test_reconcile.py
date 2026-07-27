"""Ledger + reconcile + circuit-breaker gating (schedule reliability fix).

Covers: next_wake_at write/clear, no night clamp (P8), the reconcile decision
matrix (alive-never-touch / rotated-vs-resume / accidental-close / future-hold),
breaker gating, per-session _window_alive. No iTerm/claude here — all
machine-touching calls are stubbed."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cortex import breaker, config, daemon, db, lie_down, reconcile, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    c["paths"]["ny_db_pages"] = str(tmp_path / "ny")  # isolate symlinks.ensure_all
    c["paths"]["wake_timing_log"] = str(home / "wake_timing.log")  # not under cortex_home default
    c["paths"]["handoff_file"] = str(home / "handoff.md")
    return c


def _tz(cfg):
    return config.get_tz(cfg)


# --- ledger write/clear -------------------------------------------------------

def test_ledger_write_and_clear(cfg):
    assert wake_state.get_next_wake_at(cfg) is None
    wake_state.set_next_wake_at(cfg, "2026-07-13T09:00:00+10:00")
    assert wake_state.get_next_wake_at(cfg) == "2026-07-13T09:00:00+10:00"
    wake_state.clear_next_wake_at(cfg)
    assert wake_state.get_next_wake_at(cfg) is None


def test_lie_down_persists_ledger(cfg):
    wake_state.set_awake(cfg, 1, None)  # a wake in progress
    lie_down.lie_down(cfg, force_slept="stale", next_wake_min=30)
    assert wake_state.get_next_wake_at(cfg) is not None  # ledger written by lie_down


def test_persist_next_wake_at_writes_ledger_and_kicks(cfg, monkeypatch):
    """The ledger write is a plain wake_state-level call — it is the alarm, and
    it kicks the daemon when one is configured."""
    kicks = []
    monkeypatch.setattr(lie_down, "_notify_daemon", lambda c: kicks.append(c))
    now = datetime(2026, 7, 13, 9, 0, tzinfo=_tz(cfg))
    assert lie_down.persist_next_wake_at(cfg, now) is True
    assert wake_state.get_next_wake_at(cfg) == now.isoformat()
    assert len(kicks) == 1
    assert lie_down.persist_next_wake_at(cfg, None) is True  # None clears
    assert wake_state.get_next_wake_at(cfg) is None


def test_notify_daemon_noop_without_socket(cfg, monkeypatch):
    """No [daemon] socket configured -> no socket touched at all."""
    def _boom(*_a, **_k):
        raise AssertionError("socket must not be opened")
    monkeypatch.setattr(lie_down.socket, "socket", _boom)
    lie_down._notify_daemon(cfg)  # no [daemon] section in defaults


def test_notify_daemon_sends_shell_id(cfg, monkeypatch):
    sent = []

    class _FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect(self, path):
            sent.append(("connect", path))

        def sendall(self, payload):
            sent.append(("send", payload))

    monkeypatch.setattr(lie_down.socket, "socket", lambda *a, **k: _FakeSock())
    cfg["daemon"] = {"socket_path": "/tmp/ct-daemon.sock"}
    lie_down._notify_daemon(cfg)
    assert sent == [("connect", "/tmp/ct-daemon.sock"), ("send", b"cli\n")]


def test_notify_daemon_swallows_dead_daemon(cfg, monkeypatch):
    """Daemon down (socket file present but nobody listening) -> silent no-op."""
    class _Dead:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect(self, path):
            raise ConnectionRefusedError

    monkeypatch.setattr(lie_down.socket, "socket", lambda *a, **k: _Dead())
    cfg["daemon"] = {"socket_path": "/tmp/ct-daemon.sock"}
    lie_down._notify_daemon(cfg)


def test_set_awake_clears_ledger(cfg):
    wake_state.set_next_wake_at(cfg, "2026-07-13T09:00:00+10:00")
    wake_state.set_awake(cfg, 1, None)  # a fresh wake fired -> ledger consumed
    assert wake_state.get_next_wake_at(cfg) is None


def test_lie_down_rotate_records_retired_sid(cfg):
    """lie_down(rotate=True) durably records the retiring session's sid (the
    transcript jsonl stem) at the same moment it sets the one-shot rotated
    flag — the belt-and-braces guard that outlives that flag being consumed
    by an unrelated later wake."""
    wake_state.set_awake(cfg, 1, "/t/retiring.jsonl")
    lie_down.lie_down(cfg, rotate=True, next_wake_min=30)
    assert wake_state.get_retired_sid(cfg) == "retiring"


def test_lie_down_no_rotate_leaves_retired_sid_untouched(cfg):
    wake_state.set_awake(cfg, 1, "/t/still-alive.jsonl")
    lie_down.lie_down(cfg, rotate=False, next_wake_min=30)
    assert wake_state.get_retired_sid(cfg) is None


# --- rotate: spawn authority is the wake daemon only (no direct spawn) --------

def test_lie_down_rotate_never_spawns_directly(cfg, monkeypatch):
    """Spawn authority belongs exclusively to the wake daemon: lie_down(rotate=True)
    must NOT spawn a successor itself. It only sets the one-shot rotated flag and
    writes the ledger at the requested time — the daemon fires the successor."""
    from cortex import wake
    fired = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    wake_state.set_awake(cfg, 1, "/t/retiring.jsonl")
    r = lie_down.lie_down(cfg, rotate=True, next_wake_min=30)
    assert r["rotated"] is True  # rotate flag set
    assert fired["n"] == 0  # nothing spawned from lie_down
    assert wake_state.load(cfg).get("rotated") is True  # flag left for the daemon
    assert wake_state.get_next_wake_at(cfg) is not None  # ledger armed


def test_lie_down_no_rotate_never_spawns_successor(cfg, monkeypatch):
    """A plain (non-rotate) sleep never spawns a successor — the resident stays."""
    from cortex import wake
    fired = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    wake_state.set_awake(cfg, 1, "/t/alive.jsonl")
    lie_down.lie_down(cfg, rotate=False, next_wake_min=30)
    assert fired["n"] == 0


def test_run_wake_two_concurrent_spawn_entrants_only_one_spawns(cfg, monkeypatch, tmp_path):
    """07-20 live race repro: a SIGKILLed resident (simulated crash, no rotate) +
    a concurrent ctl wake both pass the "no resident" check before either used to
    spawn (two unlocked steps) -> two identical windows landed. Two threads both
    call wake.run_wake through the real window/spawn branch (_window_wake ->
    _spawn_wake, with only window.respawn/watchdog.spawn/osascript-adjacent calls
    stubbed) against the SAME on-disk state dir; window.respawn sleeps briefly so
    both threads are inside the classify-then-spawn window at the same time if
    unlocked. Exactly one must actually spawn; the loser, re-checking _window_alive
    under the serialization lock, must see the winner's now-live window and skip."""
    import threading
    import time as _time
    from pathlib import Path
    from cortex import transcript, wake, watchdog, window

    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    spawn_calls = {"n": 0}
    lock_for_calls = threading.Lock()
    # A REAL file (unlike a bare "/t/new.jsonl" string) so transcript.mtime's
    # p.stat() (called by the loser's "ear" branch) never raises FileNotFoundError
    # in the background thread -- mirrors production, where the winner's spawn
    # really creates this file before _wait_new_transcript returns its path.
    new_transcript_path = tmp_path / "new.jsonl"
    new_transcript_path.write_text("{}")
    NEW_TRANSCRIPT = str(new_transcript_path)

    def _respawn_stub(c, initial_prompt=None, resume_sid=None):
        with lock_for_calls:
            spawn_calls["n"] += 1
            # The winner records its new session; from here _window_alive reads True.
            wake_state.set_session_id(cfg, "new-iterm-sid")
        _time.sleep(0.05)  # widen the race window
        return "new-iterm-sid"
    monkeypatch.setattr(window, "respawn", _respawn_stub)
    # _window_alive = a session is recorded (the winner's spawn set it). Under the
    # serialization lock the loser sees it live and skips.
    monkeypatch.setattr(wake, "_window_alive",
                        lambda c: bool(wake_state.get_session_id(c)))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: NEW_TRANSCRIPT)
    # transcript.newest must agree with the recorded hint the winner's commit
    # just wrote (both resolve to the SAME real file in production -- the spawn
    # actually creates it before _wait_new_transcript returns it). Without this,
    # the loser's in-lock classification (now running strictly AFTER the winner,
    # since classify+dispatch is fully serialized -- Fix 1) sees a recorded
    # transcript hint the on-disk "newest" lookup never confirms, misreads that
    # as a /clear (prev != cur) and classifies "fresh" -> a SECOND spawn.
    monkeypatch.setattr(transcript, "newest", lambda c: Path(NEW_TRANSCRIPT))
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    # Fix 1 (codex adversarial-review): classification now runs INSIDE the spawn
    # lock, so the loser's classification happens AFTER it acquires the lock --
    # i.e. after the winner already recorded a session id above. Its
    # classification then reaches window.is_running()/_session_alive (a real
    # osascript call, blocked by conftest's process guard) instead of the
    # no-sid-yet short-circuit it hit under the old classify-before-lock
    # ordering. Stub these so the loser's re-classification stays in-process.
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 4242)
    # If the loser classifies alive-resident, _window_wake runs the real typed
    # path: type_wake_signal (osascript, blocked by conftest) then _signal_landed,
    # which polls with real time.sleep up to ear_timeout_sec (default 90s).
    # t.join(timeout=10) returns but the thread keeps sleeping, so the pytest
    # process cannot exit -> intermittent 90s hang (race-dependent). Stub the
    # typing + landing probe so no thread ever blocks on it; the test only
    # asserts how many spawns fired, not delivery timing.
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda cfg, now, token=None: True)
    monkeypatch.setattr(wake, "_signal_landed",
                        lambda cfg, before, timeout: True)

    def _fire():
        conn = db.connect(cfg)
        try:
            now = datetime.now(_tz(cfg))
            decision = {"wake": True, "reasons": [], "gated_by": [],
                        "wake_reasons": "test",
                        "explanation": "concurrent entrant"}
            wake.run_wake(conn, cfg, decision, now=now)
        finally:
            conn.close()

    t1 = threading.Thread(target=_fire)
    t2 = threading.Thread(target=_fire)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert spawn_calls["n"] == 1  # exactly one entrant actually spawned a window
    # The winner recorded its session before releasing the lock, so the loser's
    # _window_alive recheck saw a live window and skipped: only one window.respawn
    # call ever fired, so at most one real iTerm window exists.


# --- no night clamp (P8: gate-end clamp retired) ------------------------------

def test_ledger_write_has_no_night_clamp(cfg):
    """P8: the gate-end clamp is gone — a due time that once fell 'inside the old
    window' is now persisted at its REAL time (else the 120-360 roaming band would
    collapse to the gate end)."""
    tz = _tz(cfg)
    mid_night = datetime(2026, 7, 13, 2, 0, tzinfo=tz)
    assert lie_down.persist_next_wake_at(cfg, mid_night) is True
    ledger = wake_state.get_next_wake_at(cfg)
    assert ledger is not None and "02:00" in ledger  # unchanged, no clamp


def test_lie_down_reports_real_next_wake(cfg):
    """lie_down()'s reported next_wake matches the ledger exactly (no clamp)."""
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=20)
    ledger = wake_state.get_next_wake_at(cfg)
    assert ledger is not None and r["next_wake"] is not None
    assert r["next_wake"] in ledger  # HH:MM substring of the ISO ledger


# --- reconcile decision matrix ------------------------------------------------

def _fire_spy(monkeypatch):
    calls = {}

    def fake_fire(conn, cfg, why):
        calls["why"] = why
        return f"fired: {why}"

    monkeypatch.setattr(reconcile, "_fire_dead_window", fake_fire)
    # Adoption runs before any dead-window fire; with no manual window to adopt
    # (the default) it must be a no-op so the fire/hold matrix is exercised.
    monkeypatch.setattr(reconcile, "_adopt_manual_window", lambda cfg: None)
    return calls


def test_reconcile_alive_never_touched(cfg, monkeypatch):
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
    calls = _fire_spy(monkeypatch)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now - timedelta(minutes=5)).isoformat())  # overdue
    st = {"awake": True}
    assert reconcile._reconcile(None, cfg, st, now) is None
    assert "why" not in calls  # alive window is never fired at


def test_reconcile_due_ledger_dead_window_fires(cfg, monkeypatch):
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    calls = _fire_spy(monkeypatch)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now - timedelta(minutes=1)).isoformat())
    msg = reconcile._reconcile(None, cfg, {}, now)
    assert "ledger due" in calls["why"]
    assert msg.startswith("fired:")


def test_reconcile_future_ledger_holds(cfg, monkeypatch):
    """A future ledger alarm is authoritative: _reconcile must return a hold
    (not None) so the daemon short-circuits and no other wake path can fire
    early, e.g. right after `ctl sleep --min 30`."""
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    calls = _fire_spy(monkeypatch)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now + timedelta(minutes=20)).isoformat())
    msg = reconcile._reconcile(None, cfg, {}, now)
    assert msg is not None and "hold" in msg.lower()
    assert "why" not in calls  # future alarm -> caught at due time, no re-arm


def test_reconcile_accidental_close_resumes(cfg, monkeypatch):
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    calls = _fire_spy(monkeypatch)
    wake_state.set_session_id(cfg, "SID-1")
    wake_state.update(cfg, awake=True)  # awake, no next_wake_at
    now = datetime.now(_tz(cfg))
    st = wake_state.load(cfg)
    msg = reconcile._reconcile(None, cfg, st, now)
    assert "accidental close" in calls["why"]
    assert msg.startswith("fired:")


def test_fire_dead_window_accidental_close_resumes(cfg, monkeypatch):
    """Accidental close of an awake window (no rotate flag) with a recoverable
    session -> RESUME the same conversation (conversation = identity), not a
    fresh spawn. _spawn_wake is called with resume=True."""
    from cortex import transcript, wake, window
    cfg["pacemaker"]["dry_run"] = False
    cfg["wake"]["mode"] = "window"
    wake_state.set_session_id(cfg, "SID-1")
    wake_state.update(cfg, transcript="/t/live-sid.jsonl")  # recoverable, not retired
    monkeypatch.setattr(window, "is_running", lambda: False)  # dead resident
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    captured = {}
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, c, now, resume=False, **kw:
                        captured.update(resume=resume) or {"mode": "window"})
    conn = db.connect(cfg)
    try:
        reconcile._fire_dead_window(conn, cfg, "accidental close of awake window")
    finally:
        conn.close()
    assert captured.get("resume") is True  # same conversation resumed, not fresh


# --- silent resume of a window closed while ASLEEP ---------------------------

def _silent_resume_env(cfg, monkeypatch, *, claude_sid="claude-sid-1"):
    """Dead resident, a resumable claude session, and every machine-touching
    call stubbed. Returns the recorder dict."""
    from cortex import wake, window
    rec = {"respawn": None, "bell": 0}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    monkeypatch.setattr(reconcile, "_adopt_manual_window", lambda cfg: None)
    monkeypatch.setattr(window, "claude_session_id", lambda c: claude_sid)
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None:
        rec.__setitem__("respawn", (initial_prompt, resume_sid)) or "iterm-new")
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda *a, **k: rec.__setitem__("bell", rec["bell"] + 1))
    monkeypatch.setattr(window, "inject_prompt",
                        lambda *a, **k: rec.__setitem__("bell", rec["bell"] + 1))
    return rec


def test_asleep_dead_window_silently_resumes(cfg, monkeypatch):
    """Window closed while the shell is ASLEEP: reopen the SAME conversation
    (--resume, no baked prompt), re-record the resident session id, and change
    nothing else — no bell typed, still asleep, ledger untouched."""
    rec = _silent_resume_env(cfg, monkeypatch)
    now = datetime.now(_tz(cfg))
    due = (now + timedelta(minutes=20)).isoformat()
    wake_state.set_next_wake_at(cfg, due)
    wake_state.set_session_id(cfg, "iterm-old")

    msg = reconcile._reconcile(None, cfg, wake_state.load(cfg), now)

    assert rec["respawn"] == (None, "claude-sid-1")  # resumed, no opener baked
    assert rec["bell"] == 0                          # NOT a new turn
    assert msg is not None and "silent resume" in msg
    st = wake_state.load(cfg)
    assert st.get("awake") is not True                # still asleep
    assert st.get("session_id") == "iterm-new"        # new window recorded
    assert wake_state.get_next_wake_at(cfg) == due    # ledger untouched


def test_asleep_silent_resume_skips_retired_session(cfg, monkeypatch):
    """A sid already retired by a rotate is never resumed — the scheduled wake
    spawns a fresh brain instead."""
    rec = _silent_resume_env(cfg, monkeypatch, claude_sid="retired-sid")
    wake_state.update(cfg, retired_sid="retired-sid")
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now + timedelta(minutes=20)).isoformat())
    msg = reconcile._reconcile(None, cfg, wake_state.load(cfg), now)
    assert rec["respawn"] is None
    assert "hold" in msg.lower()


def test_awake_dead_window_future_ledger_is_not_silently_resumed(cfg, monkeypatch):
    """AWAKE + dead window is the wake path's business (bell on resume) — the
    silent path must not touch it."""
    rec = _silent_resume_env(cfg, monkeypatch)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now + timedelta(minutes=20)).isoformat())
    wake_state.update(cfg, awake=True)
    msg = reconcile._reconcile(None, cfg, wake_state.load(cfg), now)
    assert rec["respawn"] is None
    assert "hold" in msg.lower()


def test_wake_time_dead_window_still_rings(cfg, monkeypatch):
    """At wake time (ledger DUE) the dead-window path is unchanged: the normal
    wake fires (resume + bell), not the silent resume."""
    rec = _silent_resume_env(cfg, monkeypatch)
    calls = _fire_spy(monkeypatch)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now - timedelta(minutes=1)).isoformat())
    msg = reconcile._reconcile(None, cfg, wake_state.load(cfg), now)
    assert "ledger due" in calls["why"] and msg.startswith("fired:")
    assert rec["respawn"] is None  # went through the wake path, not silent resume


def test_silent_resume_drops_commit_when_a_wake_lands_mid_spawn(cfg, monkeypatch):
    """Epoch guard: a wake/user reset flipping awake while the window is coming
    up wins — the silent path does not overwrite the resident session id."""
    from cortex import wake, window
    rec = _silent_resume_env(cfg, monkeypatch)

    def _respawn_then_wake(c, initial_prompt=None, resume_sid=None):
        rec["respawn"] = (initial_prompt, resume_sid)
        wake_state.set_awake(cfg, None, None)  # a real wake lands mid-spawn
        return "iterm-new"

    monkeypatch.setattr(window, "respawn", _respawn_then_wake)
    wake_state.set_session_id(cfg, "iterm-old")
    assert "not recorded" in (wake.resume_asleep(cfg) or "")
    assert wake_state.load(cfg).get("session_id") == "iterm-old"


def test_reconcile_breaker_holds_everything(cfg, monkeypatch):
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    calls = _fire_spy(monkeypatch)
    breaker.pause(cfg)
    now = datetime.now(_tz(cfg))
    overdue = (now - timedelta(minutes=5)).isoformat()
    wake_state.set_next_wake_at(cfg, overdue)  # overdue
    msg = reconcile._reconcile(None, cfg, {}, now)
    assert "breaker held" in msg.lower()
    assert "why" not in calls  # nothing fires while held
    # The alarm survives the hold -> it fires on the first pass after a clear.
    assert wake_state.get_next_wake_at(cfg) == overdue


def test_breaker_flag_roundtrip(cfg):
    assert breaker.holds(cfg, "cli") is False
    breaker.pause(cfg)
    assert breaker.holds(cfg, "cli") is True
    assert breaker.holds(cfg, "tg") is True  # default scope = all
    assert breaker.release(cfg) is True
    assert breaker.holds(cfg, "cli") is False
    assert breaker.release(cfg) is False  # already clear


def test_breaker_single_shell_scope(cfg):
    breaker.pause(cfg, "tg")
    assert breaker.holds(cfg, "tg") is True
    assert breaker.holds(cfg, "cli") is False


# --- ledger authoritative-hold + consumption (codex review P1-1/P1-2/P1-3) ----

def test_reconcile_future_hold_short_circuits_the_daemon(cfg, monkeypatch):
    """P1-1: a dead window + future ledger alarm must short-circuit the daemon
    pass — the awake gate is never reached and no wake fires early."""
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    monkeypatch.setattr(reconcile, "_adopt_manual_window", lambda cfg: None)

    def _boom(*a, **k):
        raise AssertionError("no wake while a future ledger holds")
    monkeypatch.setattr(reconcile, "_handle_awake", _boom)
    monkeypatch.setattr("cortex.wake.run_wake", _boom)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now + timedelta(minutes=20)).isoformat())
    d = daemon.WakeDaemon(cfg, scheduler=object(), clock=lambda: 0.0)
    assert "hold" in d.reconcile_once().lower()
    assert d.business_once() == "business: nothing due"


def test_fire_dead_window_dry_run_consumes_ledger(cfg):
    """P1-2: a due-ledger fire in dry_run must replace next_wake_at with the
    freshly redrawn floor, not leave the stale due timestamp (else every
    subsequent tick re-fires the same reconcile wake)."""
    cfg["pacemaker"]["dry_run"] = True
    now = datetime.now(_tz(cfg))
    stale_due = now - timedelta(minutes=1)
    wake_state.set_next_wake_at(cfg, stale_due.isoformat())
    conn = db.connect(cfg)
    try:
        reconcile._fire_dead_window(conn, cfg, "ledger due, window dead")
    finally:
        conn.close()
    new_due = wake_state.get_next_wake_at(cfg)
    assert new_due is not None
    assert new_due != stale_due.isoformat()


def test_breaker_short_circuits_before_reconcile(cfg, monkeypatch):
    """A held breaker holds everything: the daemon pass returns before running
    the reconcile body, so no wake path fires."""
    breaker.pause(cfg)

    def _boom(*a, **k):
        raise AssertionError("_reconcile must not run while held")
    monkeypatch.setattr(reconcile, "_reconcile", _boom)
    d = daemon.WakeDaemon(cfg, scheduler=object(), clock=lambda: 0.0)
    assert "breaker held" in d.reconcile_once().lower()


# --- per-session _window_alive ------------------------------------------------

def test_window_alive_is_per_session(cfg, monkeypatch):
    """_window_alive must prove liveness via the recorded session's OWN tty
    (window._claude_on_session_tty), never the cwd fallback — so another claude
    window in cortex_home can't fake a dead session alive."""
    from cortex import wake, window
    wake_state.set_session_id(cfg, "SID-1")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    # find_claude_pid (with its cwd fallback) would return a pid for a foreign
    # window; if _window_alive used it, this would falsely read alive.
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 99999)
    monkeypatch.setattr(window, "_claude_on_session_tty", lambda c, sid: False)
    assert wake._window_alive(cfg) is False  # per-session check wins
    monkeypatch.setattr(window, "_claude_on_session_tty", lambda c, sid: True)
    assert wake._window_alive(cfg) is True


# --- ctl CLI ------------------------------------------------------------------

def test_ctl_pause_resume(cfg, monkeypatch, capsys):
    from cortex import ctl
    monkeypatch.setattr(ctl.config, "load", lambda: cfg)
    monkeypatch.setattr(ctl, "_receipt", lambda c, m: None)
    ctl.main(["pause"])
    assert breaker.holds(cfg, "cli") is True
    assert breaker.holds(cfg, "tg") is True
    ctl.main(["resume"])
    assert breaker.holds(cfg, "cli") is False


def test_ctl_pause_single_shell(cfg, monkeypatch):
    from cortex import ctl
    monkeypatch.setattr(ctl.config, "load", lambda: cfg)
    ctl.main(["pause", "--shell", "tg"])
    assert breaker.holds(cfg, "tg") is True
    assert breaker.holds(cfg, "cli") is False


def test_ctl_pause_puts_live_cli_window_down(cfg, monkeypatch):
    """ct-pause reuses the EXISTING proxy lie_down path (the one the watchdog
    fuse uses) — no new interrupt mechanism."""
    from cortex import ctl
    from cortex import lie_down as lie_down_mod
    seen = {}
    monkeypatch.setattr(lie_down_mod, "lie_down",
                        lambda c, **kw: seen.update(kw) or {})
    wake_state.set_awake(cfg, 1, None)
    line = ctl.cmd_pause(cfg)
    assert seen.get("force_slept") == "ct-pause"
    assert "put down" in line


def test_ctl_pause_is_silent_no_outbox_row(cfg):
    """A manual pause must not write a tg receipt: no outbox row at all (only
    an auto trip, via watchdog._fuse, announces on tg)."""
    from cortex import ctl
    conn = db.connect(cfg)
    conn.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, from_sid TEXT,"
                 " from_channel TEXT, target TEXT, body TEXT,"
                 " status TEXT NOT NULL DEFAULT 'pending')")
    conn.commit()
    conn.close()
    ctl.cmd_pause(cfg)
    conn = db.connect(cfg)
    try:
        count = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_ctl_wake_clears_the_breaker(cfg, monkeypatch):
    from cortex import ctl
    import cortex.wake as wake_mod
    breaker.pause(cfg)
    monkeypatch.setattr(wake_mod, "_window_alive", lambda c: True)
    monkeypatch.setattr(wake_state, "is_awake", lambda c: True)
    line = ctl.cmd_wake(cfg)
    assert breaker.holds(cfg, "cli") is False
    assert line.startswith("breaker cleared; ")


def test_ctl_status_reports_the_breaker(cfg):
    from cortex import ctl
    assert "breaker: clear" in ctl.cmd_status(cfg)
    breaker.pause(cfg, "cli")
    line = ctl.cmd_status(cfg)
    assert "breaker: ON scope=cli reason=manual" in line


def test_ctl_sleep_dead_window_sets_ledger(cfg):
    from cortex import ctl
    msg = ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=True)
    assert wake_state.get_next_wake_at(cfg) is not None
    assert wake_state.load(cfg).get("rotated") is True
    assert "ledger set" in msg


def test_ctl_sleep_gates_on_awake_not_liveness(cfg):
    """P2-A: a resident window can be alive-but-dormant (asleep). cmd_sleep
    must gate the live-window injection on the awake marker, not liveness —
    else the requested minutes/rotate silently drop via claim_lie_down's
    'not awake' no-op."""
    from cortex import ctl
    wake_state.set_session_id(cfg, "SID-1")  # a resident session exists
    # awake marker NOT set -> even if the window were alive, must fall to the
    # ledger-direct path, not the injection path.
    msg = ctl.cmd_sleep(cfg, until=None, minutes=15, rotate=False)
    assert "ledger set" in msg
    assert wake_state.get_next_wake_at(cfg) is not None


def test_ctl_sleep_live_window_rotate_delivers_marker_with_args(cfg, monkeypatch):
    """P2-1: `sleep --rotate` on a live+awake window delivers the covert CTL
    marker carrying mins + rotate=true (the body renders marrow-side from these
    args). Only the marker + args reach the window, never the instruction body."""
    from cortex import ctl, window
    wake_state.set_awake(cfg, 1, None)
    captured = {}
    monkeypatch.setattr(window, "deliver_covert_marker",
                        lambda c, line: captured.setdefault("line", line) or "typed")
    ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=True)
    assert "[CTL]" in captured["line"]
    assert "mins=30" in captured["line"]
    assert "rotate=true" in captured["line"]
    assert "lie_down(" not in captured["line"]  # body not on screen


def test_ctl_sleep_live_window_no_rotate_omits_rotate_true(cfg, monkeypatch):
    from cortex import ctl, window
    wake_state.set_awake(cfg, 1, None)
    captured = {}
    monkeypatch.setattr(window, "deliver_covert_marker",
                        lambda c, line: captured.setdefault("line", line) or "typed")
    ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=False)
    assert "rotate=false" in captured["line"]
    assert "rotate=true" not in captured["line"]



# --- deletion guard -----------------------------------------------------------

def test_pacemaker_package_is_gone():
    """T11 P4: the pacemaker package + its tick entry point are deleted whole —
    no shim, no re-export, nothing importable."""
    import importlib
    for name in ("cortex.pacemaker", "cortex.pacemaker_tick", "cortex.sentinel"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)
    # the relocated reconcile logic imports cleanly on its own
    importlib.reload(reconcile)


def test_ctl_pause_stamps_the_wake_ledger_row(cfg, monkeypatch):
    """T3: pausing a live cli window lands the pause on the ledger — the wake
    row carries force_slept='ct-pause' and the shell it put down."""
    from cortex import ctl, occupancy, transcript
    monkeypatch.setattr(transcript, "window_tokens", lambda c: 1234)
    conn = db.connect(cfg)
    try:
        now = datetime.now(_tz(cfg))
        wid = occupancy.log_activation_wake_row(conn, now, "ctl")
    finally:
        conn.close()
    wake_state.set_awake(cfg, wid, None)

    line = ctl.cmd_pause(cfg)

    assert "put down" in line
    conn = db.connect(cfg)
    try:
        row = conn.execute(
            "SELECT force_slept, shell, tokens FROM ct_wake_log WHERE id=?",
            (wid,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM ct_wake_log").fetchone()[0]
    finally:
        conn.close()
    assert (row["force_slept"], row["shell"], row["tokens"]) == ("ct-pause", "cli", 1234)
    assert total == 1  # the pause stamps the open row, it does not add one


def test_ctl_pause_without_live_window_writes_no_row(cfg):
    """T3: a pause that finds no live window writes nothing to the ledger."""
    from cortex import ctl
    db.connect(cfg).close()  # ensure the table exists

    ctl.cmd_pause(cfg)

    conn = db.connect(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ct_wake_log").fetchone()[0] == 0
    finally:
        conn.close()
