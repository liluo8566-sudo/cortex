"""On-disk wakeup note: one file, one section per shell.

Every shell (cli / tg / wx) renders its OWN note, so a single-blob file meant
whichever shell wrote last won and a human reading it could not tell whose data
it was. The file is now sectioned:

    ## cli · sid=45ce3c2c
    [AUTOMATED WAKE SIGNAL — ...]
    Now: 20:19 Mon | Last active: 3min ago

    ## tg · sid=5982c26a
    [AUTOMATED WAKE SIGNAL — ...]
    Now: 20:21 Mon | Last active: 1min ago

Rules: the heading is DISPLAY-ONLY (never part of injected text); no blank line
inside a section body; one blank line between sections; existing sections keep
their order and a new shell appends at the end. Sids are all-or-nothing across
the file — one shell whose sid cannot be resolved degrades EVERY heading to a
bare `## <shell>`, so the reader never has to wonder why one section is labelled
and its neighbour is not. Writes take an exclusive flock
on `<path>.lock` and read-modify-write + atomic replace, so one shell's write
can never drop another shell's section.

marrow reads the same file (its own tiny parser — separate repo, no shared
import); the heading shape below is the cross-repo contract.
"""
from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from pathlib import Path

_HEADING_PREFIX = "## "
_SID_PREFIX = "sid="
_SEP = " · "
_SID_LEN = 8


def heading(shell: str, sid: str | None = None) -> str:
    """`## <shell> · sid=<8 chars>`; no sid available -> `## <shell>` (never
    fabricate one)."""
    sid = (sid or "").strip()
    if not sid:
        return f"{_HEADING_PREFIX}{shell}"
    return f"{_HEADING_PREFIX}{shell}{_SEP}{_SID_PREFIX}{sid[:_SID_LEN]}"


def _shell_of(line: str) -> str | None:
    """Shell id of a heading line, or None when the line is not a heading."""
    if not line.startswith(_HEADING_PREFIX):
        return None
    rest = line[len(_HEADING_PREFIX):].strip()
    return rest.split(_SEP)[0].strip().split()[0] if rest else None


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(shell, body)] in file order. Content before the first heading (a legacy
    unattributed blob) is dropped — it belongs to no shell."""
    out: list[tuple[str, str]] = []
    shell: str | None = None
    buf: list[str] = []
    for line in (text or "").splitlines():
        s = _shell_of(line)
        if s is not None:
            if shell is not None:
                out.append((shell, "\n".join(buf).strip()))
            shell, buf = s, []
            continue
        if shell is not None:
            buf.append(line)
    if shell is not None:
        out.append((shell, "\n".join(buf).strip()))
    return out


def read_section(text: str, shell: str) -> str | None:
    """This shell's body with the heading stripped; None when absent/empty."""
    for s, body in split_sections(text):
        if s == shell:
            return body or None
    return None


def render_file(sections: list[tuple[str, str, str | None]]) -> str:
    """[(shell, body, sid)] -> full file text. All-or-nothing sids: if any
    rendered section has no sid, none of the headings carry one."""
    kept = [(sh, body, sid) for sh, body, sid in sections if body]
    if not all((sid or "").strip() for _, _, sid in kept):
        kept = [(sh, body, None) for sh, body, _ in kept]
    blocks = [f"{heading(sh, sid)}\n{body}".rstrip() for sh, body, sid in kept]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def write_section(path: str | os.PathLike[str], shell: str, body: str,
                  sid: str | None = None,
                  sid_for: Callable[[str], str | None] | None = None) -> None:
    """Replace THIS shell's section, leaving every other section untouched and
    in place; a new shell appends at the end. Exclusive flock + atomic replace
    so concurrent shells cannot clobber each other.

    `sid_for(shell)` re-resolves the OTHER sections' sids live (the file's own
    recorded value is the fallback). Without it the all-or-nothing rule would
    ratchet: one write by a shell with no sid strips every label out of the file
    and no later write could ever bring them back."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (body or "").strip()
    lock = open(str(p) + ".lock", "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            existing = p.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        sections: list[tuple[str, str, str | None]] = []
        seen = False
        for s, b in split_sections(existing):
            if s == shell:
                sections.append((shell, body, sid))
                seen = True
            else:
                other = None
                if sid_for is not None:
                    try:
                        other = sid_for(s)
                    except Exception:
                        other = None
                sections.append((s, b, other or _sid_of(existing, s)))
        if not seen:
            sections.append((shell, body, sid))
        _atomic_write(p, render_file(sections))
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _sid_of(text: str, shell: str) -> str | None:
    """Preserve another shell's recorded sid across a rewrite."""
    for line in (text or "").splitlines():
        if _shell_of(line) != shell:
            continue
        _, _, tail = line.partition(_SEP)
        tail = tail.strip()
        if tail.startswith(_SID_PREFIX):
            return tail[len(_SID_PREFIX):].strip() or None
        return None
    return None


def _atomic_write(p: Path, text: str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
