"""cortex.kick ack kind: argparse acceptance + reason-queue flow."""
from __future__ import annotations

import json

import pytest

from cortex import kick, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "cortex"
    (home / "state").mkdir(parents=True)
    return {
        "core": {"timezone": "Australia/Melbourne"},
        "paths": {
            "marrow_db": str(tmp_path / "marrow.db"),
            "cortex_home": str(home),
            "wake_state_file": str(home / "state" / "wake_state.json"),
            "wakeup_note_file": str(home / "wakeup_note.md"),
            "watchdog_pidfile": str(home / "state" / "watchdog.pid"),
            "wake_audit_log": str(home / "state" / "wake_audit.log"),
        },
        "kick": {
            "reason_ack": "已阅：{text}",
            "reason_reply": 'Msg #{id} replied: "{text}"',
            "max_reasons": 8,
        },
    }


@pytest.fixture
def _stub_spawn(monkeypatch):
    calls = []
    monkeypatch.setattr(kick, "_kick_daemon",
                        lambda cfg, kind: calls.append(kind) or True)
    return calls


def _ws(cfg) -> dict:
    return json.loads(wake_state.wake_state_path(cfg).read_text())


def test_kick_ack_argparse_accepted(cfg, _stub_spawn):
    """--kind ack is a valid choice; the kick function accepts it without error."""
    result = kick.kick(cfg, "ack", text="手帐第三页")
    assert result["ok"] is True


def test_kick_ack_asleep_queues_reason(cfg, _stub_spawn):
    """ack asleep: reason queued in kick_reasons, gen bumped, daemon kicked."""
    wake_state.update(cfg, awake=False, gen=1, state_id="aabb")
    result = kick.kick(cfg, "ack", text="巡山第五页")
    assert result["ok"] is True
    assert not result["awake"]
    assert result["ticked"] is True
    d = _ws(cfg)
    assert d["kick_reasons"] == ["已阅：巡山第五页"]
    assert d["gen"] == 2


def test_kick_ack_awake_queues_reason_opens_round(cfg, _stub_spawn):
    """ack awake: reason queued, kick_round marked, daemon kicked."""
    wake_state.update(cfg, awake=True, gen=3, state_id="ccdd")
    result = kick.kick(cfg, "ack", text="日记页")
    assert result["ok"] is True
    assert result["awake"] is True
    assert result["round_opened"] is True
    d = _ws(cfg)
    assert d["kick_reasons"] == ["已阅：日记页"]
    assert d.get("kick_round") is True
    assert d["gen"] == 3  # no epoch bump when awake


def test_kick_ack_no_template_queues_empty_reason(cfg, _stub_spawn):
    """ack with no reason_ack template queues nothing (empty reason skipped)."""
    del cfg["kick"]["reason_ack"]
    wake_state.update(cfg, awake=False, gen=0, state_id="eeff")
    result = kick.kick(cfg, "ack", text="some text")
    assert result["ok"] is True
    d = _ws(cfg)
    assert d.get("kick_reasons") in (None, [])


def test_kick_ack_main_exits_zero(cfg, monkeypatch):
    """main(['--kind', 'ack', '--text', '...']) exits 0."""
    import cortex.kick as kick_mod

    monkeypatch.setattr(kick_mod, "_kick_daemon", lambda cfg, kind: True)
    monkeypatch.setattr("cortex.config.load", lambda path=None: cfg)
    wake_state.update(cfg, awake=False, gen=0, state_id="ffgg")
    rc = kick_mod.main(["--kind", "ack", "--text", "手帐第三页"])
    assert rc == 0
