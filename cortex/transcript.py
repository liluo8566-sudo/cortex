"""Read the resident cortex interactive session transcript. Claude Code writes
one JSONL per session under ~/.claude/projects/<munged-cwd>/; newest file =
current window. Exposes mtime + window-token occupancy (last assistant usage).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from cortex import config


def _munge(path: str) -> str:
    """Claude Code munges the cwd into the projects dir name by replacing every
    non-alphanumeric char with '-' (verified: /Users/.../.config/marrow/cortex
    -> -Users-...--config-marrow-cortex)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def transcript_dir(cfg: dict) -> Path:
    raw = cfg["paths"].get("transcript_dir") or ""
    if raw:
        return Path(raw).expanduser()
    home = str(config.cortex_home(cfg))
    return Path.home() / ".claude" / "projects" / _munge(home)


def newest(cfg: dict) -> Path | None:
    d = transcript_dir(cfg)
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


_LINEAGE_SCAN_LINES = 20  # first user message shows up within the first few
                          # lines (queue-operation/mode/permission-mode/
                          # file-history-snapshot preamble); no need to scan far.


def _first_user_content(p: Path) -> str | None:
    """First user-role message's content (str form) within the first
    _LINEAGE_SCAN_LINES lines of a session jsonl. Minimal parse — stops at the
    first match, never loads the whole file. None if no user message found in
    that window or the file is unreadable."""
    try:
        with p.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= _LINEAGE_SCAN_LINES:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get("message")
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        # Content-block form: concatenate text blocks only.
                        parts = [b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"]
                        return "".join(parts)
                    return None
    except OSError:
        return None
    return None


# The baked first prompt is "<emoji> <marker> HH:MM" (see window.fresh_initial_
#_prompt) — a short line. The marker sits a few chars in (emoji + space), so a
# bounded PREFIX search (not a full-content substring scan) is what actually
# discriminates a window-lineage session from a headless one: a genuine
# window's first message is short and the marker sits near its start; a digest
# archive's first message is a multi-KB blob that can quote/contain the marker
# substring deep inside it too (live-confirmed) but never near its start.
_LINEAGE_MARKER_PREFIX_CHARS = 24


def newest_window_lineage(cfg: dict, marker: str) -> Path | None:
    """The newest session jsonl in the transcript dir whose FIRST user message
    carries the wake bell prefix marker (config wake.wake_bell_template prefix,
    e.g. '☀️') within its first _LINEAGE_MARKER_PREFIX_CHARS chars — i.e.
    a genuine window-lineage session (every cortex window since dccb3d4 is
    launched with the marker baked into its first prompt), not a headless
    one-shot (marrow's sessionend digest also runs `claude -p` against this
    same cwd, so its archive lands in the same projects dir and can be the
    mtime-newest file, yet its first message is a large archived/quoted blob
    that can contain the marker substring far from its start).

    Iterates candidates by mtime DESC and returns the first hit; digest/
    headless archives fail the check and are skipped. None if no candidate
    qualifies (caller falls back to the recorded hint)."""
    d = transcript_dir(cfg)
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        content = _first_user_content(p)
        if content is not None and marker in content[:_LINEAGE_MARKER_PREFIX_CHARS]:
            return p
    return None


def window_transcript(cfg: dict) -> Path | None:
    """The newest GENUINE cortex-window jsonl, resolved WITHOUT the recorded
    wake_state pointer: newest_window_lineage first, bare newest() only when no
    candidate carries the marker at all.

    Distinct from resident_transcript on purpose. resident_transcript short-
    circuits on the recorded pointer, so it can never observe that the window
    rolled to a different session; callers that COMPARE the current file against
    the recorded one (wake classification) need this variant, which still skips
    headless `claude -p` archives sharing the projects dir."""
    marker = lineage_marker(cfg)
    if marker:
        lineage = newest_window_lineage(cfg, marker)
        if lineage is not None:
            return lineage
    return newest(cfg)


def mtime(cfg: dict) -> float | None:
    """Modification time of the RESIDENT window's transcript.

    Resident, not newest: the headless shells (marrow's sessionend digest, the
    tg cortex) run `claude -p` against the same cwd, so their jsonls land in the
    same projects dir and are routinely mtime-newest. Reading one of those made
    every mtime consumer — ear-landing polls, the hard-interrupt probe, the
    reconcile idle/stale gate, the duty fresh-vs-resume gate — judge a foreign
    session instead of this window."""
    p = resident_transcript(cfg)
    return p.stat().st_mtime if p else None


# Bytes read from the tail on the FIRST silence-scan chunk; if no qualifying user
# turn is found there (one huge tool-result row can fill the whole tail), the scan
# doubles the window and re-reads, up to _TAIL_MAX_BYTES, so a giant final row can
# never hide the real last-user message behind it (tonight's fixed-64KiB bug).
_TAIL_BYTES = 65536
_TAIL_MAX_BYTES = 4 * 1024 * 1024


def resident_transcript(cfg: dict) -> Path | None:
    """The RESIDENT window's transcript jsonl for silence checks — NOT bare
    newest(). newest() can return a headless `claude -p` digest that shares the
    projects dir and is mtime-newest; reading it for user-silence would miss the
    real window entirely. Resolution order: (1) the recorded wake_state.transcript
    (the window this wake actually spawned/resumed) when it still exists; else
    (2) newest_window_lineage (newest jsonl whose first message carries the wake
    marker = a genuine window, skipping digests); else (3) newest() as a last
    resort. None only when nothing readable exists."""
    from cortex import wake_state
    try:
        raw = wake_state.load(cfg).get("transcript")
    except Exception:
        raw = None
    if raw:
        p = Path(str(raw)).expanduser()
        if p.exists():
            return p
    marker = lineage_marker(cfg)
    if marker:
        lineage = newest_window_lineage(cfg, marker)
        if lineage is not None:
            return lineage
    return newest(cfg)


def lineage_marker(cfg: dict) -> str:
    """Marker leading a genuine cortex window's first prompt = the spawn-opener
    template prefix (e.g. '☀️'; window.fresh_initial_prompt)."""
    wcfg = cfg.get("wake", {})
    return str(wcfg.get("spawn_opener_template") or "☀️ {hm}").split("{hm}", 1)[0].strip()


def bell_marker(cfg: dict) -> str:
    """Prefix of the bell TYPED into a live resident window
    ([wake].wake_bell_template) — a machine line, never user speech."""
    wcfg = cfg.get("wake", {})
    return str(wcfg.get("wake_bell_template") or "⏰ {hm}").split("{hm}", 1)[0].strip()


# Leading decoration tolerated before a machine marker: whitespace + at most a
# couple of decoration glyphs (⏳U+23F3 ☀️U+2600 ⚙️U+2699), VS16, ZWJ. Narrowed
# to real decoration ranges — deliberately EXCLUDES CJK/kana/hangul so a Chinese
# user message quoting a marker keeps its lead char and still resets the timer.
_MARKER_LEAD_RE = re.compile(
    r"^\s*(?:[\U00002300-\U000027BF\U00002B00-\U00002BFF"
    r"\U0001F300-\U0001FAFF\U0000FE0F\U0000200D]\s*){0,3}")


def _line_starts_with_marker(text: str, markers: list[str]) -> bool:
    """True iff ANY line of *text* begins with a machine marker, tried RAW first
    (so an emoji-leading marker — e.g. the bell prefix '☀️' or a multi-codepoint
    ZWJ template like '🧚‍♀️' — matches itself directly) and only if that
    misses, after a tolerated leading decoration run is stripped (so a TEXT
    marker like '[NEW ROUND]' still matches past a machine-written emoji lead,
    e.g. '⏳ [NEW ROUND] …'). A real user message merely quoting a marker
    mid-sentence never matches either path, so it still resets the silence
    timer (zero false positives on real speech)."""
    if not markers:
        return False
    for line in text.splitlines() or [text]:
        if any(line.startswith(mk) for mk in markers):
            return True
        head = _MARKER_LEAD_RE.sub("", line, count=1)
        if any(head.startswith(mk) for mk in markers):
            return True
    return False


def _line_markers(cfg: dict) -> list[str]:
    """Machine-line markers that identify a NON-user turn arriving down the ear
    channel (wake bell, tuck-in / free-round injection). A transcript entry whose
    user-role text contains any of these is a system write, NOT a real user
    message, so it must not reset the silence timer. Aligned with marrow's
    is_machine_line (cortex_bridge.py): wake marker + tuck-in marker family."""
    wcfg = cfg.get("wake", {})
    out = []
    # Visible prefixes of both machine wake lines: the fresh-spawn opener
    # (lineage_marker) and the bell typed into a live resident (bell_marker).
    for m in (lineage_marker(cfg), bell_marker(cfg)):
        if m and m not in out:
            out.append(m)
    for m in wcfg.get("machine_line_markers") or config.DEFAULT_MACHINE_LINE_MARKERS:
        m = str(m).strip()
        if m and m not in out:
            out.append(m)
    return out


def _user_text(msg: dict) -> str | None:
    """Real user-typed text of a user-role message, or None when this envelope is
    NOT a genuine user turn. A user-role message carrying only tool_result blocks
    (role=user tool-result envelope — Claude Code wraps every MCP/tool return this
    way) has no text block: _user_text returns None so it never resets the silence
    clock (tonight's tool-result-as-user-presence bug). Only a message with actual
    text content yields a (possibly empty) string; an all-tool_result / empty
    envelope is None = not a user turn."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        has_text_block = any(
            isinstance(b, dict) and b.get("type") == "text" for b in content)
        if not has_text_block:
            return None  # tool_result / non-text envelope, not real user speech
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)
    return None


def _entry_ts(o: dict) -> float | None:
    """Epoch seconds of a transcript entry's ISO `timestamp`, or None."""
    from datetime import datetime
    raw = o.get("timestamp")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


def last_user_message_mtime(cfg: dict) -> float | None:
    """Epoch-seconds timestamp of the LAST real user message in the current
    transcript, tail-read (never loads the whole file). Assistant turns, system
    writes and the ear-delivered injections (wake bell / tuck-in / free-round
    lines — `_line_markers`) do NOT count: those are machine writes that
    must not reset the silence timer (the "永远睡不到alarm" bug family). Returns
    None when no qualifying user message is found or the transcript is
    missing/unreadable — callers fall back to hold behaviour.

    Reads the RESIDENT transcript (resident_transcript, not bare newest — a
    headless digest must never be mistaken for the window). Scans backward in a
    growing tail (start _TAIL_BYTES, double up to _TAIL_MAX_BYTES) so a single
    huge final row (e.g. one multi-MB tool_result) can never bury the last user
    turn behind it."""
    p = resident_transcript(cfg)
    if not p:
        return None
    markers = _line_markers(cfg)
    try:
        size = p.stat().st_size
    except OSError:
        return None
    tail = _TAIL_BYTES
    while True:
        try:
            with p.open("rb") as f:
                partial = size > tail
                if partial:
                    f.seek(size - tail)
                    f.readline()  # drop the partial first line
                chunk = f.read()
        except OSError:
            return None
        latest = _scan_last_user_ts(chunk, markers)
        if latest is not None:
            return latest
        if not partial or tail >= _TAIL_MAX_BYTES:
            return None  # whole file scanned / cap hit, no qualifying user turn
        tail = min(tail * 2, _TAIL_MAX_BYTES)


def _scan_last_user_ts(chunk: bytes, markers: list[str]) -> float | None:
    """Newest real-user-message ts in a decoded jsonl byte chunk, or None."""
    latest: float | None = None
    for raw in chunk.splitlines():
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        text = _user_text(o.get("message"))
        if text is None:
            continue  # not a user turn (assistant / tool_result / empty envelope)
        if _line_starts_with_marker(text, markers):
            continue  # machine line down the ear channel, not a real user turn
        ts = _entry_ts(o)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def user_silent_min(cfg: dict) -> float | None:
    """Minutes since the last real user message (`last_user_message_mtime`), or
    None when it cannot be determined. The single silence source shared by the
    watchdog poll and the tick awake gate. WINDOW-LOCAL: reads only the resident
    cortex transcript. For all-channel silence use `global_user_silent_min`."""
    import time
    ts = last_user_message_mtime(cfg)
    return (time.time() - ts) / 60.0 if ts is not None else None


def _marrow_db_last_user_ts(cfg: dict) -> float | None:
    """Epoch seconds of the LAST user message across ALL channels (cli/tg/wx/ct),
    read from marrow's events table READ-ONLY. Machine/injected lines are already
    excluded at marrow's ingestion (transcript.rows_from_records drops harness +
    machine-marker rows), so `role='user'` is a genuine user turn on every
    channel. `timestamp` is ISO-8601 UTC (e.g. '2026-07-19T16:18:35.959Z').

    Returns None on ANY failure — missing db file, locked, schema mismatch, no
    user rows, or an unparseable timestamp. None is the caller's "unknown" and
    must NEVER be silently substituted by a window-local source (that reintroduces
    the window-only-silence bug)."""
    import sqlite3
    from datetime import datetime

    db_path = config.marrow_db_path(cfg)
    if not db_path.exists():
        return None
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT timestamp FROM events WHERE role='user' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    raw = str(row[0]).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def global_user_silent_min(cfg: dict) -> float | None:
    """Minutes since the last user message across ALL channels (marrow db). The
    user active on cli/tg/wx must not be judged silent just because THIS cortex
    window's own transcript is quiet.

    result = minutes since max(marrow-db last user ts, resident-transcript last
    user ts). The transcript term can only SHORTEN silence, never substitute for
    the db: if the marrow-db query fails (missing / locked / schema mismatch /
    no rows / bad ts) return None so the caller holds — NEVER fall back to
    transcript-only (that silently reintroduces the window-only-silence bug)."""
    import time
    db_ts = _marrow_db_last_user_ts(cfg)
    if db_ts is None:
        return None
    tx_ts = last_user_message_mtime(cfg)
    latest = db_ts if tx_ts is None else max(db_ts, tx_ts)
    return (time.time() - latest) / 60.0


def window_tokens(cfg: dict) -> int:
    """Context-window occupancy = the last assistant message's usage totals
    (input + cache read + cache creation + output). Grows with the conversation;
    drives rotate (respawn) + fuse thresholds. 0 if no transcript/usage yet.

    Read from the RESIDENT transcript, never bare newest: a headless `claude -p`
    run (marrow digest, tg cortex) sharing the projects dir is often mtime-
    newest, and measuring its occupancy misreports this window's fill to the
    fuse, the lie_down ledger and the duty fresh-vs-resume gate alike."""
    p = resident_transcript(cfg)
    if not p:
        return 0
    total = 0
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return 0
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        msg = o.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if u:
            total = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                     + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
    return total
