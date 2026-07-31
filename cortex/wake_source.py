"""Why a shell woke — a one-shot line staged for the next wakeup note.

A duty rotation (ctl duty / the transfer tool) stages a rendered source line for
the shell it is about to kick; the shell's own wakeup note renders it on the way
in, so the session reads why it came up instead of guessing. An auto wake stages
nothing and its note is unchanged.

Storage follows the existing per-shell split — cli in wake_state.json, every
other shell in its `<shell_state_dir>/<shell>.json` ledger — so the line travels
with the shell it belongs to and never leaks into the other one's note.

Pending-visible, like kick reasons: a read RENDERS without clearing, and only the
render that actually delivers the note (note_render --mirror) takes it. A note
that is assembled and then discarded therefore never swallows the line.
"""

from __future__ import annotations

import logging

from cortex import config

logger = logging.getLogger(__name__)

KEY = "wake_source"

CLI_SHELL = "cli"

KIND_TRANSFER = "transfer"
KIND_CTL = "ctl"

_TEMPLATE_KEY = {KIND_TRANSFER: "transfer_source_text",
                 KIND_CTL: "ctl_source_text"}


def render(cfg: dict, kind: str, *, shell: str, hold: str | None,
           from_shell: str | None = None) -> str:
    """Fill the [duty] template for `kind`. Unknown kind or an empty template
    yields "" (no line). A bad placeholder in a user-edited template falls back
    to the raw template rather than raising — a broken string must not cost the
    wake."""
    d = cfg.get("duty") or {}
    tmpl = str(d.get(_TEMPLATE_KEY.get(kind, "")) or "").strip()
    if not tmpl:
        return ""
    fields = {"shell": shell,
              "from_shell": from_shell or "",
              "hold": hold or str(d.get("hold_none_label") or "no")}
    try:
        return tmpl.format(**fields)
    except (KeyError, IndexError, ValueError):
        return tmpl


def stage(cfg: dict, shell: str, text: str) -> None:
    """Park `text` as the next note's source line for `shell`. Empty text is a
    no-op; best-effort, since a lost tag must never cost the kick itself."""
    text = str(text or "").strip()
    if not text:
        return
    try:
        _write(cfg, shell, text)
    except Exception as e:  # noqa: BLE001 — the wake outranks its own label
        logger.warning("wake source staging failed for %s (%s)", shell, e)


def peek(cfg: dict, shell: str | None = None) -> str:
    """Staged line without clearing it — the render path that may be discarded."""
    try:
        return str(_read(cfg, shell or CLI_SHELL) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def take(cfg: dict, shell: str | None = None) -> str:
    """Read and clear — the delivering render. A clear that fails still returns
    the line: a repeated tag beats a lost one."""
    shell = shell or CLI_SHELL
    text = peek(cfg, shell)
    if text:
        try:
            _write(cfg, shell, None)
        except Exception as e:  # noqa: BLE001
            logger.warning("wake source clear failed for %s (%s)", shell, e)
    return text


def _write(cfg: dict, shell: str, text: str | None) -> None:
    shell = str(shell).strip().lower()
    if shell == CLI_SHELL:
        from cortex import wake_state
        wake_state.update(cfg, **{KEY: text})
        return
    from cortex import shell_ledger
    shell_ledger.write(config.shell_state_dir(cfg), shell, {KEY: text})


def _read(cfg: dict, shell: str) -> str | None:
    shell = str(shell).strip().lower()
    if shell == CLI_SHELL:
        from cortex import wake_state
        return wake_state.load(cfg).get(KEY)
    from cortex import shell_ledger
    return shell_ledger.read(config.shell_state_dir(cfg), shell).get(KEY)
