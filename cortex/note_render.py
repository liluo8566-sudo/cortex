"""Render-only CLI: print a FRESH wakeup note to stdout, no side effects.

The wake-time note is assembled once and frozen to disk; a rotated window then
gets a stale file. This entry re-renders at injection time so "Now:" and the
Window SID always reflect the caller's current moment and transcript.

Contract: read-only by default. No ct_wake_log writes, no wake_state writes, no
shell ledger writes, no Replay (marrow's turn_inject is the single replay
channel). --transcript supplies the Window-line SID (Path(...).stem[:8]) — the
caller's own transcript, correct even after rotation. Print the note; exit 0.

--mirror is the ONE opt-in write: it also stores the rendered note in this
shell's section of the on-disk wakeup note (note_file.write_section, other
shells untouched). Every shell that renders through this entry — marrow's
injection path and the synapse-tg shell host — gets its own attributed section
from the same code, so no repo has to duplicate the file format.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from cortex import config, db, note


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a fresh wakeup note.")
    parser.add_argument("--transcript", default=None,
                        help="caller transcript path; stem[:8] -> Window SID")
    parser.add_argument("--no-ct", action="store_true",
                        help="skip ct-note peek — the marrow hook delivers ct "
                             "notes via outbox.deliver, so rendering them here "
                             "would double them in the same payload")
    parser.add_argument("--shell", default=None,
                        help="shell id this note is rendered for; scopes the "
                             "wake ledger read. Unset = the unqualified (cli) "
                             "render")
    parser.add_argument("--mirror", action="store_true",
                        help="also write the rendered note into this shell's "
                             "section of the on-disk wakeup note file")
    args = parser.parse_args()

    cfg = config.load()
    tz = config.get_tz(cfg)
    now = datetime.now(tz)

    window_sid = None
    if args.transcript:
        window_sid = Path(str(args.transcript)).stem[:8]

    conn = db.connect(cfg)
    try:
        data = note.gather(conn, cfg, now, window_sid=window_sid,
                           shell=args.shell)
        if args.no_ct:
            data["ct_notes"] = []
        text = note.render(cfg, now, data)
        if args.mirror:
            from cortex import note_file, wake_state
            shell = args.shell or note.CLI_SHELL
            # Heading sid = this shell's recorded claude sid (the same id the
            # note's Last-active line is read for); the caller's transcript stem
            # is only the fallback, so every shell labels its section alike.
            sid = wake_state.shell_claude_sid(cfg, shell) or window_sid
            note_file.write_section(
                wake_state.wakeup_note_path(cfg), shell, text, sid,
                sid_for=lambda s: wake_state.shell_claude_sid(cfg, s))
        print(text)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
