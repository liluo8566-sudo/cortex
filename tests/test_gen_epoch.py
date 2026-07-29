"""Generation-counter cancellation-epoch tests (BUG A + BUG B and the
reconcile / legacy-line guards).

The epoch (gen + state_id) is a fail-closed cancellation token: every deferred
actor captures it at birth and re-validates under a strict lock before each
consequential side effect. A user message (or newer claim) bumps gen, so the
old actor's late side effect is dropped. These tests drive the interleavings
with threading.Event seams — no real waits, no real subprocesses.
"""
from __future__ import annotations

import threading

import pytest

from cortex import config, db, lie_down, wake_state, watchdog


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


def _seed_awake(cfg, transcript="/t/old.jsonl"):
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "w"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, transcript)
    return wid


def _user_reset(cfg):
    """Mimic marrow's _cortex_user_wake_reset on the shared temp state: bump gen,
    flip awake, clear next_wake_at + tuck_pending."""
    def _m(d):
        d["gen"] = int(d.get("gen", 0)) + 1
        d["awake"] = True
        d["user_replied_this_wake"] = True
        d.pop("tuck_pending", None)
        d.pop("next_wake_at", None)
        return True
    # Unconditional bump via conditional_mutate(token=None).
    wake_state.conditional_mutate(cfg, None, _m)


# ── epoch primitives ──────────────────────────────────────────────────────────

def test_epoch_initialised_on_first_touch(cfg):
    gen, sid = wake_state.current_epoch(cfg)
    assert gen == 0 and isinstance(sid, str) and sid


def test_claim_lie_down_bumps_gen_and_returns_token(cfg):
    _seed_awake(cfg)
    g0, _ = wake_state.current_epoch(cfg)
    snap = wake_state.claim_lie_down(cfg, force_slept="stale")
    assert snap is not None
    tok = snap["claim_token"]
    g1, _ = wake_state.current_epoch(cfg)
    assert g1 == g0 + 1 == tok[0]


def test_failed_claim_does_not_bump(cfg):
    # not awake -> claim returns None and must NOT bump gen.
    wake_state.current_epoch(cfg)  # init
    g0, _ = wake_state.current_epoch(cfg)
    assert wake_state.claim_lie_down(cfg) is None
    g1, _ = wake_state.current_epoch(cfg)
    assert g1 == g0


def test_token_current_and_stale(cfg):
    gen, sid = wake_state.current_epoch(cfg)
    assert wake_state.token_current(cfg, (gen, sid)) is True
    wake_state.bump_gen(cfg)
    assert wake_state.token_current(cfg, (gen, sid)) is False
    assert wake_state.token_current(cfg, None) is True  # legacy


def test_conditional_mutate_drops_stale(cfg):
    gen, sid = wake_state.current_epoch(cfg)
    wake_state.bump_gen(cfg)
    with pytest.raises(wake_state.StateValidationError):
        wake_state.conditional_mutate(cfg, (gen, sid), lambda d: d.update(x=1))


# ── BUG A: user message during a still-running lie_down cancels the new alarm ──

def test_bug_a_user_reset_right_after_claim_suppresses_all(cfg, monkeypatch):
    """lie_down paused right after the claim (gen bumped), before the next
    wake is booked; a user reset fires on the same state; on release EVERY late side
    effect is suppressed: no next-wake booking, watchdog NOT killed, rotate suppressed,
    no ledger."""
    _seed_awake(cfg, transcript="/t/old.jsonl")

    released = threading.Event()
    reached = threading.Event()

    # Seam: block inside occupancy.lie_down (next-wake booking) — the first late
    # action, right after the claim. Actually pause BEFORE it via _token_ok so the
    # whole tail sees the stale token.
    from cortex import occupancy
    real_lie_down = occupancy.lie_down

    def paused_lie_down(conn, cfg_, **kw):
        reached.set()
        released.wait(2.0)
        return real_lie_down(conn, cfg_, **kw)

    monkeypatch.setattr("cortex.occupancy.lie_down", paused_lie_down)
    kicked = []
    monkeypatch.setattr("cortex.lie_down._notify_daemon",
                        lambda cfg_: kicked.append(1))
    killed_wd = []
    monkeypatch.setattr("cortex.lie_down._kill_watchdog",
                        lambda cfg_: killed_wd.append(True))

    def run_lie_down():
        lie_down.lie_down(cfg, force_slept="stale", rotate=True, next_wake_min=20)

    t = threading.Thread(target=run_lie_down)
    t.start()
    assert reached.wait(2.0)
    _user_reset(cfg)  # user arrives: bump gen, invalidate the claim's alarm chain
    released.set()
    t.join(3.0)
    assert not t.is_alive()

    st = wake_state.load(cfg)
    assert st.get("next_wake_at") is None      # ledger not armed for stale claim
    assert not st.get("rotated")               # rotate suppressed
    assert st.get("retired_sid") is None
    assert killed_wd == []                      # watchdog kill gated out
    assert kicked == []                         # no daemon kick for a dead claim
    assert st.get("awake") is True              # user reset owns the live wake


def _alerts_table(cfg):
    conn = db.connect(cfg)
    conn.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY,"
                 " severity TEXT, type TEXT, message TEXT, source TEXT)")
    conn.commit()
    conn.close()


def _alert_rows(cfg):
    conn = db.connect(cfg)
    try:
        return conn.execute("SELECT type, message FROM alerts").fetchall()
    finally:
        conn.close()


def test_rotate_race_alerts_and_does_not_raise(cfg, monkeypatch):
    """Rotate's conditional_mutate hits a superseded-epoch race (past the
    _token_ok gate, e.g. a newer claim lands mid-call): lie_down must not
    raise, rotated stays False, and the give-up is no longer silent — one
    alerts row plus a wake_audit log line."""
    _alerts_table(cfg)
    _seed_awake(cfg, transcript="/t/race.jsonl")

    def _boom(cfg_, token, mutate):
        raise wake_state.StateValidationError("superseded")

    monkeypatch.setattr(lie_down.wake_state, "conditional_mutate", _boom)

    result = lie_down.lie_down(cfg, force_slept="stale", rotate=True, next_wake_min=20)

    assert result["rotated"] is False

    rows = _alert_rows(cfg)
    assert len(rows) == 1
    assert rows[0]["type"] == "cortex_rotate_failed"

    log = config.wake_audit_log_path(cfg).read_text()
    assert "rotate_failed" in log


@pytest.fixture
def typed(monkeypatch):
    """Capture free-round keystrokes at the window boundary (delivery is typed)."""
    from cortex import window
    lines: list[str] = []
    monkeypatch.setattr(window, "inject_prompt",
                        lambda cfg, text: lines.append(text) or True)
    return lines


# ── BUG B: silence_action must not tuck-in after the session lay down ──────────

def test_bug_b_tuck_in_suppressed_after_claim(cfg, monkeypatch, typed):
    """silence_action blocked after capturing gen (seam at text build); a
    lie_down claims on the main thread; on release no tuck-in line, no
    tuck_pending."""
    _seed_awake(cfg)
    wake_state.update(cfg, user_replied_this_wake=True)

    reached = threading.Event()
    released = threading.Event()

    real_build = watchdog._build_tuck_in_line

    def paused_build(cfg_, mins):
        reached.set()
        released.wait(2.0)
        return real_build(cfg_, mins)

    monkeypatch.setattr(watchdog, "_build_tuck_in_line", paused_build)

    out = {}

    def run_silence():
        # silent_min well past silent_max (20) -> chat tier tuck-in path.
        out["r"] = watchdog.silence_action(cfg, 999.0)

    t = threading.Thread(target=run_silence)
    t.start()
    assert reached.wait(2.0)
    # The session lies down (claim bumps gen) while silence_action is mid-build.
    snap = wake_state.claim_lie_down(cfg, force_slept="stale")
    assert snap is not None
    released.set()
    t.join(3.0)
    assert not t.is_alive()

    st = wake_state.load(cfg)
    # No tuck_pending stamped (the claim bumped gen -> stamp mutation dropped).
    assert st.get("tuck_pending") is None
    # Nothing typed into the window.
    assert "[NEW ROUND]" not in "\n".join(typed)


def test_silence_action_tuck_in_happy_path(cfg, typed):
    """No interleaving: past silent_max stamps tuck_pending and types exactly
    one free-round line (regression that the fix keeps the normal path)."""
    _seed_awake(cfg)
    wake_state.update(cfg, user_replied_this_wake=True)
    action = watchdog.silence_action(cfg, 999.0)
    assert action == "free-round appended"
    st = wake_state.load(cfg)
    assert st.get("tuck_pending") is not None
    assert "\n".join(typed).count("[NEW ROUND]") == 1


# ── reconcile: stale-snapshot side effects suppressed ─────────────────────────

def test_reconcile_stale_snapshot_suppresses_reap(cfg, monkeypatch):
    """_handle_awake with a snapshot gen that no longer matches the live epoch
    holds instead of reaping."""
    _seed_awake(cfg)
    from cortex import reconcile
    st = wake_state.load(cfg)
    snap_gen = st["gen"]
    # A newer epoch lands after the snapshot (e.g. a user reset).
    wake_state.bump_gen(cfg)
    conn = db.connect(cfg)
    try:
        msg = reconcile._handle_awake(conn, cfg, st, snap_gen=snap_gen)
    finally:
        conn.close()
    assert "superseded" in msg
    # Still awake — no reap happened.
    assert wake_state.load(cfg).get("awake") is True


# ── legacy line tolerance (marrow-side parse mirrored here for the wire form) ──

def test_wake_receipt_token_roundtrip(cfg):
    """The epoch token now lives in the wake_state receipt, not the visible line.
    The on-screen bell is human text only."""
    from datetime import datetime
    from cortex import wake_state, window
    now = datetime(2030, 1, 1, 9, 0, tzinfo=config.get_tz(cfg))
    line = window.wake_signal_line(cfg, now, token=(7, "abcd1234"))
    assert "{g" not in line  # no token on screen
    window.write_wake_receipt(cfg, now, token=(7, "abcd1234"))
    r = wake_state.load(cfg)["wake_receipt"]
    assert r["gen"] == 7 and r["state_id"] == "abcd1234"
    assert r["text"] == line
    # Token-less receipt carries null gen/state_id.
    window.write_wake_receipt(cfg, now)
    r2 = wake_state.load(cfg)["wake_receipt"]
    assert r2["gen"] is None and r2["state_id"] is None
