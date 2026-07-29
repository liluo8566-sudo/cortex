from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex import wake

TZ = timezone(timedelta(hours=10))
DAY1 = datetime(2026, 7, 3, 21, 0, tzinfo=TZ)

DECISION = {"wake": True, "reasons": [], "gated_by": [], "explanation": "test wake",
            "wake_reasons": "ctl"}


@pytest.fixture(autouse=True)
def events_table(marrow_conn):
    marrow_conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, session_id TEXT, timestamp TEXT, "
        "role TEXT, content TEXT, ts_start TEXT, ts_end TEXT)"
    )
    marrow_conn.commit()


@pytest.fixture
def wcfg(base_cfg, tmp_path):
    cfg = dict(base_cfg)
    cfg["paths"] = {
        **base_cfg["paths"],
        "cortex_home": str(tmp_path / "cortex_home"),
        "wishlist_file": str(tmp_path / "cortex_home" / "wishlist.md"),
        "ny_db_pages": str(tmp_path / "ny"),
        "wake_timing_log": str(tmp_path / "wake_timing.log"),
    }
    cfg["marrow"] = {"venv_python": ""}
    cfg["wake"] = {}
    return cfg


def test_assemble_note_real_data(marrow_conn, wcfg):
    text = wake.assemble_note(marrow_conn, wcfg, DAY1)
    assert "Now:" not in text  # Now line deleted — hook injects current time
    assert "Wake:" not in text
    assert len(text) < 1000


def test_run_wake_creates_ny_symlinks(monkeypatch, marrow_conn, wcfg):
    monkeypatch.setattr(wake, "_classify_wake", lambda cfg: ("ear", False))
    monkeypatch.setattr(wake, "_window_wake",
                        lambda conn, cfg, t, now, respawn=False, **kw:
                        {"mode": "window", "session_id": None, "text": None})
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1)

    from pathlib import Path
    ny = Path(wcfg["paths"]["ny_db_pages"])
    assert (ny / "wishlist.md").is_symlink()
    assert (ny / "wishlist.md").resolve() == Path(wcfg["paths"]["wishlist_file"]).resolve()


def test_window_failure_alerts_and_gives_up_the_round(monkeypatch, marrow_conn, wcfg):
    """No windowless fallback: a failed window path raises a marrow alert, audits
    the give-up and returns mode="failed" — the caller re-arms the next wake on any
    non-window result and the next tick retries."""
    marrow_conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, severity TEXT,"
                        " type TEXT, message TEXT, source TEXT)")
    marrow_conn.commit()
    monkeypatch.setattr(wake, "_classify_wake", lambda cfg: ("ear", False))
    monkeypatch.setattr(wake, "_window_wake",
                        lambda conn, cfg, t, now, respawn=False, **kw: None)

    res = wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1)

    assert res["mode"] == "failed"
    row = marrow_conn.execute("SELECT severity, type, source FROM alerts").fetchone()
    assert row["severity"] == "warn" and row["type"] == "cortex_wake_window_failed"
    assert row["source"].startswith("cortex_wake:")
    # No wake=1 activation row: nothing was woken.
    n = marrow_conn.execute(
        "SELECT COUNT(*) AS n FROM ct_wake_log WHERE wake=1").fetchone()["n"]
    assert n == 0


# --------------------------------------------------------------------------- #
# Rotate (handoff round-trip): a rotated/respawned resident window is a fresh
# brain and must receive the previous brain's handoff note.
# --------------------------------------------------------------------------- #

@pytest.fixture
def rot_cfg(wcfg, tmp_path):
    """wcfg + handoff note config + a written handoff file."""
    cfg = dict(wcfg)
    cfg["paths"] = {**wcfg["paths"], "handoff_file": str(tmp_path / "handoff.md"),
                    "wake_state_file": str(tmp_path / "wake_state.json")}
    cfg["note"] = {"handoff_wake_kinds": ["rotate"],
                   "handoff_title": "handoff-note"}
    Path(cfg["paths"]["handoff_file"]).write_text("carry this to your next self")
    return cfg


def test_window_rotated_flag_path(monkeypatch, rot_cfg):
    from cortex import wake_state, window, transcript
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.set_rotated(rot_cfg)
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda cfg: 4242)
    monkeypatch.setattr(transcript, "newest", lambda cfg: None)
    assert wake._window_rotated(rot_cfg) is True
    # Fix 1: classification only PEEKS the rotate flag now (the one-shot consume is
    # deferred to after the fresh successor is live), so a second check still sees
    # the flag set -> still True. The flag is consumed by run_wake, not here.
    assert wake._window_rotated(rot_cfg) is True
    assert wake_state.peek_rotated(rot_cfg) is True  # not consumed by classification


def test_window_rotated_transcript_diff_path(monkeypatch, rot_cfg):
    from cortex import wake_state, window, transcript
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.update(rot_cfg, transcript="/t/old.jsonl")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda cfg: 4242)
    monkeypatch.setattr(transcript, "newest", lambda cfg: Path("/t/new.jsonl"))
    assert wake._window_rotated(rot_cfg) is True


def test_window_rotated_dead_window_is_fresh(monkeypatch, rot_cfg):
    from cortex import wake_state, window
    wake_state.set_session_id(rot_cfg, "sid-1")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: False)
    assert wake._window_rotated(rot_cfg) is True


def test_window_rotated_claude_dead_is_fresh(monkeypatch, rot_cfg):
    """Session exists but its `claude` process died (SIGINT/crash) -> bare
    shell -> treated as fresh so ensure_window's relaunch gets the handoff."""
    from cortex import wake_state, window
    wake_state.set_session_id(rot_cfg, "sid-1")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda cfg: None)
    assert wake._window_rotated(rot_cfg) is True


def test_window_unrotated_resume_stays_non_fresh(monkeypatch, rot_cfg):
    """Plain wake into a live, un-rotated window: same transcript, no flag ->
    NOT fresh (no handoff; replay continuity lives in the window's own context)."""
    from cortex import wake_state, window, transcript
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.update(rot_cfg, transcript="/t/same.jsonl")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda cfg: 4242)
    monkeypatch.setattr(transcript, "newest", lambda cfg: Path("/t/same.jsonl"))
    assert wake._window_rotated(rot_cfg) is False


def test_window_wake_rotate_respawns(monkeypatch, marrow_conn, rot_cfg):
    """Full window-branch: a rotated window (same local day) respawns fresh.
    The handoff now injects at SessionStart (marrow), not in the note."""
    # run_wake calls _classify_wake directly now (codex adversarial-review Fix 1:
    # classification is a single lock-protected call returning (plan,
    # rotate_driven)), not the back-compat _window_wake_plan wrapper.
    # rotate_driven=True here means a real rotate flag must be observably set
    # (peek_rotated) too -- the belt-and-braces guard checks it independently.
    monkeypatch.setattr(wake, "_classify_wake", lambda cfg: ("fresh", True))
    from cortex import wake_state as _wake_state
    monkeypatch.setattr(_wake_state, "peek_rotated", lambda c: True)
    monkeypatch.setattr(_wake_state, "take_rotated", lambda c: True)
    captured = {}
    def fake_window_wake(conn, cfg, note_text, now, respawn=False, **kw):
        captured["text"] = note_text
        captured["respawn"] = respawn
        return {"mode": "window", "session_id": None, "text": None}
    monkeypatch.setattr(wake, "_window_wake", fake_window_wake)
    # same-day second wake (not rebirth): seed today's session date
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)  # first wake seeds state
    captured.clear()
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1 + timedelta(hours=1))
    assert "handoff-note" not in captured["text"]  # handoff moved to SessionStart
    assert captured["respawn"] is True  # rotate -> fresh self-arming window


def test_rotate_flag_makes_next_wake_fresh(monkeypatch, marrow_conn, rot_cfg):
    """Freshness comes only from the rotate path now (no rebirth): a set rotate
    flag makes the next window wake respawn a fresh brain with the handoff note.
    This is the mechanism the night close relies on for the first post-night wake."""
    from cortex import wake_state
    wake_state.set_rotated(rot_cfg)
    captured = {}
    monkeypatch.setattr(wake, "_window_wake",
                        lambda conn, cfg, t, now, respawn=False, **kw:
                        captured.update(text=t, respawn=respawn) or
                        {"mode": "window", "session_id": None, "text": None})
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)
    assert captured["respawn"] is True          # rotate flag -> fresh respawn


def test_window_wake_unrotated_no_handoff(monkeypatch, marrow_conn, rot_cfg):
    """Un-rotated same-day wake: no handoff in the note."""
    # run_wake calls _classify_wake directly now (see test_window_wake_rotate_respawns).
    monkeypatch.setattr(wake, "_classify_wake", lambda cfg: ("ear", False))
    captured = {}
    monkeypatch.setattr(wake, "_window_wake",
                        lambda conn, cfg, t, now, respawn=False, **kw:
                        captured.update(text=t, respawn=respawn) or
                        {"mode": "window", "session_id": None, "text": None})
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)
    captured.clear()
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1 + timedelta(hours=1))
    assert "handoff-note" not in captured["text"]
    assert captured["respawn"] is False         # live un-rotated window: no respawn


# --------------------------------------------------------------------------- #
# retired_sid: durable per-session rotate guard (belt-and-braces over the
# one-shot `rotated` flag / stale `transcript` pointer going out of sync).
# --------------------------------------------------------------------------- #

def test_window_wake_plan_clears_transcript_on_rotate_consume(rot_cfg):
    """The transcript pointer is cleared while the rotate flag still stands (Fix 1:
    the plan peeks the flag and clears the retiring pointer in the same step), so
    nothing in between can read it as still live. The one-shot flag itself is
    consumed later by run_wake, after the fresh successor is verified."""
    from cortex import wake_state
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.update(rot_cfg, transcript="/t/retiring.jsonl")
    wake_state.set_rotated(rot_cfg)
    assert wake._window_wake_plan(rot_cfg) == "fresh"
    assert wake_state.load(rot_cfg).get("transcript") is None
    assert wake_state.peek_rotated(rot_cfg) is True  # flag survives classification


def test_resume_or_fresh_dead_normal_resume(monkeypatch, marrow_conn, rot_cfg):
    """Baseline: an un-retired resumable sid resumes normally."""
    from cortex import transcript, wake_state
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    wake_state.update(rot_cfg, transcript="/t/alive-sid.jsonl")
    captured = {}
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, cfg, now, resume=False, **kw:
                        captured.update(resume=resume) or {"mode": "window"})
    wake._resume_or_fresh_dead(marrow_conn, rot_cfg, DAY1, "test")
    assert captured["resume"] is True


def test_resume_or_fresh_dead_retired_sid_forces_fresh(monkeypatch, marrow_conn, rot_cfg):
    """Coordinator repro: `rotated` already consumed by an earlier wake, but
    the stale transcript pointer still resolves to the retired session's sid.
    retired_sid must block the resume and force a fresh spawn instead — this
    is the single choke point both ctl.cmd_wake's dead-branch and tick
    reconcile's resume share (_window_wake -> _resume_or_fresh_dead)."""
    from cortex import transcript, wake_state
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    # rotated already consumed elsewhere; stale pointer still names the
    # retired session (durably recorded via set_retired_sid at rotate time).
    wake_state.update(rot_cfg, transcript="/t/retired-sid.jsonl")
    wake_state.set_retired_sid(rot_cfg, "/t/retired-sid.jsonl")
    assert wake_state.load(rot_cfg).get("rotated") is None  # already consumed
    captured = {}
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, cfg, now, resume=False, **kw:
                        captured.update(resume=resume) or {"mode": "window"})
    wake._resume_or_fresh_dead(marrow_conn, rot_cfg, DAY1, "test")
    assert captured["resume"] is False  # never resumes a retired session


def test_resume_or_fresh_dead_resume_spawn_failure_falls_back_to_fresh(
        monkeypatch, marrow_conn, rot_cfg):
    """A resume ATTEMPT that fails to land (the resume spawn returns None — bad/
    gone sid, claude errors out, window doesn't come up) must NEVER leave the
    caller with nothing: it retries once as a fresh spawn, never falling all
    the way through to headless from here."""
    from cortex import transcript, wake_state, window
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    wake_state.update(rot_cfg, transcript="/t/alive-sid.jsonl")
    calls = []

    def _spawn_wake_stub(conn, cfg, now, resume=False, **kw):
        calls.append(resume)
        if resume:
            return None  # resume spawn failed to land
        return {"mode": "window"}
    monkeypatch.setattr(wake, "_spawn_wake", _spawn_wake_stub)
    monkeypatch.setattr(window, "write_note", lambda cfg, text: None)
    result = wake._resume_or_fresh_dead(marrow_conn, rot_cfg, DAY1, "test")
    assert calls == [True, False]  # resume tried first, fresh retried on failure
    assert result["mode"] == "window"  # never nothing, even after the resume failure


# --------------------------------------------------------------------------- #
# BUG A: every set_awake path binds a wake=1 row so "Last wake" counts it
# --------------------------------------------------------------------------- #

def _wake_rows(conn):
    return conn.execute(
        "SELECT id, reasons, force_slept FROM ct_wake_log WHERE wake=1 "
        "ORDER BY id").fetchall()


def test_log_activation_wake_row_writes_tagged_row(marrow_conn):
    """A non-tick wake logs its OWN wake=1 row, tagged, force_slept NULL (so
    force_slept-based auto-rate stats stay unaffected)."""
    from cortex import occupancy
    wid = occupancy.log_activation_wake_row(marrow_conn, DAY1, "user")
    assert isinstance(wid, int)
    rows = _wake_rows(marrow_conn)
    assert len(rows) == 1
    assert rows[0]["id"] == wid
    assert rows[0]["reasons"] == "user"
    assert rows[0]["force_slept"] is None


def test_wake_log_id_writes_fresh_row_for_non_tick_wake(marrow_conn):
    """Chokepoint: a tagged (user/ctl/reconcile/rotate) wake gets a FRESH row
    even when an older scheduled row exists — so 'Last wake' never reuses a
    stale noon row (the BUG A symptom)."""
    # A stale scheduled row hours ago (the noon row in the incident).
    old_ts = (DAY1 - timedelta(minutes=280)).astimezone(timezone.utc).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons) VALUES (?, 1, 0, 'floor')",
        (old_ts,))
    marrow_conn.commit()
    old_id = _wake_rows(marrow_conn)[0]["id"]

    wid = wake._wake_log_id(marrow_conn, DAY1, "user")
    assert wid != old_id  # a new row, not the stale one
    rows = _wake_rows(marrow_conn)
    assert len(rows) == 2
    assert rows[-1]["reasons"] == "user"


def test_wake_log_id_falsy_reasons_logs_unknown_row(marrow_conn):
    """Falsy wake_reasons is not a live path (every real run_wake producer
    passes a truthy tag -- ctl/reconcile/user/rotate), but the chokepoint
    must never adopt an unrelated pre-existing wake=1 row for it; it logs its
    own fresh row tagged 'unknown' instead."""
    ts = DAY1.astimezone(timezone.utc).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons, explanation) "
        "VALUES (?, 1, 0, 'floor', '14:00 floor check due')",
        (ts,))
    marrow_conn.commit()
    old_id = _wake_rows(marrow_conn)[0]["id"]

    wid = wake._wake_log_id(marrow_conn, DAY1, None)
    assert wid != old_id  # never adopts the unrelated old row
    rows = _wake_rows(marrow_conn)
    assert len(rows) == 2
    assert rows[-1]["reasons"] == "unknown"


def test_main_print_note_no_marrow_call(monkeypatch, marrow_conn, wcfg, capsys):
    monkeypatch.setattr(wake.config, "load", lambda: wcfg)
    monkeypatch.setattr(wake.db, "connect", lambda cfg: marrow_conn)

    rc = wake.main(["--print-note"])

    assert rc == 0
    # wcfg carries no [note] config -> an empty note (just the trailing
    # newline from print()) is the correct, legitimate output here.
    out = capsys.readouterr().out
    assert out == "\n"


def test_main_force_wake_tags_ctl_reasons(monkeypatch, marrow_conn, wcfg):
    """Codex P2: `python -m cortex.wake --force` must carry a non-tick
    wake_reasons tag (like ctl/reconcile), or a manual force-wake reuses the
    latest old scheduled row exactly like the BUG A symptom this patch fixed."""
    monkeypatch.setattr(wake.config, "load", lambda: wcfg)
    monkeypatch.setattr(wake.db, "connect", lambda cfg: marrow_conn)
    captured = {}
    monkeypatch.setattr(
        wake, "run_wake",
        lambda conn, cfg, decision, now=None: captured.update(decision=decision))

    rc = wake.main(["--force"])

    assert rc == 0
    assert captured["decision"]["wake_reasons"] == "ctl"
