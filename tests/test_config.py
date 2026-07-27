from __future__ import annotations

from pathlib import Path

from cortex import config


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = config.load(tmp_path / "does_not_exist.toml")
    assert cfg["core"]["timezone"] == ""  # empty = follow OS timezone
    assert cfg["paths"]["marrow_db"] == ""
    assert cfg["geofence"]["enabled"] is False
    assert cfg["health"]["enabled"] is False
    assert cfg["knowledgec"]["categories"]["default"] == "uncategorized"


def test_load_merges_overrides(tmp_path):
    toml_path = tmp_path / "cortex.toml"
    toml_path.write_text(
        """
[core]
timezone = "UTC"

[paths]
geofence_file = "/tmp/geo.txt"

[geofence]
enabled = true

[knowledgec.categories]
"com.example.app" = "dev"
"""
    )
    cfg = config.load(toml_path)
    assert cfg["core"]["timezone"] == "UTC"
    assert cfg["paths"]["geofence_file"] == "/tmp/geo.txt"
    assert cfg["geofence"]["enabled"] is True
    assert cfg["knowledgec"]["categories"]["com.example.app"] == "dev"
    assert cfg["knowledgec"]["categories"]["default"] == "uncategorized"


def test_every_injected_prompt_carries_a_machine_marker(tmp_path):
    """Phase 3 D8: every watcher/system line written into the cortex window (so
    it lands as a user-role turn) must begin with a recognised machine marker,
    else recall/tl read it as user speech. Grep-level guard over all marker/prompt
    lines. FUSE/CTL bodies now live marrow-side and are injected covertly — cortex
    only writes their MARKER lines, which must be marked."""
    from cortex import transcript

    cfg = config.load(tmp_path / "none.toml")
    markers = transcript._line_markers(cfg)  # bell prefix + machine_line_markers

    def marked(text: str) -> bool:
        return any(m in text for m in markers)

    wake = cfg["wake"]
    assert marked(wake["tuck_in_text"])
    assert marked(wake["fuse_marker"])
    assert marked(wake["ctl_sleep_marker"])
    # the family covers the new fuse / ctl / command markers
    for needle in ("[FUSE]", "[CTL]", "[CMD"):
        assert needle in markers


def test_path_helpers_default_when_empty():
    cfg = config.load(Path("/does/not/exist.toml"))
    assert config.marrow_db_path(cfg) == config.DEFAULT_MARROW_DB
    assert config.knowledgec_db_path(cfg) == config.DEFAULT_KNOWLEDGEC_DB
    assert config.geofence_file_path(cfg) is None
    assert config.health_export_path(cfg) is None


def test_user_name_reads_persona_section(tmp_path):
    """Current marrow layout: user_name lives under [persona]."""
    marrow_cfg = tmp_path / "config.toml"
    marrow_cfg.write_text(
        """
[persona]
user_name = "念念"
"""
    )
    cfg = config.load(tmp_path / "cortex.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    assert config.user_name(cfg) == "念念"


def test_user_name_falls_back_to_legacy_top_level(tmp_path):
    """Old-layout marrow config: user_name at top level, no [persona] section."""
    marrow_cfg = tmp_path / "config.toml"
    marrow_cfg.write_text('user_name = "Legacy"\n')
    cfg = config.load(tmp_path / "cortex.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    assert config.user_name(cfg) == "Legacy"


def test_user_name_defaults_when_marrow_config_absent(tmp_path):
    cfg = config.load(tmp_path / "cortex.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "does_not_exist" / "marrow.db")
    assert config.user_name(cfg) == "the user"


# ── T6: shells single source (marrow [cortex].shells) ─────────────────────────

def _cfg_with_marrow_dir(tmp_path):
    cfg = config.load(tmp_path / "cortex.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    return cfg


def test_shells_defaults_to_cli_when_marrow_config_absent(tmp_path):
    cfg = _cfg_with_marrow_dir(tmp_path)
    assert config.shell_enabled(cfg) is True
    assert config.shell_enabled(cfg, "tg") is False


def test_shells_reads_from_marrow_config(tmp_path):
    (tmp_path / "config.toml").write_text('[cortex]\nshells = ["tg"]\n')
    cfg = _cfg_with_marrow_dir(tmp_path)
    assert config.shell_enabled(cfg) is False
    assert config.shell_enabled(cfg, "TG") is True


def test_leftover_core_shells_key_warns_not_fatal(tmp_path, caplog):
    """cortex.toml [core].shells is no longer read; presence just warns once."""
    toml_path = tmp_path / "cortex.toml"
    toml_path.write_text('[core]\nshells = ["tg"]\n')
    with caplog.at_level("WARNING"):
        cfg = config.load(toml_path)
    assert any("[core].shells" in r.message for r in caplog.records)
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    # behaviour driven by marrow config only — the leftover key has no effect
    assert config.shell_enabled(cfg) is True


def test_wake_daemon_noops_when_cli_shell_off(tmp_path, monkeypatch, capsys):
    """Heartbeat entry exits before touching the DB or the lock when cli is not
    a shell in marrow's [cortex].shells."""
    from cortex import daemon

    (tmp_path / "config.toml").write_text('[cortex]\nshells = []\n')
    cfg = _cfg_with_marrow_dir(tmp_path)
    monkeypatch.setattr(daemon.config, "load", lambda: cfg)

    def _boom(*a, **kw):
        raise AssertionError("db.connect must not run with the cli shell off")

    monkeypatch.setattr(daemon.db, "connect", _boom)
    assert daemon.main([]) == 0
    assert "cli shell off" in capsys.readouterr().out


def test_watchdog_noops_when_cli_shell_off(tmp_path, monkeypatch):
    """Watchdog entry never writes its pidfile with the cli shell off in
    marrow's [cortex].shells."""
    from cortex import watchdog

    (tmp_path / "config.toml").write_text('[cortex]\nshells = []\n')
    cfg = _cfg_with_marrow_dir(tmp_path)
    monkeypatch.setattr(watchdog.config, "load", lambda: cfg)

    def _boom(*a, **kw):
        raise AssertionError("watchdog must not start with the cli shell off")

    monkeypatch.setattr(watchdog.wake_state, "watchdog_pidfile_path", _boom)
    assert watchdog.main([]) == 0
