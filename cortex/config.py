"""Config loader: ~/.config/marrow/cortex.toml (override via CORTEX_CONFIG env).

Tolerant: missing file or missing keys fall back to defaults. Never raises
on a missing config file.
"""
from __future__ import annotations

import copy
import logging
import os
import tomllib
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "marrow" / "cortex.toml"
DEFAULT_MARROW_DB = Path.home() / ".config" / "marrow" / "marrow.db"
DEFAULT_KNOWLEDGEC_DB = (
    Path.home() / "Library" / "Application Support" / "Knowledge" / "knowledgeC.db"
)
# Per-shell rolling log; this repo is the cli shell (mirror of marrow
# [cortex].handoff_file_pattern = "handoff-{shell}.md"). Override: paths.handoff_file.
DEFAULT_HANDOFF = Path.home() / ".config" / "marrow" / "cortex" / "handoff-cli.md"
DEFAULT_CORTEX_HOME = Path.home() / ".config" / "marrow" / "cortex"
DEFAULT_NY_DB_PAGES = Path.home() / "Desktop" / "NY" / "db-pages"
DEFAULT_MARROW_REPO = Path.home() / "CC-Lab" / "marrow"
DEFAULT_WAKE_TIMING_LOG = Path.home() / ".config" / "marrow" / "logs" / "wake_timing.log"

# Single source of truth for the machine-line marker family (wake bell /
# free-round / fuse / ctl / slash-command). Referenced by _DEFAULTS below AND
# by transcript._line_markers' fallback so the two can never drift.
DEFAULT_MACHINE_LINE_MARKERS = ["[NEW ROUND]", "[FUSE]", "[CTL]", "[CMD"]

_DEFAULTS: dict[str, Any] = {
    # Empty = follow the OS timezone; set an IANA name only for headless/VPS
    # deploys where the OS tz differs from the user's.
    "core": {"timezone": ""},
    "paths": {
        "marrow_db": "",
        "knowledgec_db": "",
        "geofence_file": "",
        "health_export": "",
        "affect_flag_file": "",
        "self_schedule_file": "",
        "handoff_file": "",
        "cortex_home": "",
        "wishlist_file": "",
        "ny_db_pages": "",
        "wake_timing_log": "",
        "wake_audit_log": "",
        # Non-cli shell ledgers (<dir>/<shell>.json, written by each shell's
        # host). Mirror of marrow [cortex].shell_state_dir. Empty =
        # <marrow config dir>/state/shells.
        "shell_state_dir": "",
    },
    # ear_timeout_sec = how long a typed wake (alive-resident bell) is given to
    # land before respawning fresh; wake_prompt = the first prompt baked into a
    # freshly spawned window — JUST an emoji so nothing readable shows in the
    # user's face; the full wake instructions are injected by marrow's
    # UserPromptSubmit hook when this exact emoji is submitted in a cortex
    # window (the note path itself is read from config, not this prompt);
    # say_sound = the sound say() plays when it fronts the window.
    "wake": {
        # Auto-adopt a cortex window the user opened `claude` in herself (in
        # cortex_home) but never registered: the daemon reconcile records it as
        # the resident (under the spawn lock) instead of firing a duplicate window.
        "auto_adopt": True,
        "ear_timeout_sec": 90,
        "wake_prompt": "☀️",
        # Visible wake lines = human text ONLY (no machine marker, no epoch
        # token). The machine data (template/gen/state_id/rearm) is written to
        # the wake_state receipt sidecar instead (window.write_wake_receipt);
        # the marrow hook matches the on-screen line against that receipt.
        # {hm} = local HH:MM. The static PREFIX (text before {hm}) is persisted
        # into wake_state so the consumer can shape-match without cortex config.
        #   spawn_opener_template — baked as the FIRST prompt of a freshly
        #     spawned window (fresh_initial_prompt); also the window-lineage
        #     marker (transcript.lineage_marker).
        #   wake_bell_template — TYPED into an already-running resident window
        #     (scheduled wake on a live window, resume bell).
        "spawn_opener_template": "☀️ {hm}",
        "wake_bell_template": "⏰ {hm}",
        # Receipt time-to-live (minutes): the consumer ignores a pending receipt
        # older than this, and the producer overwrites it on every new bell.
        "receipt_ttl_min": 15,
        "say_sound": "Glass",
        # lie_down(next_wake_min=N) clamp (minutes): [0, next_wake_max], every
        # hour (0 = immediate re-wake).
        "next_wake_max": 360,
        # Minutes until the next wake when no caller picked one (proxy
        # lie_down, daemon/ctl/reconcile re-arm after a failed round).
        # Cross-repo interval contract — keep these three equal:
        # [wake.watchdog].silent_max_min here, this key, and synapse tg
        # [cortex].shell_idle_min.
        "default_sleep_min": 20,
        # Throttle (minutes) on the wake_state lock give-up alert — one row per
        # window however many times the lock is lost.
        "lock_alert_throttle_min": 60,
        # Typed into the window at every free-round injection (silence
        # cycle or kick carrier): the free-round note above it, this line last as
        # the final cue. MUST contain the tuck_in_marker family string
        # ("[NEW ROUND]") — that machine-line detection is what keeps the line
        # from resetting the silence timer; without it every injection would
        # re-arm its own cycle (perpetual loop trap). {mins}/{user} optional
        # placeholders; {user} = marrow user_name.
        "tuck_in_text": "⏳ [NEW ROUND] Time for a new turn — try something else "
                         "you fancy, go talk to {user} if you miss her, or "
                         "lie_down for a rest.",
        # Markers that identify a NON-user turn (wake bell / free-round / fuse /
        # ctl / slash-command line), so they never reset the silence timer
        # and downstream memory drops them. The bell prefix (lineage_marker) is
        # added automatically. Substring match, so "[CMD" catches every ⚙️ [CMD ct-*].
        "machine_line_markers": list(DEFAULT_MACHINE_LINE_MARKERS),
        # Every free-round injection (silence cycle or kick carrier) appends a
        # freshly rendered wakeup note above the marker line (note content only,
        # no behavioural nudge).
        "free_round_note": True,
        # Covert-delivery markers typed directly into the window (Monitor
        # retired, T11 P3). Only the marker line reaches the window; the full
        # instruction body is injected invisibly by the
        # marrow hook keyed on the marker (fuse -> [cortex].fuse_prompt_text;
        # ctl -> [cortex].ctl_sleep_text, rendered from the mins/rotate args the
        # ctl marker line carries).
        "fuse_marker": "⚙️ [FUSE]",
        "ctl_sleep_marker": "⚙️ [CTL]",
    },
    # marrow repo invocation for subprocess helpers (separate venv/deps, C3).
    "marrow": {
        "venv_python": str(DEFAULT_MARROW_REPO / ".venv" / "bin" / "python"),
    },
    "knowledgec": {"stream_name": "/app/usage"},
    "knowledgec.categories": {"default": "uncategorized"},
    "geofence": {"enabled": False},
    "health": {"enabled": False},
    # launchd tick cadence (seconds). Baked into plists at install time.
    "tick": {
        "collect_interval_sec": 1800,
        # OAuth usage % snapshot (marrow subprocess) each collect tick.
        "usage_snapshot": True,
    },
    # Wake brake: gates whether a fired ledger wake actually spawns a window or
    # is log-only (still re-arming the next wake). Covers both fire paths —
    # daemon business + reconcile dead-window.
    "pacemaker": {
        "dry_run": True,
    },
    # External-wake (cortex.kick) reason lines rendered as plain lines into the
    # wakeup note (no section header), then cleared on delivery. A bridge/cli
    # poke appends one; note.py renders + consumes it. {id} = outbox note id;
    # {text} = her reply body (truncated by the bridge); {minutes} = silence min.
    "kick": {
        "reason_reply": 'Msg #{id} replied: "{text}"',
        "reason_timeout": "Msg #{id} no reply in {minutes}min",
        "reason_morning": "She's up — day mode",
        "reason_note": "New note #{id}",
        # Cap the pending-flag list so a stuck bridge can't grow it unbounded.
        "max_reasons": 8,
    },
    # Wakeup note knobs. Every field is deterministic now, so the old whole-note
    # max_chars cap is gone; per-source limits below keep each line bounded.
    # OSS: identity/display strings stay in config, never hardcoded in .py.
    "note": {
        # Machine-origin tag prepended as the VERY FIRST line of every wakeup
        # note (before title), so the model treats the ☀️ bell/wake-prompt turn
        # that carried this note as an automated scheduler signal, NOT a message
        # the user personally typed. Note-copy only -- does NOT change the bell
        # text or receipt matching. "" omits it.
        "wake_machine_tag":
            "[AUTOMATED WAKE SIGNAL — Note delivered by the scheduler]",
        # Optional first line of the wakeup note (e.g. a nickname for the
        # note), followed by a blank line then the usual content. "" omits it.
        "title": "",
        # Pending self-schedule entries surface only when due within this window.
        "pending_window_min": 15,
        # Appended to the "Now: … | Last active: …" line while the circuit
        # breaker covers THIS shell (scope all/<shell>). {reason} = manual /
        # auto_fuse, {scope} = all / cli / tg. "" omits the tag.
        "pause_tag": "(paused: {reason})",
        # Reply-receipt line (C11): one per sent note she has replied to since the
        # last note. {id}/{channel}/{sent_hm}/{replied_hm}/{text} render from the
        # marrow outbox row at note time. "" omits receipts entirely.
        "receipt_line": 'Note #{id} ({channel} {sent_hm}): she replied {replied_hm} "{text}"',
        # One-line turn-end reminder appended at the very end of every rendered
        # note. "" omits it (default: the silence cycle + free-round injection
        # already carries this information, no reminder needed).
        "turn_end_text": "",
        # Header written into a freshly-created wishlist.md (append-only file,
        # never overwritten). Display text — customise freely.
        "wishlist_header":
            "# Wishlist\n\n(owed treats / wants / self-rewards — append-only)\n",
    },
    # Cross-channel note delivery into the cortex window. Header for a ct-targeted
    # outbox note rendered by the wakeup note (mirror of marrow [outbox].inject_header
    # — must stay in sync so hook-delivered and note-delivered notes read the same).
    # {channel}/{sid4}/{time} render from the outbox row. "" = body only.
    "outbox": {
        "inject_header": "📮 Message from {channel}·{sid4} {time}",
    },
    # cortex.daemon: the always-on cli timing loop (scheduler-hosted). Owns the
    # reconcile cadence and the business deadline (next_wake_at / silence round).
    "daemon": {
        "enabled": True,
        # Scheduler shell key. The reconcile deadline uses "<shell>.reconcile";
        # lie_down's socket kick sends "<shell>" (the business key).
        "shell": "cli",
        # Kick socket. "" -> <state_dir>/cortex-daemon.sock. Keep the resolved
        # path under 104 bytes (AF_UNIX limit) — checked at resolve time.
        "socket_path": "",
        "kick_timeout_sec": 1.0,
        # Reconcile cadence (also the scheduler's max sleep).
        "reconcile_interval_sec": 60,
        # Re-arm gap for the business key when nothing is pending.
        "safety_horizon_sec": 300,
        # Re-arm gap after a held/failed business fire (anti-spin).
        "retry_interval_sec": 60,
        # Singleton lock. "" -> <state_dir>/daemon.lock.
        "lock_path": "",
    },
}

_SECTIONS = (
    "core", "paths", "knowledgec", "geofence", "health",
    "tick", "pacemaker", "marrow",
    "wake", "note", "kick", "outbox", "daemon",
)


def shell_enabled(cfg: dict, shell: str = "cli") -> bool:
    """Is `shell` listed in marrow's [cortex].shells (T6: single source, no
    cortex.toml copy)? Missing/unreadable marrow config or missing key falls
    back to ["cli"] — the cli heartbeat entries exit early when their own
    shell is switched off there."""
    raw = ["cli"]
    p = marrow_config_dir(cfg) / "config.toml"
    try:
        if p.is_file():
            with p.open("rb") as f:
                data = tomllib.load(f)
            shells = (data.get("cortex") or {}).get("shells")
            if isinstance(shells, list):
                raw = shells
    except (OSError, ValueError, TypeError) as e:
        logger.warning("shell_enabled: marrow config read failed (%s) — using default ['cli']", e)
    return shell.strip().lower() in [str(s).strip().lower() for s in raw]


def shell_id(cfg: dict) -> str:
    """The shell this cortex process drives ([daemon].shell, default 'cli') —
    the breaker scope and the value stamped on its ct_wake_log rows."""
    return str((cfg.get("daemon") or {}).get("shell") or "cli")


def wake_clamps(cfg: dict) -> dict[str, int]:
    """The wake-clamp numbers rendered into note/tool text (never hardcoded).
    lie_down bounds [0, next_wake_max] from [wake]; idle bar from
    [wake.watchdog].silent_max_min."""
    w = cfg.get("wake", {})
    wd = w.get("watchdog", {})
    return {
        "next_wake_min": 0,
        "next_wake_max": int(w.get("next_wake_max", 360)),
        "silent_max_min": int(wd.get("silent_max_min", 20)),
    }


def _config_path() -> Path:
    override = os.environ.get("CORTEX_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def _merge(defaults: dict, loaded: dict) -> dict:
    merged = dict(defaults)
    for key, val in loaded.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load(path: Path | None = None) -> dict[str, Any]:
    """Load cortex config with tolerant fallback to defaults."""
    cfg_path = path or _config_path()
    loaded: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as f:
            loaded = tomllib.load(f)

    cfg = {section: copy.deepcopy(_DEFAULTS[section]) for section in _SECTIONS}
    for section in _SECTIONS:
        if section in loaded:
            cfg[section] = _merge(cfg[section], loaded[section])

    # Legacy fallback: an old config.toml may still carry the pre-rename
    # [bulletin] section (renamed to [note]) — merge it in if present.
    if "bulletin" in loaded:
        cfg["note"] = _merge(cfg["note"], loaded["bulletin"])

    # T6: [core].shells moved to marrow's [cortex].shells (single source) —
    # a leftover key here is ignored, never fatal, just noted once.
    if "shells" in loaded.get("core", {}):
        logger.warning(
            "cortex.toml [core].shells is ignored — shells are now driven by "
            "marrow's [cortex].shells only")

    categories = dict(_DEFAULTS["knowledgec.categories"])
    loaded_categories = loaded.get("knowledgec", {}).get("categories", {})
    categories.update(loaded_categories)
    cfg["knowledgec"]["categories"] = categories

    return cfg


def os_tz() -> tzinfo:
    """OS local timezone. Resolves the IANA zone via /etc/localtime so DST
    transitions stay correct; falls back to the current fixed offset."""
    try:
        parts = Path("/etc/localtime").resolve().parts
        if "zoneinfo" in parts:
            return ZoneInfo("/".join(parts[parts.index("zoneinfo") + 1:]))
    except Exception:
        pass
    return datetime.now().astimezone().tzinfo


def get_tz(cfg: dict) -> tzinfo:
    """[core] timezone if set, else the OS local timezone."""
    name = (cfg.get("core", {}).get("timezone") or "").strip()
    return ZoneInfo(name) if name else os_tz()


def marrow_db_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("marrow_db") or ""
    return Path(raw).expanduser() if raw else DEFAULT_MARROW_DB


def marrow_config_dir(cfg: dict) -> Path:
    """The shared marrow config/state dir (parent of marrow.db). Home of
    config.toml, breaker.json and fuse_events.json — the cross-repo protocol
    files cortex, marrow and the synapse bridges all read."""
    return marrow_db_path(cfg).parent


def user_name(cfg: dict, default: str = "the user") -> str:
    """The user's name, read from marrow's config.toml (sibling of cortex.toml in
    the shared config dir) — cortex inherits it, never carries its own copy (OSS:
    no hardcoded persona in code). `default` when marrow config is absent/blank."""
    db = marrow_db_path(cfg)
    marrow_cfg = db.parent / "config.toml"
    try:
        if marrow_cfg.exists():
            with marrow_cfg.open("rb") as f:
                data = tomllib.load(f)
            name = str(data.get("persona", {}).get("user_name") or "").strip()
            if not name:
                name = str(data.get("user_name") or "").strip()  # legacy top-level layout
            if name:
                return name
    except (OSError, ValueError):
        pass
    return default


def knowledgec_db_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("knowledgec_db") or ""
    return Path(raw).expanduser() if raw else DEFAULT_KNOWLEDGEC_DB


def geofence_file_path(cfg: dict) -> Path | None:
    raw = cfg["paths"].get("geofence_file") or ""
    return Path(raw).expanduser() if raw else None


def health_export_path(cfg: dict) -> Path | None:
    raw = cfg["paths"].get("health_export") or ""
    return Path(raw).expanduser() if raw else None


def affect_flag_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("affect_flag_file") or ""
    return Path(raw).expanduser() if raw else state_dir(cfg) / "affect_flag.json"


def self_schedule_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("self_schedule_file") or ""
    return Path(raw).expanduser() if raw else state_dir(cfg) / "self_schedule.json"


def handoff_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("handoff_file") or ""
    return Path(raw).expanduser() if raw else DEFAULT_HANDOFF


def shell_state_dir(cfg: dict) -> Path:
    """Directory holding the non-cli shell ledgers (<shell>.json). Mirror of
    marrow's [cortex].shell_state_dir; empty = <marrow config dir>/state/shells."""
    raw = cfg["paths"].get("shell_state_dir") or ""
    if raw:
        return Path(raw).expanduser()
    return marrow_db_path(cfg).parent / "state" / "shells"


def cortex_home(cfg: dict) -> Path:
    """cwd for the resumed full-env marrow cortex session (Decided 07-03 pm)."""
    raw = cfg["paths"].get("cortex_home") or ""
    return Path(raw).expanduser() if raw else DEFAULT_CORTEX_HOME


def state_dir(cfg: dict) -> Path:
    """Runtime state subdirectory (json/log/lock/pid files). Lives under
    cortex_home/state/ to keep runtime artefacts out of the notebook root."""
    d = cortex_home(cfg) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


_SOCKET_PATH_MAX = 104  # macOS sun_path (AF_UNIX) capacity, including the NUL


def daemon_socket_path(cfg: dict) -> Path:
    """The wake daemon's kick socket. [daemon].socket_path, or
    <state_dir>/cortex-daemon.sock when unset. Raises ValueError when the
    resolved path cannot fit an AF_UNIX address (a bind would fail at runtime)."""
    raw = str((cfg.get("daemon") or {}).get("socket_path") or "").strip()
    p = Path(raw).expanduser() if raw else state_dir(cfg) / "cortex-daemon.sock"
    if len(str(p).encode("utf-8")) >= _SOCKET_PATH_MAX:
        raise ValueError(
            f"[daemon].socket_path too long for AF_UNIX ({_SOCKET_PATH_MAX} bytes): {p}")
    return p


def wishlist_path(cfg: dict) -> Path:
    """Pure append-only md, fixed path (Decided 07-03 eve). Mirrors marrow's
    own [cortex].wishlist_path default ('' -> <home>/wishlist.md)."""
    raw = cfg["paths"].get("wishlist_file") or ""
    return Path(raw).expanduser() if raw else cortex_home(cfg) / "wishlist.md"


def ny_db_pages_dir(cfg: dict) -> Path:
    raw = cfg["paths"].get("ny_db_pages") or ""
    return Path(raw).expanduser() if raw else DEFAULT_NY_DB_PAGES


def wishlist_header(cfg: dict) -> str:
    """Header written into a freshly-created wishlist.md. Display text; config-driven."""
    return cfg.get("note", {}).get("wishlist_header") or _DEFAULTS["note"]["wishlist_header"]


def wake_timing_log_path(cfg: dict) -> Path:
    """Shared wake-latency probe log (cortex marks + marrow stream-event marks).
    Default: ~/.config/marrow/logs/wake_timing.log."""
    raw = cfg["paths"].get("wake_timing_log") or ""
    return Path(raw).expanduser() if raw else DEFAULT_WAKE_TIMING_LOG


def wake_audit_log_path(cfg: dict) -> Path:
    """Wake-state audit trail (alarm epoch/generation events). Byte-shared with
    marrow's [cortex].wake_audit_log_file so both sides append to one file.
    Default: <cortex_home>/state/wake_audit.log. Override via [paths].wake_audit_log."""
    raw = cfg["paths"].get("wake_audit_log") or ""
    return Path(raw).expanduser() if raw else state_dir(cfg) / "wake_audit.log"
