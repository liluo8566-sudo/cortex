from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cortex import config, note

MEL = ZoneInfo("Australia/Melbourne")
NOW = datetime(2026, 7, 8, 14, 30, tzinfo=MEL)


@pytest.fixture
def cfg(tmp_path):
    # Pure defaults: point load at a nonexistent path so no live cortex.toml leaks in.
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["cortex_home"] = str(tmp_path / "home")
    # These tests assert the note BODY structure (Now: / title). The Fix 5
    # machine-origin tag is a separate first line, verified in test_wake_regime_fixes;
    # blank it here so the body assertions (startswith "Now:" / title) stay exact.
    c["note"]["wake_machine_tag"] = ""
    return c


def make_events_table(conn):
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "timestamp TEXT, role TEXT, content TEXT, channel TEXT)"
    )
    conn.commit()


def _row_id(conn, content: str) -> int:
    """events.id of the row carrying `content` (row-id cursor tests)."""
    return conn.execute("SELECT id FROM events WHERE content=?",
                        (content,)).fetchone()[0]


def make_outbox_table(conn):
    conn.execute(
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT, from_sid TEXT, from_channel TEXT, target TEXT, body TEXT, "
        "status TEXT, sent_at TEXT, replied_at TEXT, reply_text TEXT, "
        "receipt_seen INTEGER NOT NULL DEFAULT 0, "
        "claimed_by TEXT, claimed_at TEXT)"
    )
    conn.commit()


def _add_ct_note(conn, *, id, from_sid="cafe1234", from_channel="tg",
                 body="miss you", status="pending",
                 created_at="2026-07-08T04:00:00Z"):
    conn.execute(
        "INSERT INTO outbox (id, created_at, from_sid, from_channel, target, body,"
        " status) VALUES (?, ?, ?, ?, 'ct', ?, ?)",
        (id, created_at, from_sid, from_channel, body, status))
    conn.commit()


def _add_receipt(conn, *, id, target="tg", from_channel="ct",
                 sent_at="2026-07-08T04:00:00Z", replied_at="2026-07-08T04:05:00Z",
                 reply_text="miss you", seen=0):
    conn.execute(
        "INSERT INTO outbox (id, from_channel, target, status, sent_at, replied_at,"
        " reply_text, receipt_seen) VALUES (?, ?, ?, 'sent', ?, ?, ?, ?)",
        (id, from_channel, target, sent_at, replied_at, reply_text, seen))
    conn.commit()


# --------------------------------------------------------------------------- #
# render — full note + omissions
# --------------------------------------------------------------------------- #

class _FloorReason:
    """Stand-in for a retired trigger-reason object (the Wake line is gone; the
    renderer must still ignore whatever `reasons` carries)."""
    kind = "floor"
    detail = "floor check due"


def test_render_full_note(cfg):
    data = {
        "wake_parts": ["wander"],
        "last_wake": {"minutes_ago": 12, "force_slept": None},
        "last_active": {"minutes_ago": 12},
        "active_app": "Google Chrome",
        "pending": [{"hm": "00:18", "intent": "去看看老婆睡了没"}],
    }
    text = note.render(cfg, NOW, data)
    assert "Wake:" not in text  # reason line retired
    assert text.startswith("Now: 14:30 Wed | Last active: 12min ago")
    assert "Plan Used" not in text  # budget line retired
    assert "Active (Mac): Google Chrome" in text
    # header = exactly the two lines above, nothing else
    assert text.split("\n\n---\n\n")[0].split("\n") == [
        "Now: 14:30 Wed | Last active: 12min ago",
        "Active (Mac): Google Chrome",
    ]
    assert "Pending self-schedule: due 00:18 去看看老婆睡了没" in text
    # block separators
    assert "\n\n---\n\n" in text
    # cal/rem retired
    assert "Cal:" not in text and "Rem:" not in text


def test_render_omits_absent_lines(cfg):
    text = note.render(cfg, NOW, {})
    assert "Wake:" not in text  # reason line retired
    assert text.startswith("Now: 14:30 Wed")
    assert "Last active:" not in text
    assert "Plan Used:" not in text
    assert "Active (Mac):" not in text
    assert "Pending" not in text
    assert "### Replay" not in text


def test_render_kick_reasons_plain_lines_no_header(cfg):
    data = {"kick_reasons": ['Msg #3 replied: "miss you"', "She's up — day mode"]}
    text = note.render(cfg, NOW, data)
    assert 'Msg #3 replied: "miss you"' in text
    assert "She's up — day mode" in text
    assert "### Woke for" not in text          # header rejected
    assert "Woke for" not in text


def test_render_kick_reasons_absent_when_empty(cfg):
    assert "Woke for" not in note.render(cfg, NOW, {"kick_reasons": []})


def _home_cfg(cfg, tmp_path):
    """cfg with tmp_path state paths so wake_state writes stay off the live dir
    (conftest hard-wall)."""
    home = tmp_path / "cortex"
    (home / "state").mkdir(parents=True, exist_ok=True)
    cfg = dict(cfg)
    cfg["paths"] = {
        **cfg.get("paths", {}),
        "cortex_home": str(home),
        "wake_state_file": str(home / "state" / "wake_state.json"),
        "watchdog_pidfile": str(home / "state" / "watchdog.pid"),
    }
    return cfg


def test_gather_kick_reasons_settle_on_injection(cfg, tmp_path):
    import sqlite3

    from cortex import wake_state
    cfg = _home_cfg(cfg, tmp_path)
    wake_state.update(cfg, kick_reasons=['Msg #1 replied: "hi"'])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    make_events_table(conn)
    # consume_kick=True + settle=True (real injection) renders + clears the flags.
    data = note.gather(conn, cfg, NOW, consume_kick=True, settle=True)
    assert data["kick_reasons"] == ['Msg #1 replied: "hi"']
    assert wake_state.load(cfg).get("kick_reasons") in (None, [])
    # A second settled note sees nothing (already consumed).
    data2 = note.gather(conn, cfg, NOW, consume_kick=True, settle=True)
    assert data2["kick_reasons"] == []


def test_gather_kick_reasons_peek_without_settle_keeps_pending(cfg, tmp_path):
    import sqlite3

    from cortex import wake_state
    cfg = _home_cfg(cfg, tmp_path)
    wake_state.update(cfg, kick_reasons=['Msg #1 replied: "hi"'])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    make_events_table(conn)
    # consume_kick=True, settle=False (frozen note that may be discarded):
    # renders the reason but does NOT clear it — pending-visible.
    data = note.gather(conn, cfg, NOW, consume_kick=True)
    assert data["kick_reasons"] == ['Msg #1 replied: "hi"']
    assert wake_state.load(cfg).get("kick_reasons") == ['Msg #1 replied: "hi"']


def test_gather_passive_render_peeks_kick_reasons(cfg, tmp_path):
    import sqlite3

    from cortex import wake_state
    cfg = _home_cfg(cfg, tmp_path)
    wake_state.update(cfg, kick_reasons=['Msg #2 no reply in 30min'])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    make_events_table(conn)
    # consume_kick=False (render-only path) PEEKS the reason so an un-injected
    # kick surfaces in the fresh render — must NOT clear it.
    data = note.gather(conn, cfg, NOW)
    assert data["kick_reasons"] == ['Msg #2 no reply in 30min']
    assert wake_state.load(cfg).get("kick_reasons") == ['Msg #2 no reply in 30min']


# --------------------------------------------------------------------------- #
# reply receipts (P12 / C11)
# --------------------------------------------------------------------------- #

def _receipt_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    make_events_table(conn)
    make_outbox_table(conn)
    return conn


def test_render_receipts_plain_lines(cfg):
    data = {"receipts": ['Note #3 (tg 14:00): she replied 14:05 "hi"']}
    text = note.render(cfg, NOW, data)
    assert 'Note #3 (tg 14:00): she replied 14:05 "hi"' in text


def test_gather_receipt_render_and_settle_once(cfg, tmp_path):
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_receipt(conn, id=3, reply_text="miss you too")
    # consume_kick=True + settle=True (real injection) stamps receipt_seen.
    data = note.gather(conn, cfg, NOW, consume_kick=True, settle=True)
    assert data["receipts"] == [
        'Note #3 (tg 14:00): she replied 14:05 "miss you too"']
    assert conn.execute(
        "SELECT receipt_seen FROM outbox WHERE id=3").fetchone()[0] == 1
    # A second settled note sees nothing (already stamped).
    data2 = note.gather(conn, cfg, NOW, consume_kick=True, settle=True)
    assert data2["receipts"] == []


def test_gather_receipt_peek_without_settle_keeps_seen_zero(cfg, tmp_path):
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_receipt(conn, id=3, reply_text="miss you too")
    # consume_kick=True, settle=False (frozen note): renders but does NOT stamp.
    data = note.gather(conn, cfg, NOW, consume_kick=True)
    assert data["receipts"] == [
        'Note #3 (tg 14:00): she replied 14:05 "miss you too"']
    assert conn.execute(
        "SELECT receipt_seen FROM outbox WHERE id=3").fetchone()[0] == 0


def test_gather_receipt_passive_render_peeks_seen_zero(cfg, tmp_path):
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_receipt(conn, id=5)
    # consume_kick=False (render-only path) PEEKS the receipt (renders it) but
    # must NOT stamp receipt_seen.
    data = note.gather(conn, cfg, NOW)
    assert data["receipts"] == [
        'Note #5 (tg 14:00): she replied 14:05 "miss you"']
    assert conn.execute(
        "SELECT receipt_seen FROM outbox WHERE id=5").fetchone()[0] == 0


def test_gather_receipt_ignores_unreplied_and_other_sender(cfg, tmp_path):
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_receipt(conn, id=1, replied_at=None, reply_text=None)  # no reply yet
    _add_receipt(conn, id=2, from_channel="cli")               # not cortex-sent
    data = note.gather(conn, cfg, NOW, consume_kick=True)
    assert data["receipts"] == []


# --------------------------------------------------------------------------- #
# ct-targeted outbox note delivery via note render (P12 add-on)
# --------------------------------------------------------------------------- #

def test_gather_ct_note_claimed_pending_visible_then_settle(cfg, tmp_path):
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_ct_note(conn, id=9, from_sid="cafe1234", from_channel="tg", body="睡了吗")
    # consume_kick=True, settle=False (frozen note): claims pending -> claimed
    # (pending-visible), renders, but does NOT mark sent.
    data = note.gather(conn, cfg, NOW, consume_kick=True)
    assert len(data["ct_notes"]) == 1
    note_txt = data["ct_notes"][0]
    assert "📮 Message from tg·cafe" in note_txt   # C1 header, channel + sid4
    assert "睡了吗" in note_txt                      # body verbatim
    assert conn.execute(
        "SELECT status FROM outbox WHERE id=9").fetchone()[0] == "claimed"
    assert "睡了吗" in note.render(cfg, NOW, data)
    # An un-settled claimed note re-renders on the next claim (at-least-once).
    data2 = note.gather(conn, cfg, NOW, consume_kick=True)
    assert len(data2["ct_notes"]) == 1
    # A real injection (settle=True) marks it sent — then it is gone.
    data3 = note.gather(conn, cfg, NOW, consume_kick=True, settle=True)
    assert len(data3["ct_notes"]) == 1
    assert conn.execute(
        "SELECT status FROM outbox WHERE id=9").fetchone()[0] == "sent"
    assert note.gather(conn, cfg, NOW, consume_kick=True, settle=True)[
        "ct_notes"] == []


def test_gather_ct_note_settle_consume_once(cfg, tmp_path):
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_ct_note(conn, id=9)
    # settle=True on the first call -> marked sent -> gone on the second.
    assert note.gather(conn, cfg, NOW, consume_kick=True, settle=True)["ct_notes"]
    assert note.gather(conn, cfg, NOW, consume_kick=True, settle=True)[
        "ct_notes"] == []


def test_gather_ct_note_passive_render_does_not_claim(cfg, tmp_path):
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_ct_note(conn, id=9)
    # render-only path (consume_kick=False) must NOT claim/deliver the note
    data = note.gather(conn, cfg, NOW)
    assert data["ct_notes"] == []
    assert conn.execute(
        "SELECT status FROM outbox WHERE id=9").fetchone()[0] == "pending"


def test_gather_passive_render_peeks_claimed_ct_note_read_only(cfg, tmp_path):
    # A pending-visible (claimed, un-settled) ct note surfaces read-only in a
    # passive render (consume_kick=False) — never claims, never settles.
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_ct_note(conn, id=9, body="睡了吗", status="claimed")
    data = note.gather(conn, cfg, NOW)
    assert len(data["ct_notes"]) == 1
    assert "睡了吗" in data["ct_notes"][0]
    # untouched: still claimed, never marked sent
    assert conn.execute(
        "SELECT status FROM outbox WHERE id=9").fetchone()[0] == "claimed"


def test_ct_note_claim_exactly_one_owner(cfg, tmp_path):
    # Exactly-one-owner: a pending row claimed once cannot be claimed a second
    # time (guard on status='pending'). The first claimer owns it; a racing
    # claim finds nothing pending. Re-render of the claimed row is idempotent.
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_ct_note(conn, id=9, body="睡了吗")
    first = note._consume_ct_notes(cfg, conn, claimed_by="cortex.note")
    assert len(first) == 1
    assert conn.execute(
        "SELECT claimed_by FROM outbox WHERE id=9").fetchone()[0] == "cortex.note"
    # A different path re-rendering the already-claimed row does not re-claim
    # (claimed_by stays the first owner) — idempotent re-render, no double claim.
    second = note._consume_ct_notes(cfg, conn, claimed_by="cortex.free_round")
    assert len(second) == 1  # re-rendered (at-least-once), not re-claimed
    assert conn.execute(
        "SELECT claimed_by FROM outbox WHERE id=9").fetchone()[0] == "cortex.note"


def test_ct_note_claimed_by_hook_not_reclaimed_by_note(cfg, tmp_path):
    # Cross-path exactly-one-owner: a row the hook already SENT is never
    # re-delivered by the note path (both pending-claim and claimed-re-render
    # skip a 'sent' row).
    cfg = _home_cfg(cfg, tmp_path)
    conn = _receipt_conn()
    _add_ct_note(conn, id=9, status="sent")       # hook already delivered it
    assert note.gather(conn, cfg, NOW, consume_kick=True)["ct_notes"] == []


def test_render_turn_end_line_omitted_by_default(cfg):
    # Default turn_end_text is "" (T2: the silence cycle carries this info now).
    text = note.render(cfg, NOW, {})
    assert "NOTE:" not in text


def test_render_turn_end_line_appears_with_custom_template(cfg):
    # Clamp numbers still render from config when a custom template is set.
    # T3: the merged clamp floor is 0 for every hour (0 = immediate re-wake).
    cfg["note"]["turn_end_text"] = (
        "NOTE: lie_down(next_wake_min=N) [{next_wake_min}-{next_wake_max}].")
    text = note.render(cfg, NOW, {})
    assert text.rstrip().endswith(
        "NOTE: lie_down(next_wake_min=N) [0-360].")


def test_render_title_prepended_single_newline(cfg):
    cfg["note"]["title"] = "📮 小道消息"
    text = note.render(cfg, NOW, {})
    assert text.startswith("📮 小道消息\nNow: ")


def test_render_title_empty_omits_it(cfg):
    text = note.render(cfg, NOW, {})
    assert text.startswith("Now: ")
    assert "小道消息" not in text


def test_render_force_slept_marker(cfg):
    data = {"last_wake": {"minutes_ago": 40, "force_slept": "timeout"}}
    text = note.render(cfg, NOW, data)
    assert "Last active: 40min ago (force-slept: timeout)" in text


def test_render_auto_sleep_is_neutral(cfg):
    """force_slept='auto' = routine silence sleep -> NO force-incident tag.
    Rows stay queryable, but the note reads it as ordinary."""
    data = {"last_wake": {"minutes_ago": 40, "force_slept": "auto"}}
    text = note.render(cfg, NOW, data)
    assert "Last active: 40min ago" in text
    assert "force-slept" not in text


def test_render_no_wake_line_ever(cfg):
    """The 'Wake:' reason line is fully retired — gone from every render."""
    for data in ({}, {"wake_parts": ["wander"]}, {"last_wake": {"minutes_ago": 5, "force_slept": None}}):
        assert "Wake:" not in note.render(cfg, NOW, data)


# --------------------------------------------------------------------------- #
# DB-sourced facts
# --------------------------------------------------------------------------- #

def test_last_wake_skips_current_and_marks_force_slept(marrow_conn):
    prev = (NOW - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).isoformat()
    cur = (NOW - timedelta(seconds=10)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept) VALUES (?, 1, 0, ?)",
        [(prev, "timeout"), (cur, None)],
    )
    marrow_conn.commit()
    lw = note._last_wake(marrow_conn, NOW)
    assert lw["minutes_ago"] == 30
    assert lw["force_slept"] == "timeout"
    assert lw["ts"] == prev


def test_last_wake_none_when_only_current(marrow_conn):
    cur = (NOW - timedelta(seconds=5)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES (?, 1, 0)", (cur,))
    marrow_conn.commit()
    assert note._last_wake(marrow_conn, NOW) is None


def test_last_active_newest_row_for_this_shell(marrow_conn, cfg):
    """Newest ct_activity row for THIS shell's channel gives the minutes; other
    shells' rows are ignored even when they are more recent. The dead 'ct'
    channel (cortex as a standalone channel) is never consulted."""
    ct = (NOW - timedelta(minutes=900)).astimezone(ZoneInfo("UTC")).isoformat()
    tg = (NOW - timedelta(minutes=1)).astimezone(ZoneInfo("UTC")).isoformat()
    cli = (NOW - timedelta(minutes=7)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.executemany(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        [(ct, "s0", "ct"), (tg, "s1", "tg"), (cli, "s2", "cli")],
    )
    marrow_conn.commit()
    la = note._last_active(marrow_conn, cfg, NOW, "cli")
    assert la["minutes_ago"] == 7
    assert la["ts"] == cli
    la_tg = note._last_active(marrow_conn, cfg, NOW, "tg")
    assert la_tg["minutes_ago"] == 1
    assert la_tg["ts"] == tg


def test_last_active_defaults_to_cli_shell(marrow_conn, cfg):
    """No shell argument -> the cli shell (the unqualified render)."""
    cli = (NOW - timedelta(minutes=3)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        (cli, "s2", "cli"))
    marrow_conn.commit()
    assert note._last_active(marrow_conn, cfg, NOW)["minutes_ago"] == 3


def test_last_active_none_without_row_for_this_shell(marrow_conn, cfg):
    """No row on this shell's channel -> None (render falls back to the wake
    row) — another shell's activity must never fill the gap."""
    cli = (NOW - timedelta(minutes=2)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        (cli, "s2", "cli"))
    marrow_conn.commit()
    assert note._last_active(marrow_conn, cfg, NOW, "tg") is None


def test_render_last_active_falls_back_to_wake_minutes(cfg):
    """No last_active -> the line uses the wake row's minutes but keeps the
    'Last active:' label and the force-slept suffix."""
    data = {"last_wake": {"minutes_ago": 22, "force_slept": "timeout"}}
    text = note.render(cfg, NOW, data)
    assert "Last active: 22min ago (force-slept: timeout)" in text


def test_render_last_active_overrides_wake_minutes(cfg):
    """last_active minutes win over the wake row's minutes; the suffix still
    comes from the wake row."""
    data = {
        "last_wake": {"minutes_ago": 40, "force_slept": "timeout"},
        "last_active": {"minutes_ago": 3},
    }
    text = note.render(cfg, NOW, data)
    assert "Last active: 3min ago (force-slept: timeout)" in text


def test_pending_within_window(cfg, tmp_path, monkeypatch):
    sp = tmp_path / "ss.json"
    due_soon = (NOW + timedelta(minutes=10)).isoformat()
    due_far = (NOW + timedelta(minutes=40)).isoformat()
    sp.write_text(json.dumps([
        {"due_at": due_soon, "intent": "喝水"},
        {"due_at": due_far, "intent": "太远"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "self_schedule_path", lambda c: sp)
    pend = note._pending(cfg, NOW)
    assert pend == [{"hm": (NOW + timedelta(minutes=10)).strftime("%H:%M"), "intent": "喝水"}]


def test_pending_missing_file(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "self_schedule_path", lambda c: tmp_path / "nope.json")
    assert note._pending(cfg, NOW) == []


def test_pending_naive_aware_and_garbage_mixed(cfg, tmp_path, monkeypatch):
    """Regression: a naive (offset-free local) due_at used to raise TypeError
    comparing naive vs aware datetimes, crashing the whole note. Naive + aware
    + garbage entries in one file -> the two valid ones render, garbage
    skipped, no exception."""
    sp = tmp_path / "ss.json"
    naive_due = (NOW + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    aware_due = (NOW + timedelta(minutes=10)).isoformat()
    sp.write_text(json.dumps([
        {"due_at": naive_due, "intent": "naive"},
        {"due_at": aware_due, "intent": "aware"},
        {"due_at": "not-a-date", "intent": "garbage"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "self_schedule_path", lambda c: sp)
    pend = note._pending(cfg, NOW)
    assert pend == [
        {"hm": (NOW + timedelta(minutes=5)).strftime("%H:%M"), "intent": "naive"},
        {"hm": (NOW + timedelta(minutes=10)).strftime("%H:%M"), "intent": "aware"},
    ]


def test_frontmost_app_locked_returns_none(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "loginwindow\n"
    monkeypatch.setattr(note.subprocess, "run", lambda *a, **k: FakeProc())
    assert note._frontmost_app() is None


def test_frontmost_app_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no osascript")
    monkeypatch.setattr(note.subprocess, "run", boom)
    assert note._frontmost_app() is None


def test_frontmost_app_ok(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "WeChat\n"
    monkeypatch.setattr(note.subprocess, "run", lambda *a, **k: FakeProc())
    assert note._frontmost_app() == "WeChat"


# --------------------------------------------------------------------------- #
# gather integration (external facts stubbed)
# --------------------------------------------------------------------------- #

def test_gather_end_to_end(marrow_conn, cfg, tmp_path, monkeypatch):
    # Isolate wake_state so a stale cursor on a real machine's live state
    # (diff-mode baseline, D6) can never filter out this fixture's row.
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "hi", "wx"))
    marrow_conn.commit()

    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    data = note.gather(marrow_conn, cfg, NOW, decision={
        "reasons": [_FloorReason()]})
    assert "wake_parts" not in data  # Wake reason line retired
    assert "budget" not in data  # budget line retired
    assert "replay" not in data  # Replay section deleted — turn_inject is the channel
    assert "handoff" not in data  # handoff moved to SessionStart
    text = note.render(cfg, NOW, data)
    assert text.startswith("Now: ")
    assert "Wake:" not in text


# --------------------------------------------------------------------------- #
# gather diff mode (D6): last_note_row_id cursor persisted + advanced per render
# --------------------------------------------------------------------------- #

def _seed_prev_wake(conn, minutes_ago: int) -> str:
    """Write a prior wake=1 row `minutes_ago` before NOW; return its ISO ts."""
    ts = (NOW - timedelta(minutes=minutes_ago)).astimezone(ZoneInfo("UTC")).isoformat()
    conn.execute("INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES (?, 1, 0)", (ts,))
    conn.commit()
    return ts


def test_last_wake_after_short_rotate_cycle_reports_minutes(marrow_conn):
    """BUG A (note side): with the previously-missing wake rows now written, a
    16-min rotate cycle's most recent wake row (outside the current-wake epsilon)
    is what 'Last wake' reports — ~16min, not the noon scheduled wake hours back."""
    # Noon scheduled wake (hours ago) + a rotate-cycle wake 16 min ago.
    noon = (NOW - timedelta(minutes=280)).astimezone(ZoneInfo("UTC")).isoformat()
    rotate = (NOW - timedelta(minutes=16)).astimezone(ZoneInfo("UTC")).isoformat()
    cur = (NOW - timedelta(seconds=5)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons) VALUES (?, 1, 0, ?)",
        [(noon, "floor"), (rotate, "rotate"), (cur, "user")])
    marrow_conn.commit()
    lw = note._last_wake(marrow_conn, NOW)
    assert lw["minutes_ago"] == 16  # the rotate cycle wake, not noon


def test_gather_survives_naive_due_at_self_schedule(marrow_conn, cfg, tmp_path, monkeypatch):
    """Live-repro regression: a self_schedule.json entry with an offset-free
    (naive) due_at must not crash gather()/render() end-to-end."""
    make_events_table(marrow_conn)
    marrow_conn.commit()

    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    sp = tmp_path / "ss.json"
    naive_due = (NOW + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    sp.write_text(json.dumps([{"due_at": naive_due, "intent": "x"}]), encoding="utf-8")
    monkeypatch.setattr(config, "self_schedule_path", lambda c: sp)

    data = note.gather(marrow_conn, cfg, NOW)
    assert data["pending"] == [
        {"hm": (NOW + timedelta(minutes=5)).strftime("%H:%M"), "intent": "x"}
    ]
    text = note.render(cfg, NOW, data)
    assert "Pending self-schedule" in text


# --------------------------------------------------------------------------- #
# Window-line SID override (caller transcript beats wake_state)
# --------------------------------------------------------------------------- #

def _isolate_wake_state(cfg, tmp_path, state: dict | None):
    from cortex import wake_state
    p = tmp_path / "wake_state.json"
    cfg["paths"]["wake_state_file"] = str(p)
    if state is not None:
        p.write_text(json.dumps(state), encoding="utf-8")
    return p


def test_gather_window_sid_override(marrow_conn, cfg, tmp_path, monkeypatch):
    """Caller-supplied window_sid wins in the gathered data even when wake_state
    carries a stale (or no) transcript — awake_since still comes from wake_state.
    Neither is rendered any more (Window line retired); the data keys stay for
    callers that pass window_sid (watchdog / note_render --transcript)."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    since = (NOW - timedelta(minutes=3)).astimezone(ZoneInfo("UTC")).isoformat()
    _isolate_wake_state(cfg, tmp_path, {
        "transcript": "/x/deadbeef00.jsonl", "awake_since": since})

    data = note.gather(marrow_conn, cfg, NOW, window_sid="feed1234")
    assert data["window_sid"] == "feed1234"
    assert data["awake_since_hm"] == (NOW - timedelta(minutes=3)).strftime("%H:%M")
    assert "Window:" not in note.render(cfg, NOW, data)


def test_gather_window_sid_falls_back_to_wake_state(marrow_conn, cfg, tmp_path, monkeypatch):
    """No override -> Window SID comes from wake_state.transcript (legacy path)."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    _isolate_wake_state(cfg, tmp_path, {"transcript": "/x/abcd1234ef.jsonl"})

    data = note.gather(marrow_conn, cfg, NOW)
    assert data["window_sid"] == "abcd1234"


def test_gather_window_sid_only_when_wake_state_empty(marrow_conn, cfg, tmp_path, monkeypatch):
    """Override lands in the data even with no awake_since/no state."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    _isolate_wake_state(cfg, tmp_path, {})

    data = note.gather(marrow_conn, cfg, NOW, window_sid="cafe0001")
    assert data["window_sid"] == "cafe0001"
    assert data["awake_since_hm"] is None
    assert "Window:" not in note.render(cfg, NOW, data)


# --------------------------------------------------------------------------- #
# note_render CLI entry — fresh render, no side effects
# --------------------------------------------------------------------------- #

def test_note_render_main_prints_fresh_note_no_writes(tmp_path, monkeypatch, capsys):
    from cortex import config as _config, db as _db, note_render
    dbp = tmp_path / "marrow.db"
    _db.connect_path(dbp).close()  # create schema
    before = dbp.stat().st_mtime

    _cfg = _config.load(path=tmp_path / "absent.toml")
    _cfg["paths"]["marrow_db"] = str(dbp)
    _cfg["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    _cfg["paths"]["cortex_home"] = str(tmp_path / "home")
    monkeypatch.setattr(_config, "load", lambda path=None: _cfg)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    monkeypatch.setattr("sys.argv", ["note_render", "--transcript", "/t/feed1234ab.jsonl"])

    note_render.main()
    out = capsys.readouterr().out
    assert "Now: " in out
    assert "Window:" not in out  # Window/SID line retired
    # no wake_state written, DB not mutated by a fresh render
    assert not (tmp_path / "ws.json").exists()


def _render_cli(tmp_path, monkeypatch, capsys, argv):
    """note_render.main() over a fixed DB, returning its stdout."""
    from cortex import config as _config, db as _db, note_render
    tmp_path.mkdir(parents=True, exist_ok=True)
    dbp = tmp_path / "marrow.db"
    conn = _db.connect_path(dbp)
    make_events_table(conn)
    conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        [
            ("s", "2026-07-08T03:00:00+00:00", "assistant", "tg shell self talk", "tg"),
            ("s", "2026-07-08T03:01:00+00:00", "assistant", "cli shell self talk", "ct"),
        ],
    )
    conn.commit()
    conn.close()

    _cfg = _config.load(path=tmp_path / "absent.toml")
    _cfg["paths"]["marrow_db"] = str(dbp)
    _cfg["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    _cfg["paths"]["cortex_home"] = str(tmp_path / "home")
    monkeypatch.setattr(_config, "load", lambda path=None: _cfg)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    monkeypatch.setattr("sys.argv", ["note_render", *argv])

    class _FrozenNow(datetime):          # a minute boundary must not flake this
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr(note_render, "datetime", _FrozenNow)
    note_render.main()
    return capsys.readouterr().out


def test_note_render_carries_no_replay(tmp_path, monkeypatch, capsys):
    """note_render never carries Replay — marrow's turn_inject is the single
    replay channel for every session."""
    out = _render_cli(tmp_path, monkeypatch, capsys, [])
    assert "Now:" in out
    assert "### Replay" not in out
    assert "tg shell self talk" not in out
    # the live tg bridge shape (note_render_cmd) still renders a note body
    out = _render_cli(tmp_path / "shelled", monkeypatch, capsys,
                      ["--no-ct", "--shell", "tg"])
    assert "Now:" in out
    assert "### Replay" not in out


# --------------------------------------------------------------------------- #
# per-shell wake ledger (T1) + force-slept reason copy (T4)
# --------------------------------------------------------------------------- #

def test_last_wake_is_scoped_to_its_own_shell(marrow_conn):
    """T1: a cli row never surfaces for the tg shell, and vice versa — one
    shell's force-slept marker can never appear on another shell's page."""
    cli_ts = (NOW - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).isoformat()
    tg_ts = (NOW - timedelta(minutes=90)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept, shell) "
        "VALUES (?, 1, 0, ?, ?)",
        [(cli_ts, "ct-pause", "cli"), (tg_ts, None, "tg")],
    )
    marrow_conn.commit()

    tg = note._last_wake(marrow_conn, NOW, "tg")
    assert tg["minutes_ago"] == 90 and tg["force_slept"] is None
    cli = note._last_wake(marrow_conn, NOW, "cli")
    assert cli["minutes_ago"] == 30 and cli["force_slept"] == "ct-pause"
    # No shell given = the cli render (unqualified default).
    assert note._last_wake(marrow_conn, NOW)["ts"] == cli_ts


def test_last_wake_none_when_other_shell_only(marrow_conn):
    """T1: the tg page shows nothing when only cli rows exist."""
    ts = (NOW - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept, shell) "
        "VALUES (?, 1, 0, 'ct-pause', 'cli')", (ts,))
    marrow_conn.commit()
    assert note._last_wake(marrow_conn, NOW, "tg") is None


def test_gather_render_hides_other_shells_force_slept(marrow_conn, cfg):
    """T1 end-to-end: a cli force-slept row does not reach a tg render."""
    ts = (NOW - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept, shell) "
        "VALUES (?, 1, 0, 'ct-pause', 'cli')", (ts,))
    marrow_conn.commit()
    tg_text = note.render(cfg, NOW, note.gather(marrow_conn, cfg, NOW, shell="tg"))
    assert "force-slept" not in tg_text
    cli_text = note.render(cfg, NOW, note.gather(marrow_conn, cfg, NOW, shell="cli"))
    assert "(force-slept: ct-pause)" in cli_text


def test_render_force_slept_shows_raw_reason(cfg):
    """T4: the marker names the row's force_slept value."""
    for reason in ("ct-pause", "fuse", "stale"):
        text = note.render(cfg, NOW, {"last_wake": {"minutes_ago": 5,
                                                    "force_slept": reason}})
        assert f"(force-slept: {reason})" in text


def test_render_force_slept_silent_for_auto_and_empty(cfg):
    """T4: 'auto' (routine) and empty stay silent."""
    for reason in ("auto", "", None):
        text = note.render(cfg, NOW, {"last_wake": {"minutes_ago": 5,
                                                    "force_slept": reason}})
        assert "force-slept" not in text
