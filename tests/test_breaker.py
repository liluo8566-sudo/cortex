"""Circuit breaker ("main switch"): state file, rolling fuse tally, auto trip,
and the cli choke points. No real window / claude / db side effects — the
watchdog fuse ladder is stubbed at its boundary."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from cortex import breaker, config, db, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["handoff_file"] = str(home / "handoff.md")
    c["paths"]["wake_timing_log"] = str(home / "wake_timing.log")
    return c


@pytest.fixture
def cdir(cfg, tmp_path):
    return tmp_path


def _write_marrow_config(cdir, body: str) -> None:
    (cdir / "config.toml").write_text(body, encoding="utf-8")


# --- state file ---------------------------------------------------------------

def test_absent_file_is_clear(cdir):
    assert breaker.read(cdir) is None
    assert breaker.covers(cdir, "cli") is False
    assert breaker.covers(cdir, "tg") is False


def test_trip_and_clear_roundtrip(cdir):
    st = breaker.trip(cdir, "all", breaker.REASON_MANUAL)
    assert st["scope"] == "all" and st["reason"] == "manual"
    assert breaker.breaker_path(cdir).exists()
    on_disk = json.loads(breaker.breaker_path(cdir).read_text())
    assert set(on_disk) == {"scope", "reason", "ts"}
    assert datetime.fromisoformat(on_disk["ts"])  # parseable iso
    assert breaker.covers(cdir, "cli") and breaker.covers(cdir, "tg")
    assert breaker.clear(cdir) is True
    assert breaker.breaker_path(cdir).exists() is False
    assert breaker.clear(cdir) is False


def test_scope_is_per_shell(cdir):
    breaker.trip(cdir, "cli")
    assert breaker.covers(cdir, "cli") is True
    assert breaker.covers(cdir, "tg") is False
    breaker.trip(cdir, "tg")
    assert breaker.covers(cdir, "tg") is True
    assert breaker.covers(cdir, "cli") is False


def test_merge_unions_two_single_shell_pauses(cdir):
    """A manual pause never releases the shell an earlier one holds."""
    breaker.trip(cdir, "cli", breaker.REASON_MANUAL, merge=True)
    st = breaker.trip(cdir, "tg", breaker.REASON_MANUAL, merge=True)
    assert st["scope"] == "all"
    assert breaker.covers(cdir, "cli") and breaker.covers(cdir, "tg")


def test_merge_keeps_scope_all(cdir):
    breaker.trip(cdir, "all", breaker.REASON_AUTO)
    st = breaker.trip(cdir, "cli", breaker.REASON_MANUAL, merge=True)
    assert st["scope"] == "all"
    assert st["reason"] == "manual"
    assert breaker.covers(cdir, "tg") is True


def test_merge_same_shell_only_refreshes(cdir):
    first = breaker.trip(cdir, "tg", breaker.REASON_MANUAL, now=_ago(cdir, 1),
                         merge=True)
    st = breaker.trip(cdir, "tg", breaker.REASON_MANUAL, merge=True)
    assert st["scope"] == "tg"
    assert st["ts"] > first["ts"]
    assert breaker.covers(cdir, "cli") is False


def test_merge_from_clear_writes_the_scope_asked_for(cdir):
    st = breaker.trip(cdir, "cli", breaker.REASON_MANUAL, merge=True)
    assert st["scope"] == "cli"
    assert breaker.covers(cdir, "tg") is False


def test_auto_trip_does_not_merge(cdir):
    """The auto fuse always writes scope "all", which already covers whatever
    single-shell hold stood before it."""
    breaker.trip(cdir, "tg", breaker.REASON_MANUAL, merge=True)
    st = breaker.trip(cdir, "all", breaker.REASON_AUTO)
    assert st["scope"] == "all" and st["reason"] == "auto_fuse"


def test_cfg_pause_merges(cfg, cdir):
    breaker.pause(cfg, "cli")
    assert breaker.pause(cfg, "tg")["scope"] == "all"


def _ago(cdir, seconds: float) -> datetime:
    return datetime.now().astimezone() - timedelta(seconds=seconds)


def test_clear_shell_narrows_scope_all(cdir):
    """Releasing one half of an all-shell hold leaves the OTHER shell held,
    same record (reason/ts preserved) — the hold only got smaller."""
    st = breaker.trip(cdir, "all", breaker.REASON_AUTO)
    assert breaker.clear(cdir, "tg") is True
    after = breaker.read(cdir)
    assert after["scope"] == "cli"
    assert after["reason"] == breaker.REASON_AUTO and after["ts"] == st["ts"]
    assert breaker.covers(cdir, "cli") is True
    assert breaker.covers(cdir, "tg") is False


def test_clear_shell_matching_scope_removes_record(cdir):
    breaker.trip(cdir, "tg")
    assert breaker.clear(cdir, "tg") is True
    assert breaker.read(cdir) is None
    assert breaker.breaker_path(cdir).exists() is False


def test_clear_shell_other_scope_is_a_noop(cdir):
    breaker.trip(cdir, "cli")
    assert breaker.clear(cdir, "tg") is False
    assert breaker.read(cdir)["scope"] == "cli"


def test_clear_shell_when_already_clear(cdir):
    assert breaker.clear(cdir, "cli") is False
    assert breaker.read(cdir) is None


def test_clear_shell_unknown_name_leaves_the_hold(cdir):
    """An unknown shell has no 'other shell' to narrow to — refuse rather than
    guess (argparse already restricts the CLI to cli|tg)."""
    breaker.trip(cdir, "all")
    assert breaker.clear(cdir, "wx") is False
    assert breaker.read(cdir)["scope"] == "all"


def test_clear_shell_write_is_atomic_no_tmp_left(cdir):
    breaker.trip(cdir, "all")
    breaker.clear(cdir, "cli")
    assert [p.name for p in cdir.iterdir() if ".tmp." in p.name] == []


def test_corrupt_file_reads_as_clear(cdir, caplog):
    breaker.breaker_path(cdir).write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert breaker.read(cdir) is None
    assert breaker.covers(cdir, "cli") is False
    assert "unreadable" in caplog.text


def test_wrong_shape_file_reads_as_clear(cdir, caplog):
    breaker.breaker_path(cdir).write_text('["all"]', encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert breaker.read(cdir) is None
    assert "unexpected shape" in caplog.text


def test_empty_scope_reads_as_clear(cdir):
    breaker.breaker_path(cdir).write_text('{"scope": ""}', encoding="utf-8")
    assert breaker.read(cdir) is None


def test_write_is_atomic_no_tmp_left(cdir):
    breaker.trip(cdir, "all")
    leftovers = [p.name for p in cdir.iterdir() if ".tmp." in p.name]
    assert leftovers == []


# --- settings -----------------------------------------------------------------

def test_settings_default_when_no_marrow_config(cdir):
    s = breaker.settings(cdir)
    assert s["enabled"] is True
    assert s["fuse_threshold"] == 2
    assert s["window_hours"] == 24


def test_settings_read_from_marrow_config(cdir):
    _write_marrow_config(cdir, "[cortex.breaker]\nfuse_threshold = 5\n"
                               "window_hours = 6\nenabled = false\n")
    s = breaker.settings(cdir)
    assert s["fuse_threshold"] == 5
    assert s["window_hours"] == 6
    assert s["enabled"] is False
    # Unset keys still fall back to defaults.
    assert "{count}" in s["trip_message"]


def test_settings_bad_toml_falls_back(cdir, caplog):
    _write_marrow_config(cdir, "[cortex.breaker\nbroken")
    with caplog.at_level("WARNING"):
        assert breaker.settings(cdir)["fuse_threshold"] == 2


# --- rolling fuse tally -------------------------------------------------------

def test_tally_counts_across_shells(cdir):
    _write_marrow_config(cdir, "[cortex.breaker]\nenabled = false\n")
    assert breaker.record_fuse(cdir, "cli") == 1
    assert breaker.record_fuse(cdir, "tg") == 2
    assert breaker.record_fuse(cdir, "cli") == 3
    shells = [e["shell"] for e in
              json.loads(breaker.fuse_path(cdir).read_text())["events"]]
    assert shells == ["cli", "tg", "cli"]


def test_tally_prunes_outside_the_window(cdir):
    _write_marrow_config(cdir, "[cortex.breaker]\nenabled = false\n"
                               "window_hours = 24\n")
    now = datetime.now().astimezone()
    breaker.record_fuse(cdir, "cli", now=now - timedelta(hours=30))
    breaker.record_fuse(cdir, "tg", now=now - timedelta(hours=25))
    # Both are older than the window -> pruned on the next write.
    assert breaker.record_fuse(cdir, "cli", now=now) == 1


def test_tally_keeps_events_inside_the_window(cdir):
    _write_marrow_config(cdir, "[cortex.breaker]\nenabled = false\n"
                               "window_hours = 24\n")
    now = datetime.now().astimezone()
    breaker.record_fuse(cdir, "cli", now=now - timedelta(hours=23))
    assert breaker.record_fuse(cdir, "tg", now=now) == 2


def test_tally_tolerates_bare_iso_strings(cdir):
    """A legacy/hand-written list of bare ISO strings still counts + normalises."""
    _write_marrow_config(cdir, "[cortex.breaker]\nenabled = false\n")
    now = datetime.now().astimezone()
    breaker.fuse_path(cdir).write_text(
        json.dumps({"events": [(now - timedelta(hours=1)).isoformat()]}),
        encoding="utf-8")
    assert breaker.record_fuse(cdir, "cli", now=now) == 2


def test_tally_corrupt_file_restarts_clean(cdir):
    _write_marrow_config(cdir, "[cortex.breaker]\nenabled = false\n")
    breaker.fuse_path(cdir).write_text("garbage", encoding="utf-8")
    assert breaker.record_fuse(cdir, "cli") == 1


# --- auto trip ----------------------------------------------------------------

def test_threshold_trips_the_breaker_for_all_shells(cdir):
    count, tripped = breaker.record_fuse_and_maybe_trip(cdir, "cli")
    assert count == 1 and tripped is None
    count, tripped = breaker.record_fuse_and_maybe_trip(cdir, "tg")
    assert count == 2
    assert tripped["scope"] == "all"
    assert tripped["reason"] == "auto_fuse"
    assert breaker.covers(cdir, "cli") and breaker.covers(cdir, "tg")


def test_disabled_tallies_but_never_trips(cdir):
    _write_marrow_config(cdir, "[cortex.breaker]\nenabled = false\n"
                               "fuse_threshold = 2\n")
    breaker.record_fuse_and_maybe_trip(cdir, "cli")
    count, tripped = breaker.record_fuse_and_maybe_trip(cdir, "tg")
    assert count == 2
    assert tripped is None
    assert breaker.read(cdir) is None


def test_threshold_zero_never_trips(cdir):
    _write_marrow_config(cdir, "[cortex.breaker]\nfuse_threshold = 0\n")
    _, tripped = breaker.record_fuse_and_maybe_trip(cdir, "cli")
    assert tripped is None


def test_trip_message_renders_config_copy(cdir):
    _write_marrow_config(cdir, "[cortex.breaker]\nwindow_hours = 12\n"
                               'trip_message = "fuse {count} in {hours}h ({scope})"\n')
    assert breaker.trip_message(cdir, 2, "all") == "fuse 2 in 12h (all)"


# --- cli choke point: watchdog ------------------------------------------------

def _stub_fuse_ladder(monkeypatch, cfg):
    """Every real side effect of the fuse ladder, stubbed at its boundary."""
    from cortex import lie_down as lie_down_mod
    from cortex import watchdog, window
    monkeypatch.setattr(window, "send_esc", lambda c: None)
    monkeypatch.setattr(window, "deliver_covert_marker", lambda c, line: "typed")
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    monkeypatch.setattr(lie_down_mod, "lie_down", lambda c, **kw: {})
    monkeypatch.setattr(watchdog, "_log", lambda *a, **k: None)
    cfg["wake"].setdefault("watchdog", {})["fuse_handoff_grace_sec"] = 0


def test_watchdog_fuse_records_a_fuse_event(cfg, cdir, monkeypatch):
    from cortex import watchdog
    _stub_fuse_ladder(monkeypatch, cfg)
    _write_marrow_config(cdir, "[cortex.breaker]\nenabled = false\n")
    watchdog._fuse(cfg, grace=0.0)
    events = json.loads(breaker.fuse_path(cdir).read_text())["events"]
    assert [e["shell"] for e in events] == ["cli"]


def test_watchdog_fuse_trip_writes_alert_and_tg_note(cfg, cdir, monkeypatch):
    from cortex import watchdog
    _stub_fuse_ladder(monkeypatch, cfg)
    conn = db.connect(cfg)
    conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, severity TEXT,"
                 " type TEXT, message TEXT, source TEXT)")
    conn.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, from_sid TEXT,"
                 " from_channel TEXT, target TEXT, body TEXT,"
                 " status TEXT NOT NULL DEFAULT 'pending')")
    conn.commit()
    conn.close()

    breaker.record_fuse(cdir, "tg")  # 1st fuse already on the tally (other shell)
    watchdog._fuse(cfg, grace=0.0)   # 2nd -> trip

    st = breaker.read(cdir)
    assert st["scope"] == "all" and st["reason"] == "auto_fuse"
    conn = db.connect(cfg)
    try:
        alert = conn.execute(
            "SELECT severity, type, message FROM alerts").fetchone()
        note = conn.execute(
            "SELECT target, body, status FROM outbox").fetchone()
    finally:
        conn.close()
    assert alert[0] == "critical" and alert[1] == "cortex_breaker_tripped"
    assert "2" in alert[2]
    assert note[0] == "tg" and note[2] == "pending"
    assert note[1] == alert[2]


def test_watchdog_fuse_trip_survives_a_missing_db(cfg, cdir, monkeypatch):
    """The breaker must stand even when the announcement cannot be written."""
    from cortex import watchdog
    _stub_fuse_ladder(monkeypatch, cfg)
    breaker.record_fuse(cdir, "tg")
    watchdog._fuse(cfg, grace=0.0)  # marrow.db has no alerts/outbox tables
    assert breaker.read(cdir)["reason"] == "auto_fuse"


def test_watchdog_loop_skips_work_while_held(cfg, monkeypatch):
    from cortex import transcript, wake, watchdog
    breaker.pause(cfg)
    wake_state.set_awake(cfg, 1, None)
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(watchdog, "_log", lambda *a, **k: None)
    monkeypatch.setattr(transcript, "user_silent_min",
                        lambda c: pytest.fail("no reap while held"))
    calls = {"n": 0}

    def _sleep(_s):
        calls["n"] += 1
        if calls["n"] >= 2:
            wake_state.claim_lie_down(cfg)  # clears awake -> the loop retires
    monkeypatch.setattr(watchdog.time, "sleep", _sleep)
    assert watchdog.run(cfg) == 0
