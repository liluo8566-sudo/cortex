"""duty.transfer: the on-duty shell hands over to the other one.

Same boundary stubs as test_duty_apply — run_wake, the proxy lie_down and the
shell-host socket kick are all replaced."""
from __future__ import annotations

import json

import pytest

from cortex import breaker, config, duty, wake_state


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
    seen = []

    def _run_wake(conn, c, decision, now=None):
        seen.append("wake_cli")
        return {"mode": "window"}

    async def _send_kick(path, shell):
        seen.append(f"kick_{shell}")

    def _lie_down(c, **kw):
        seen.append("put_down")
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


# --- target ------------------------------------------------------------------

def test_other_shell_is_the_one_the_caller_is_not():
    assert duty.other_shell("cli") == "tg"
    assert duty.other_shell("tg") == "cli"
    assert duty.other_shell("TG ") == "cli"


def test_other_shell_unknown_id():
    assert duty.other_shell("wx") is None
    assert duty.other_shell("") is None


# --- handover ----------------------------------------------------------------

def test_cli_caller_is_held_and_tg_woken(cfg, cdir, trace):
    duty.write(cdir, "cli")
    wake_state.set_awake(cfg, 1, None)
    res = duty.transfer(cfg, "cli")
    assert res["ok"] is True and res["target"] == "tg"
    assert res["mode"] == "tg" and res["hold"] == "cli"
    assert res["woken"] == ["tg"] and res["put_down"] is True
    assert trace == ["put_down", "kick_tg"]
    assert duty.read(cdir)["hold"] == "cli"


def test_tg_caller_is_held_and_cli_woken(cfg, cdir, trace):
    duty.write(cdir, "tg")
    res = duty.transfer(cfg, "tg")
    assert res["ok"] is True and res["target"] == "cli"
    assert res["mode"] == "cli" and res["hold"] == "tg"
    assert res["woken"] == ["cli"]
    assert trace == ["wake_cli"]
    assert duty.read(cdir)["hold"] == "tg"


def test_transfer_carries_the_fresh_gate(cfg, cdir, trace):
    duty.write(cdir, "tg")
    from cortex import transcript as tx
    p = tx.transcript_dir(cfg)
    p.mkdir(parents=True, exist_ok=True)
    (p / "session.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 80_001, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 0}}}))
    res = duty.transfer(cfg, "tg")
    assert res["fresh"] == ["cli"]
    assert wake_state.load(cfg)["rotated"] is True


# --- caller rotation ----------------------------------------------------------

def test_rotate_retires_the_tg_caller_in_its_ledger(cfg, cdir, trace):
    from cortex import shell_ledger
    duty.write(cdir, "tg")
    res = duty.transfer(cfg, "tg", rotate=True)
    assert res["ok"] is True and res["rotated"] is True
    assert shell_ledger.read(config.shell_state_dir(cfg), "tg") == {
        "rotate_pending": True}
    assert trace == ["wake_cli"]


def test_rotate_retires_the_cli_caller_before_its_put_down(cfg, cdir, trace):
    duty.write(cdir, "cli")
    wake_state.set_awake(cfg, 1, "/t/abc-123.jsonl")
    res = duty.transfer(cfg, "cli", rotate=True)
    assert res["ok"] is True and res["put_down"] is True
    d = wake_state.load(cfg)
    assert d["rotated"] is True and d["retired_sid"] == "abc-123"


def test_rotate_never_touches_the_woken_shell(cfg, cdir, trace):
    from cortex import shell_ledger
    duty.write(cdir, "cli")
    wake_state.set_awake(cfg, 1, None)
    duty.transfer(cfg, "cli", rotate=True)
    assert "rotate_pending" not in shell_ledger.read(
        config.shell_state_dir(cfg), "tg")


def test_default_transfer_retires_nobody(cfg, cdir, trace):
    from cortex import shell_ledger
    duty.write(cdir, "tg")
    wake_state.set_awake(cfg, 1, "/t/abc-123.jsonl")
    res = duty.transfer(cfg, "tg")
    assert res["rotated"] is False
    assert shell_ledger.read(config.shell_state_dir(cfg), "tg") == {}
    d = wake_state.load(cfg)
    assert d.get("rotated") is not True and d.get("retired_sid") is None


def test_a_refused_transfer_retires_nothing(cfg, cdir, trace):
    from cortex import shell_ledger
    breaker.trip(cdir, "all", breaker.REASON_MANUAL)
    duty.transfer(cfg, "tg", rotate=True)
    assert shell_ledger.read(config.shell_state_dir(cfg), "tg") == {}


# --- refusals ----------------------------------------------------------------

def test_unknown_shell_refuses(cfg, cdir, trace):
    res = duty.transfer(cfg, "wx")
    assert res["ok"] is False and "wx" in res["error"]
    assert not duty.duty_path(cdir).exists()
    assert trace == []


def test_standing_breaker_refuses_and_touches_neither_file(cfg, cdir, trace):
    breaker.trip(cdir, "all", breaker.REASON_MANUAL)
    duty.write(cdir, "cli")
    wake_state.set_awake(cfg, 1, None)
    before_b = breaker.breaker_path(cdir).read_bytes()
    before_d = duty.duty_path(cdir).read_bytes()
    res = duty.transfer(cfg, "cli")
    assert res == {"ok": False, "error": duty.ERR_BREAKER_HELD}
    assert breaker.breaker_path(cdir).read_bytes() == before_b
    assert duty.duty_path(cdir).read_bytes() == before_d
    assert trace == []


def test_fuse_breaker_refuses_too(cfg, cdir, trace):
    breaker.trip(cdir, "cli", breaker.REASON_AUTO)
    res = duty.transfer(cfg, "cli")
    assert res["ok"] is False
    assert trace == []


def test_duty_hold_alone_never_refuses(cfg, cdir, trace):
    duty.write(cdir, "cli")
    assert duty.transfer(cfg, "cli")["ok"] is True


# --- cli ---------------------------------------------------------------------

def test_main_prints_the_outcome_as_json(cfg, cdir, trace, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    assert duty.main(["--transfer", "tg"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["target"] == "cli"


def test_main_rotate_flag_reaches_the_caller(cfg, cdir, trace, monkeypatch, capsys):
    from cortex import shell_ledger
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    assert duty.main(["--transfer", "tg", "--rotate"]) == 0
    assert json.loads(capsys.readouterr().out)["rotated"] is True
    assert shell_ledger.read(config.shell_state_dir(cfg), "tg") == {
        "rotate_pending": True}


def test_main_prints_a_refusal_as_json(cfg, cdir, trace, monkeypatch, capsys):
    breaker.trip(cdir, "all", breaker.REASON_MANUAL)
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    assert duty.main(["--transfer", "cli"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": False, "error": duty.ERR_BREAKER_HELD}
