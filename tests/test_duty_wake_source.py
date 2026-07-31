"""Wake-source tags: a shell woken BY a duty rotation reads why it came up.

The line is staged per shell before the kick (cli -> wake_state, tg -> its
ledger) and rendered as the first header line of that shell's own wakeup note.
An auto wake stages nothing and its note is byte-identical to before.

Boundary stubs mirror test_duty_apply: run_wake, the proxy lie_down and the
shell-host socket kick never touch the machine."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from cortex import (config, duty, note, shell_ledger, wake_source, wake_state)


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
def stubs(cfg, cdir, monkeypatch):
    def _run_wake(conn, c, decision, now=None):
        return {"mode": "window"}

    async def _send_kick(path, shell):
        return None

    def _lie_down(c, **kw):
        wake_state.update(c, awake=False)
        return {}

    from synapse_core import scheduler

    from cortex import lie_down as lie_down_mod
    from cortex import wake as wake_mod
    monkeypatch.setattr(wake_mod, "run_wake", _run_wake)
    monkeypatch.setattr(wake_mod, "_window_alive", lambda c: False)
    monkeypatch.setattr(scheduler, "send_kick", _send_kick)
    monkeypatch.setattr(lie_down_mod, "lie_down", _lie_down)


def _tg_slot(cfg):
    return shell_ledger.read(config.shell_state_dir(cfg), "tg").get("wake_source")


def _cli_slot(cfg):
    return wake_state.load(cfg).get("wake_source")


# --- rendering ----------------------------------------------------------------

def test_transfer_line_fills_origin_target_and_hold(cfg):
    assert wake_source.render(cfg, wake_source.KIND_TRANSFER,
                              shell="tg", hold="cli", from_shell="cli") == (
        "🔄 transferred from cli | tg on, cli hold")


def test_ctl_line_fills_target_and_hold(cfg):
    assert wake_source.render(cfg, wake_source.KIND_CTL,
                              shell="cli", hold="tg") == (
        "Kicked by /ct-duty - cli on, tg hold")


def test_absent_hold_renders_the_configured_label(cfg):
    assert wake_source.render(cfg, wake_source.KIND_CTL,
                              shell="cli", hold=None) == (
        "Kicked by /ct-duty - cli on, no hold")


def test_empty_template_omits_the_line(cfg):
    cfg["duty"]["ctl_source_text"] = ""
    assert wake_source.render(cfg, wake_source.KIND_CTL,
                              shell="cli", hold="tg") == ""


def test_unknown_placeholder_falls_back_to_the_raw_template(cfg):
    cfg["duty"]["ctl_source_text"] = "woken {nope}"
    assert wake_source.render(cfg, wake_source.KIND_CTL,
                              shell="cli", hold="tg") == "woken {nope}"


# --- staging ------------------------------------------------------------------

def test_transfer_stages_the_transfer_tag_on_the_incoming_shell(cfg, cdir, stubs):
    duty.write(cdir, "cli")
    res = duty.transfer(cfg, "cli")
    assert res["ok"] is True and res["target"] == "tg"
    assert _tg_slot(cfg) == "🔄 transferred from cli | tg on, cli hold"
    assert _cli_slot(cfg) is None


def test_transfer_from_tg_stages_the_cli_tag(cfg, cdir, stubs):
    duty.write(cdir, "tg")
    duty.transfer(cfg, "tg")
    assert _cli_slot(cfg) == "🔄 transferred from tg | cli on, tg hold"
    assert _tg_slot(cfg) is None


def test_ctl_duty_stages_the_duty_tag(cfg, cdir, stubs):
    from cortex import ctl
    line, code = ctl.cmd_duty(cfg, "tg")
    assert code == 0
    assert _tg_slot(cfg) == "Kicked by /ct-duty - tg on, cli hold"


def test_ctl_duty_all_stages_a_tag_on_both_shells(cfg, cdir, stubs):
    from cortex import ctl
    ctl.cmd_duty(cfg, "all")
    assert _tg_slot(cfg) == "Kicked by /ct-duty - tg on, no hold"
    assert _cli_slot(cfg) == "Kicked by /ct-duty - cli on, no hold"


def test_ctl_duty_off_stages_nothing(cfg, cdir, stubs):
    from cortex import ctl
    ctl.cmd_duty(cfg, "off")
    assert _tg_slot(cfg) is None and _cli_slot(cfg) is None


def test_an_auto_apply_stages_nothing(cfg, cdir, stubs):
    duty.apply(cfg, "tg")
    assert _tg_slot(cfg) is None and _cli_slot(cfg) is None


# --- consumption --------------------------------------------------------------

def test_take_clears_the_slot_and_peek_does_not(cfg):
    wake_source.stage(cfg, "cli", "line one")
    assert wake_source.peek(cfg, "cli") == "line one"
    assert wake_source.peek(cfg, "cli") == "line one"
    assert wake_source.take(cfg, "cli") == "line one"
    assert wake_source.take(cfg, "cli") == ""


def test_slots_are_per_shell(cfg):
    wake_source.stage(cfg, "cli", "cli line")
    wake_source.stage(cfg, "tg", "tg line")
    assert wake_source.take(cfg, "tg") == "tg line"
    assert wake_source.peek(cfg, "cli") == "cli line"


def test_empty_text_is_never_staged(cfg):
    wake_source.stage(cfg, "cli", "   ")
    assert _cli_slot(cfg) is None


# --- note render ---------------------------------------------------------------

def _render(cfg, data):
    return note.render(cfg, datetime.now(config.get_tz(cfg)), data)


def test_the_tag_leads_the_note_header(cfg):
    text = _render(cfg, {"wake_source": "Kicked by /ct-duty - cli on, tg hold"})
    body = text.split("\n")
    assert "Kicked by /ct-duty - cli on, tg hold" in body
    tag = cfg["note"]["wake_machine_tag"]
    assert body.index("Kicked by /ct-duty - cli on, tg hold") == body.index(tag) + 1


def test_an_auto_note_is_unchanged_without_a_tag(cfg):
    assert _render(cfg, {"wake_source": ""}) == _render(cfg, {})


@pytest.fixture
def conn(cfg):
    from cortex import db
    c = db.connect(cfg)
    yield c
    c.close()


def test_gather_peeks_by_default(cfg, conn):
    wake_source.stage(cfg, "cli", "staged line")
    assert note.gather(conn, cfg, datetime.now(config.get_tz(cfg)),
                       shell="cli")["wake_source"] == "staged line"
    assert _cli_slot(cfg) == "staged line"


def test_gather_consumes_for_the_delivering_render(cfg, conn):
    wake_source.stage(cfg, "cli", "staged line")
    now = datetime.now(config.get_tz(cfg))
    assert note.gather(conn, cfg, now, shell="cli",
                       consume_source=True)["wake_source"] == "staged line"
    assert note.gather(conn, cfg, now, shell="cli")["wake_source"] == ""


def test_gather_reads_the_tg_slot_for_a_tg_render(cfg, conn):
    wake_source.stage(cfg, "tg", "tg line")
    data = note.gather(conn, cfg, datetime.now(config.get_tz(cfg)), shell="tg")
    assert data["wake_source"] == "tg line"


# --- retired_sid guard (Fix C) --------------------------------------------------

def _over_threshold_transcript(cfg, name: str = "old.jsonl"):
    from cortex import transcript as tx
    d = tx.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 90_000, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 0}}}) + "\n",
        encoding="utf-8")
    return p


def test_a_fresh_wake_without_a_pointer_keeps_the_recorded_retired_sid(
        cfg, cdir, stubs):
    """The gate fires (an over-full window) while wake_state carries no
    transcript pointer — the retirement recorded by an earlier rotate must
    survive, not be wiped to None."""
    _over_threshold_transcript(cfg)
    wake_state.update(cfg, retired_sid="keepme", transcript=None)
    assert duty._wake_cli(cfg, datetime.now(config.get_tz(cfg))) is True
    assert wake_state.get_retired_sid(cfg) == "keepme"


def test_a_fresh_wake_records_the_retiring_pointer(cfg, cdir, stubs):
    p = _over_threshold_transcript(cfg)
    wake_state.update(cfg, retired_sid="stale", transcript=str(p))
    assert duty._wake_cli(cfg, datetime.now(config.get_tz(cfg))) is True
    assert wake_state.get_retired_sid(cfg) == "old"
