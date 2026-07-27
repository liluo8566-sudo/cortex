from __future__ import annotations

from cortex import db


def test_migrate_creates_all_tables(marrow_conn):
    tables = {
        row["name"]
        for row in marrow_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ct_%'"
        ).fetchall()
    }
    expected = {
        "ct_app_usage",
        "ct_category_usage",
        "ct_geofence",
        "ct_geofence_cursor",
        "ct_health",
        "ct_activity",
        "ct_collector_log",
    }
    assert expected.issubset(tables)


def test_migrate_is_idempotent(marrow_conn):
    db.migrate(marrow_conn)
    db.migrate(marrow_conn)  # must not raise


def test_migrate_adds_net_tokens_column(marrow_conn):
    cols = {row[1] for row in marrow_conn.execute("PRAGMA table_info(ct_wake_log)")}
    assert "net_tokens" in cols


def test_migrate_backfills_net_tokens_on_pre_migration_db(tmp_path):
    """A DB created before net_tokens existed (only tokens/force_slept) gets the
    column added on the next connect, existing rows left NULL (COALESCE handles
    the fallback at read time), no data loss."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE ct_wake_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, wake INTEGER NOT NULL, dry_run INTEGER NOT NULL, "
        "reasons TEXT, gated_by TEXT, explanation TEXT, tokens INTEGER, force_slept TEXT);"
    )
    conn.execute("INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES ('t',1,0,500)")
    conn.commit()
    conn.close()

    conn = db.connect_path(path)  # runs migrate()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_wake_log)")}
    assert "net_tokens" in cols
    row = conn.execute("SELECT tokens, net_tokens FROM ct_wake_log").fetchone()
    assert row["tokens"] == 500 and row["net_tokens"] is None
    conn.close()


def test_log_collector_run(marrow_conn):
    db.log_collector_run(marrow_conn, "knowledgec", ok=True)
    db.log_collector_run(marrow_conn, "geofence", ok=False, error="boom")
    rows = marrow_conn.execute("SELECT source, ok, error FROM ct_collector_log ORDER BY id").fetchall()
    assert rows[0]["source"] == "knowledgec"
    assert rows[0]["ok"] == 1
    assert rows[0]["error"] is None
    assert rows[1]["source"] == "geofence"
    assert rows[1]["ok"] == 0
    assert rows[1]["error"] == "boom"


def test_migrate_adds_shell_column_backfilled_cli(marrow_conn):
    """T1: fresh DB carries the shell column; new rows default to 'cli'."""
    cols = {row[1] for row in marrow_conn.execute("PRAGMA table_info(ct_wake_log)")}
    assert "shell" in cols
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES ('t',1,0)")
    marrow_conn.commit()
    assert marrow_conn.execute(
        "SELECT shell FROM ct_wake_log").fetchone()["shell"] == "cli"


def test_migrate_backfills_shell_on_pre_migration_db(tmp_path):
    """T1: a live-shape DB written before the shell column gets it on the next
    connect, existing rows backfilled to 'cli'. Re-running migrate is a no-op
    (idempotent) and never touches the backfilled values."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE ct_wake_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, wake INTEGER NOT NULL, dry_run INTEGER NOT NULL, "
        "reasons TEXT, gated_by TEXT, explanation TEXT, tokens INTEGER, "
        "force_slept TEXT, net_tokens INTEGER);"
    )
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept) "
        "VALUES ('t',1,0,'fuse')")
    conn.commit()
    conn.close()

    conn = db.connect_path(path)  # runs migrate()
    try:
        db.migrate(conn)  # second pass must not raise or change anything
        rows = conn.execute(
            "SELECT force_slept, shell FROM ct_wake_log").fetchall()
        assert [(r["force_slept"], r["shell"]) for r in rows] == [("fuse", "cli")]
    finally:
        conn.close()
