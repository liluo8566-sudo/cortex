"""duty.apply: hold-first ordering, the cli put-down, and the wake each newly
released shell gets with its fresh-vs-resume gate.

Every side effect is stubbed at its boundary — run_wake, the proxy lie_down and
the shell-host socket kick. The one unstubbed kick is the dead-socket case
(connect to a path that does not exist)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import pytest

from cortex import config, duty, shell_ledger, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["handoff_file"] = str(home / "handoff.md")
    c["paths"]["wake_timing_log"] = str(home / "wake_timing.log")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


@pytest.fixture
def cdir(cfg, tmp_path):
    return tmp_path


@pytest.fixture
def trace(cfg, cdir, monkeypatch):
    """Record every transition action in order, each stamped with the hold that
    was on disk at the moment it ran."""
    seen = []

    def _mark(what):
        seen.append((what, duty.read(cdir)["hold"]))

    def _run_wake(conn, c, decision, now=None):
        _mark("wake_cli")
        return {"mode": "window"}

    async def _send_kick(path, shell):
        _mark(f"kick_{shell}")

    def _lie_down(c, **kw):
        _mark("put_down")
        seen.append(("put_down_kwargs", kw))
        wake_state.update(c, awake=False)
        return {}

    from synapse_core import scheduler

    from cortex import lie_down as lie_down_mod
    from cortex import wake as wake_mod
    monkeypatch.setattr(wake_mod, "run_wake", _run_wake)
    monkeypatch.setattr(wake_mod, "_window_alive", lambda c: False)
    monkeypatch.setattr(scheduler, "send_kick", _send_kick)
    monkeypatch.setattr(lie_down_mod, "lie_down", _lie_down)
    return seen


def _acts(trace):
    return [what for what, _ in trace if not what.endswith("_kwargs")]


def _holds(trace):
    return [hold for what, hold in trace if not what.endswith("_kwargs")]


def _kwargs(trace, what):
    return next(v for k, v in trace if k == f"{what}_kwargs")


def _ledger(cfg):
    return shell_ledger.read(config.shell_state_dir(cfg), "tg")


def _transcript(cfg, tokens: int, age_hours: float = 0.0):
    from cortex import transcript as tx
    p = tx.transcript_dir(cfg)
    p.mkdir(parents=True, exist_ok=True)
    f = p / "session.jsonl"
    f.write_text(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": tokens, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 0}}}))
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(f, (old, old))
    return f


def _tg_ledger(cfg, **fields):
    shell_ledger.write(config.shell_state_dir(cfg), "tg", fields)


def _iso_ago(cfg, hours: float) -> str:
    return (datetime.now(config.get_tz(cfg)) - timedelta(hours=hours)).isoformat()


# --- ordering -----------------------------------------------------------------

def test_hold_lands_before_any_kick(cfg, cdir, trace):
    duty.write(cdir, "tg")
    _transcript(cfg, 10)
    duty.apply(cfg, "cli")
    assert _acts(trace) == ["wake_cli"]
    assert _holds(trace) == ["tg"]


def test_cli_is_put_down_before_the_tg_kick(cfg, cdir, trace):
    duty.write(cdir, "cli")
    wake_state.set_awake(cfg, 1, None)
    duty.apply(cfg, "tg")
    assert _acts(trace) == ["put_down", "kick_tg"]
    assert _holds(trace) == ["cli", "cli"]
    assert _kwargs(trace, "put_down") == {"force_slept": "duty", "book_alarm": False}


def test_put_down_skipped_when_the_cli_window_is_not_awake(cfg, cdir, trace):
    res = duty.apply(cfg, "tg")
    assert res["put_down"] is False
    assert "put_down" not in _acts(trace)


def test_put_down_skipped_when_the_hold_already_covered_cli(cfg, cdir, trace):
    duty.write(cdir, "tg")
    wake_state.set_awake(cfg, 1, None)
    res = duty.apply(cfg, "off")
    assert res["put_down"] is False
    assert _acts(trace) == []


# --- cli fresh-vs-resume gate -------------------------------------------------

def test_cli_resumes_under_both_thresholds(cfg, cdir, trace):
    duty.write(cdir, "tg")
    _transcript(cfg, 79_000, age_hours=7)
    res = duty.apply(cfg, "cli")
    assert res["fresh"] == [] and res["woken"] == ["cli"]
    assert wake_state.load(cfg).get("rotated") is None
    assert _acts(trace) == ["wake_cli"]


def test_cli_spawns_fresh_over_the_token_threshold(cfg, cdir, trace):
    duty.write(cdir, "tg")
    _transcript(cfg, 80_001)
    wake_state.update(cfg, transcript="/t/old.jsonl")
    res = duty.apply(cfg, "cli")
    assert res["fresh"] == ["cli"]
    st = wake_state.load(cfg)
    assert st["rotated"] is True and st["retired_sid"] == "old"


def test_cli_spawns_fresh_over_the_age_threshold(cfg, cdir, trace):
    duty.write(cdir, "tg")
    _transcript(cfg, 100, age_hours=9)
    res = duty.apply(cfg, "cli")
    assert res["fresh"] == ["cli"]
    assert wake_state.load(cfg)["rotated"] is True


def test_no_transcript_is_not_treated_as_stale(cfg, cdir, trace):
    duty.write(cdir, "tg")
    res = duty.apply(cfg, "cli")
    assert res["fresh"] == []
    assert wake_state.load(cfg).get("rotated") is None


# --- tg wake ------------------------------------------------------------------

def test_tg_wake_books_a_due_round_without_rotating(cfg, cdir, trace):
    duty.write(cdir, "cli")
    _tg_ledger(cfg, occupancy=79_000, last_user_ts=_iso_ago(cfg, 1))
    res = duty.apply(cfg, "tg")
    assert res["woken"] == ["tg"] and res["fresh"] == []
    d = _ledger(cfg)
    assert d["next_wake_at"] and "rotate_pending" not in d
    assert _acts(trace) == ["kick_tg"]


def test_tg_wake_rotates_over_the_occupancy_threshold(cfg, cdir, trace):
    duty.write(cdir, "cli")
    _tg_ledger(cfg, occupancy=80_001, last_user_ts=_iso_ago(cfg, 1))
    res = duty.apply(cfg, "tg")
    assert res["fresh"] == ["tg"]
    assert _ledger(cfg)["rotate_pending"] is True


def test_tg_wake_rotates_over_the_age_threshold(cfg, cdir, trace):
    duty.write(cdir, "cli")
    _tg_ledger(cfg, occupancy=10, last_user_ts=_iso_ago(cfg, 9),
               last_note_ts=_iso_ago(cfg, 10))
    assert duty.apply(cfg, "tg")["fresh"] == ["tg"]
    assert _ledger(cfg)["rotate_pending"] is True


def test_tg_age_takes_the_later_of_the_two_stamps(cfg, cdir, trace):
    duty.write(cdir, "cli")
    _tg_ledger(cfg, last_user_ts=_iso_ago(cfg, 20), last_note_ts=_iso_ago(cfg, 1))
    assert duty.apply(cfg, "tg")["fresh"] == []
    assert "rotate_pending" not in _ledger(cfg)


def test_empty_tg_ledger_wakes_without_rotating(cfg, cdir, trace):
    duty.write(cdir, "cli")
    assert duty.apply(cfg, "tg")["fresh"] == []
    assert "rotate_pending" not in _ledger(cfg)


def test_dead_tg_socket_never_raises_and_the_booking_stands(cfg, cdir):
    duty.write(cdir, "cli")
    res = duty.apply(cfg, "tg")
    assert res["woken"] == ["tg"]
    assert _ledger(cfg)["next_wake_at"]


# --- modes --------------------------------------------------------------------

def test_mode_all_kicks_both_once(cfg, cdir, trace):
    duty.write(cdir, "off")
    res = duty.apply(cfg, "all")
    assert res["hold"] is None
    assert _acts(trace) == ["kick_tg", "wake_cli"]
    assert res["woken"] == ["tg", "cli"]


def test_mode_all_kicks_both_even_with_nothing_held(cfg, cdir, trace):
    duty.write(cdir, "all")
    duty.apply(cfg, "all")
    assert _acts(trace) == ["kick_tg", "wake_cli"]


def test_mode_off_holds_everything_and_kicks_nobody(cfg, cdir, trace):
    res = duty.apply(cfg, "off")
    assert res["hold"] == "all" and res["woken"] == []
    assert _acts(trace) == []
    assert shell_ledger.state_path(config.shell_state_dir(cfg), "tg").exists() is False


def test_repeating_a_mode_kicks_the_named_shell_again(cfg, cdir, trace):
    duty.write(cdir, "cli")
    duty.apply(cfg, "cli")
    assert _acts(trace) == ["wake_cli"]


def test_naming_tg_from_a_clean_state_wakes_tg(cfg, cdir, trace):
    res = duty.apply(cfg, "tg")
    assert res["woken"] == ["tg"]
    assert _acts(trace) == ["kick_tg"]
    assert _ledger(cfg)["next_wake_at"]


def test_naming_cli_from_a_clean_state_wakes_cli(cfg, cdir, trace):
    res = duty.apply(cfg, "cli")
    assert res["woken"] == ["cli"]
    assert _acts(trace) == ["wake_cli"]


def test_a_repeat_cli_kick_no_ops_on_a_live_awake_window(cfg, cdir, monkeypatch):
    """The already-awake guard in the cli pipeline is what makes repeat kicks
    safe — duty must go through it, not around."""
    from cortex import wake as wake_mod
    monkeypatch.setattr(wake_mod, "_window_alive", lambda c: True)
    monkeypatch.setattr(wake_mod, "run_wake",
                        lambda *a, **k: pytest.fail("re-drove a live window"))
    wake_state.set_awake(cfg, 1, None)
    assert duty.apply(cfg, "cli")["woken"] == ["cli"]
    assert wake_state.load(cfg)["awake"] is True


def test_garbage_mode_does_not_raise(cfg, cdir, trace):
    duty.write(cdir, "off")
    res = duty.apply(cfg, "sideways")
    assert res["hold"] is None
    assert duty.read(cdir)["hold"] is None


def test_apply_never_writes_the_breaker(cfg, cdir, trace):
    from cortex import breaker
    duty.apply(cfg, "off")
    assert breaker.breaker_path(cdir).exists() is False


# --- serialisation ------------------------------------------------------------

def test_after_hold_runs_between_the_write_and_the_actions(cfg, cdir, trace):
    seen = []
    duty.apply(cfg, "tg", after_hold=lambda: seen.append(duty.read(cdir)["hold"]))
    assert seen == ["cli"]
    assert _acts(trace) == ["kick_tg"]


def test_two_opposing_transitions_never_interleave(cfg, cdir, monkeypatch):
    """Concurrent apply calls are serialised end to end: every action runs under
    the hold of the transition that issued it, and the file matches the winner.
    Without the lock a kick lands under the other transition's hold — both
    shells active."""
    import threading
    seen = []
    lock = threading.Lock()

    def _mark(what):
        with lock:
            seen.append((what, duty.read(cdir)["hold"]))
        time.sleep(0.05)

    def _run_wake(conn, c, decision, now=None):
        _mark("wake_cli")
        return {"mode": "window"}

    async def _send_kick(path, shell):
        _mark(f"kick_{shell}")

    from synapse_core import scheduler

    from cortex import wake as wake_mod
    monkeypatch.setattr(wake_mod, "run_wake", _run_wake)
    monkeypatch.setattr(wake_mod, "_window_alive", lambda c: False)
    monkeypatch.setattr(scheduler, "send_kick", _send_kick)

    start = threading.Barrier(2)

    def _apply(mode):
        start.wait()
        duty.apply(cfg, mode)

    threads = [threading.Thread(target=_apply, args=(m,)) for m in ("cli", "tg")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = {"wake_cli": "tg", "kick_tg": "cli"}
    assert sorted(what for what, _ in seen) == ["kick_tg", "wake_cli"]
    assert all(expected[what] == hold for what, hold in seen)
    final = duty.read(cdir)
    assert final["mode"] in ("cli", "tg")
    assert final["hold"] == duty.hold_for(final["mode"])
