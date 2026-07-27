"""Sectioned wakeup note file: one section per shell, no shell may clobber
another, headings are display-only."""
from __future__ import annotations

from cortex import note_file


def _p(tmp_path):
    return tmp_path / "wakeup_note.md"


def test_heading_with_and_without_sid():
    assert note_file.heading("cli", "45ce3c2c-fde9-4dab") == "## cli · sid=45ce3c2c"
    assert note_file.heading("tg", None) == "## tg"
    assert note_file.heading("tg", "  ") == "## tg"


def test_write_section_creates_attributed_section(tmp_path):
    p = _p(tmp_path)
    note_file.write_section(p, "cli", "[SIG]\nNow: 20:19 Mon\nActive: Code",
                            "45ce3c2c-fde9")
    assert p.read_text() == (
        "## cli · sid=45ce3c2c\n[SIG]\nNow: 20:19 Mon\nActive: Code\n")


def test_second_shell_appends_and_first_survives(tmp_path):
    p = _p(tmp_path)
    note_file.write_section(p, "cli", "cli-body", "aaaaaaaabbbb")
    note_file.write_section(p, "tg", "tg-body", "ccccccccdddd")
    assert p.read_text() == (
        "## cli · sid=aaaaaaaa\ncli-body\n\n## tg · sid=cccccccc\ntg-body\n")


def test_rewrite_keeps_order_and_other_shells_sid(tmp_path):
    p = _p(tmp_path)
    note_file.write_section(p, "cli", "cli-1", "aaaaaaaabbbb")
    note_file.write_section(p, "tg", "tg-1", "ccccccccdddd")
    note_file.write_section(p, "cli", "cli-2", "eeeeeeeeffff")
    assert p.read_text() == (
        "## cli · sid=eeeeeeee\ncli-2\n\n## tg · sid=cccccccc\ntg-1\n")


def test_legacy_headless_blob_is_dropped(tmp_path):
    """A pre-section single-blob file belongs to no shell — it must not survive
    as an unattributed lump at the top."""
    p = _p(tmp_path)
    p.write_text("[SIG]\nNow: stale\n")
    note_file.write_section(p, "cli", "fresh")
    assert p.read_text() == "## cli\nfresh\n"


def test_read_section_strips_heading_and_isolates_shells(tmp_path):
    p = _p(tmp_path)
    note_file.write_section(p, "cli", "cli-body", "aaaaaaaa")
    note_file.write_section(p, "tg", "tg-line-1\ntg-line-2", "cccccccc")
    text = p.read_text()
    assert note_file.read_section(text, "cli") == "cli-body"
    assert note_file.read_section(text, "tg") == "tg-line-1\ntg-line-2"
    assert note_file.read_section(text, "wx") is None
    assert note_file.read_section("headless blob", "cli") is None


def test_body_keeps_its_own_line_breaks(tmp_path):
    """Three body lines stay three lines — no blank line is inserted between
    them (the file is read in source view)."""
    p = _p(tmp_path)
    note_file.write_section(p, "cli", "a\nb\nc")
    assert p.read_text() == "## cli\na\nb\nc\n"
