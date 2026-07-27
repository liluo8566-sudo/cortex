"""P17 night: all-channel silence source (transcript.global_user_silent_min).

The night self-check must NOT judge the user silent just because THIS cortex
window's own transcript is quiet — the user active on cli/tg/wx all night is
still present. Source = max(marrow-db last user ts over ALL channels, resident
transcript last user ts). The transcript term may only SHORTEN silence; a
marrow-db failure returns None (hold), NEVER a transcript-only fallback.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from cortex import config, transcript


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


def _make_db(cfg, rows):
    """rows = list of (role, channel, iso_ts). Minimal events table."""
    path = config.marrow_db_path(cfg)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT NOT NULL, timestamp TEXT NOT NULL, role TEXT NOT NULL, "
        "content TEXT NOT NULL, channel TEXT)"
    )
    conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) "
        "VALUES ('s', ?, ?, 'x', ?)",
        [(ts, role, ch) for (role, ch, ts) in rows],
    )
    conn.commit()
    conn.close()


def _iso(ago_min, *, zulu=True):
    dt = datetime.now(timezone.utc) - timedelta(minutes=ago_min)
    s = dt.isoformat()
    if zulu:
        s = s.replace("+00:00", "Z")
    return s


def _no_transcript(monkeypatch):
    monkeypatch.setattr(transcript, "last_user_message_mtime", lambda cfg: None)


def _transcript_ago(monkeypatch, ago_min):
    ts = time.time() - ago_min * 60.0
    monkeypatch.setattr(transcript, "last_user_message_mtime", lambda cfg: ts)


# --- db is the primary source -------------------------------------------------

def test_db_only_all_channels(cfg, monkeypatch):
    _no_transcript(monkeypatch)
    _make_db(cfg, [
        ("user", "cli", _iso(60)),
        ("user", "tg", _iso(10)),   # newest user turn = 10min ago (tg)
        ("assistant", "cli", _iso(1)),  # assistant must not count
    ])
    assert 9.5 < transcript.global_user_silent_min(cfg) < 10.5


def test_ct_channel_user_counts(cfg, monkeypatch):
    """A user message typed into cortex's own window (channel 'ct') is real
    presence and must reset the timer."""
    _no_transcript(monkeypatch)
    _make_db(cfg, [("user", "cli", _iso(60)), ("user", "ct", _iso(5))])
    assert 4.5 < transcript.global_user_silent_min(cfg) < 5.5


# --- combine: max(db, transcript) ---------------------------------------------

def test_db_newer_wins(cfg, monkeypatch):
    _transcript_ago(monkeypatch, 40)   # transcript stale
    _make_db(cfg, [("user", "cli", _iso(5))])   # db fresher
    assert 4.5 < transcript.global_user_silent_min(cfg) < 5.5


def test_transcript_newer_wins(cfg, monkeypatch):
    _transcript_ago(monkeypatch, 3)    # transcript fresher (shortens silence)
    _make_db(cfg, [("user", "cli", _iso(30))])
    assert 2.5 < transcript.global_user_silent_min(cfg) < 3.5


# --- failure modes => None (NEVER transcript fallback) ------------------------

def test_missing_db_returns_none_even_with_transcript(cfg, monkeypatch):
    _transcript_ago(monkeypatch, 3)    # transcript HAS fresh data...
    # ...but no db file exists -> must still be None, not the transcript value.
    assert transcript.global_user_silent_min(cfg) is None


def test_no_user_rows_returns_none_even_with_transcript(cfg, monkeypatch):
    _transcript_ago(monkeypatch, 3)
    _make_db(cfg, [("assistant", "cli", _iso(2))])  # only assistant rows
    assert transcript.global_user_silent_min(cfg) is None


def test_schema_mismatch_returns_none(cfg, monkeypatch):
    _transcript_ago(monkeypatch, 3)
    path = config.marrow_db_path(cfg)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE other (x INTEGER)")   # no events table
    conn.commit()
    conn.close()
    assert transcript.global_user_silent_min(cfg) is None


def test_bad_timestamp_returns_none(cfg, monkeypatch):
    _transcript_ago(monkeypatch, 3)
    _make_db(cfg, [("user", "cli", "not-a-timestamp")])
    assert transcript.global_user_silent_min(cfg) is None


# --- timezone handling --------------------------------------------------------

def test_zulu_timestamp_parsed(cfg, monkeypatch):
    _no_transcript(monkeypatch)
    _make_db(cfg, [("user", "cli", _iso(15, zulu=True))])   # '...Z'
    assert 14.0 < transcript.global_user_silent_min(cfg) < 16.0


def test_offset_timestamp_parsed(cfg, monkeypatch):
    _no_transcript(monkeypatch)
    _make_db(cfg, [("user", "cli", _iso(15, zulu=False))])  # '...+00:00'
    assert 14.0 < transcript.global_user_silent_min(cfg) < 16.0


def test_naive_timestamp_treated_as_utc(cfg, monkeypatch):
    _no_transcript(monkeypatch)
    naive = (datetime.now(timezone.utc) - timedelta(minutes=15)) \
        .replace(tzinfo=None).isoformat()
    _make_db(cfg, [("user", "cli", naive)])
    assert 14.0 < transcript.global_user_silent_min(cfg) < 16.0
