"""Health export ingest — tolerant skeleton.

Fields are UNKNOWN yet (waits on the user's export shortcut/app). Philosophy: show what exists. Every
top-level key in the JSON export becomes one raw row, date-stamped by the
export's own date (or file mtime if the export has none), so a fresh
session can tell a stale export from a fresh one. No field-specific
parsing -- that waits until the real export shape is known.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, tzinfo

from cortex import db
from cortex.config import health_export_path, get_tz


def _flatten(prefix: str, value, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten(key, v, out)
    else:
        out[prefix] = json.dumps(value) if not isinstance(value, str) else value


def _export_date(payload: dict, path, tz: tzinfo) -> str:
    for date_key in ("date", "export_date", "day"):
        if isinstance(payload.get(date_key), str):
            return payload[date_key][:10]
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone(tz).date().isoformat()


def collect(conn: sqlite3.Connection, cfg: dict) -> None:
    if not cfg["health"].get("enabled"):
        return

    path = health_export_path(cfg)
    if path is None:
        raise ValueError("health.enabled=true but paths.health_export is empty")
    if not path.exists():
        raise FileNotFoundError(f"health export not found at {path}")

    tz = get_tz(cfg)
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise ValueError("health export root must be a JSON object")

    date = _export_date(payload, path, tz)
    flat: dict[str, str] = {}
    _flatten("", payload, flat)

    now = db.utcnow_iso()
    for key, value in flat.items():
        if not key:
            continue
        conn.execute(
            "INSERT INTO ct_health (date, source, key, value, ingested_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(date, source, key) DO UPDATE SET value=excluded.value, ingested_at=excluded.ingested_at",
            (date, str(path), key, value, now),
        )
    conn.commit()
