"""Phase 4 — resident singleton audit.

Invariant: exactly ONE live cortex resident at any time (0 and 2 are both
wrong), unless night-sleep or explicit pause/mute. These tests simulate the
three incident shapes with state-file fixtures and assert self-heal:
  - double-wake (2 residents): watchdog.spawn is idempotent (a live recorded
    pid = no second spawn); ctl.cmd_wake on an alive+awake window is a no-op.
  - watchdog-death-during-wait: the awake gate respawns a dead watchdog.
  - stale epoch: a superseded snapshot (gen moved) holds, no respawn/reap.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cortex import config, db, reconcile, wake_state, watchdog


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    c["paths"]["ny_db_pages"] = str(tmp_path / "ny")
    return c


# --- _pid_alive ---------------------------------------------------------------

def test_pid_alive_self_true():
    import os
    assert watchdog._pid_alive(os.getpid()) is True


def test_pid_alive_none_and_dead_false():
    assert watchdog._pid_alive(None) is False
    assert watchdog._pid_alive(0) is False
    # a very high pid is almost certainly not a live process
    assert watchdog._pid_alive(4_000_000_000) is False


# --- spawn singleton guard (double-watchdog prevention) -----------------------

def test_spawn_skips_when_recorded_pid_alive(cfg, monkeypatch):
    """A live recorded watchdog pid whose IDENTITY confirms it is the cortex
    watchdog (ps command line names cortex.watchdog) = already on duty: spawn
    returns it, never launches a second subprocess."""
    import os
    wake_state.watchdog_pidfile_path(cfg).write_text(str(os.getpid()))
    # Identity check (FIX 7): ps -p <pid> -o command= must name cortex.watchdog.
    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda *a, **k: type("R", (), {
                            "returncode": 0,
                            "stdout": "python -m cortex.watchdog"})())
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 999})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 0  # no second watchdog
    assert pid == os.getpid()  # returns the live one


def test_spawn_launches_when_recorded_pid_recycled(cfg, monkeypatch):
    """FIX 7: a live recorded pid whose command line is NOT the watchdog (recycled
    pid inherited by an unrelated process) must NOT count as on-duty — spawn a
    fresh watchdog instead of heal-skipping forever."""
    import os
    wake_state.watchdog_pidfile_path(cfg).write_text(str(os.getpid()))
    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda *a, **k: type("R", (), {
                            "returncode": 0,
                            "stdout": "/usr/bin/some-unrelated-process"})())
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 4243})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 1  # recycled pid ignored -> fresh watchdog spawned
    assert pid == 4243


def test_spawn_launches_when_no_record(cfg, monkeypatch):
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 4242})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 1
    assert pid == 4242


def test_concurrent_spawn_launches_only_one(cfg, monkeypatch):
    """FIX 2: the singleton check+spawn is serialised (flock) and the parent
    writes the pid claim before releasing — two concurrent callers can never both
    launch a watchdog. Simulate the race: two threads call spawn together; only
    ONE Popen fires, both return the same live pid."""
    import threading
    spawned = {"n": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def fake_popen(*a, **k):
        with lock:
            spawned["n"] += 1
        return type("P", (), {"pid": 7777})()
    monkeypatch.setattr(watchdog.subprocess, "Popen", fake_popen)

    # Identity check reads the pidfile claim: alive+watchdog once a pid==7777 is
    # recorded (the parent writes it inside the lock before releasing).
    def fake_alive(cfg_, pid):
        return pid == 7777
    monkeypatch.setattr(watchdog, "_watchdog_pid_alive", fake_alive)

    results = {}

    def worker(idx):
        barrier.wait()
        results[idx] = watchdog.spawn(cfg)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert spawned["n"] == 1  # exactly one watchdog launched despite the race
    assert results[0] == results[1] == 7777  # both callers got the live pid


def test_spawn_launches_when_recorded_pid_dead(cfg, monkeypatch):
    wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead pid
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 4243})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 1  # dead record -> fresh spawn
    assert pid == 4243


# --- awake-gate watchdog-liveness heal (watchdog death during a wait) ---------

def _awake_window(cfg, conn):
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "w"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    wake_state.set_awake(cfg, wid, None)
    return wid


def test_awake_gate_respawns_dead_watchdog(cfg, monkeypatch):
    """Watchdog died mid-wake: the reconcile awake gate must respawn one so the
    resident regains 60s polling + exact-time fuse."""
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        snap_gen = st.get("gen")
        wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead
        monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
        monkeypatch.setattr(watchdog, "silence_action", lambda *a, **k: None)
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        reconcile._handle_awake(conn, cfg, st, snap_gen=snap_gen)
        assert respawned["n"] == 1  # dead watchdog respawned
    finally:
        conn.close()


def test_awake_gate_no_respawn_when_watchdog_alive(cfg, monkeypatch):
    import os
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        snap_gen = st.get("gen")
        wake_state.watchdog_pidfile_path(cfg).write_text(str(os.getpid()))  # alive
        monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
        monkeypatch.setattr(watchdog, "silence_action", lambda *a, **k: None)
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        reconcile._handle_awake(conn, cfg, st, snap_gen=snap_gen)
        assert respawned["n"] == 0  # live watchdog -> no second spawn
    finally:
        conn.close()


def test_awake_gate_stale_epoch_holds_no_respawn(cfg, monkeypatch):
    """Stale snapshot (gen moved since the pass opened = a user reset / lie_down):
    the awake gate must hold and NOT respawn a watchdog against a dead epoch."""
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        stale_gen = (st.get("gen") or 0) - 1  # snapshot older than live
        wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        msg = reconcile._handle_awake(conn, cfg, st, snap_gen=stale_gen)
        assert "superseded" in msg
        assert respawned["n"] == 0  # never respawn against a stale epoch
    finally:
        conn.close()
