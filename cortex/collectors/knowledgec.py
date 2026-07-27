"""macOS knowledgeC.db app-usage collector.

Query verified live against a real knowledgeC.db 2026-07-03 (Full Disk
Access granted): ZOBJECT rows with ZSTREAMNAME='/app/usage' carry one row
per app-focus session, ZVALUESTRING = bundle id, ZSTARTDATE/ZENDDATE =
CoreData timestamps (seconds since 2001-01-01 UTC, offset 978307200 from
Unix epoch). This is the standard documented ZOBJECT/ZSTREAMNAME shape.

Re-runnable: each run recomputes per-day aggregates from source rows and
upserts (REPLACE), so results always match knowledgeC for that day.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, tzinfo

from cortex import db
from cortex.config import knowledgec_db_path, get_tz

COREDATA_EPOCH_OFFSET = 978307200  # 2001-01-01T00:00:00Z in Unix seconds


def _open_readonly(path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _local_date(unix_ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone(tz).date().isoformat()


def read_usage_rows(conn: sqlite3.Connection, stream_name: str) -> list[tuple[str, float, float]]:
    """Returns (bundle_id, start_unix, end_unix) tuples."""
    cur = conn.execute(
        "SELECT ZVALUESTRING, ZSTARTDATE, ZENDDATE FROM ZOBJECT "
        "WHERE ZSTREAMNAME = ? AND ZVALUESTRING IS NOT NULL "
        "AND ZSTARTDATE IS NOT NULL AND ZENDDATE IS NOT NULL",
        (stream_name,),
    )
    rows = []
    for bundle_id, start, end in cur.fetchall():
        rows.append((bundle_id, start + COREDATA_EPOCH_OFFSET, end + COREDATA_EPOCH_OFFSET))
    return rows


def aggregate(rows: list[tuple[str, float, float]], tz: tzinfo, categories: dict[str, str]) -> tuple[dict, dict]:
    """Aggregate seconds per (date, bundle_id) and (date, category)."""
    app_seconds: dict[tuple[str, str], float] = defaultdict(float)
    for bundle_id, start_unix, end_unix in rows:
        duration = max(0.0, end_unix - start_unix)
        if duration == 0:
            continue
        date = _local_date(start_unix, tz)
        app_seconds[(date, bundle_id)] += duration

    category_seconds: dict[tuple[str, str], float] = defaultdict(float)
    default_category = categories.get("default", "uncategorized")
    for (date, bundle_id), seconds in app_seconds.items():
        category = categories.get(bundle_id, default_category)
        category_seconds[(date, category)] += seconds

    return app_seconds, category_seconds


def collect(marrow_conn: sqlite3.Connection, cfg: dict) -> None:
    kc_path = knowledgec_db_path(cfg)
    if not kc_path.exists():
        raise FileNotFoundError(f"knowledgeC.db not found at {kc_path}")

    stream_name = cfg["knowledgec"].get("stream_name", "/app/usage")
    categories = cfg["knowledgec"].get("categories", {})
    tz = get_tz(cfg)

    kc_conn = _open_readonly(kc_path)
    try:
        rows = read_usage_rows(kc_conn, stream_name)
    finally:
        kc_conn.close()

    app_seconds, category_seconds = aggregate(rows, tz, categories)
    now = db.utcnow_iso()

    for (date, bundle_id), seconds in app_seconds.items():
        marrow_conn.execute(
            "INSERT INTO ct_app_usage (date, bundle_id, seconds, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date, bundle_id) DO UPDATE SET seconds=excluded.seconds, updated_at=excluded.updated_at",
            (date, bundle_id, seconds, now),
        )
    for (date, category), seconds in category_seconds.items():
        marrow_conn.execute(
            "INSERT INTO ct_category_usage (date, category, seconds, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date, category) DO UPDATE SET seconds=excluded.seconds, updated_at=excluded.updated_at",
            (date, category, seconds, now),
        )
    marrow_conn.commit()
