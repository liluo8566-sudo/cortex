"""Tests for cortex.kick ack kind and note.render kick context rendering."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cortex import config, note

MEL = ZoneInfo("Australia/Melbourne")
NOW = datetime(2026, 7, 26, 14, 30, tzinfo=MEL)


@pytest.fixture
def cfg(tmp_path):
    return config.load(path=tmp_path / "absent.toml")


# --------------------------------------------------------------------------- #
# kick.main — argument parsing and DND gate
# --------------------------------------------------------------------------- #

def test_kick_ack_argparse_accepted(tmp_path, monkeypatch):
    """--kind ack is a valid choice; argparse must not exit 2."""
    from cortex import config as _cfg, wake_state, wake

    c = _cfg.load(path=tmp_path / "absent.toml")
    c["paths"]["cortex_home"] = str(tmp_path / "ch")
    c["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")

    from cortex import db
    db.connect_path(tmp_path / "marrow.db").close()

    monkeypatch.setattr(_cfg, "load", lambda path=None: c)
    monkeypatch.setattr(wake, "_window_alive", lambda cfg: False)
    monkeypatch.setattr(wake_state, "is_awake", lambda cfg: False)

    # Stub run_wake to avoid real wakeup side-effects.
    import cortex.kick as kick_mod
    monkeypatch.setattr(kick_mod, "write_signal", lambda *a, **kw: None)
    monkeypatch.setattr("cortex.wake.run_wake",
                        lambda conn, cfg, decision, now: {"mode": "headless"})
    from cortex.pacemaker import integration
    monkeypatch.setattr(integration, "lie_down", lambda conn, cfg: None)
    monkeypatch.setattr(wake_state, "set_next_wake_at", lambda cfg, v: None)

    from cortex.kick import main
    rc = main(["--kind", "ack", "--text", "巡山手帐第三页"])
    assert rc == 0


def test_kick_ack_not_skipped_when_paused(tmp_path, monkeypatch):
    """ack bypasses the DND/paused check — active knock must get through."""
    from cortex import config as _cfg, wake_state, wake

    c = _cfg.load(path=tmp_path / "absent.toml")
    c["paths"]["cortex_home"] = str(tmp_path / "ch")
    c["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")

    from cortex import db
    db.connect_path(tmp_path / "marrow.db").close()

    # Mark DND paused.
    ws_path = tmp_path / "ws.json"
    import json
    ws_path.write_text(json.dumps({"paused": True}))

    monkeypatch.setattr(_cfg, "load", lambda path=None: c)
    monkeypatch.setattr(wake, "_window_alive", lambda cfg: False)
    monkeypatch.setattr(wake_state, "is_awake", lambda cfg: False)

    import cortex.kick as kick_mod
    ran = {}
    monkeypatch.setattr(kick_mod, "write_signal", lambda *a, **kw: ran.setdefault("write", True))
    def _fake_run_wake(conn, cfg, decision, now):
        ran["wake"] = True
        return {"mode": "headless"}

    monkeypatch.setattr("cortex.wake.run_wake", _fake_run_wake)
    from cortex.pacemaker import integration
    monkeypatch.setattr(integration, "lie_down", lambda conn, cfg: None)
    monkeypatch.setattr(wake_state, "set_next_wake_at", lambda cfg, v: None)

    from cortex.kick import main
    rc = main(["--kind", "ack", "--text", "日记页"])
    assert rc == 0
    # Must have proceeded to write_signal / run_wake, not returned early.
    assert ran.get("write") is True


def test_kick_other_kind_skipped_when_paused(tmp_path, monkeypatch):
    """Non-ack/reply kinds ARE skipped under DND — control to confirm the gate."""
    from cortex import config as _cfg, wake_state

    c = _cfg.load(path=tmp_path / "absent.toml")
    c["paths"]["cortex_home"] = str(tmp_path / "ch")
    c["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")

    ws_path = tmp_path / "ws.json"
    import json
    ws_path.write_text(json.dumps({"paused": True}))

    monkeypatch.setattr(_cfg, "load", lambda path=None: c)

    import cortex.kick as kick_mod
    ran = {}
    monkeypatch.setattr(kick_mod, "write_signal", lambda *a, **kw: ran.setdefault("write", True))

    from cortex.kick import main
    rc = main(["--kind", "morning"])
    assert rc == 0
    assert "write" not in ran  # skipped at DND gate


# --------------------------------------------------------------------------- #
# kick.main — awake+alive path calls inject_prompt for ack
# --------------------------------------------------------------------------- #

def test_kick_ack_awake_alive_injects_prompt(tmp_path, monkeypatch):
    """Awake+alive: ack injects a prompt containing the text, returns 0."""
    from cortex import config as _cfg, wake_state, wake, window

    c = _cfg.load(path=tmp_path / "absent.toml")
    c["paths"]["cortex_home"] = str(tmp_path / "ch")
    c["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")

    monkeypatch.setattr(_cfg, "load", lambda path=None: c)
    monkeypatch.setattr(wake, "_window_alive", lambda cfg: True)
    monkeypatch.setattr(wake_state, "is_awake", lambda cfg: True)

    injected = {}
    monkeypatch.setattr(window, "inject_prompt",
                        lambda cfg, text: injected.setdefault("text", text) or True)

    from cortex.kick import main
    rc = main(["--kind", "ack", "--text", "手帐第五页｜好看"])
    assert rc == 0
    assert "injected" in injected or "text" in injected
    prompt = injected.get("text", "")
    assert "已阅" in prompt
    assert "手帐第五页｜好看" in prompt


def test_kick_ack_awake_alive_inject_failure_returns_0(tmp_path, monkeypatch):
    """inject_prompt returning False logs a warning but still returns 0."""
    from cortex import config as _cfg, wake_state, wake, window

    c = _cfg.load(path=tmp_path / "absent.toml")
    c["paths"]["cortex_home"] = str(tmp_path / "ch")
    c["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")

    monkeypatch.setattr(_cfg, "load", lambda path=None: c)
    monkeypatch.setattr(wake, "_window_alive", lambda cfg: True)
    monkeypatch.setattr(wake_state, "is_awake", lambda cfg: True)
    monkeypatch.setattr(window, "inject_prompt", lambda cfg, text: False)

    from cortex.kick import main
    assert main(["--kind", "ack", "--text", "x"]) == 0


# --------------------------------------------------------------------------- #
# note.render — kick context rendering
# --------------------------------------------------------------------------- #

def test_render_kick_ack_emits_stamp_line(cfg):
    """kick={kind:ack, text:...} renders the 已阅章 line in the note."""
    data = {"kick": {"kind": "ack", "text": "手帐第三页｜看完了", "note_id": None, "ts": ""}}
    text = note.render(cfg, NOW, data)
    assert "💌 已阅章：手帐第三页｜看完了" in text


def test_render_kick_ack_no_text_renders_bare_stamp(cfg):
    """ack kick with empty text still renders the stamp line."""
    data = {"kick": {"kind": "ack", "text": "", "note_id": None, "ts": ""}}
    text = note.render(cfg, NOW, data)
    assert "💌 已阅章" in text


def test_render_kick_none_emits_nothing(cfg):
    """kick=None leaves no kick line in the note — no blank section."""
    data = {"kick": None}
    text = note.render(cfg, NOW, data)
    assert "💌" not in text
    assert "Kick:" not in text
    assert "已阅" not in text


def test_render_kick_absent_key_emits_nothing(cfg):
    """Missing kick key behaves the same as None."""
    text = note.render(cfg, NOW, {})
    assert "💌" not in text
    assert "Kick:" not in text
