"""Silence source = last REAL user message (transcript.user_silent_min).

The single silence timer must count from the user's last message only. Assistant
turns, system writes and the ear-delivered injections (wake bell / free-round /
night lines) must NOT reset it — the "永远睡不到alarm一直窜出来" bug family.
"""
from __future__ import annotations

import json
import time

import pytest

from cortex import config, transcript


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


def _write(cfg, entries):
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text("\n".join(json.dumps(e) for e in entries))


def _iso(ago_min):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=ago_min)).isoformat()


def _user(text, ago_min):
    return {"type": "user", "timestamp": _iso(ago_min),
            "message": {"role": "user", "content": text}}


def _assistant(ago_min):
    return {"type": "assistant", "timestamp": _iso(ago_min),
            "message": {"role": "assistant", "content": "reply"}}


def test_last_user_message_drives_silence(cfg):
    _write(cfg, [_user("hey", 30), _user("still here", 10)])
    assert 9.5 < transcript.user_silent_min(cfg) < 10.5


def test_no_user_message_returns_none(cfg):
    _write(cfg, [_assistant(5)])
    assert transcript.last_user_message_mtime(cfg) is None
    assert transcript.user_silent_min(cfg) is None


# --- the three non-user write types must NOT reset the timer ------------------

def test_assistant_turn_does_not_reset(cfg):
    # User spoke 20 min ago; the assistant answered 1 min ago -> silence stays ~20.
    _write(cfg, [_user("q", 20), _assistant(1)])
    assert 19.0 < transcript.user_silent_min(cfg) < 21.0


def test_free_round_injection_does_not_reset(cfg):
    # The free-round line lands down the ear channel as a USER-role turn; it must
    # be ignored so it never resets its own timer.
    _write(cfg, [
        _user("q", 20),
        _user("⏳ [NEW ROUND] 20 min since ... Choose again ...", 1),
    ])
    assert 19.0 < transcript.user_silent_min(cfg) < 21.0


def test_wake_bell_line_does_not_reset(cfg):
    # Shipped default RESIDENT bell prefix ('⏰ {hm}' -> '⏰').
    _write(cfg, [
        _user("q", 25),
        _user("⏰ 14:03", 2),
    ])
    assert 24.0 < transcript.user_silent_min(cfg) < 26.0


def test_spawn_opener_line_does_not_reset(cfg):
    # The fresh-spawn opener is a SECOND machine shape ('☀️ {hm}' -> '☀️'); both
    # prefixes feed _line_markers, so neither counts as user speech.
    _write(cfg, [
        _user("q", 25),
        _user("☀️ 14:03", 2),
    ])
    assert 24.0 < transcript.user_silent_min(cfg) < 26.0


def test_wake_bell_zwj_static_template_does_not_reset(cfg):
    # Static (no {hm}) multi-codepoint ZWJ bell template must also self-match
    # as a machine line (regression: emoji-leading marker previously never
    # matched itself once the leading-decoration strip consumed it).
    cfg["wake"]["wake_bell_template"] = "[🧚‍♀️ 笨鸭换岗成功]"
    _write(cfg, [
        _user("q", 25),
        _user("[🧚‍♀️ 笨鸭换岗成功]", 2),
    ])
    assert 24.0 < transcript.user_silent_min(cfg) < 26.0


def test_free_round_marker_line_does_not_reset(cfg):
    _write(cfg, [_user("q", 18), _user("⏳ [NEW ROUND] free-round line", 1)])
    assert 17.0 < transcript.user_silent_min(cfg) < 19.0


# --- real user speech quoting a marker MUST still reset the timer -------------

def test_marker_quoted_mid_sentence_resets(cfg):
    """Line-start match only: a real user message merely quoting a marker
    mid-sentence is user activity and resets the silence timer. Previously the
    substring check (`mk in text`) dropped it -> silence wrongly stayed high."""
    _write(cfg, [
        _user("q", 20),
        _user("did the [NEW ROUND] path fire, or is [CORTEX-WAKE] stuck?", 1),
    ])
    assert 0.5 < transcript.user_silent_min(cfg) < 1.5  # reset by the real msg


def test_cjk_lead_before_marker_resets(cfg):
    """CJK/kana/hangul are outside the decoration class, so a Chinese message
    opening with a real word then a marker line-starts on the CJK char (not the
    bracket) -> real user activity -> resets the timer."""
    _write(cfg, [
        _user("q", 22),
        _user("看 [FUSE] path fired?", 1),
    ])
    assert 0.5 < transcript.user_silent_min(cfg) < 1.5


def test_content_block_form_user_message(cfg):
    # Content-block (list) form is concatenated to text and honoured.
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(json.dumps({
        "type": "user", "timestamp": _iso(8),
        "message": {"role": "user",
                    "content": [{"type": "text", "text": "hi there"}]}}))
    assert 7.5 < transcript.user_silent_min(cfg) < 8.5


def test_tail_read_ignores_head_on_large_file(cfg):
    # A user message far in the past (head) + a recent one (tail): tail-read still
    # finds the recent one; performance stays flat regardless of file size.
    filler = [_assistant(5) for _ in range(4000)]
    _write(cfg, [_user("old", 500)] + filler + [_user("recent", 3)])
    t = time.perf_counter()
    s = transcript.user_silent_min(cfg)
    assert (time.perf_counter() - t) < 0.1  # sub-100ms even on a big file
    assert 2.5 < s < 3.5


# --- FIX 4: role=user tool_result envelopes must NOT reset the clock ----------

def _tool_result(ago_min):
    """A role=user envelope carrying only a tool_result block — Claude Code wraps
    every MCP/tool return this way. No text block -> not real user speech."""
    return {"type": "user", "timestamp": _iso(ago_min),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1",
                 "content": [{"type": "text", "text": "tool output"}]}]}}


def test_tool_result_envelope_does_not_reset(cfg):
    """FIX 4: cortex's own MCP tool call produces a role=user tool-result
    envelope; counting it as user presence caused tonight's '16 min since user'
    read while the user hadn't spoken. It must be ignored -> silence stays high."""
    _write(cfg, [_user("q", 16), _tool_result(1), _assistant(1)])
    assert 15.0 < transcript.user_silent_min(cfg) < 17.0


def test_tool_result_only_transcript_returns_none(cfg):
    """A transcript with only tool-result envelopes (no real user turn) -> None,
    same as an assistant-only transcript (never a spurious 0)."""
    _write(cfg, [_tool_result(2), _assistant(1), _tool_result(1)])
    assert transcript.last_user_message_mtime(cfg) is None
    assert transcript.user_silent_min(cfg) is None


# --- FIX 5: a giant final row must not bury the last user turn ----------------

def test_huge_final_row_does_not_hide_user_turn(cfg):
    """FIX 5: one multi-hundred-KB tool_result as the LAST row can fill the whole
    fixed 64KiB tail; the old readline()-drop discarded the real user turn ahead
    of it -> None -> treated as 0 (safety net misfires). The growing-chunk scan
    doubles the window until the user turn is found."""
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    giant = "x" * 300_000  # bigger than the initial 64KiB tail window
    rows = [
        _user("real user turn", 12),
        _assistant(11),
        {"type": "user", "timestamp": _iso(1),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t", "content": giant}]}},
    ]
    (d / "s.jsonl").write_text("\n".join(json.dumps(e) for e in rows))
    s = transcript.user_silent_min(cfg)
    assert s is not None and 11.0 < s < 13.0  # found behind the giant row


# --- FIX 3: silence source reads the RESIDENT window, not a newer digest ------

def test_resident_transcript_prefers_wake_state_over_newer_digest(cfg, tmp_path):
    """FIX 3: a headless `claude -p` digest can be the mtime-newest jsonl in the
    same projects dir. Silence checks must read the RESIDENT window
    (wake_state.transcript), not the digest — else they miss the real window."""
    import os
    from cortex import wake_state
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    resident = d / "resident.jsonl"
    resident.write_text(json.dumps(_user("real user turn", 20)))
    digest = d / "digest.jsonl"
    digest.write_text(json.dumps(_user("archived blob", 1)))
    # Make the digest the mtime-newest file.
    now = time.time()
    os.utime(resident, (now - 100, now - 100))
    os.utime(digest, (now, now))
    assert transcript.newest(cfg) == digest  # digest is mtime-newest
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    wake_state.update(cfg, transcript=str(resident))
    assert transcript.resident_transcript(cfg) == resident
    # Silence source reflects the resident's 20-min-old user turn, not the digest.
    assert 19.0 < transcript.user_silent_min(cfg) < 21.0
