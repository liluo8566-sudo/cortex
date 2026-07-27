"""B3v tests: deterministic logic for wake_state, transcript parsing, and
lie_down (self-schedule clearing + token recording). No iTerm/osascript here —
window control is verified live. Uses a temp cortex_home + temp DB."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from cortex import config, db, lie_down, transcript, wake_state, window


def test_spawn_barrier_refuses_real_window_under_pytest(cfg):
    """Source-level spawn guard: window._spawn is the single genuine
    window-creation choke point (ensure_window + respawn both route here). Under
    pytest (PYTEST_CURRENT_TEST set) it must raise WindowError BEFORE any
    osascript runs, so an unmocked test path fails loudly instead of launching a
    real iTerm window + claude. Belt-and-braces with the conftest subprocess
    guard; covers future tests / out-of-repo callers the fixture cannot reach.
    ensure_window and respawn inherit the barrier since both call _spawn."""
    with pytest.raises(window.WindowError, match="refusing real window spawn"):
        window._spawn(cfg)
    # respawn routes through _spawn -> same barrier, no osascript reached.
    with pytest.raises(window.WindowError, match="refusing real window spawn"):
        window.respawn(cfg)


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dbfile = tmp_path / "marrow.db"
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(dbfile)
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


# --- wake_state ---------------------------------------------------------------

def test_wake_state_roundtrip(cfg):
    assert wake_state.is_awake(cfg) is False
    wake_state.set_session_id(cfg, "SID-1")
    assert wake_state.get_session_id(cfg) == "SID-1"
    wake_state.set_awake(cfg, 42, "/x/y.jsonl")
    d = wake_state.load(cfg)
    assert d["awake"] is True and d["wake_log_id"] == 42
    assert d["session_id"] == "SID-1"  # awake marker preserves other keys
    wake_state.clear_awake(cfg)
    assert wake_state.is_awake(cfg) is False
    assert wake_state.get_session_id(cfg) == "SID-1"  # session id survives


# --- transcript ---------------------------------------------------------------

def test_munge_matches_claude_dir():
    assert transcript._munge("/Users/x/.config/marrow/cortex") == \
        "-Users-x--config-marrow-cortex"


def test_window_tokens_last_usage(cfg):
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    rows = [
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 1, "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 2, "output_tokens": 3}}},
        {"type": "user", "message": {"role": "user"}},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 5, "cache_read_input_tokens": 90_000,
            "cache_creation_input_tokens": 1_000, "output_tokens": 500}}},
    ]
    (d / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    assert transcript.window_tokens(cfg) == 5 + 90_000 + 1_000 + 500


def test_window_tokens_no_transcript(cfg):
    assert transcript.window_tokens(cfg) == 0
    assert transcript.mtime(cfg) is None


def test_net_tokens_helper_removed():
    """transcript.net_tokens is deleted — Cortex Today now sums per-window final
    occupancy, not a per-turn net spend."""
    assert not hasattr(transcript, "net_tokens")


# --- lie_down: self-schedule clearing ----------------------------------------

def test_clear_due_self_schedule(cfg):
    now = datetime.now(timezone.utc)
    past = (now - timedelta(minutes=5)).isoformat()
    future = (now + timedelta(hours=2)).isoformat()
    p = config.self_schedule_path(cfg)
    p.write_text(json.dumps([
        {"due_at": past, "intent": "gone"},
        {"due_at": future, "intent": "kept"},
    ]))
    removed = lie_down._clear_due_self_schedule(cfg)
    assert removed == 1
    left = json.loads(p.read_text())
    assert [x["intent"] for x in left] == ["kept"]


def test_clear_due_self_schedule_naive_local(cfg):
    """Offset-free (naive) due_at is read as Australia/Melbourne local time."""
    tz = config.get_tz(cfg)
    now_local = datetime.now(tz)
    past_naive = (now_local - timedelta(minutes=10)).replace(tzinfo=None).isoformat()
    future_naive = (now_local + timedelta(hours=4)).replace(tzinfo=None).isoformat()
    p = config.self_schedule_path(cfg)
    p.write_text(json.dumps([
        {"due_at": past_naive, "intent": "past-local"},
        {"due_at": future_naive, "intent": "future-local"},
    ]))
    assert lie_down._clear_due_self_schedule(cfg) == 1
    assert [x["intent"] for x in json.loads(p.read_text())] == ["future-local"]


def test_clear_due_self_schedule_bare_dict(cfg):
    """A bare dict (not wrapped in a list) is tolerated: treated as one entry,
    and the file is always rewritten as a list."""
    now = datetime.now(timezone.utc)
    past = (now - timedelta(minutes=5)).isoformat()
    p = config.self_schedule_path(cfg)
    p.write_text(json.dumps({"due_at": past, "intent": "gone"}))
    removed = lie_down._clear_due_self_schedule(cfg)
    assert removed == 1
    left = json.loads(p.read_text())
    assert left == []


# --- lie_down: token recording into ct_wake_log ------------------------------

def test_window_wake_alive_types_bell(cfg, monkeypatch):
    """Alive resident window: _window_wake writes the note file, TYPES ONE bell
    line (no respawn, no note-as-prompt), logs its own fresh wake row (never
    adopting an unrelated pre-existing one), sets the awake marker, and lights
    the watchdog — verified without osascript."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "dispatch"))
    conn.commit()
    old_wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]

    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: calls.setdefault("respawn", True))
    monkeypatch.setattr(
        window, "type_wake_signal",
        lambda c, now, token=None: calls.setdefault("signal", True))
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: True)
    monkeypatch.setattr(watchdog, "spawn", lambda c: calls.setdefault("watchdog", True))

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "NOTE-BODY", _dt.now(timezone.utc),
                            wake_reasons="user")
    conn.close()
    assert res == {"mode": "window", "session_id": None, "text": None}
    assert "respawn" not in calls               # live window is not respawned
    assert calls["signal"] is True              # bell typed once
    assert calls["watchdog"] is True
    # note body written into this shell's (cli) section of the note file
    assert wake_state.wakeup_note_path(cfg).read_text() == "## cli\nNOTE-BODY\n"
    d = wake_state.load(cfg)
    assert d["awake"] is True
    assert d["wake_log_id"] is not None and d["wake_log_id"] != old_wid


def test_window_wake_respawn_delivers_note_as_prompt(cfg, monkeypatch):
    """respawn=True (rotate/rebirth) spawns a FRESH window with the emoji +
    bell-marker first prompt baked in (fresh_initial_prompt) — no signal
    append, no notification (silent wake) — and sets the awake marker +
    watchdog. The marker in the baked prompt is what makes marrow's hook
    inject the full wakeup note into the new window."""
    from cortex import transcript, wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "respawn"))
    conn.commit()
    old_wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]

    calls = {}
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: calls.setdefault("prompt", initial_prompt))
    assert not hasattr(window, "spawn_greeting")  # greeting mechanism removed
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now, token=None: calls.setdefault("signal", True))
    # New session jsonl appears promptly (skip the real 8s poll).
    monkeypatch.setattr(transcript, "newest",
                        lambda c: __import__("pathlib").Path("/t/new.jsonl"))
    monkeypatch.setattr(watchdog, "spawn", lambda c: calls.setdefault("watchdog", True))

    from datetime import datetime as _dt
    now = _dt.now(timezone.utc)
    res = wake._window_wake(conn, cfg, "N", now, respawn=True, wake_reasons="ctl")
    conn.close()
    assert res["mode"] == "window"
    # Visible baked prompt = human text only (template) — no marker/token on
    # screen. The bell text lives in the wake_state receipt written before the
    # spawn (a fresh spawn carries no epoch token -> gen None, so the marrow
    # staleness check fails open and always processes the fresh wake).
    assert calls["prompt"] == window.spawn_opener_line(cfg, now)
    assert re.search(r"\{g\d+:[0-9a-fA-F]+\}", calls["prompt"]) is None
    r = wake_state.load(cfg)["wake_receipt"]
    assert r["text"] == calls["prompt"]
    # Fix 4: the fresh receipt now carries the captured epoch token (gen is an int,
    # not None). set_awake uses bump=False so the live gen still EQUALS this
    # receipt gen -> the marrow hook reads the receipt as current and injects the
    # note (a bump would make it stale and suppress the wake).
    assert isinstance(r["gen"], int)
    assert r["state_id"] == wake_state.load(cfg)["state_id"]
    assert wake_state.load(cfg)["gen"] == r["gen"]  # not bumped past the receipt
    assert "signal" not in calls                # fresh path never types a bell
    assert calls["watchdog"] is True
    d = wake_state.load(cfg)
    assert d["awake"] is True
    assert d["wake_log_id"] is not None and d["wake_log_id"] != old_wid


def test_window_wake_ear_epoch_reject_writes_no_phantom_row(cfg, monkeypatch):
    """Codex P2: when set_awake's expected_gen check loses the race (a user
    message flipped awake + bumped gen between the ear signal and here), the
    ear branch must NOT have already committed a tagged activation row — that
    row would be a phantom (belongs to a wake that never happened) while the
    user's own wake gets its own row. Fix: the row is only bound AFTER
    set_awake succeeds, via a conditional_mutate keyed to its returned token."""
    from cortex import wake, wake_state, watchdog, window

    conn = db.connect(cfg)
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(
        window, "type_wake_signal", lambda c, now, token=None: True)
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: True)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    # Simulate a user message racing in: bump gen right after current_epoch()
    # is captured inside _window_wake, before set_awake's conditional check.
    real_current_epoch = wake_state.current_epoch
    bumped = {"done": False}

    def racing_current_epoch(c):
        gen, sid = real_current_epoch(c)
        if not bumped["done"]:
            bumped["done"] = True
            wake_state.bump_gen(c)  # the "user message" racing in
        return gen, sid

    monkeypatch.setattr(wake_state, "current_epoch", racing_current_epoch)

    from datetime import datetime as _dt
    wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc), wake_reasons="user")
    conn.close()
    # set_awake's expected_gen check silently lost the race (pre-existing
    # contract: the caller does not treat this as a hard failure — someone
    # else already owns the wake). The regression this guards: no phantom
    # ct_wake_log row for the wake that never actually won.
    conn = db.connect(cfg)
    n = conn.execute("SELECT COUNT(*) AS n FROM ct_wake_log WHERE wake=1").fetchone()["n"]
    conn.close()
    assert n == 0  # no phantom activation row written for the losing wake


def test_bind_wake_log_id_stale_token_inserts_nothing(cfg, monkeypatch):
    """Structural fix (3rd round, codex gate): the insert now happens INSIDE
    the same conditional_mutate closure that binds it, so a stale token means
    the closure never runs at all — nothing is ever inserted, so there is
    nothing to compensate/delete afterward (the prior delete-based design's
    core flaw)."""
    from cortex import wake, wake_state

    conn = db.connect(cfg)
    token = wake_state.current_epoch(cfg)  # set_awake's returned token
    # Another actor intervenes AFTER set_awake returned `token`, before the bind.
    wake_state.bump_gen(cfg)

    from datetime import datetime as _dt
    wake._bind_wake_log_id(conn, cfg, _dt.now(timezone.utc), "user", token)

    n = conn.execute("SELECT COUNT(*) AS n FROM ct_wake_log WHERE wake=1").fetchone()["n"]
    conn.close()
    assert n == 0  # nothing was ever inserted -- not inserted-then-deleted
    assert wake_state.load(cfg).get("wake_log_id") is None  # never bound


def test_bind_wake_log_id_racing_scheduled_wake_cannot_adopt(cfg, monkeypatch):
    """Adoption hole (codex gate): an in-flight ear activation must get its OWN
    fresh row, never adopting an unrelated pre-existing wake=1 row (e.g. an old
    row left with an `explanation` set). A separate _wake_log_id call with a
    falsy wake_reasons (not a live path -- every real producer passes a
    truthy tag) must likewise never adopt either existing row; it logs its own
    tagged "unknown" row instead."""
    from cortex import wake, wake_state

    conn = db.connect(cfg)
    # A pre-existing unrelated wake=1 row (old-style explanation column set).
    decision_id = conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "14:00 floor check due")).lastrowid
    conn.commit()

    token = wake_state.current_epoch(cfg)  # still current -> the ear bind wins
    from datetime import datetime as _dt
    now = _dt.now(timezone.utc)
    wake._bind_wake_log_id(conn, cfg, now, "user", token)

    ear_wid = wake_state.load(cfg).get("wake_log_id")
    rows = conn.execute(
        "SELECT id, reasons, explanation FROM ct_wake_log WHERE wake=1 ORDER BY id"
    ).fetchall()
    # The ear activation won and bound its OWN fresh row, not the decision row.
    assert ear_wid is not None and ear_wid != decision_id
    assert {r["id"] for r in rows} == {decision_id, ear_wid}

    # A falsy wake_reasons arrival must never adopt either existing row -- it
    # logs its own tagged "unknown" row instead.
    scheduled_wid = wake._wake_log_id(conn, now, None)
    conn.close()
    assert scheduled_wid not in (decision_id, ear_wid)


def test_bind_wake_log_id_fails_fast_under_db_write_contention(cfg, monkeypatch):
    """4th/5th round (codex gate): the shared connection's busy_timeout is 30s
    (db.connect_path). A DB INSERT/SELECT held under write contention while
    inside the locked closure could hold _strict_flock for up to 30s, starving
    competing set_awake/claim_lie_down (their own 5s deadline). Simulate real
    contention with a SECOND connection holding an uncommitted write txn on
    the same db file: the activation must still complete within ~1s, write no
    row, and leave wake_log_id None -- accounting is best-effort, the state
    machine never waits on the ledger.

    codex gate P1: a PRE-EXISTING pacemaker decision row (explanation set)
    must be seeded here -- the prior version of this test missed the bug
    because with no decision row present, the fallback SELECT had nothing to
    adopt. With one present, a failed activation insert must NOT fall through
    to it (that reuse is scheduled-wake-only): wake_log_id stays None (never
    the old row's id), the old row is untouched, and `conn` is left with no
    open transaction (the failed insert's implicit txn is rolled back)."""
    import sqlite3
    import time
    from cortex import wake, wake_state

    conn = db.connect(cfg)
    # A genuine pacemaker decision row a SCHEDULED wake would be allowed to
    # reuse -- this activation-tagged wake must never adopt it.
    decision_id = conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "14:00 floor check due")).lastrowid
    conn.commit()

    token = wake_state.current_epoch(cfg)  # still current -> the bind proceeds

    # A second connection holds an uncommitted write transaction on the SAME
    # db file, so any writer (and, under a rollback-journal, potentially a
    # reader) on `conn` contends for the lock.
    blocker = sqlite3.connect(cfg["paths"]["marrow_db"])
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES ('x', 1, 0)")
    try:
        from datetime import datetime as _dt
        started = time.monotonic()
        wake._bind_wake_log_id(conn, cfg, _dt.now(timezone.utc), "user", token)
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert elapsed < 2.0  # fails fast, nowhere near the connection's real 30s
    # Never adopted the pre-existing decision row, and never bound at all.
    bound = wake_state.load(cfg).get("wake_log_id")
    assert bound is None
    assert bound != decision_id

    # `conn` has no open transaction left -- the failed insert's implicit txn
    # was rolled back, so a fresh write can start immediately.
    assert conn.in_transaction is False
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()

    # The old decision row is untouched, and no new row was written by us.
    rows = conn.execute(
        "SELECT id, explanation FROM ct_wake_log WHERE wake=1").fetchall()
    # `conn`'s OWN busy_timeout was restored to its normal (pre-override) value
    # -- the short window never leaks into any other caller sharing this
    # connection.
    restored = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert [r["id"] for r in rows] == [decision_id]
    assert rows[0]["explanation"] == "14:00 floor check due"
    assert restored != wake._BIND_INSERT_BUSY_TIMEOUT_MS


def test_bind_wake_log_id_lock_hiccup_leaves_real_row_intact(cfg, monkeypatch):
    """StateValidationError also covers lock timeout / unreadable state, not
    just a stale token (codex gate finding #2). Even on that failure mode, a
    PRE-EXISTING real wake's row (e.g. the row a different already-bound wake
    is using) must be left untouched -- the new structure never deletes
    anything, so this holds by construction."""
    from cortex import wake, wake_state

    conn = db.connect(cfg)
    real_id = conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "rotate")).lastrowid
    conn.commit()

    def failing_conditional_mutate(cfg_, token_, mutate):
        raise wake_state.StateValidationError("lock acquire timeout")

    monkeypatch.setattr(wake_state, "conditional_mutate", failing_conditional_mutate)

    from datetime import datetime as _dt
    wake._bind_wake_log_id(conn, cfg, _dt.now(timezone.utc), "user",
                           (0, "sid"))

    rows = conn.execute(
        "SELECT id, reasons FROM ct_wake_log WHERE wake=1").fetchall()
    conn.close()
    assert [r["id"] for r in rows] == [real_id]  # untouched
    assert rows[0]["reasons"] == "rotate"


def test_window_wake_ear_miss_alive_types_rearm_not_respawn(cfg, monkeypatch):
    """Ladder 2a: ear miss on an ALIVE window -> type the rearm bell line (no
    respawn), poll again; land -> ear wake. No fresh window is spawned."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "rearm"))
    conn.commit()
    old_wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]

    calls = {"respawn": 0}
    typed = []
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None: calls.__setitem__("respawn", calls["respawn"] + 1))
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now, token=None: typed.append(token) or True)
    # first poll (original signal) misses, second poll (after rearm) lands
    landings = iter([False, True])
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: next(landings))
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc), wake_reasons="user")
    conn.close()
    assert res["mode"] == "window"
    assert calls["respawn"] == 0   # alive window is NOT respawned
    assert len(typed) == 2         # first bell (with epoch token), then the retype
    assert typed[0] is not None and typed[1] is None
    d = wake_state.load(cfg)
    assert d["awake"] is True
    assert d["wake_log_id"] is not None and d["wake_log_id"] != old_wid


def test_window_wake_ear_miss_dead_respawns(cfg, monkeypatch):
    """Ladder 2b: ear miss AND claude dead -> respawn fresh."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "dead"))
    conn.commit()

    calls = {"respawn": 0, "typed": 0}
    # alive on the initial gate, dead when the ladder re-checks
    alive = iter([True, False])
    monkeypatch.setattr(wake, "_window_alive", lambda c: next(alive))
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None: calls.__setitem__("respawn", calls["respawn"] + 1))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now, token=None: calls.__setitem__("typed", calls["typed"] + 1) or True)
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: False)  # never lands
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["respawn"] == 1   # dead window respawned exactly once
    assert calls["typed"] == 1     # first bell only; a dead window is not re-typed


def test_window_wake_returns_none_on_window_error(cfg, monkeypatch):
    """An osascript/iTerm failure (WindowError) in the respawn path -> None so
    the caller alerts and gives up the round; awake marker stays off."""
    from cortex import wake, window

    def boom(c, initial_prompt=None, resume_sid=None):
        raise window.WindowError("no iterm")
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead -> fresh path
    monkeypatch.setattr(window, "respawn", boom)
    from datetime import datetime as _dt
    assert wake._window_wake(None, cfg, "x", _dt.now(timezone.utc)) is None
    assert wake_state.is_awake(cfg) is False


def test_lie_down_records_tokens(cfg):
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "test wake"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()

    # seed transcript so window_tokens > 0
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 23}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg, force_slept="timeout")
    assert r["tokens"] == 123
    conn = db.connect(cfg)
    row = conn.execute("SELECT tokens, force_slept FROM ct_wake_log WHERE id=?",
                       (wid,)).fetchone()
    conn.close()
    assert row["tokens"] == 123 and row["force_slept"] == "timeout"
    assert wake_state.is_awake(cfg) is False  # marker cleared


def test_store_window_tokens_survives_floor_redraw(cfg):
    """store_window_tokens publishes to ct_pacemaker_state and window_tokens_hint
    reads it back. Survives lie_down's own floor-redraw save_state."""
    from cortex import occupancy

    conn = db.connect(cfg)
    try:
        occupancy.store_window_tokens(conn, 88_000)
        assert occupancy.window_tokens_hint(conn) == 88_000
        # a later floor-redraw save must NOT wipe it out of order
        occupancy.lie_down(conn, cfg)
        occupancy.store_window_tokens(conn, 90_000)
        assert occupancy.window_tokens_hint(conn) == 90_000
    finally:
        conn.close()


# --- signal-file ear ----------------------------------------------------------

def test_type_wake_signal_line_format(cfg, monkeypatch):
    """type_wake_signal types exactly one VISIBLE bell line = human text only
    (wake_bell_template, '⏰ HH:MM'), no machine marker on screen, and writes
    NOTHING to disk beyond the wake_state receipt (machine data)."""
    from datetime import datetime as _dt

    from cortex import wake_state, window

    typed = []
    monkeypatch.setattr(window, "inject_prompt",
                        lambda c, text: typed.append(text) or True)
    now = _dt(2026, 7, 11, 9, 5, tzinfo=timezone.utc)
    window.type_wake_signal(cfg, now, token=(3, "cafe"))
    assert typed == ["⏰ 09:05"]
    r = wake_state.load(cfg)["wake_receipt"]
    assert r["text"] == "⏰ 09:05"
    assert r["gen"] == 3 and r["state_id"] == "cafe"
    assert r["template"] == "⏰ {hm}"
    assert r["template_prefix"] == "⏰ "


def test_wake_signal_line_is_human_text_only(cfg):
    """The visible bell line is human text only (template) — no marker, no epoch
    token, and NO rearm suffix on screen (rearm now lives in the receipt)."""
    from datetime import datetime as _dt

    from cortex import window

    now = _dt(2026, 7, 11, 9, 5, tzinfo=timezone.utc)
    assert window.wake_signal_line(cfg, now) == "⏰ 09:05"
    # rearm/token no longer change the RENDERED text — receipt carries them.
    assert window.wake_signal_line(cfg, now, rearm=True) == "⏰ 09:05"
    assert window.wake_signal_line(cfg, now, token=(2, "beef")) == "⏰ 09:05"


def test_deliver_covert_marker_types_directly(cfg, monkeypatch):
    """T11 P3: the covert marker is typed straight into the window — no ear file
    write, no wait. Only the marker line reaches the screen."""
    from cortex import window

    typed = []
    monkeypatch.setattr(window, "inject_prompt",
                        lambda c, text: typed.append(text) or True)
    assert window.deliver_covert_marker(cfg, "⚙️ [FUSE]") == "typed"
    assert typed == ["⚙️ [FUSE]"]


def test_deliver_covert_marker_reports_none_without_window(cfg, monkeypatch):
    from cortex import window

    monkeypatch.setattr(window, "inject_prompt", lambda c, text: False)
    assert window.deliver_covert_marker(cfg, "⚙️ [CTL] mins=30") == "none"


def test_esc_escapes_newlines_for_one_paste(cfg):
    """A multi-line free-round body must reach iTerm as ONE AppleScript string
    (\n escapes), not as raw linefeeds that would submit line by line."""
    from cortex import window

    assert window._esc("note line\nmore\r\n⏳ [NEW ROUND]") == \
        "note line\\nmore\\r\\n⏳ [NEW ROUND]"


def test_type_wake_signal_writes_rearm_receipt(cfg, monkeypatch):
    """type_wake_signal writes a receipt with rearm=True + the visible text."""
    from datetime import datetime as _dt

    from cortex import wake_state, window

    monkeypatch.setattr(window, "inject_prompt", lambda c, text: True)
    now = _dt(2026, 7, 11, 9, 5, tzinfo=timezone.utc)
    assert window.type_wake_signal(cfg, now) is True
    r = wake_state.load(cfg)["wake_receipt"]
    assert r["text"] == "⏰ 09:05" and r["rearm"] is True


# --- wakeup note baked into the launch command --------------------------------

def test_wake_prompt_is_emoji_only(cfg):
    """wake_prompt returns the configured emoji only — the marrow hook injects
    the full note on it. No note path substitution."""
    from cortex import window

    assert window.wake_prompt(cfg) == "☀️"
    cfg["wake"]["wake_prompt"] = "GO"
    assert window.wake_prompt(cfg) == "GO"


def test_static_zwj_template_roundtrips_through_receipt(cfg):
    """A fully STATIC template (no {hm}) with a multi-codepoint ZWJ emoji renders
    verbatim and round-trips byte-exact through the wake_state receipt JSON."""
    from datetime import datetime as _dt

    from cortex import wake_state, window

    static = "[🧚‍♀️ 笨鸭换岗成功]"
    cfg["wake"]["wake_bell_template"] = static
    now = _dt(2026, 7, 19, 9, 5, tzinfo=timezone.utc)
    # No {hm}: the rendered line equals the static text verbatim.
    assert window.wake_signal_line(cfg, now) == static
    assert window.bell_template_prefix(cfg) == static  # no {hm} -> whole text
    window.write_wake_receipt(cfg, now, token=(3, "cafe"))
    r = wake_state.load(cfg)["wake_receipt"]
    assert r["text"] == static and r["template"] == static
    assert r["template_prefix"] == static
    # ZWJ code points intact (U+1F9DA U+200D U+2640 U+FE0F).
    assert [hex(ord(c)) for c in r["text"][:5]] == \
        ["0x5b", "0x1f9da", "0x200d", "0x2640", "0xfe0f"]


def test_fresh_initial_prompt_is_spawn_opener_only(cfg):
    """fresh_initial_prompt bakes JUST the visible SPAWN OPENER line
    (spawn_opener_template), independent of the resident bell template. The
    marrow hook recognizes it via the wake_state receipt."""
    from datetime import datetime, timezone
    from cortex import window

    now = datetime(2026, 7, 10, 0, 55, tzinfo=timezone.utc)
    prompt = window.fresh_initial_prompt(cfg, now)
    assert prompt == "☀️ 00:55"
    assert prompt == window.spawn_opener_line(cfg, now)
    # The resident bell is a SEPARATE key -> changing it never moves the opener.
    cfg["wake"]["wake_bell_template"] = "RING {hm}"
    assert window.fresh_initial_prompt(cfg, now) == "☀️ 00:55"

    cfg["wake"]["spawn_opener_template"] = "GO {hm}"
    assert window.fresh_initial_prompt(cfg, now) == "GO 00:55"


def test_launch_command_bakes_initial_prompt(cfg):
    """launch_command bakes a non-empty initial_prompt as claude's first
    positional prompt (single-quoted) so a fresh window acts with zero typing."""
    from cortex import window

    cmd = window.launch_command(cfg, "Read /x/note.md — act on it")
    assert cmd.rstrip().endswith("'Read /x/note.md — act on it'")
    assert "arm" not in cmd  # no arm mechanism left


def test_launch_command_no_prompt_when_none(cfg):
    """No initial prompt -> no trailing prompt arg, window still launches."""
    from cortex import window

    cmd = window.launch_command(cfg)
    assert cmd.rstrip().endswith("--dangerously-skip-permissions")


def test_arm_mechanism_retired(cfg):
    """The arm-prompt boot mechanism is fully gone."""
    from cortex import config as _config, window

    assert not hasattr(window, "arm_prompt")
    assert not hasattr(_config, "arm_prompt_path")


def test_spawn_greeting_mechanism_removed():
    """The spawn notification is gone entirely — fresh windows wake silently,
    the emoji prompt is the only trace. No greeting / _notify / display
    notification anywhere in window.py."""
    import inspect

    from cortex import window

    assert not hasattr(window, "spawn_greeting")
    assert not hasattr(window, "_notify")
    assert "display notification" not in inspect.getsource(window)


def test_no_notification_config_key():
    """spawn_greeting config key dropped; wake_prompt defaults to the emoji."""
    from pathlib import Path

    from cortex import config

    c = config.load(path=Path("/no-such.toml"))
    assert "spawn_greeting" not in c["wake"]
    assert c["wake"]["wake_prompt"] == "☀️"


def test_spawn_wake_records_new_transcript_not_stale(cfg, monkeypatch):
    """P0 regression: _spawn_wake must NOT record the pre-spawn (OLD session)
    transcript. Before the fix it called transcript.newest() right after respawn
    — the new claude has not written its jsonl yet, so it recorded the PREVIOUS
    session's path; _window_rotated then saw a mismatch every tick and respawned
    forever. After the fix it polls for the NEW jsonl (or None on timeout) and
    records that, so a second consecutive wake on the alive window takes the ear
    path, not respawn.

    Timing model (the crux): OLD exists on disk BEFORE the spawn (retiring
    window, still being written — hence mtime-newest), so _spawn_wake captures
    it in the pre-spawn snapshot; the NEW window's file appears only on a LATER
    poll. Acceptance is snapshot-absence, not mtime: the file outside the
    pre-spawn set is the new one no matter what the ledger recorded. Modelled by
    making NEW appear on disk on the 3rd poll iteration."""
    from datetime import datetime as _dt

    from cortex import transcript, wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "p0"))
    conn.commit()

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    old = tdir / "OLD.jsonl"
    new = tdir / "NEW.jsonl"
    old.write_text("{}")   # retiring window, present in the pre-spawn snapshot

    # NEW appears on disk on the 3rd poll iteration (a beat after respawn, as in
    # production). Until then only OLD (snapshotted) exists, so the poll waits.
    sleeps = {"n": 0}

    def stub_sleep(s):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            new.write_text("{}")

    monkeypatch.setattr(window, "respawn", lambda c, initial_prompt=None, resume_sid=None: "sid-new")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake.time, "sleep", stub_sleep)

    wake._spawn_wake(conn, cfg, _dt.now(timezone.utc))
    conn.close()

    recorded = wake_state.load(cfg)["transcript"]
    assert recorded == str(new)          # NEW session, not the stale OLD path
    assert recorded != str(old)          # old timing recorded OLD here — the bug

    # Second wake: window alive, same NEW transcript, no rotate flag -> ear path.
    wake_state.set_session_id(cfg, "sid-new")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 4242)
    assert wake._window_rotated(cfg) is False  # no respawn loop


def test_wait_new_transcript_rotate_accepts_file_outside_snapshot(cfg, monkeypatch):
    """22:04 rotate regression, snapshot model: the RETIRING window is still
    alive and keeps writing its own jsonl for seconds after the spawn (lie_down
    MCP return + its final turn), so that file is mtime-newest the whole time.
    It is in the pre-spawn snapshot, so the poll skips it and waits for the real
    new window's file — which appears late and is NOT in the snapshot. The poll
    returns the new one, never the mtime-newest retiring file."""
    from cortex import transcript, wake

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    old = tdir / "c3ab04de.jsonl"   # retiring window, present pre-spawn
    new = tdir / "6d6e7b9c.jsonl"   # real new window, appears late
    old.write_text("{}")
    preexisting = {"c3ab04de.jsonl"}

    # NEW lands on the 3rd poll iteration; keep OLD mtime-newest throughout to
    # prove the accept condition is snapshot-absence, not mtime.
    import os
    sleeps = {"n": 0}

    def stub_sleep(s):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            new.write_text("{}")
            bump = old.stat().st_mtime + 10  # OLD stays mtime-newest
            os.utime(old, (bump, bump))

    monkeypatch.setattr(wake.time, "sleep", stub_sleep)
    result = wake._wait_new_transcript(cfg, preexisting)
    assert result == str(new)   # skipped the snapshotted file, returned the new one


def test_wait_new_transcript_only_preexisting_times_out(cfg, monkeypatch):
    """If no file outside the pre-spawn snapshot ever lands (only the retiring
    file exists, still being written), the poll returns None — never a
    snapshotted path."""
    from cortex import transcript, wake

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    old = tdir / "c3ab04de.jsonl"
    old.write_text("{}")
    preexisting = {"c3ab04de.jsonl"}

    monkeypatch.setattr(wake.time, "sleep", lambda s: None)
    result = wake._wait_new_transcript(cfg, preexisting)
    assert result is None  # in the snapshot -> never returned


def test_wait_new_transcript_picks_newest_of_several_new(cfg, monkeypatch):
    """If several files outside the snapshot exist, the newest (by mtime) is
    returned."""
    import os
    from cortex import transcript, wake

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    (tdir / "old.jsonl").write_text("{}")
    a = tdir / "new-a.jsonl"
    b = tdir / "new-b.jsonl"
    a.write_text("{}")
    b.write_text("{}")
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))  # b is newer

    monkeypatch.setattr(wake.time, "sleep", lambda s: None)
    result = wake._wait_new_transcript(cfg, {"old.jsonl"})
    assert result == str(b)


def test_spawn_wake_timeout_records_none_not_stale(cfg, monkeypatch):
    """If the NEW jsonl never appears within the poll window, record None (never
    the stale pre-spawn path). _window_rotated then treats the None hint on an
    alive, flag-free window as NOT rotated — the fallback must not reopen the
    loop."""
    from datetime import datetime as _dt

    from cortex import transcript, wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "p0-timeout"))
    conn.commit()

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    (tdir / "OLD.jsonl").write_text("{}")  # only the stale file exists, no new one

    monkeypatch.setattr(window, "respawn", lambda c, initial_prompt=None, resume_sid=None: "sid-x")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    # Force an immediate timeout so the test does not sleep.
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: None)

    wake._spawn_wake(conn, cfg, _dt.now(timezone.utc))
    conn.close()
    assert wake_state.load(cfg)["transcript"] is None  # None, not the stale path

    wake_state.set_session_id(cfg, "sid-x")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 4242)
    assert wake._window_rotated(cfg) is False  # None hint + alive -> not rotated


# --- rotate = flag for respawn, no /clear typing ------------------------------

def test_lie_down_explicit_rotate_sets_flag_no_typing(cfg, monkeypatch):
    """rotate=True flags a respawn for the next wake (session's explicit call)
    and does NOT type /clear (type_clear is gone). rotated=True in the result."""
    from cortex import window

    # type_clear must not exist anymore
    assert not hasattr(window, "type_clear")

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "rot"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()

    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 120_000, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 500}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg, rotate=True)
    assert r["rotated"] is True
    assert wake_state.take_rotated(cfg) is True  # flag set for the next wake


def test_lie_down_no_auto_rotate_over_line(cfg):
    """A big window no longer auto-rotates on lie_down (rotate is explicit)."""
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "norot"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()

    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 200_000, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 500}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg)  # no rotate flag
    assert r["rotated"] is False
    assert wake_state.take_rotated(cfg) is False


def test_lie_down_publishes_occupancy(cfg):
    """lie_down records window occupancy to ct_wake_log.tokens and publishes it
    onto ct_pacemaker_state (window_tokens_hint). net_tokens is no longer
    written (historical column stays NULL)."""
    from cortex import occupancy

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "net"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()

    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    # total occupancy 91_500 (big cache_read)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 0, "cache_read_input_tokens": 90_000,
                  "cache_creation_input_tokens": 1_000, "output_tokens": 500}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg, force_slept="timeout")
    assert r["tokens"] == 91_500  # ct_wake_log records total occupancy
    conn = db.connect(cfg)
    try:
        assert occupancy.window_tokens_hint(conn) == 91_500  # published occupancy
        row = conn.execute(
            "SELECT tokens, net_tokens FROM ct_wake_log WHERE id=?", (wid,)).fetchone()
        assert row["tokens"] == 91_500 and row["net_tokens"] is None
    finally:
        conn.close()


# --- Cortex Today: per-window final occupancy + live window --------------------

def _seed_wake_row(cfg) -> int:
    conn = db.connect(cfg)
    try:
        conn.execute(
            "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
            (db.utcnow_iso(), "occ"))
        conn.commit()
        return conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    finally:
        conn.close()


def _write_transcript(cfg, *usages) -> None:
    """Write a session jsonl with one assistant row per usage dict (window_tokens
    = last row's occupancy)."""
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "assistant", "message": {"usage": u}}) for u in usages]
    (d / "s.jsonl").write_text("\n".join(lines))


def test_today_tokens_single_window_counts_final_once(cfg):
    """One window lying down many times (occupancy grows monotonically) counts
    ONCE — its final occupancy — not the sum of every lie-down snapshot. No live
    window occupancy published, so the whole run is the current (open) window and
    contributes only via window_tokens_hint (0 here)."""
    from cortex import occupancy
    from datetime import datetime as _dt, timezone as _tz

    now = _dt.now(_tz.utc)
    conn = db.connect(cfg)
    try:
        for occ in (5_000, 20_000, 50_000):  # one window, monotonic growth
            conn.execute(
                "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?,1,0,?)",
                (db.utcnow_iso(), occ))
        conn.commit()
        # The single monotonic run is the trailing (current) window -> no finished
        # final; live occupancy hint is unset -> 0. Not 5k+20k+50k.
        assert occupancy._finished_window_finals(conn, now) == 0
        assert occupancy.window_tokens_hint(conn) == 0
    finally:
        conn.close()


def test_today_tokens_two_windows_sum_finals(cfg):
    """Two windows in a day: occupancy drops when the second window starts. The
    FIRST window's final (its peak before the drop) is a finished final; the
    second run is the current window (added via the live hint). Finished finals
    sum to the first window's final only."""
    from cortex import occupancy
    from datetime import datetime as _dt, timezone as _tz

    now = _dt.now(_tz.utc)
    conn = db.connect(cfg)
    try:
        # window 1: 10k -> 40k (final 40k), window 2 restarts lower: 3k -> 25k
        for occ in (10_000, 40_000, 3_000, 25_000):
            conn.execute(
                "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?,1,0,?)",
                (db.utcnow_iso(), occ))
        conn.commit()
        # finished finals = window 1 final (40k); window 2 is current -> live hint
        occupancy.store_window_tokens(conn, 30_000)  # live occupancy grew past 25k
        assert occupancy._finished_window_finals(conn, now) == 40_000
        assert occupancy.window_tokens_hint(conn) == 30_000
    finally:
        conn.close()


def test_today_tokens_current_window_added_from_live_hint(cfg):
    """The current window's contribution comes from the live window_tokens hint
    (fresher than its last ct_wake_log row), added on top of finished finals."""
    from cortex import occupancy
    from datetime import datetime as _dt, timezone as _tz

    now = _dt.now(_tz.utc)
    conn = db.connect(cfg)
    try:
        occupancy.store_window_tokens(conn, 12_345)  # only a live window, no finished rows
        assert occupancy._finished_window_finals(conn, now) == 0
        assert occupancy.window_tokens_hint(conn) == 12_345
    finally:
        conn.close()


def test_lie_down_returns_next_wake_hm(cfg):
    """lie_down returns next_wake as local HH:MM (the marrow MCP wrapper surfaces
    it). An explicit next_wake_min pins the next floor to now + N (clamped)."""
    from datetime import datetime as _dt

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "nw"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)

    # 120 is within [next_wake_min=21, next_wake_max=360] -> used verbatim.
    r = lie_down.lie_down(cfg, next_wake_min=120)
    assert "next_wake" in r
    tz = config.get_tz(cfg)
    expected = (_dt.now(tz) + timedelta(minutes=120)).strftime("%H:%M")
    # allow a 1-min clock-tick skew
    assert r["next_wake"] in (
        expected,
        (_dt.now(tz) + timedelta(minutes=121)).strftime("%H:%M"))


def test_lie_down_clamps_next_wake_min_to_ceiling(cfg):
    """lie_down(next_wake_min=N) clamps to [0, next_wake_max=360] — the
    session-facing window, not the floor draw. 999 -> 360."""
    from datetime import datetime as _dt

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "clamp"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)

    r = lie_down.lie_down(cfg, next_wake_min=999)
    tz = config.get_tz(cfg)
    expected = (_dt.now(tz) + timedelta(minutes=360)).strftime("%H:%M")
    assert r["next_wake"] in (
        expected, (_dt.now(tz) + timedelta(minutes=361)).strftime("%H:%M"))


def test_lie_down_zero_is_immediate_rewake(cfg):
    """T3: the merged clamp floor is 0 for every hour — lie_down(next_wake_min=0)
    schedules an immediate re-wake, never clamped up."""
    from datetime import datetime as _dt

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "clamp-lo"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)

    r = lie_down.lie_down(cfg, next_wake_min=0)
    tz = config.get_tz(cfg)
    expected = _dt.now(tz).strftime("%H:%M")
    assert r["next_wake"] in (
        expected, (_dt.now(tz) + timedelta(minutes=1)).strftime("%H:%M"))


def test_lie_down_negative_clamps_to_zero(cfg):
    """A sub-zero value clamps up to 0 (still immediate, never negative)."""
    from datetime import datetime as _dt

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "clamp-neg"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)

    r = lie_down.lie_down(cfg, next_wake_min=-30)
    tz = config.get_tz(cfg)
    expected = _dt.now(tz).strftime("%H:%M")
    assert r["next_wake"] in (
        expected, (_dt.now(tz) + timedelta(minutes=1)).strftime("%H:%M"))


# --- resume vs fresh (item 6) -------------------------------------------------

def _write_marker_jsonl(tdir, stem: str, marker: str = "[CORTEX-WAKE]") -> None:
    """A minimal session jsonl whose first user message is the baked window
    wake prompt ('<emoji> <marker> HH:MM') — a genuine window-lineage session."""
    line = json.dumps({"message": {"role": "user", "content": f"☀️ {marker} 01:00"}})
    (tdir / f"{stem}.jsonl").write_text(line + "\n")


def _write_digest_jsonl(tdir, stem: str, marker: str = "[CORTEX-WAKE]") -> None:
    """A minimal session jsonl shaped like marrow's sessionend digest: a
    headless `claude -p` run whose first user message is a large archived
    blob that QUOTES the marker deep inside it (not near its start) — must be
    rejected as a window-lineage candidate despite containing the substring."""
    blob = ("===== BEGIN ORIGINAL TRANSCRIPT (archived data) =====\n"
            f"some prior window said {marker} somewhere in here\n"
            "===== END =====")
    line = json.dumps({"message": {"role": "user", "content": blob}})
    (tdir / f"{stem}.jsonl").write_text(line + "\n")


def test_claude_session_id_from_recorded_hint_when_no_transcript_file(cfg):
    """No transcript file exists at all (e.g. a wiped/relocated transcript
    dir) -> claude_session_id falls back to the recorded hint. None when
    neither exists."""
    from cortex import window

    assert window.claude_session_id(cfg) is None
    wake_state.update(cfg, transcript="/x/projects/cwd/abc-123.jsonl")
    assert window.claude_session_id(cfg) == "abc-123"


def test_claude_session_id_prefers_newest_over_stale_recorded_hint(cfg):
    """Live-confirmed regression: the recorded hint can be STALE-BUT-PRESENT
    (a leftover from a previous cycle, never cleared) rather than just None.
    In the died-window/no-rotate-flag scenario the newest window-lineage
    session jsonl is ALWAYS the dead session's own archive — nothing writes to
    the dir after it dies — so it must win over any recorded hint, stale or
    not, whenever a marker-bearing transcript file exists."""
    from cortex import window

    wake_state.update(cfg, transcript="/x/projects/cwd/stale-hint-uuid.jsonl")
    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    _write_marker_jsonl(tdir, "dead-session-uuid")

    assert window.claude_session_id(cfg) == "dead-session-uuid"  # newest wins


def test_claude_session_id_falls_back_to_newest_transcript_when_hint_none(cfg):
    """The recorded hint is a best-effort ~8s poll after spawn; the claude TUI
    can take 30s+ to create its session jsonl in real timing, so the hint is
    routinely None. When that happens, claude_session_id must resolve the
    NEWEST window-lineage session jsonl in the transcript dir — in the
    died-window scenario that IS the dead session's own archive."""
    from cortex import window

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    _write_marker_jsonl(tdir, "dead-session-uuid")

    assert wake_state.load(cfg).get("transcript") is None  # no recorded hint
    assert window.claude_session_id(cfg) == "dead-session-uuid"


def test_claude_session_id_none_when_no_hint_and_no_transcript(cfg):
    """No recorded hint and no transcript file at all -> None (existing fresh
    fallback), never a fabricated UUID."""
    from cortex import window

    assert window.claude_session_id(cfg) is None


def test_claude_session_id_skips_headless_digest_picks_older_marker_session(cfg):
    """Third-layer live regression: the transcript dir also holds HEADLESS
    session jsonls (marrow's sessionend digest spawns `claude -p` against the
    same cwd -> same projects dir). A digest archive can be the mtime-newest
    file yet is not a window-lineage session -> must be skipped in favour of
    an OLDER marker-bearing (real window) session, never resumed onto the
    live window."""
    from cortex import window

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    _write_marker_jsonl(tdir, "real-window-session")
    import time as _time
    _time.sleep(0.02)
    _write_digest_jsonl(tdir, "digest-session")  # newer mtime, but headless

    assert window.claude_session_id(cfg) == "real-window-session"


def test_claude_session_id_none_when_only_digest_jsonls_present(cfg):
    """Only digest/headless jsonls in the dir (no marker-bearing candidate at
    all) -> falls through to the recorded hint, then None — never resumes a
    headless archive."""
    from cortex import window

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    _write_digest_jsonl(tdir, "digest-only")

    assert window.claude_session_id(cfg) is None
    wake_state.update(cfg, transcript="/x/projects/cwd/hint-uuid.jsonl")
    assert window.claude_session_id(cfg) == "hint-uuid"  # hint fallback still works


def test_launch_command_resume_variant(cfg):
    """launch_command bakes `--resume <sid>` when resume_sid is given."""
    from cortex import window

    cmd = window.launch_command(cfg, "☀️", resume_sid="abc-123")
    assert "--resume 'abc-123'" in cmd
    assert cmd.rstrip().endswith("'☀️'")
    plain = window.launch_command(cfg, "☀️")
    assert "--resume" not in plain


def test_window_wake_dead_resumes_when_sid_present(cfg, monkeypatch):
    """Item 6: a simply-dead resident (no rotate flag) with a recorded session
    UUID and NO transcript file on disk (newest() unavailable) -> resume via
    the recorded-hint fallback (respawn resume_sid set). The relaunch prompt
    is the SAME composed emoji+marker prompt as a fresh spawn so the resumed
    window also gets its wake identity + note."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "resume"))
    conn.commit()

    wake_state.update(cfg, transcript="/x/projects/cwd/live-uuid.jsonl")
    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        (calls.__setitem__("resume_sid", resume_sid),
                         calls.__setitem__("prompt", initial_prompt)))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    # The Fix-3 fallback bell is exercised by its own tests; here we only assert
    # the resume LAUNCH stays clean, so stub it out (it would otherwise poll for a
    # the resumed window a bell).
    monkeypatch.setattr(wake, "_resume_bell", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    now = _dt.now(timezone.utc)
    res = wake._window_wake(conn, cfg, "N", now)
    conn.close()
    assert res["mode"] == "window"
    assert calls["resume_sid"] == "live-uuid"   # same conversation resumed
    # Resume = the conversation is the identity: the LAUNCH itself types no bell
    # prompt and writes no receipt (the window returns with full context; the
    # harness's own background-shell notice drives the first turn).
    assert calls["prompt"] is None
    assert "wake_receipt" not in wake_state.load(cfg)


def test_window_wake_dead_resumes_from_newest_jsonl_when_hint_none(cfg, monkeypatch):
    """Real-timing regression: the recorded hint is None (the 8s spawn poll
    timed out before the 30s+ transcript creation), but a session jsonl exists
    in the transcript dir (the dead session's own archive) -> claude_session_id
    must still resolve it, and _window_wake must resume (not fresh-spawn)."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "resume"))
    conn.commit()

    assert wake_state.load(cfg).get("transcript") is None  # no recorded hint
    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    _write_marker_jsonl(tdir, "dead-session-uuid")

    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        (calls.__setitem__("resume_sid", resume_sid),
                         calls.__setitem__("launch_command",
                                           window.launch_command(c, initial_prompt, resume_sid))))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(wake, "_resume_bell", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["resume_sid"] == "dead-session-uuid"
    assert "--resume 'dead-session-uuid'" in calls["launch_command"]


def test_window_wake_dead_resumes_newest_over_stale_recorded_hint(cfg, monkeypatch):
    """Live-retest regression: --resume fired but resumed the STALE recorded
    hint instead of the dead window's real newest archive. A leftover recorded
    hint (from a previous cycle, never cleared) must NOT win when a real
    marker-bearing transcript file exists -> the window-lineage lookup takes
    priority end-to-end through _window_wake."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "resume"))
    conn.commit()

    wake_state.update(cfg, transcript="/x/projects/cwd/stale-hint-uuid.jsonl")
    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    _write_marker_jsonl(tdir, "dead-session-real-archive")

    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        (calls.__setitem__("resume_sid", resume_sid),
                         calls.__setitem__("launch_command",
                                           window.launch_command(c, initial_prompt, resume_sid))))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(wake, "_resume_bell", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["resume_sid"] == "dead-session-real-archive"  # newest, not the stale hint
    assert "--resume 'dead-session-real-archive'" in calls["launch_command"]


def test_window_wake_dead_skips_newer_digest_resumes_older_window_session(cfg, monkeypatch):
    """Third-layer live regression end-to-end: a headless sessionend-digest
    archive is the mtime-newest jsonl in the dir, but _window_wake must never
    resume it (would expose its full worker prompt in the window) — it must
    resume the OLDER real window-lineage session instead."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "resume"))
    conn.commit()

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    _write_marker_jsonl(tdir, "real-window-session")
    import time as _time
    _time.sleep(0.02)
    _write_digest_jsonl(tdir, "digest-session")  # newer mtime, headless

    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        (calls.__setitem__("resume_sid", resume_sid),
                         calls.__setitem__("launch_command",
                                           window.launch_command(c, initial_prompt, resume_sid))))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(wake, "_resume_bell", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["resume_sid"] == "real-window-session"
    assert "--resume 'real-window-session'" in calls["launch_command"]


def test_window_wake_dead_no_sid_fresh(cfg, monkeypatch):
    """Item 6 fallback: a dead resident with NO recorded UUID -> fresh spawn
    (resume_sid None)."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "fresh"))
    conn.commit()

    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead, no transcript
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        calls.__setitem__("resume_sid", resume_sid))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["resume_sid"] is None          # no UUID -> fresh spawn


def test_window_wake_plan_rotate_flag_is_fresh(cfg, monkeypatch):
    """_window_wake_plan: rotate flag -> 'fresh' (deliberate new brain). Fix 1:
    classification only PEEKS the flag; it is NOT consumed here (the one-shot
    consume is deferred to after a fresh successor is verified live), so the flag
    survives the plan call for retry ownership on a failed spawn."""
    from cortex import wake, window

    wake_state.set_rotated(cfg)
    assert wake._window_wake_plan(cfg) == "fresh"
    assert wake_state.peek_rotated(cfg) is True   # still set (peeked, not consumed)


def test_window_wake_plan_dead_no_flag_is_resume(cfg, monkeypatch):
    """_window_wake_plan: dead window with no rotate flag -> 'resume'."""
    from cortex import wake, window

    wake_state.set_session_id(cfg, "sid-dead")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: False)  # session gone
    assert wake._window_wake_plan(cfg) == "resume"


def test_window_wake_resume_spawn_failure_falls_back_to_fresh(cfg, monkeypatch):
    """Coordinator addition: a resume ATTEMPT whose spawn fails to land (window
    doesn't come up) must never leave the caller with nothing — _window_wake
    retries once as a fresh spawn, so a live awake cortex exists after the
    wake regardless."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "resume-fail-fallback"))
    conn.commit()

    wake_state.update(cfg, transcript="/x/projects/cwd/live-uuid.jsonl")
    calls = []
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident

    def _respawn_stub(c, initial_prompt=None, resume_sid=None):
        calls.append(resume_sid)
        if resume_sid:
            raise window.WindowError("resumed window did not come up")
        return "new-iterm-sid"
    monkeypatch.setattr(window, "respawn", _respawn_stub)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res is not None and res["mode"] == "window"
    assert calls == ["live-uuid", None]  # resume tried first, fresh retried on failure
