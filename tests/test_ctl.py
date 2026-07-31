"""ctl wake/pause/resume: per-shell release + the kick each one owes.

No real window, claude, db work or scheduler host: run_wake and the shell-host
socket kick are stubbed at their boundaries. The one unstubbed case is the dead
tg socket, which is a connect() to a path that does not exist."""
from __future__ import annotations

import json

import pytest

from cortex import breaker, config, ctl, duty, shell_ledger, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["handoff_file"] = str(home / "handoff.md")
    c["paths"]["wake_timing_log"] = str(home / "wake_timing.log")
    return c


@pytest.fixture
def cdir(cfg, tmp_path):
    return tmp_path


@pytest.fixture
def stub_cli_wake(monkeypatch):
    """The cli half of ctl wake, stubbed at the run_wake boundary."""
    calls = []

    def _run_wake(conn, c, decision, now=None):
        calls.append(decision)
        return {"mode": "window"}

    from cortex import wake as wake_mod
    monkeypatch.setattr(wake_mod, "run_wake", _run_wake)
    monkeypatch.setattr(wake_mod, "_window_alive", lambda c: False)
    return calls


@pytest.fixture
def kicks(monkeypatch):
    """Capture shell-host socket kicks instead of opening one."""
    sent = []

    async def _send_kick(path, shell):
        sent.append((str(path), shell))

    from synapse_core import scheduler
    monkeypatch.setattr(scheduler, "send_kick", _send_kick)
    return sent


def _ledger(cfg):
    return shell_ledger.read(config.shell_state_dir(cfg), "tg")


# --- wake --shell tg ----------------------------------------------------------

def test_wake_tg_releases_only_tg_and_books_a_due_round(cfg, cdir, kicks):
    breaker.pause(cfg, "all")
    line = ctl.cmd_wake(cfg, "tg")
    assert breaker.read(cdir)["scope"] == "cli"  # cli stays held
    assert _ledger(cfg)["next_wake_at"]
    assert kicks == [(str(config.shell_socket_path(cfg, "tg")), "tg")]
    assert line == ("breaker cleared; wake tg: round due now, host kicked")


def test_wake_tg_does_not_touch_the_cli_pipeline(cfg, kicks, monkeypatch):
    from cortex import wake as wake_mod
    monkeypatch.setattr(wake_mod, "run_wake",
                        lambda *a, **k: pytest.fail("cli wake on --shell tg"))
    ctl.cmd_wake(cfg, "tg")


def test_wake_tg_ledger_stands_when_the_host_is_down(cfg):
    """No scheduler listening: the kick fails, the booking is the durable half."""
    line = ctl.cmd_wake(cfg, "tg")
    assert _ledger(cfg)["next_wake_at"]
    assert line == ("wake tg: round due now, host unreachable — "
                    "fires on its next pass")


def test_wake_tg_merges_into_the_existing_ledger(cfg, kicks):
    p = config.shell_state_dir(cfg) / "tg.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_id": "keep-me", "occupancy": 12}))
    ctl.cmd_wake(cfg, "tg")
    d = _ledger(cfg)
    assert d["session_id"] == "keep-me" and d["occupancy"] == 12
    assert d["next_wake_at"]


# --- wake --shell cli / all ---------------------------------------------------

def test_wake_cli_releases_only_cli_and_skips_tg(cfg, cdir, stub_cli_wake, kicks):
    breaker.pause(cfg, "all")
    line = ctl.cmd_wake(cfg, "cli")
    assert breaker.read(cdir)["scope"] == "tg"
    assert kicks == []
    assert shell_ledger.state_path(config.shell_state_dir(cfg), "tg").exists() is False
    assert line == "breaker cleared; wake cli: resume/spawn (mode=window)"
    assert len(stub_cli_wake) == 1


def test_wake_all_clears_everything_and_kicks_both(cfg, cdir, stub_cli_wake, kicks):
    breaker.pause(cfg, "all")
    line = ctl.cmd_wake(cfg)
    assert breaker.read(cdir) is None
    assert _ledger(cfg)["next_wake_at"]
    assert len(kicks) == 1 and len(stub_cli_wake) == 1
    assert line == ("breaker cleared; wake tg: round due now, host kicked | "
                    "wake cli: resume/spawn (mode=window)")


def test_wake_all_with_a_clear_breaker_has_no_prefix(cfg, stub_cli_wake, kicks):
    assert ctl.cmd_wake(cfg).startswith("wake tg:")


def test_wake_cli_no_op_while_on_duty(cfg, monkeypatch, kicks):
    from cortex import wake as wake_mod
    monkeypatch.setattr(wake_mod, "_window_alive", lambda c: True)
    monkeypatch.setattr(wake_mod, "run_wake",
                        lambda *a, **k: pytest.fail("double activation"))
    wake_state.set_awake(cfg, 1, None)
    assert ctl.cmd_wake(cfg, "cli") == (
        "wake cli: already awake on duty -> no-op (one resident)")


# --- pause --------------------------------------------------------------------

def test_pause_merges_instead_of_replacing(cfg, cdir):
    ctl.cmd_pause(cfg, "cli")
    line = ctl.cmd_pause(cfg, "tg")
    assert breaker.read(cdir)["scope"] == "all"
    assert "scope=all" in line


def test_pause_one_shell_leaves_scope_all_alone(cfg, cdir):
    ctl.cmd_pause(cfg)
    ctl.cmd_pause(cfg, "cli")
    assert breaker.read(cdir)["scope"] == "all"


# --- resume -------------------------------------------------------------------

def test_resume_cli_books_a_due_now_alarm(cfg):
    """ct-pause books nothing, so a bare release would sleep forever."""
    ctl.cmd_pause(cfg, "cli")
    assert wake_state.get_next_wake_at(cfg) is None
    line = ctl.cmd_resume(cfg, "cli")
    assert wake_state.get_next_wake_at(cfg)
    assert line.endswith("; cli alarm booked now")


def test_resume_cli_keeps_an_armed_alarm(cfg):
    ctl.cmd_pause(cfg, "cli")
    wake_state.set_next_wake_at(cfg, "2030-01-01T08:00:00+11:00")
    line = ctl.cmd_resume(cfg, "cli")
    assert wake_state.get_next_wake_at(cfg) == "2030-01-01T08:00:00+11:00"
    assert "alarm booked" not in line


def test_resume_tg_books_nothing_for_cli(cfg):
    ctl.cmd_pause(cfg, "tg")
    line = ctl.cmd_resume(cfg, "tg")
    assert wake_state.get_next_wake_at(cfg) is None
    assert "alarm booked" not in line


def test_resume_never_writes_an_outbox_receipt(cfg):
    """The clear_message receipt is gone — resume touches no db at all."""
    from cortex import db
    ctl.cmd_pause(cfg)
    ctl.cmd_resume(cfg)
    conn = db.connect(cfg)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    finally:
        conn.close()
    assert "outbox" not in tables
    assert not hasattr(ctl, "_receipt")
    assert "clear_message" not in breaker.DEFAULTS


def test_resume_when_nothing_is_held(cfg):
    assert ctl.cmd_resume(cfg) == "resume: breaker already clear — nothing held"


# --- duty ---------------------------------------------------------------------

@pytest.fixture
def duty_cfg(cfg, tmp_path):
    cfg["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return cfg


def test_duty_accepts_every_mode(duty_cfg, cdir, stub_cli_wake, kicks):
    for mode in duty.MODES:
        line, code = ctl.cmd_duty(duty_cfg, mode)
        assert code == 0
        assert duty.read(cdir) == {"mode": mode,
                                   "hold": duty.hold_for(mode),
                                   "ts": duty.read(cdir)["ts"]}
        assert f"duty: mode={mode}" in line


def test_duty_rejects_an_unknown_mode(duty_cfg, cdir):
    line, code = ctl.cmd_duty(duty_cfg, "sideways")
    assert code == 1
    assert "unknown mode" in line and "cli|tg|off|all" in line
    assert duty.duty_path(cdir).exists() is False


def test_duty_bad_mode_exits_non_zero(duty_cfg, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda: duty_cfg)
    assert ctl.main(["duty", "sideways"]) == 1
    assert "unknown mode" in capsys.readouterr().out


def test_duty_mode_survives_a_round_trip_through_main(duty_cfg, cdir, kicks,
                                                      stub_cli_wake, monkeypatch,
                                                      capsys):
    monkeypatch.setattr(config, "load", lambda: duty_cfg)
    assert ctl.main(["duty", "tg"]) == 0
    assert duty.read(cdir)["hold"] == "cli"
    assert "duty: mode=tg hold=cli" in capsys.readouterr().out


def test_duty_names_the_on_duty_shell_and_kicks_it(duty_cfg, cdir, kicks,
                                                   stub_cli_wake):
    line, code = ctl.cmd_duty(duty_cfg, "tg")
    assert code == 0 and "woken=tg" in line
    assert kicks == [(str(config.shell_socket_path(duty_cfg, "tg")), "tg")]
    assert stub_cli_wake == []


def test_duty_clears_the_breaker_and_applies_once(duty_cfg, cdir, kicks,
                                                  stub_cli_wake, monkeypatch):
    """An explicit duty command outranks a standing trip — the fuse scope goes,
    and the released shell is woken by exactly one apply."""
    ctl.cmd_duty(duty_cfg, "cli")
    breaker.trip(cdir, breaker.SCOPE_ALL, breaker.REASON_AUTO)
    applied = []
    real_apply = duty.apply

    def _apply(c, mode, **kw):
        applied.append(mode)
        return real_apply(c, mode, **kw)

    monkeypatch.setattr(duty, "apply", _apply)
    line, code = ctl.cmd_duty(duty_cfg, "tg")
    assert code == 0
    assert applied == ["tg"]
    assert breaker.read(cdir) is None
    assert duty.read(cdir)["mode"] == "tg" and duty.read(cdir)["hold"] == "cli"
    assert _ledger(duty_cfg)["next_wake_at"]
    assert kicks == [(str(config.shell_socket_path(duty_cfg, "tg")), "tg")]
    assert line.startswith("breaker cleared; duty: mode=tg hold=cli woken=tg")


def test_duty_clears_the_breaker_between_the_hold_and_the_kick(
        duty_cfg, cdir, kicks, monkeypatch):
    """No instant with both enforcement files clear, and no kick delivered under
    a standing breaker: the duty hold is on disk when the breaker goes, and the
    breaker is gone when the shell is woken."""
    breaker.trip(cdir, breaker.SCOPE_ALL, breaker.REASON_AUTO)
    seen = []

    async def _send_kick(path, shell):
        seen.append(("kick", duty.read(cdir)["hold"], breaker.read(cdir)))

    real_clear = breaker.clear

    def _clear(config_dir, shell=None):
        seen.append(("clear", duty.read(cdir)["hold"], breaker.read(cdir)))
        return real_clear(config_dir, shell)

    from synapse_core import scheduler
    monkeypatch.setattr(scheduler, "send_kick", _send_kick)
    monkeypatch.setattr(breaker, "clear", _clear)

    ctl.cmd_duty(duty_cfg, "tg")
    assert [what for what, _h, _b in seen] == ["clear", "kick"]
    assert seen[0][1] == "cli" and seen[0][2] is not None
    assert seen[1][1] == "cli" and seen[1][2] is None


def test_duty_off_holds_both_and_kicks_nothing(duty_cfg, cdir, kicks,
                                               monkeypatch):
    from cortex import wake as wake_mod
    monkeypatch.setattr(wake_mod, "run_wake",
                        lambda *a, **k: pytest.fail("wake under duty off"))
    line, code = ctl.cmd_duty(duty_cfg, "off")
    assert code == 0
    assert duty.read(cdir)["hold"] == "all"
    assert kicks == []
    assert "woken=-" in line


# --- status -------------------------------------------------------------------

def test_status_reads_all_when_no_duty_file_exists(cfg):
    assert "duty: mode=all hold=-" in ctl.cmd_status(cfg)


def test_status_shows_the_current_duty_mode_and_hold(duty_cfg, kicks,
                                                     stub_cli_wake):
    ctl.cmd_duty(duty_cfg, "tg")
    assert "duty: mode=tg hold=cli" in ctl.cmd_status(duty_cfg)
