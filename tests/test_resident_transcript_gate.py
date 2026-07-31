"""Foreign jsonls in the shared projects dir must not be mistaken for this
window's transcript.

The headless shells (marrow's sessionend digest, the tg cortex) run `claude -p`
against the same cwd, so their session files land in the SAME projects dir and
are routinely mtime-newest. Two live consequences this module locks down:

  * the duty fresh-vs-resume gate measured the foreign file's tokens/mtime and
    judged a 89k cli window as a resumable 72k one;
  * wake classification compared the foreign file against the recorded pointer,
    saw a mismatch and respawned a perfectly healthy window as "fresh".
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import pytest

from cortex import config, duty, transcript, wake, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["transcript_dir"] = str(tmp_path / "projects")
    return c


def _usage_row(tokens: int) -> str:
    return json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": tokens, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 0}}})


def _opener_row(cfg: dict) -> str:
    marker = transcript.lineage_marker(cfg)
    return json.dumps({"message": {"role": "user", "content": f"{marker} 09:30"}})


def _window_jsonl(cfg, name: str, tokens: int, *, age_hours: float = 0.0):
    """A genuine window session: the wake marker leads its first user message."""
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(_opener_row(cfg) + "\n" + _usage_row(tokens) + "\n",
                 encoding="utf-8")
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(p, (old, old))
    return p


def _foreign_jsonl(cfg, name: str, tokens: int):
    """A headless `claude -p` archive: no marker near the start of its first
    message, and always the mtime-newest file in the dir."""
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    blob = "archived digest " * 200 + transcript.lineage_marker(cfg)
    p.write_text(json.dumps({"message": {"role": "user", "content": blob}})
                 + "\n" + _usage_row(tokens) + "\n", encoding="utf-8")
    now = time.time()
    os.utime(p, (now, now))
    return p


# --- Fix A: the gate measures the resident window ------------------------------

def test_window_tokens_ignores_a_fresher_foreign_jsonl(cfg):
    win = _window_jsonl(cfg, "window.jsonl", 89_000)
    _foreign_jsonl(cfg, "digest.jsonl", 72_000)
    wake_state.update(cfg, transcript=str(win))
    assert transcript.window_tokens(cfg) == 89_000


def test_mtime_ignores_a_fresher_foreign_jsonl(cfg):
    win = _window_jsonl(cfg, "window.jsonl", 100, age_hours=9)
    _foreign_jsonl(cfg, "digest.jsonl", 100)
    wake_state.update(cfg, transcript=str(win))
    assert transcript.mtime(cfg) == pytest.approx(win.stat().st_mtime)


def test_duty_gate_sees_the_full_cli_window_behind_a_foreign_jsonl(cfg):
    """Tonight's regression: 89k window judged resumable because the tg shell's
    own in-flight transcript measured 72k."""
    win = _window_jsonl(cfg, "window.jsonl", 89_000)
    _foreign_jsonl(cfg, "digest.jsonl", 72_000)
    wake_state.update(cfg, transcript=str(win))
    assert duty._cli_needs_fresh(cfg, datetime.now(config.get_tz(cfg))) is True


def test_duty_gate_still_resumes_a_small_recent_window(cfg):
    win = _window_jsonl(cfg, "window.jsonl", 10_000)
    _foreign_jsonl(cfg, "digest.jsonl", 90_000)
    wake_state.update(cfg, transcript=str(win))
    assert duty._cli_needs_fresh(cfg, datetime.now(config.get_tz(cfg))) is False


def test_gate_falls_back_to_lineage_when_no_pointer_is_recorded(cfg):
    """No recorded pointer: the marker-carrying file still wins over the
    mtime-newest headless archive."""
    _window_jsonl(cfg, "window.jsonl", 89_000)
    _foreign_jsonl(cfg, "digest.jsonl", 100)
    assert duty._cli_needs_fresh(cfg, datetime.now(config.get_tz(cfg))) is True


# --- Fix B: no spurious fresh respawn ------------------------------------------

@pytest.fixture
def alive_window(cfg, monkeypatch):
    """A healthy resident: iTerm up, session alive, claude running, no rotate."""
    from cortex import window
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 4242)
    wake_state.update(cfg, session_id="w0")
    return cfg


def test_no_fresh_respawn_when_a_foreign_jsonl_is_newest(cfg, alive_window):
    win = _window_jsonl(cfg, "window.jsonl", 100)
    wake_state.update(cfg, transcript=str(win))
    _foreign_jsonl(cfg, "digest.jsonl", 100)
    assert wake._classify_wake(cfg) == ("ear", False)


def test_fresh_when_the_recorded_pointer_is_a_stale_window(cfg, alive_window):
    """A genuine window-to-window roll is still classified fresh."""
    _window_jsonl(cfg, "old.jsonl", 100, age_hours=2)
    new = _window_jsonl(cfg, "new.jsonl", 100)
    wake_state.update(cfg, transcript=str(
        transcript.transcript_dir(cfg) / "old.jsonl"))
    assert wake._classify_wake(cfg) == ("fresh", False)
    assert new.exists()


def test_rotate_flag_still_outranks_the_transcript_comparison(cfg, alive_window):
    win = _window_jsonl(cfg, "window.jsonl", 100)
    wake_state.update(cfg, transcript=str(win))
    wake_state.set_rotated(cfg)
    assert wake._classify_wake(cfg) == ("fresh", True)


def test_window_transcript_prefers_lineage_over_the_newest_foreign_file(cfg):
    win = _window_jsonl(cfg, "window.jsonl", 100, age_hours=3)
    _foreign_jsonl(cfg, "digest.jsonl", 100)
    assert transcript.window_transcript(cfg) == win


def test_window_transcript_falls_back_to_newest_without_any_lineage(cfg):
    p = _foreign_jsonl(cfg, "digest.jsonl", 100)
    assert transcript.window_transcript(cfg) == p
