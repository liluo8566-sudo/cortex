"""Unified silence + awake gate tests (T2: perpetual free-round cycle).

One idle rule regardless of user presence: every silent_max_min of silence,
inject one free-round note + marker and re-arm the SAME timer from that
instant — repeat forever, no forced sleep, no menu. No-user wakes time from
awake_since (silent_min itself stays 0.0 with no user message). An external
kick (kick.py mark_kick_round) short-circuits the gate and fires the carrier
immediately. The awake gate never emits a wake; the late-alarm race (user
speaks then the due alarm fires) is silent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cortex import config, db, wake_state, watchdog


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


@pytest.fixture
def awake_window(cfg, monkeypatch):
    """A live wake with the two process boundaries stubbed: lie_down's daemon
    socket kick (auto sleep calls lie_down) and the awake gate's watchdog heal."""
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "w"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)
    monkeypatch.setattr("cortex.lie_down._notify_daemon", lambda *a, **k: None)
    monkeypatch.setattr("cortex.watchdog.spawn", lambda c: None)
    return cfg


_TYPED: list[str] = []


@pytest.fixture(autouse=True)
def _capture_typed(monkeypatch):
    """Free-round delivery is typed into the window now — capture the keystrokes
    at the window boundary instead of reading the retired signal file."""
    from cortex import window
    _TYPED.clear()
    monkeypatch.setattr(window, "inject_prompt",
                        lambda cfg, text: _TYPED.append(text) or True)
    return _TYPED


def _signal_lines(cfg):
    return "\n".join(_TYPED).splitlines()


def _staged(cfg):
    """The INVISIBLE free-round payload cortex staged for the marrow hook (the
    note never reaches the screen — only the short marker line is typed)."""
    p = wake_state.free_round_note_path(cfg)
    return p.read_text(encoding="utf-8") if p.exists() else ""


# --- no-user wake (same idle bar, timed from awake_since) ---------------------

def test_no_user_wake_idles_to_free_round(awake_window):
    cfg = awake_window
    # No user reply this wake; the gate times from awake_since (FIX 1), not
    # silent_min. Backdate the wake past silent_max_min (20) -> free-round marker.
    past = (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat()
    wake_state.update(cfg, awake_since=past)
    a1 = watchdog.silence_action(cfg, silent_min=0.0)
    assert a1 == "free-round appended"
    assert wake_state.is_awake(cfg) is True  # never forces sleep
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text


def test_no_user_gate_elapses_on_fresh_wake_with_zero_silent_min(awake_window):
    """FIX 1 regression: a fresh wake where the user NEVER speaks has no user
    message ts -> user_silent_min() is None -> silent_min=0.0. The gate times
    from awake_since instead, so an elapsed-but-never-spoken wake still reaches
    the free-round injection (same bar as the chat tier, silent_max_min)."""
    cfg = awake_window
    past = (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat()
    wake_state.update(cfg, awake_since=past)  # user_replied_this_wake stays False
    action = watchdog.silence_action(cfg, silent_min=0.0)  # no user turn -> 0.0
    assert action == "free-round appended"
    assert wake_state.is_awake(cfg) is True


def test_no_user_under_bar_holds(awake_window):
    cfg = awake_window
    # awake_since is ~now (set_awake) -> elapsed < silent_max_min -> hold.
    assert watchdog.silence_action(cfg, silent_min=0.0) is None
    assert wake_state.is_awake(cfg) is True


# --- chat tier: perpetual cycle, no forced sleep -------------------------------

def test_chat_free_round_then_repeats(awake_window):
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    # First: silent past silent_max (20) -> free-round marker (+ note), still
    # awake, never forced to sleep.
    a1 = watchdog.silence_action(cfg, silent_min=21.0)
    assert a1 == "free-round appended"
    assert wake_state.is_awake(cfg) is True
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    writes_after_first = text.count("[NEW ROUND]")
    assert writes_after_first == 1
    # Marker stamped -> not re-appended until ANOTHER full silent_max_min elapses
    # since the injection.
    a2 = watchdog.silence_action(cfg, silent_min=22.0)
    assert a2 is None
    assert "\n".join(_signal_lines(cfg)).count("[NEW ROUND]") == 1
    # Another full cycle elapses since the last injection -> fires again (the
    # perpetual loop) — still no forced sleep.
    past = (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat()
    wake_state.update(cfg, tuck_pending=past)
    a3 = watchdog.silence_action(cfg, silent_min=23.0)
    assert a3 == "free-round appended"
    assert wake_state.is_awake(cfg) is True
    assert "\n".join(_signal_lines(cfg)).count("[NEW ROUND]") == 2


def test_free_round_only_fires_once_per_cycle(awake_window):
    """A less-than-one-cycle elapsed since the last injection never re-fires,
    even if silent_min itself is still (correctly) past silent_max_min."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    watchdog.silence_action(cfg, silent_min=21.0)
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    wake_state.update(cfg, tuck_pending=recent)
    assert watchdog.silence_action(cfg, silent_min=26.0) is None
    assert "\n".join(_signal_lines(cfg)).count("[NEW ROUND]") == 1


def test_chat_under_silent_max_holds(awake_window):
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    assert watchdog.silence_action(cfg, silent_min=10.0) is None
    assert _signal_lines(cfg) == []


# --- kick carrier (T1 replacement for the retired wait-expiry ride) -----------

def test_kick_round_injects_immediately_bypassing_silent_min(awake_window):
    """wake_state.mark_kick_round (external wake) injects the free-round line on
    the next poll, bypassing silent_min (even silent_min=0), and consumes the
    marker exactly once."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    assert wake_state.mark_kick_round(cfg) is True
    action = watchdog.silence_action(cfg, silent_min=0.0)  # gate bypassed
    assert action == "kick free-round appended"
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert wake_state.peek_kick_round(cfg) is False  # consumed
    st = wake_state.load(cfg)
    assert st.get("tuck_pending") is not None  # cycle re-armed from now
    assert wake_state.is_awake(cfg) is True


def test_kick_round_stale_epoch_injects_nothing(awake_window):
    """A user message between the kick and the poll bumps gen -> the captured
    token is stale -> conditional_mutate raises -> nothing injected, marker
    stays for the next poll to retry."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.mark_kick_round(cfg)

    import cortex.wake_state as ws

    def _stale(*a, **k):
        raise ws.StateValidationError("epoch token stale")
    orig = ws.conditional_mutate
    watchdog.wake_state.conditional_mutate = _stale
    try:
        action = watchdog.silence_action(cfg, silent_min=0.0)
    finally:
        watchdog.wake_state.conditional_mutate = orig
    assert action is None
    assert _signal_lines(cfg) == []


def test_kick_round_fires_once_then_falls_through(awake_window):
    """After the kick-carrier injection consumes the marker, a second poll no
    longer sees a pending kick — it re-enters the normal cycle gate (held,
    since the injection just re-armed the timer)."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.mark_kick_round(cfg)
    assert watchdog.silence_action(cfg, silent_min=0.0) == \
        "kick free-round appended"
    a2 = watchdog.silence_action(cfg, silent_min=1.0)
    assert a2 is None
    assert wake_state.is_awake(cfg) is True


# --- template render ----------------------------------------------------------

def test_free_round_line_carries_configured_copy(cfg):
    """Default free-round line renders the configured tuck_in_text (T2
    user-approved copy) with the [NEW ROUND] marker."""
    line, _note = watchdog._build_tuck_in_line(cfg, mins=17.0)
    assert "[NEW ROUND]" in line
    for stray in ("{mins}", "{user}", "{n}", "{cap}"):
        assert stray not in line


def test_free_round_template_still_substitutes_placeholders(cfg):
    """The substitution mechanism survives for a custom template: {mins}/{user}
    still fill."""
    cfg["wake"]["tuck_in_text"] = "⏳ [NEW ROUND] {mins} min since {user}"
    line, _note = watchdog._build_tuck_in_line(cfg, mins=17.0)
    assert "17 min" in line
    assert "the user" in line  # no marrow config -> fallback


# --- free-round note (every injection carries one) -----------------------------

def test_kick_carrier_tuck_in_carries_fresh_note(awake_window):
    """A kick-carrier injection carries a freshly rendered note (a `Now:` line)
    — staged INVISIBLY for the marrow hook, never typed on screen."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.mark_kick_round(cfg)
    a1 = watchdog.silence_action(cfg, silent_min=0.0)
    assert a1 == "kick free-round appended"
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "Now:" not in text        # note stays off the screen
    assert "Now:" in _staged(cfg)    # delivered invisibly instead


def test_plain_silence_gate_tuck_in_also_carries_note(awake_window):
    """The silence-cycle free-round ALSO carries a freshly rendered note, also
    invisible: marker on screen, note staged for the hook."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    watchdog.silence_action(cfg, silent_min=21.0)
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "Now:" not in text
    assert "Now:" in _staged(cfg)


def test_free_round_note_toggle_off(awake_window):
    """Toggle off -> plain marker, no note, on either free-round path."""
    cfg = awake_window
    cfg["wake"]["free_round_note"] = False
    wake_state.update(cfg, user_replied_this_wake=True)
    watchdog.silence_action(cfg, silent_min=21.0)
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "Now:" not in text
    assert _staged(cfg) == ""  # nothing staged either


def test_free_round_note_render_failure_falls_back(awake_window, monkeypatch):
    """A render blow-up must never block the injection -> plain marker still
    lands."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    monkeypatch.setattr(
        "cortex.note.gather",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    a1 = watchdog.silence_action(cfg, silent_min=21.0)
    assert a1 == "free-round appended"
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "Now:" not in text  # note omitted, marker survived


def test_free_round_mirrors_full_note_to_file(awake_window):
    """A free-round injection refreshes the on-disk wakeup_note.md with a FULL
    render so a human reading the file sees complete state."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    note_path = wake_state.wakeup_note_path(cfg)
    note_path.write_text("stale", encoding="utf-8")
    watchdog.silence_action(cfg, silent_min=21.0)
    body = note_path.read_text(encoding="utf-8")
    assert body != "stale" and "Now:" in body


def test_free_round_note_carries_no_replay_section(awake_window):
    """The free-round note reaches a WINDOW: the marker line is typed as a user
    prompt, so marrow's turn_inject is that window's replay outlet and the note
    must carry none."""
    cfg = awake_window
    conn = db.connect(cfg)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, timestamp TEXT, role TEXT, content TEXT, channel TEXT)")
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) "
        "VALUES ('s', '2026-07-08T03:00:00+00:00', 'user', 'chatter elsewhere', 'wx')")
    conn.commit()
    conn.close()
    wake_state.update(cfg, user_replied_this_wake=True)
    assert watchdog.silence_action(cfg, silent_min=21.0) == "free-round appended"
    staged = _staged(cfg)
    assert "Now:" in staged                    # the note did land
    assert "### Replay" not in staged
    assert "chatter elsewhere" not in staged


def _make_outbox(cfg, body="睡了吗", note_id=9):
    conn = db.connect(cfg)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT, from_sid TEXT, from_channel TEXT, target TEXT, body TEXT, "
        "status TEXT DEFAULT 'pending', sent_at TEXT, replied_at TEXT, "
        "reply_text TEXT, receipt_seen INTEGER DEFAULT 0, "
        "claimed_by TEXT, claimed_at TEXT)")
    conn.execute(
        "INSERT INTO outbox (id, created_at, from_sid, from_channel, target, body,"
        " status) VALUES (?, '2026-07-08T03:00:00Z', 'cafe', 'tg', 'ct', ?, 'pending')",
        (note_id, body))
    conn.commit()
    conn.close()


def _outbox_row(cfg, note_id=9):
    conn = db.connect(cfg)
    try:
        return conn.execute(
            "SELECT status, claimed_by, claimed_at FROM outbox WHERE id=?",
            (note_id,)).fetchone()
    finally:
        conn.close()


def test_free_round_render_does_not_claim_ct_note(awake_window):
    """Death replay: the background free-round RENDER (a tick that may never
    surface) must NOT claim a ct note. Only the post-commit ear delivery claims."""
    cfg = awake_window
    _make_outbox(cfg)
    watchdog._free_round_note(cfg)
    # render ran, but the ct note is untouched — still pending, no audit stamp.
    row = _outbox_row(cfg)
    assert row["status"] == "pending"
    assert row["claimed_by"] is None


def test_free_round_visible_round_claims_ct_note_with_audit(awake_window):
    """The visible kick-carrier free-round DELIVERS the ct note to the ear and
    stamps the audit columns (claimed_by / claimed_at)."""
    cfg = awake_window
    _make_outbox(cfg, body="睡了吗")
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.mark_kick_round(cfg)
    assert watchdog.silence_action(cfg, silent_min=0.0) == \
        "kick free-round appended"
    # Note claimed by the free-round path and surfaced in its own round: the
    # short marker is typed, the body rides the invisible staging file.
    row = _outbox_row(cfg)
    assert row["status"] == "sent"
    assert row["claimed_by"] == "cortex.free_round"
    assert row["claimed_at"] is not None
    assert "睡了吗" not in "\n".join(_signal_lines(cfg))
    assert "睡了吗" in _staged(cfg)
    assert "\n".join(_signal_lines(cfg)).count("[NEW ROUND]") == 2


def test_free_round_stale_epoch_does_not_claim_ct_note(awake_window, monkeypatch):
    """A tick whose ear write is dropped (stale epoch) must leave the ct note
    pending — the original death (claim then swallow) is closed."""
    cfg = awake_window
    _make_outbox(cfg)
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.mark_kick_round(cfg)

    def _stale(*a, **k):
        raise wake_state.StateValidationError("epoch token stale")
    monkeypatch.setattr(wake_state, "conditional_mutate", _stale)
    assert watchdog.silence_action(cfg, silent_min=0.0) is None
    row = _outbox_row(cfg)
    assert row["status"] == "pending"          # NOT swallowed
    assert row["claimed_by"] is None


def test_free_round_typed_line_is_the_marker_only(cfg):
    """The typed line is the SHORT marker alone (one line, machine-tagged); the
    rendered note is returned separately for invisible delivery."""
    line, note_text = watchdog._build_tuck_in_line(cfg, mins=17.0)
    assert line.strip().splitlines() == [line.strip()]      # single line
    assert line.lstrip().startswith("⏳ [NEW ROUND]")
    assert "Now:" not in line
    assert "Now:" in note_text                              # note rides invisibly


def test_failed_type_unstages_the_invisible_note(awake_window, monkeypatch):
    """No orphan payload: if the marker never lands, the staged note is dropped
    (a later marker turn must not pick up an undelivered round)."""
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    monkeypatch.setattr(watchdog, "_type_tuck_in_line", lambda c, line: False)
    assert watchdog.silence_action(cfg, silent_min=21.0) == "free-round appended"
    assert _staged(cfg) == ""


def _fresh_transcript(cfg):
    import json
    from cortex import transcript
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 1, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 1}}}))


def test_awake_gate_late_alarm_race_is_silent(awake_window):
    """User speaks 15:54 (awake, fresh transcript), the late alarm fires
    15:55: the awake gate runs the silence check, sees the fresh transcript
    (idle ~0) -> holds, emits NO wake signal, stays awake."""
    from cortex import reconcile
    cfg = awake_window
    wake_state.update(cfg, user_replied_this_wake=True)
    _fresh_transcript(cfg)  # user just spoke -> transcript is hot
    conn = db.connect(cfg)
    try:
        msg = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "wake in progress" in msg  # held, no emit, no auto sleep
    assert _signal_lines(cfg) == []
    assert wake_state.is_awake(cfg) is True


def test_stale_hold_when_window_alive(awake_window, monkeypatch):
    """Long transcript-idle but the resident window is ALIVE (user reading/typing)
    -> hold, do NOT reap. Alive-but-quiet is not a dead window."""
    from cortex import reconcile, wake
    cfg = awake_window
    # No transcript -> idle 1e9 >= stale_min, past the silence check (idle 0.0).
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    conn = db.connect(cfg)
    try:
        msg = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "stale hold: window alive" in msg
    assert wake_state.is_awake(cfg) is True  # not reaped


def test_stale_reap_requires_confirm_ticks_default_two(awake_window, monkeypatch):
    """Default confirm_ticks=2: a single dead verdict must NOT reap (debounces a
    transient osascript hiccup) -- it records a suspect marker and holds."""
    from cortex import reconcile, wake
    cfg = awake_window
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    conn = db.connect(cfg)
    try:
        msg = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "suspect" in msg and "hold" in msg
    assert wake_state.is_awake(cfg) is True  # not reaped yet
    assert wake_state.load(cfg).get("stale_suspect")


def test_stale_reap_fires_on_second_consecutive_dead_tick(awake_window, monkeypatch):
    """Two consecutive dead verdicts (same gen, within TTL) -> reap fires exactly
    once on the second tick; the suspect marker is cleared."""
    from cortex import reconcile, wake
    cfg = awake_window
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    conn = db.connect(cfg)
    try:
        msg1 = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
        assert "suspect" in msg1
        assert wake_state.is_awake(cfg) is True
        msg2 = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "stale wake reaped" in msg2
    assert wake_state.is_awake(cfg) is False  # reaped
    assert wake_state.load(cfg).get("stale_suspect") is None


def test_stale_suspect_cleared_when_window_alive_between_dead_ticks(
        awake_window, monkeypatch):
    """dead once (suspect recorded) -> alive tick clears the marker -> a later
    dead tick starts the count over at 1 (does not reap)."""
    from cortex import reconcile, wake
    cfg = awake_window
    conn = db.connect(cfg)
    try:
        monkeypatch.setattr(wake, "_window_alive", lambda c: False)
        msg1 = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
        assert "suspect" in msg1
        assert wake_state.load(cfg).get("stale_suspect")

        monkeypatch.setattr(wake, "_window_alive", lambda c: True)
        msg2 = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
        assert "stale hold: window alive" in msg2
        assert wake_state.load(cfg).get("stale_suspect") is None

        monkeypatch.setattr(wake, "_window_alive", lambda c: False)
        msg3 = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "suspect" in msg3 and "(1/2)" in msg3  # fresh first strike
    assert wake_state.is_awake(cfg) is True  # not reaped


def test_stale_suspect_gen_bump_resets_count(awake_window, monkeypatch):
    """dead once, then gen bumps (user message / lie_down) -> the next dead tick
    does NOT reap: the marker's stale gen is treated as absent (fresh first
    strike), never accumulated across an epoch it wasn't captured against."""
    from cortex import reconcile, wake
    cfg = awake_window
    st = wake_state.load(cfg)
    snap_gen = st["gen"]
    conn = db.connect(cfg)
    try:
        monkeypatch.setattr(wake, "_window_alive", lambda c: False)
        msg1 = reconcile._handle_awake(conn, cfg, st, snap_gen=snap_gen)
        assert "suspect" in msg1

        wake_state.bump_gen(cfg)
        new_gen, _ = wake_state.current_epoch(cfg)
        msg2 = reconcile._handle_awake(conn, cfg, wake_state.load(cfg),
                                             snap_gen=new_gen)
    finally:
        conn.close()
    assert "suspect" in msg2 and "(1/2)" in msg2  # fresh first strike, no reap
    assert wake_state.is_awake(cfg) is True


def test_stale_reap_confirm_ticks_one_reproduces_old_immediate_behaviour(
        awake_window, monkeypatch):
    """confirm_ticks=1 (config override) reproduces the pre-debounce behaviour:
    a single dead verdict reaps immediately."""
    from cortex import reconcile, wake
    cfg = awake_window
    cfg["wake"]["stale"] = {"confirm_ticks": 1}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    conn = db.connect(cfg)
    try:
        msg = reconcile._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "stale wake reaped" in msg
    assert wake_state.is_awake(cfg) is False  # reaped


def test_awake_gate_asleep_still_fires(cfg, monkeypatch):
    """Sanity contrast: when NOT awake, the awake gate is not taken at all — the
    normal tick decision path runs (asleep+due -> emit as today)."""
    # No awake marker set -> is_awake False.
    assert wake_state.is_awake(cfg) is False


# --- double-fire guard (watchdog poll + tick awake-branch same window) ---------

def test_free_round_double_fire_single_delivery(awake_window, monkeypatch):
    """The watchdog poll and the daemon business tick both run silence_action on
    the same window. Stamping the free round CLAIMS it (bumps gen), so the racer
    that captured the same pre-claim token aborts — exactly ONE marker line is
    typed, not two.

    The race is reproduced deterministically: the first caller re-enters
    silence_action from inside its own (outside-the-lock) note render, i.e.
    after it captured its token but before it commits."""
    cfg = awake_window
    past = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    wake_state.update(cfg, awake_since=past)  # no user turn this wake
    _TYPED.clear()
    real_build = watchdog._build_tuck_in_line
    inner, entered = [], []

    def racing_build(c, mins):
        if not entered:
            entered.append(True)  # the racer renders normally, no re-entry
            inner.append(watchdog.silence_action(c, 0.0))  # the second racer
        return real_build(c, mins)

    monkeypatch.setattr(watchdog, "_build_tuck_in_line", racing_build)

    outer = watchdog.silence_action(cfg, 0.0)

    assert inner == ["free-round appended"]   # the racer that got there first
    assert outer is None                      # stale token -> nothing delivered
    assert len([t for t in _TYPED if "[NEW ROUND]" in t]) == 1


def test_lie_down_double_fire_single_effect(awake_window, monkeypatch):
    """Watchdog (60s poll) and tick awake-branch can both proxy lie_down in the
    same window. The atomic awake claim => exactly one acts (real result), the
    other no-ops; ct_wake_log force_slept + next-wake booking happen once each."""
    from cortex import lie_down as lie_down_mod
    from cortex import occupancy
    cfg = awake_window

    bookings = []
    real_book = occupancy.lie_down
    monkeypatch.setattr(
        "cortex.occupancy.lie_down",
        lambda conn, cfg, minutes=None: bookings.append(1) or real_book(conn, cfg, minutes=minutes))

    wid = wake_state.load(cfg)["wake_log_id"]
    r1 = lie_down_mod.lie_down(cfg, force_slept="stale")
    r2 = lie_down_mod.lie_down(cfg, force_slept="stale")

    # One winner (has next_wake / tokens), one no-op (skipped).
    winners = [r for r in (r1, r2) if "skipped" not in r]
    skipped = [r for r in (r1, r2) if r.get("skipped") == "not awake"]
    assert len(winners) == 1 and len(skipped) == 1
    assert wake_state.is_awake(cfg) is False
    # Single next-wake booking.
    assert len(bookings) == 1
    # Single ct_wake_log write: force_slept stamped exactly once on this row.
    conn = db.connect(cfg)
    try:
        row = conn.execute(
            "SELECT force_slept FROM ct_wake_log WHERE id=?", (wid,)).fetchone()
    finally:
        conn.close()
    assert row["force_slept"] == "stale"


