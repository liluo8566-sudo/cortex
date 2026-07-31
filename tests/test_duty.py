"""Duty rotation state file: mode -> hold mapping, fail-open reads, and the
union with the manual breaker at the cli choke point. Pure file io — no window,
socket or db side effects."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from cortex import breaker, config, duty


@pytest.fixture
def cdir(tmp_path):
    return tmp_path


# --- mapping ------------------------------------------------------------------

@pytest.mark.parametrize("mode,hold", [
    ("off", "all"),
    ("cli", "tg"),
    ("tg", "cli"),
    ("all", None),
])
def test_mode_maps_to_hold(cdir, mode, hold):
    assert duty.hold_for(mode) == hold
    st = duty.write(cdir, mode)
    assert st["mode"] == mode and st["hold"] == hold
    assert duty.read(cdir)["hold"] == hold


def test_write_roundtrip_on_disk(cdir):
    duty.write(cdir, "tg")
    on_disk = json.loads(duty.duty_path(cdir).read_text())
    assert set(on_disk) == {"mode", "hold", "ts"}
    assert on_disk["mode"] == "tg" and on_disk["hold"] == "cli"
    assert datetime.fromisoformat(on_disk["ts"])


def test_path_is_breaker_sibling(cdir):
    assert duty.duty_path(cdir).parent == breaker.breaker_path(cdir).parent
    assert duty.duty_path(cdir).name == "duty.json"


def test_write_is_last_writer_wins(cdir):
    duty.write(cdir, "off")
    assert duty.read(cdir)["hold"] == "all"
    duty.write(cdir, "cli")
    assert duty.read(cdir) | {"ts": ""} == {"mode": "cli", "hold": "tg", "ts": ""}


# --- fail-open ----------------------------------------------------------------

def test_absent_file_reads_clear(cdir):
    st = duty.read(cdir)
    assert (st["mode"], st["hold"]) == ("all", None)
    assert duty.covers(cdir, "cli") is False
    assert duty.covers(cdir, "tg") is False


@pytest.mark.parametrize("body", [
    "{ not json",
    "[]",
    '{"mode": "sideways"}',
    "{}",
])
def test_unreadable_file_reads_clear(cdir, body):
    duty.duty_path(cdir).write_text(body, encoding="utf-8")
    st = duty.read(cdir)
    assert (st["mode"], st["hold"]) == ("all", None)
    assert duty.covers(cdir, "cli") is False
    assert duty.covers(cdir, "tg") is False


def test_unknown_hold_value_is_ignored(cdir):
    duty.duty_path(cdir).write_text(
        json.dumps({"mode": "cli", "hold": "elsewhere", "ts": ""}), encoding="utf-8")
    assert duty.read(cdir)["hold"] is None
    assert duty.covers(cdir, "tg") is False


# --- cross-repo agreement: hold alone drives enforcement -----------------------

@pytest.mark.parametrize("body,held", [
    ('{"mode": "bogus", "hold": "tg"}', "tg"),
    ('{"hold": "all"}', "all"),
    ('{"mode": "cli", "hold": "elsewhere"}', None),
    ('{"mode": "cli"}', None),
])
def test_both_repos_enforce_the_same_hold(cdir, body, held):
    """A corrupt mode never invalidates a valid hold and an invalid hold reads as
    no hold — the tg bridge reads `hold` alone, so cortex must agree field for
    field or the two shells disagree on the same file."""
    from synapse_core import breaker as tg_breaker

    duty.duty_path(cdir).write_text(body, encoding="utf-8")
    assert duty.read(cdir)["hold"] == held
    for shell in ("cli", "tg"):
        covered = held in ("all", shell)
        assert duty.covers(cdir, shell) is covered
        assert tg_breaker.covers(cdir, shell) is covered


# --- union with the manual breaker --------------------------------------------

def test_duty_hold_alone_holds_the_shell(cdir):
    duty.write(cdir, "cli")
    assert breaker.read(cdir) is None
    assert breaker.covers(cdir, "tg") is True
    assert breaker.covers(cdir, "cli") is False


def test_manual_breaker_alone_holds_the_shell(cdir):
    breaker.trip(cdir, "tg", breaker.REASON_MANUAL)
    assert duty.read(cdir)["hold"] is None
    assert breaker.covers(cdir, "tg") is True
    assert breaker.covers(cdir, "cli") is False


def test_both_union_into_the_wider_hold(cdir):
    breaker.trip(cdir, "cli", breaker.REASON_MANUAL)
    duty.write(cdir, "cli")
    assert breaker.covers(cdir, "cli") is True
    assert breaker.covers(cdir, "tg") is True


def test_duty_all_releases_without_touching_the_breaker(cdir):
    breaker.trip(cdir, "cli", breaker.REASON_MANUAL)
    duty.write(cdir, "off")
    duty.write(cdir, "all")
    assert breaker.covers(cdir, "cli") is True
    assert breaker.covers(cdir, "tg") is False
    assert breaker.read(cdir)["scope"] == "cli"


def test_duty_never_writes_the_breaker_file(cdir):
    duty.write(cdir, "off")
    assert breaker.breaker_path(cdir).exists() is False


def test_holds_sees_the_duty_hold(tmp_path):
    cfg = config.load(path=tmp_path / "no-such.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    cfg["paths"]["cortex_home"] = str(tmp_path / "home")
    assert breaker.holds(cfg, "cli") is False
    duty.write(tmp_path, "tg")
    assert breaker.holds(cfg, "cli") is True
    assert breaker.holds(cfg, "tg") is False


# --- config -------------------------------------------------------------------

def test_duty_config_defaults(tmp_path):
    cfg = config.load(path=tmp_path / "no-such.toml")
    assert cfg["duty"] == {
        "fresh_token_threshold": 80000,
        "fresh_age_hours": 8,
        "transfer_source_text":
            "🔄 transferred from {from_shell} | {shell} on, {hold} hold",
        "ctl_source_text": "Kicked by /ct-duty - {shell} on, {hold} hold",
        "hold_none_label": "no",
    }


def test_duty_config_overrides(tmp_path):
    p = tmp_path / "cortex.toml"
    p.write_text("[duty]\nfresh_age_hours = 3\n", encoding="utf-8")
    cfg = config.load(path=p)
    assert cfg["duty"]["fresh_age_hours"] == 3
    assert cfg["duty"]["fresh_token_threshold"] == 80000
