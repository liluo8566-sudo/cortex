"""iOS Shortcuts geofence log ingest.

Observed real file 2026-07-03 (location_log.txt, iCloud Shortcuts drive):
lines look like "HH:MM event text" (e.g. "20:11 arrived: home"), appended
by an "arrived/left" automation. No per-line or per-file date field exists
in the observed sample, and the file is not rotated per day -- so date is
NOT recoverable from the line itself.

ASSUMPTION (unverified against multi-day data): each collector run reads
only newly-appended bytes since the last run (byte-offset cursor per
file), and stamps those new lines with the local date at ingest time.
This is accurate because the Shortcut write latency is near-zero
(verified ~0s), so a
line only sits unread across a collector tick, not across days -- unless
the collector is down for a full day, in which case backlog lines would
be mis-dated to the catch-up day. Flag to the user if that gap matters.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from cortex import db
from cortex.config import geofence_file_path, get_tz

LINE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s+(.+)$")


def parse_lines(text: str) -> list[tuple[str, str, str]]:
    """Returns (time HH:MM, event text, raw_line) for lines matching the
    'HH:MM event' shape. Non-matching lines (headers, manual test lines)
    are skipped defensively.
    """
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        hh, mm, event = m.groups()
        results.append((f"{int(hh):02d}:{mm}", event.strip(), line))
    return results


def _get_cursor(conn: sqlite3.Connection, source_file: str) -> int:
    row = conn.execute(
        "SELECT byte_offset FROM ct_geofence_cursor WHERE source_file = ?", (source_file,)
    ).fetchone()
    return row["byte_offset"] if row else 0


def _set_cursor(conn: sqlite3.Connection, source_file: str, offset: int) -> None:
    conn.execute(
        "INSERT INTO ct_geofence_cursor (source_file, byte_offset) VALUES (?, ?) "
        "ON CONFLICT(source_file) DO UPDATE SET byte_offset=excluded.byte_offset",
        (source_file, offset),
    )


def _read_new_complete_lines(path, offset: int) -> tuple[bytes, int]:
    """Read bytes from offset, keeping only complete (newline-terminated)
    lines; returns (processed_bytes, new_offset)."""
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read()

    if not chunk:
        return b"", offset
    if chunk.endswith(b"\n"):
        return chunk, offset + len(chunk)
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return b"", offset  # partial line only, wait for more
    processed = chunk[: last_nl + 1]
    return processed, offset + len(processed)


def collect(conn: sqlite3.Connection, cfg: dict) -> None:
    if not cfg["geofence"].get("enabled"):
        return

    path = geofence_file_path(cfg)
    if path is None:
        raise ValueError("geofence.enabled=true but paths.geofence_file is empty")
    if not path.exists():
        raise FileNotFoundError(f"geofence file not found at {path}")

    tz = get_tz(cfg)
    source_file = str(path)
    offset = _get_cursor(conn, source_file)
    size = path.stat().st_size
    if size < offset:
        offset = 0  # file truncated or rotated, restart from beginning

    processed_bytes, new_offset = _read_new_complete_lines(path, offset)
    entries = parse_lines(processed_bytes.decode("utf-8", errors="replace"))

    today = datetime.now(tz).date().isoformat()
    now = db.utcnow_iso()
    for time_str, event, raw_line in entries:
        conn.execute(
            "INSERT INTO ct_geofence (date, time, event, raw_line, source_file, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(date, time, event) DO NOTHING",
            (today, time_str, event, raw_line, source_file, now),
        )
    _set_cursor(conn, source_file, new_offset)
    conn.commit()
