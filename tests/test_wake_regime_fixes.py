"""Post-registration-deletion hardening (5 fixes):

  Fix 1 - rotate flag consumed only AFTER the fresh successor is verified live;
          preserved on every failure path; claim+spawn serialized.
  Fix 2 - resume readiness returns a verified result or raises WindowError; a
          readiness timeout no longer looks like success.
  Fix 3 - a resumed wake types ONE machine-tagged bell as soon as the window is
          ready (T11 P3: the Monitor-notice wait is gone).
  Fix 4 - REMOVED (2026-07-20): the spawn-path set_awake CAS is gone; the
          physically-up window is the resident, unconditionally.
  Fix 5 - the wake note opens with a config-driven machine-origin tag.

No iTerm/osascript here; window control is stubbed. Temp cortex_home + temp DB.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cortex import config, db, wake_state


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
    # These two default to the LIVE ~/.config/marrow/ dir (conftest isolation
    # guard fails otherwise) -- pin them under tmp_path.
    c["paths"]["wake_timing_log"] = str(tmp_path / "wake_timing.log")
    c["paths"]["handoff_file"] = str(home / "handoff.md")
    return c


def _seed_wake_row(cfg, tag="fix") -> int:
    conn = db.connect(cfg)
    try:
        conn.execute(
            "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
            (db.utcnow_iso(), tag))
        conn.commit()
        return conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    finally:
        conn.close()


# ── Fix 1: rotate flag preserved on failure, consumed only after success ──────

def test_peek_rotated_is_non_destructive(cfg):
    """peek_rotated reads the flag without consuming it (unlike take_rotated)."""
    wake_state.set_rotated(cfg)
    assert wake_state.peek_rotated(cfg) is True
    assert wake_state.peek_rotated(cfg) is True          # still set
    assert wake_state.take_rotated(cfg) is True           # now consumed
    assert wake_state.peek_rotated(cfg) is False


def test_window_wake_plan_fresh_does_not_consume_rotate(cfg, monkeypatch):
    """Fix 1: classification only PEEKS the rotate flag -- it must survive the
    plan call so a failed spawn keeps retry ownership."""
    from cortex import wake

    wake_state.set_rotated(cfg)
    assert wake._window_wake_plan(cfg) == "fresh"
    assert wake_state.peek_rotated(cfg) is True   # NOT consumed by classification


def test_run_wake_fresh_spawn_failure_preserves_rotate_flag(cfg, monkeypatch):
    """Fix 1 core: a rotate wake whose fresh spawn FAILS (window never comes up)
    must leave the rotate flag SET, so the next wake still classifies as fresh
    and never reactivates the retired conversation. Before the fix the flag was
    cleared during classification, before the spawn, so a failed spawn dropped
    it and the retired window got resumed on the next tick."""
    from cortex import symlinks, wake, window

    _seed_wake_row(cfg, "rot-fail")
    wake_state.set_rotated(cfg)

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)

    # The fresh spawn fails to come up (osascript/iTerm WindowError) -> the round
    # is given up (alert + audit), nothing else runs.
    def boom(c, initial_prompt=None, resume_sid=None):
        raise window.WindowError("no iterm")
    monkeypatch.setattr(window, "respawn", boom)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc))
    finally:
        conn.close()

    assert wake_state.peek_rotated(cfg) is True  # flag preserved for the retry


def test_run_wake_fresh_spawn_success_consumes_rotate_flag(cfg, monkeypatch):
    """Fix 1: once the fresh successor is verified live, the one-shot rotate flag
    IS consumed (so the wake after it is not another needless respawn)."""
    from cortex import symlinks, wake, watchdog, window

    _seed_wake_row(cfg, "rot-ok")
    wake_state.set_rotated(cfg)

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "sid-new")
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc))
    finally:
        conn.close()

    assert wake_state.peek_rotated(cfg) is False  # consumed after a live successor


def test_run_wake_concurrent_rotate_second_entrant_skips(cfg, monkeypatch):
    """Fix 1 concurrent-rotate (codex adversarial-review hardening): classify +
    dispatch now happen in ONE lock-protected call (_classify_wake), so there is
    no longer a second, later peek_rotated() sample that could observe a
    different answer than the classification itself. Modelled directly: an
    entrant whose _classify_wake call returns rotate_driven=True, but a
    concurrent winner (simulated) has ALREADY consumed the flag by the time the
    belt-and-braces in-lock guard checks peek_rotated() -> this entrant still
    must skip rather than double-spawn (the guard is defense-in-depth even
    though the single-classify structure makes the gap unreachable in practice)."""
    from cortex import symlinks, wake, watchdog, window

    _seed_wake_row(cfg, "rot-concurrent")

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    spawns = {"n": 0}
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None: spawns.__setitem__("n", spawns["n"] + 1))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")

    # ONE classification call (inside the lock) reports rotate_driven=True...
    monkeypatch.setattr(wake, "_classify_wake", lambda c: ("fresh", True))
    # ...but a concurrent winner (simulated externally) already consumed the flag
    # by the time the in-lock belt-and-braces guard checks it.
    monkeypatch.setattr(wake_state, "peek_rotated", lambda c: False)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        res = wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc))
    finally:
        conn.close()

    assert spawns["n"] == 0                       # loser skipped the fresh spawn
    assert res.get("skipped") == "spawn_race_lost"


def test_classify_wake_called_exactly_once_per_run_wake(cfg, monkeypatch):
    """Fix 1 core invariant: run_wake calls _classify_wake EXACTLY ONCE per wake
    (never a second classification/peek pass outside the lock) -- the prior
    two-read design (classify, then a second later peek_rotated() for
    rotate_claim) is what let a rotate loser observe a different answer than its
    own classification."""
    from cortex import symlinks, wake, watchdog, window

    wake_state.set_rotated(cfg)
    _seed_wake_row(cfg, "single-classify")

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "sid-new")
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")

    calls = {"n": 0}
    real_classify = wake._classify_wake

    def _spy(c):
        calls["n"] += 1
        return real_classify(c)
    monkeypatch.setattr(wake, "_classify_wake", _spy)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc))
    finally:
        conn.close()
    assert calls["n"] == 1


# ── Fix 2: readiness returns verified or raises ───────────────────────────────

def test_wait_ready_raises_on_timeout(cfg, monkeypatch):
    """Fix 2: _wait_ready must RAISE WindowError when the readiness marker never
    appears (a bad/gone --resume sid or an instantly-exiting claude leaves a bare
    shell) -- it previously returned identically on found and on timeout, so a
    dead resume was recorded as an awake resident."""
    from cortex import window

    cfg["wake"]["ready_timeout_sec"] = 0.01
    monkeypatch.setattr(window, "_read_session", lambda sid: "bare shell, no marker")
    with pytest.raises(window.WindowError):
        window._wait_ready("SID-X", cfg)


def test_wait_ready_returns_when_marker_present(cfg, monkeypatch):
    """Companion: the marker present -> returns cleanly (no raise)."""
    from cortex import window

    cfg["wake"]["ready_timeout_sec"] = 1
    monkeypatch.setattr(window, "_read_session",
                        lambda sid: "footer ... accept edits ... ready")
    window._wait_ready("SID-Y", cfg)  # no exception


def test_respawn_readiness_timeout_does_not_persist_sid(cfg, monkeypatch):
    """Fix 2: a resume whose TUI never comes up must NOT record the bare shell as
    the resident session. respawn raises WindowError (from _wait_ready) before
    set_session_id, so no stale sid is left behind."""
    from cortex import window

    cfg["wake"]["ready_timeout_sec"] = 0.01
    monkeypatch.setattr(window, "_spawn",
                        lambda c, initial_prompt=None, resume_sid=None: "BARE-SID")
    monkeypatch.setattr(window, "_read_session", lambda sid: "no marker here")

    with pytest.raises(window.WindowError):
        window.respawn(cfg, resume_sid="gone-uuid")
    assert wake_state.get_session_id(cfg) is None  # bare shell never recorded


def test_spawn_wake_resume_readiness_failure_surfaces_none(cfg, monkeypatch):
    """Fix 2 end-to-end: a resume whose respawn raises (readiness timeout) makes
    _spawn_wake return None, which _resume_or_fresh_dead turns into a fresh
    retry -- the documented fresh fallback finally fires."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "resume-timeout")
    wake_state.update(cfg, transcript="/x/projects/cwd/live-uuid.jsonl")

    calls = []

    def _respawn(c, initial_prompt=None, resume_sid=None):
        calls.append(resume_sid)
        if resume_sid:
            raise window.WindowError("resumed TUI never became ready")
        return "fresh-iterm-sid"
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident
    monkeypatch.setattr(window, "respawn", _respawn)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    conn = db.connect(cfg)
    try:
        res = wake._window_wake(conn, cfg, "N", datetime.now(timezone.utc))
    finally:
        conn.close()
    assert res is not None and res["mode"] == "window"
    assert calls == ["live-uuid", None]  # resume tried, then fresh fallback fired


# ── Fix 3: resumed wake -> ONE machine-tagged bell once the window is ready ───

def _write_assistant_lines(cfg, resume_sid: str, n: int) -> None:
    from cortex import transcript

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "assistant", "message": {"role": "assistant"}} for _ in range(n)]
    (tdir / f"{resume_sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def test_resume_types_one_bell_with_epoch_token(cfg, monkeypatch):
    """T11 P3: a resumed window gets ONE typed bell immediately after the awake
    flip — no Monitor-notice wait, no transcript polling. The bell carries the
    epoch token in its receipt so a superseded wake is still suppressed."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "resume-bell")
    wake_state.update(cfg, transcript="/x/projects/cwd/resume-uuid.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(window, "claude_session_id", lambda c: "resume-uuid")
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "resumed-sid")

    typed = []
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now, token=None: typed.append(token) or True)

    conn = db.connect(cfg)
    try:
        res = wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=True)
    finally:
        conn.close()

    assert res is not None and res["mode"] == "window"
    assert len(typed) == 1                        # exactly one bell
    assert typed[0] == wake_state.current_epoch(cfg)  # epoch token carried
    assert wake_state.load(cfg)["awake"] is True  # awake flip committed first


def test_resume_bell_typing_failure_does_not_unwind_wake(cfg, monkeypatch):
    """A WindowError while typing the resume bell is swallowed — the committed
    awake flip stands (the window IS up; only the nudge failed)."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "resume-bell-fail")
    wake_state.update(cfg, transcript="/x/projects/cwd/resume-uuid.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(window, "claude_session_id", lambda c: "resume-uuid")
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "resumed-sid")

    def _boom(c, now, token=None):
        raise window.WindowError("no session")
    monkeypatch.setattr(window, "type_wake_signal", _boom)

    conn = db.connect(cfg)
    try:
        res = wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=True)
    finally:
        conn.close()
    assert res is not None and res["mode"] == "window"
    assert wake_state.load(cfg)["awake"] is True


def test_resume_launch_is_clean_no_receipt(cfg, monkeypatch):
    """The resume LAUNCH itself bakes no prompt and writes no receipt -- only the
    post-readiness bell does. Here the bell is stubbed out; assert the launch was
    clean (initial_prompt None, no receipt written at launch)."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "resume-clean")
    wake_state.update(cfg, transcript="/x/projects/cwd/live-uuid.jsonl")

    launch = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        launch.update(prompt=initial_prompt, resume_sid=resume_sid))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(wake, "_resume_bell", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    conn = db.connect(cfg)
    try:
        wake._window_wake(conn, cfg, "N", datetime.now(timezone.utc))
    finally:
        conn.close()
    assert launch["resume_sid"] == "live-uuid"
    assert launch["prompt"] is None                    # clean launch, no bell baked
    assert "wake_receipt" not in wake_state.load(cfg)  # no receipt at launch


# ── Fix 4: epoch cancellation on the slow fresh/resume spawn ──────────────────

def test_fresh_spawn_receipt_carries_epoch_token(cfg, monkeypatch):
    """Fix 4: the fresh receipt carries the captured (gen, state_id); set_awake
    uses bump=False so the LIVE gen still equals the receipt gen (a bump would
    make the marrow hook read the receipt as stale and suppress the note)."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "fresh-token")
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "sid-new")
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/abc123.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    conn = db.connect(cfg)
    try:
        wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=False)
    finally:
        conn.close()

    d = wake_state.load(cfg)
    r = d["wake_receipt"]
    assert isinstance(r["gen"], int)
    assert r["state_id"] == d["state_id"]
    assert d["gen"] == r["gen"]   # live gen == receipt gen (not bumped)
    assert d["awake"] is True
    # Fix 2: the new session id is committed atomically with the awake flip
    # (set_awake's session_id= param) -- window.respawn itself never persists it.
    assert d["session_id"] == "sid-new"
    # Registration writes cortex_claude_sid = the fresh transcript stem (the
    # claude session id), so the registration key never sits stale.
    assert d["cortex_claude_sid"] == "abc123"


# ── Resume transcript settle: --resume APPENDS, never a new file ──────────────

def _seed_resume_transcript(cfg, resume_sid: str, n_lines: int):
    """Write <resume_sid>.jsonl and return its path (the pre-existing resume
    file that `claude --resume` appends to)."""
    from cortex import transcript

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    p = tdir / f"{resume_sid}.jsonl"
    p.write_text("\n".join('{"type":"assistant"}' for _ in range(n_lines)) + "\n")
    return p


def test_resume_settle_on_old_file_growth(cfg, monkeypatch):
    """Resume: --resume APPENDS to <resume_sid>.jsonl (no new file). respawn's
    growth of that file is the settle evidence -> transcript hint = the resume
    file AND cortex_claude_sid == resume_sid (the file's stem)."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "resume-grow")
    rp = _seed_resume_transcript(cfg, "resume-uuid", 1)
    monkeypatch.setattr(window, "claude_session_id", lambda c: "resume-uuid")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_resume_bell", lambda *a, **k: None)

    def _respawn(c, initial_prompt=None, resume_sid=None):
        # --resume appends a turn to the SAME file (mtime + size grow).
        with rp.open("a") as f:
            f.write('{"type":"assistant"}\n')
        return "resumed-iterm-sid"
    monkeypatch.setattr(window, "respawn", _respawn)

    conn = db.connect(cfg)
    try:
        res = wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=True)
    finally:
        conn.close()

    assert res is not None and res["mode"] == "window"
    d = wake_state.load(cfg)
    assert d["transcript"] == str(rp)               # hint = the resume file
    assert d["cortex_claude_sid"] == "resume-uuid"  # stem == resume_sid
    assert d["session_id"] == "resumed-iterm-sid"


def test_resume_settle_on_new_file_when_degraded(cfg, monkeypatch):
    """Edge: --resume silently degrades to a FRESH session (new sid) -> a jsonl
    outside the pre-spawn snapshot appears. Fresh-file evidence wins: settle on
    it, cortex_claude_sid = the NEW stem."""
    from cortex import transcript, wake, watchdog, window

    _seed_wake_row(cfg, "resume-degrade")
    _seed_resume_transcript(cfg, "resume-uuid", 1)  # pre-existing, never grows
    tdir = transcript.transcript_dir(cfg)
    monkeypatch.setattr(window, "claude_session_id", lambda c: "resume-uuid")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_resume_bell", lambda *a, **k: None)

    def _respawn(c, initial_prompt=None, resume_sid=None):
        # Degraded: a brand-new session file appears instead of the resume file.
        (tdir / "fresh-degraded.jsonl").write_text('{"type":"assistant"}\n')
        return "degraded-iterm-sid"
    monkeypatch.setattr(window, "respawn", _respawn)

    conn = db.connect(cfg)
    try:
        wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=True)
    finally:
        conn.close()

    d = wake_state.load(cfg)
    assert d["transcript"] == str(tdir / "fresh-degraded.jsonl")
    assert d["cortex_claude_sid"] == "fresh-degraded"  # new stem, not resume_sid


def test_resume_settle_timeout_records_none(cfg, monkeypatch):
    """Timeout: neither the resume file grows NOR a new file appears within the
    poll window -> new_path None, so set_awake records transcript=None /
    cortex_claude_sid=None (the None-guarded current failure behaviour), and the
    sid still commits via session_id=."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "resume-timeout-null")
    _seed_resume_transcript(cfg, "resume-uuid", 1)  # never grows
    monkeypatch.setattr(window, "claude_session_id", lambda c: "resume-uuid")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_resume_bell", lambda *a, **k: None)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "resumed-iterm-sid")
    # Collapse the bounded poll so the timeout branch is reached instantly.
    monkeypatch.setattr(wake, "_SPAWN_TRANSCRIPT_POLL_TIMEOUT_S", 0.0)
    monkeypatch.setattr(wake.time, "sleep", lambda s: None)

    conn = db.connect(cfg)
    try:
        wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=True)
    finally:
        conn.close()

    d = wake_state.load(cfg)
    assert d["transcript"] is None            # None-guarded failure behaviour
    assert d.get("cortex_claude_sid") is None
    assert d["session_id"] == "resumed-iterm-sid"  # sid still committed


def test_fresh_path_still_uses_new_file_snapshot(cfg, monkeypatch):
    """Regression: the FRESH path is unchanged -- it still detects the NEW window
    via the pre-spawn snapshot (a file OUTSIDE `preexisting`), never the resume
    settle. A pre-existing file must be ignored; only the post-spawn new file is
    recorded."""
    from cortex import transcript, wake, watchdog, window

    _seed_wake_row(cfg, "fresh-snapshot")
    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "old-retiring.jsonl").write_text('{"type":"assistant"}\n')  # pre-existing
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    def _respawn(c, initial_prompt=None, resume_sid=None):
        assert resume_sid is None  # fresh: no resume sid
        (tdir / "brand-new.jsonl").write_text('{"type":"assistant"}\n')
        return "fresh-iterm-sid"
    monkeypatch.setattr(window, "respawn", _respawn)

    conn = db.connect(cfg)
    try:
        wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=False)
    finally:
        conn.close()

    d = wake_state.load(cfg)
    assert d["transcript"] == str(tdir / "brand-new.jsonl")  # new file, not old
    assert d["cortex_claude_sid"] == "brand-new"


# ── Fix 5: machine-origin tag on the wake note ────────────────────────────────

def test_note_render_prepends_machine_tag(cfg):
    """Fix 5: the rendered wake note opens with the config-driven machine tag so
    the model treats the delivering ☀️ turn as an automated scheduler signal,
    not user speech. Approved shape: tag / "Now:" / "Active (Mac):" on three
    consecutive lines — no blank line between the tag and the body."""
    from cortex import note

    conn = db.connect(cfg)
    try:
        now = datetime.now(timezone.utc)
        text = note.render(cfg, now, note.gather(conn, cfg, now))
    finally:
        conn.close()
    tag = cfg["note"]["wake_machine_tag"]
    assert tag  # default is non-empty
    assert text.startswith(tag)
    lines = text.split("\n")
    assert lines[0] == tag
    assert lines[1].startswith("Now:"), f"blank/wrong line after tag: {lines[:3]!r}"


def test_note_render_machine_tag_config_toggle(cfg):
    """Fix 5 config-first: blanking wake_machine_tag omits the line entirely; the
    tag is never hardcoded in .py."""
    from cortex import note

    cfg["note"]["wake_machine_tag"] = ""
    conn = db.connect(cfg)
    try:
        now = datetime.now(timezone.utc)
        text = note.render(cfg, now, note.gather(conn, cfg, now))
    finally:
        conn.close()
    assert not text.startswith("[AUTOMATED WAKE SIGNAL")


# ===========================================================================
# codex adversarial-review, round 2: deterministic interleaving tests for the
# four high findings against c553d52 (rotate sampling gap / stale sid
# persistence / pre-readiness assistant turn / state_id ABA). Each test drives
# the exact interleave the finding describes, not just the end-state assertion.
# ===========================================================================

def test_interleave_rotate_sampling_gap_no_double_spawn(cfg, monkeypatch):
    """Finding 1 interleave: classification must be ONE lock-protected read, not
    two (plan, then a LATER separate peek_rotated() for rotate_claim). Drives the
    exact gap: entrant A's _classify_wake call reports rotate_driven=True: a
    concurrent winner (simulated) then consumes the flag BEFORE A's belt-and-
    braces re-check -- A must see the flag gone and skip, never double-spawning.
    (The single-classify structure makes this gap structurally unreachable in
    production; this test exercises the surviving guard directly.)"""
    from cortex import symlinks, wake, watchdog, window

    _seed_wake_row(cfg, "interleave-rotate")
    wake_state.set_rotated(cfg)

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    spawns = {"n": 0}
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None: spawns.__setitem__("n", spawns["n"] + 1))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")

    # _classify_wake runs for real (sees the real flag -> ("fresh", True)), but a
    # concurrent winner (modelled directly) consumes the flag the INSTANT after
    # classification returns, before this entrant's in-lock re-peek runs.
    real_classify = wake._classify_wake

    def _classify_then_race(c):
        result = real_classify(c)
        wake_state.take_rotated(c)  # the "winner" consumes it right here
        return result
    monkeypatch.setattr(wake, "_classify_wake", _classify_then_race)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        res = wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc))
    finally:
        conn.close()

    assert spawns["n"] == 0                       # never double-spawned
    assert res.get("skipped") == "spawn_race_lost"


def test_interleave_stale_sid_never_overwrites_live_resident(cfg, monkeypatch):
    """Finding 2 interleave: the epoch advances WHILE the window is booting
    (between _wait_ready succeeding and the caller's set_awake CAS). Drives the
    exact ordering: respawn() returns a verified sid WITHOUT persisting it; a
    'concurrent' actor (simulated inside the respawn stub, i.e. strictly between
    epoch capture and the CAS) commits ITS OWN new resident sid and bumps gen;
    the original (now-stale) spawn's CAS must reject and its sid must never reach
    wake_state -- the live resident stays the concurrent actor's sid throughout."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "interleave-sid")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")

    def _respawn_then_concurrent_actor_wins(c, initial_prompt=None, resume_sid=None):
        # Interleave point: THIS stale actor's window just became ready (respawn
        # about to return its verified sid) -- but before it can commit, a
        # concurrent actor (e.g. a user reset spawning its own fresh window) wins
        # the race: it commits its own session id + bumps the epoch first.
        wake_state.set_awake(cfg, None, "/concurrent/winner.jsonl",
                             session_id="concurrent-winner-sid")
        return "stale-actor-sid"  # verified-ready, but never committed
    monkeypatch.setattr(window, "respawn", _respawn_then_concurrent_actor_wins)

    token = wake_state.current_epoch(cfg)  # captured BEFORE the interleaved respawn
    conn = db.connect(cfg)
    try:
        # Directly exercise the CAS ordering _spawn_wake relies on: capture token
        # (already done above), respawn (interleaves the concurrent winner in),
        # then the conditional commit with the STALE token.
        new_sid = window.respawn(cfg, resume_sid=None)
        new_epoch = wake_state.set_awake(
            cfg, None, "/stale/actor.jsonl", expected_token=token, bump=False,
            session_id=new_sid)
    finally:
        conn.close()

    assert new_epoch is None  # the stale actor's CAS rejected
    d = wake_state.load(cfg)
    # The live resident is STILL the concurrent winner's -- the stale actor's sid
    # ("stale-actor-sid") never touched wake_state at all.
    assert d["session_id"] == "concurrent-winner-sid"
    assert d["transcript"] == "/concurrent/winner.jsonl"


def test_interleave_pre_readiness_assistant_turn_still_gets_one_bell(cfg, monkeypatch):
    """A harness-driven assistant turn written DURING the `claude --resume`
    launch/readiness window no longer changes anything: the bell is typed once,
    unconditionally, after the awake flip."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "interleave-preready")
    wake_state.update(cfg, transcript="/x/projects/cwd/resume-uuid.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")
    monkeypatch.setattr(window, "claude_session_id", lambda c: "resume-uuid")

    typed = []
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now, token=None: typed.append(token) or True)

    def _respawn_writes_turn_during_readiness(c, initial_prompt=None, resume_sid=None):
        _write_assistant_lines(cfg, "resume-uuid", 1)
        return "resumed-iterm-sid"
    monkeypatch.setattr(window, "respawn", _respawn_writes_turn_during_readiness)

    conn = db.connect(cfg)
    try:
        res = wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=True)
    finally:
        conn.close()

    assert res is not None and res["mode"] == "window"
    assert len(typed) == 1                       # exactly one bell, no dup
    assert wake_state.load(cfg)["awake"] is True  # awake flip still committed


def test_interleave_state_id_aba_rejects_recreated_state(cfg, monkeypatch):
    """Finding 4 interleave: wake_state.json is DELETED and RECREATED (e.g. a
    corrupt-state repair, or a wipe) landing back on the SAME gen but a NEW
    state_id, strictly BETWEEN the token capture and the CAS. A gen-only compare
    would pass (ABA) and let the stale actor overwrite the recreated state; the
    FULL (gen, state_id) token must reject it."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "interleave-aba")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, preexisting: "/t/new.jsonl")

    token = wake_state.current_epoch(cfg)  # (gen, original_state_id)

    def _respawn_recreates_state_same_gen(c, initial_prompt=None, resume_sid=None):
        # Interleave: delete + recreate the state file BETWEEN token capture and
        # the CAS, landing back on the SAME gen with a DIFFERENT state_id (the
        # ABA wake_state.json's _ensure_epoch re-seeds on first touch after a
        # wipe -- a fresh random state_id, same starting gen 0/whatever it was).
        wake_state.wake_state_path(cfg).unlink(missing_ok=True)
        new_gen, new_state_id = wake_state.current_epoch(cfg)  # re-seeds the file
        assert new_gen == token[0]              # same gen (the ABA condition)
        assert new_state_id != token[1]          # different identity
        return "stale-sid"
    monkeypatch.setattr(window, "respawn", _respawn_recreates_state_same_gen)

    conn = db.connect(cfg)
    try:
        new_sid = window.respawn(cfg, resume_sid=None)
        # The stale actor's CAS uses the ORIGINAL token (captured before the
        # interleaved delete/recreate) -- gen matches the recreated file's gen,
        # but state_id does not.
        new_epoch = wake_state.set_awake(
            cfg, None, "/stale/actor.jsonl", expected_token=token, bump=False,
            session_id=new_sid)
    finally:
        conn.close()

    assert new_epoch is None  # rejected despite the gen-only match (ABA closed)
    d = wake_state.load(cfg)
    assert d.get("session_id") != "stale-sid"   # stale actor never committed
    assert d.get("awake") is not True
