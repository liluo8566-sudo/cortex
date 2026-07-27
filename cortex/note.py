"""Wakeup note: the床头字条 (bedside note) handed to a cortex session on wake.

gather() is the I/O layer — DB reads plus a best-effort macOS frontmost-app
probe. Every external source is wrapped in try/except so a failure omits its
line rather than crashing the wake. render() is pure — no I/O, no DB — so it
can be unit-tested with synthetic data.

Layout: a header block (Now / Active), then `---`-separated blocks
for pending self-schedule. The handoff injects at
SessionStart (marrow), not here. Cal/Rem lines retired (global inject pending).
The old "Wake:" reason line is gone — reasons carry no signal (desire engine
retired, wander-only).
"""
from __future__ import annotations

import json
import subprocess
import sqlite3
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from cortex import config
from cortex.occupancy import parse_due_at

# A wake row younger than this many seconds is treated as *this* wake (the tick
# logs it before the note is assembled), so "Last wake" reports the one before.
_CURRENT_WAKE_EPSILON_S = 90


def _tz(cfg: dict) -> tzinfo:
    return config.get_tz(cfg)


def _parse_utc(ts_iso: str) -> datetime:
    return datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))


def _local_hm(ts_iso: str, cfg: dict) -> str:
    return _parse_utc(ts_iso).astimezone(_tz(cfg)).strftime("%H:%M")


def _note_cfg(cfg: dict) -> dict:
    return cfg.get("note", {}) or {}


class _ClampDefaults(dict):
    """format_map source that leaves unknown placeholders literal, so a custom
    turn_end_text template never crashes on a placeholder not in the clamp set."""
    def __missing__(self, key):
        return "{" + key + "}"


def _consume_kick_reasons(cfg: dict, ws: dict, settle: bool = True) -> list[str]:
    """Read the pending kick reason flags (cortex.kick appended them under the
    strict lock). settle=True CLEARS exactly those from wake_state (a real
    injection settles them); settle=False PEEKS — renders them without clearing
    so a passive re-render / a frozen note that may be discarded surfaces them
    again (pending-visible; at-least-once). Called only by consume_kick=True
    paths. Reasons that arrived between the load and the clear are preserved
    (list-tail re-read). Best-effort: any lock failure returns the loaded reasons
    WITHOUT clearing — a duplicate reason line beats a lost wake signal."""
    reasons = ws.get("kick_reasons")
    if not isinstance(reasons, list) or not reasons:
        return []
    reasons = [str(r) for r in reasons if str(r).strip()]
    if not reasons:
        return []
    if not settle:
        return reasons
    n = len(reasons)
    try:
        from cortex import wake_state

        def _clear(d: dict):
            cur = d.get("kick_reasons")
            if isinstance(cur, list):
                d["kick_reasons"] = cur[n:]
                if not d["kick_reasons"]:
                    d.pop("kick_reasons", None)
            return None

        wake_state.conditional_mutate(cfg, None, _clear)
    except Exception:
        pass
    return reasons


def _receipt_channel(cfg: dict) -> str:
    """from_channel tag on cortex-authored outbox notes (msg tool stamps it)."""
    return str(_note_cfg(cfg).get("cortex_channel") or "ct")


def _consume_receipts(cfg: dict, conn: sqlite3.Connection, now: datetime,
                      settle: bool = True) -> list[str]:
    """Reply receipts (P12/C11): outbox notes cortex sent that she has replied to
    but not yet shown (receipt_seen=0, replied_at NOT NULL). Render one C11 line
    each. settle=True stamps receipt_seen=1 (a real injection settles it, so the
    receipt shows exactly once); settle=False PEEKS — renders without stamping so
    a passive re-render / discarded frozen note surfaces it again. Called only
    from consume_kick=True paths; render-only re-renders never stamp. Best-effort:
    any DB error -> no receipts, no stamp. Blank template -> receipts disabled."""
    tmpl = str(_note_cfg(cfg).get("receipt_line") or "").strip()
    if not tmpl or conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id, target, sent_at, replied_at, reply_text FROM outbox"
            " WHERE from_channel=? AND replied_at IS NOT NULL AND receipt_seen=0"
            " ORDER BY id",
            (_receipt_channel(cfg),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []
    lines: list[str] = []
    for r in rows:
        sent_hm = _local_hm(r["sent_at"], cfg) if r["sent_at"] else "?"
        replied_hm = _local_hm(r["replied_at"], cfg) if r["replied_at"] else "?"
        text = " ".join((r["reply_text"] or "").split())
        try:
            lines.append(tmpl.format(id=r["id"], channel=r["target"] or "?",
                                     sent_hm=sent_hm, replied_hm=replied_hm,
                                     text=text))
        except (KeyError, IndexError, ValueError):
            lines.append(tmpl)
    if settle:
        try:
            ids = [r["id"] for r in rows]
            qmarks = ",".join("?" for _ in ids)
            with conn:
                conn.execute(
                    f"UPDATE outbox SET receipt_seen=1 WHERE id IN ({qmarks})",
                    ids)
        except sqlite3.Error:
            pass
    return lines


def _render_ct_note(cfg: dict, row: sqlite3.Row) -> str:
    """One ct-targeted outbox note: [outbox].inject_header + body verbatim. Mirror
    of marrow outbox._render_note so hook-delivered and note-delivered notes read
    identically (same header, same placeholders channel/sid4/time)."""
    tmpl = str((cfg.get("outbox", {}) or {}).get("inject_header") or "")
    from_sid = row["from_sid"] or ""
    sid4 = from_sid[:4] if from_sid else "????"
    hhmm = _local_hm(row["created_at"], cfg) if row["created_at"] else ""
    if tmpl:
        try:
            header = tmpl.format(channel=row["from_channel"] or "?", sid4=sid4,
                                 time=hhmm)
        except (KeyError, IndexError, ValueError):
            header = tmpl
        return f"{header}\n{row['body']}"
    return str(row["body"] or "")


def _consume_ct_notes(cfg: dict, conn: sqlite3.Connection,
                      claimed_by: str = "cortex.note",
                      settle: bool = False) -> list[str]:
    """Claim + render outbox notes targeting the cortex window (target='ct').
    At-most-once claim via the SAME atomic UPDATE marrow's hook uses (WHERE
    status='pending', guarded on rowcount) — exactly-one-owner. Also re-renders
    rows still in the 'claimed' pending-visible state (a prior assemble froze
    them into a note that never reached the model) so the payload surfaces again.

    settle (default False): the claim leaves the row 'claimed' = pending-visible;
    ONLY a real injection settles it to 'sent'. The free-round path passes
    settle=True (its ear write is the injection); the wake-assemble path leaves
    settle=False so the marrow hook (the actual injector) settles it. Called only
    from VISIBLE-round paths — never a passive re-render or off-screen tick.
    `claimed_by` stamps the audit column. Best-effort."""
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id FROM outbox WHERE status='pending' AND target='ct'"
            " ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    owned: list[int] = []
    for r in rows:
        rid = r["id"]
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE outbox SET status='claimed', claimed_by=?,"
                    " claimed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id=? AND status='pending'",
                    (claimed_by, rid))
            if cur.rowcount != 1:
                continue  # lost the claim race (hook took it) — skip
            owned.append(rid)
        except sqlite3.Error:
            continue
    try:
        prior = conn.execute(
            "SELECT id FROM outbox WHERE status='claimed' AND target='ct'"
            " ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        prior = []
    ids = sorted({r["id"] for r in prior} | set(owned))
    delivered: list[str] = []
    for rid in ids:
        try:
            full = conn.execute(
                "SELECT id, created_at, from_sid, from_channel, body"
                " FROM outbox WHERE id=?", (rid,)).fetchone()
            if full is not None:
                delivered.append(_render_ct_note(cfg, full))
        except sqlite3.Error:
            continue
    if settle and ids:
        try:
            qmarks = ",".join("?" for _ in ids)
            with conn:
                conn.execute(
                    "UPDATE outbox SET status='sent',"
                    " sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id IN (" + qmarks + ")", ids)
        except sqlite3.Error:
            pass
    return delivered


def claim_ct_notes_text(cfg: dict, conn: sqlite3.Connection,
                        claimed_by: str) -> str:
    """Claim + render pending ct-notes as one joined block (own connection at the
    caller). Used by the free-round path to consume notes ONLY after its ear write
    has committed — so an off-screen tick never swallows a note. Returns "" when
    none. Best-effort: any error -> "" (no claim)."""
    try:
        notes = _consume_ct_notes(cfg, conn, claimed_by=claimed_by, settle=True)
    except Exception:
        return ""
    return "\n\n".join(n for n in notes if n)


def _peek_ct_notes(cfg: dict, conn: sqlite3.Connection) -> list[str]:
    """Read-only render of pending-visible ct notes (status='claimed', not yet
    settled to 'sent'). A passive re-render (marrow render_module / --print-note /
    SessionStart) shows an un-injected note again WITHOUT claiming, settling, or
    mutating any row. Best-effort -> []."""
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id, created_at, from_sid, from_channel, body"
            " FROM outbox WHERE status='claimed' AND target='ct' ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_render_ct_note(cfg, r) for r in rows]


# --------------------------------------------------------------------------- #
# DB-sourced facts
# --------------------------------------------------------------------------- #

def _last_wake(conn: sqlite3.Connection, now: datetime,
               shell: str | None = None) -> dict | None:
    """Previous wake=1 row's age + force_slept marker for THIS shell (rows carry
    a shell column, default 'cli'), so one shell's force-slept marker can never
    surface on another shell's page. Skips a row logged for the current wake
    (younger than the epsilon)."""
    try:
        rows = conn.execute(
            "SELECT ts, force_slept FROM ct_wake_log WHERE wake = 1 AND shell = ? "
            "ORDER BY ts DESC LIMIT 3",
            (shell or CLI_SHELL,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        try:
            age = now - _parse_utc(row["ts"])
        except (TypeError, ValueError):
            continue
        if age.total_seconds() < _CURRENT_WAKE_EPSILON_S:
            continue
        return {
            "minutes_ago": int(age.total_seconds() // 60),
            "force_slept": row["force_slept"],
            "ts": row["ts"],
        }
    return None


def _last_active(conn: sqlite3.Connection, cfg: dict, now: datetime,
                 shell: str | None = None) -> dict | None:
    """Age of THIS shell's last reply, from the newest ct_activity row whose
    channel is the shell id (cli / tg / wx). Cortex stopped being a standalone
    channel when it moved inside the tg/cli shells, so the activity rows now
    carry the host shell's channel. At inject time the current turn's Stop has
    not fired, so the newest row is the previous reply — no epsilon skip needed.
    None when the table is absent or has no row for this shell."""
    try:
        row = conn.execute(
            "SELECT MAX(ts) AS ts FROM ct_activity WHERE channel = ?",
            (shell or CLI_SHELL,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row["ts"]:
        return None
    try:
        age = now - _parse_utc(row["ts"])
    except (TypeError, ValueError):
        return None
    return {"minutes_ago": int(age.total_seconds() // 60), "ts": row["ts"]}


CLI_SHELL = "cli"



# --------------------------------------------------------------------------- #
# External best-effort facts (cadence CLI, osascript, handoff file)
# --------------------------------------------------------------------------- #

def _frontmost_app() -> str | None:
    """macOS frontmost application name. Locked screen / login window / any
    failure -> None (line omitted)."""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application '
             'process whose frontmost is true'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = out.stdout.strip()
    if out.returncode != 0 or not name or name in ("loginwindow",):
        return None
    return name


def _pending(cfg: dict, now: datetime) -> list[dict]:
    """self_schedule.json entries due within pending_window_min from now.
    due_at may be tz-aware or offset-free local ISO (parse_due_at handles
    both); a garbage/unparseable entry is skipped, never crashes the note."""
    path = config.self_schedule_path(cfg)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    window = _note_cfg(cfg).get("pending_window_min", 15)
    horizon = now + timedelta(minutes=window)
    tz = _tz(cfg)
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            due = parse_due_at(item.get("due_at"), tz)
            if due is None or due > horizon:
                continue
            out.append({
                "hm": due.astimezone(tz).strftime("%H:%M"),
                "intent": item.get("intent", ""),
            })
        except (ValueError, TypeError):
            continue
    return out


# --------------------------------------------------------------------------- #
# gather / render
# --------------------------------------------------------------------------- #

def _safe(fn, *args, default=None):
    """Run one gather section; any exception -> default (section omitted).
    Belt-and-braces on top of each helper's own try/except: no data failure
    may crash note assembly (module design: best-effort throughout)."""
    try:
        return fn(*args)
    except Exception:
        return default


def gather(
    conn: sqlite3.Connection,
    cfg: dict,
    now: datetime,
    decision: dict | None = None,
    *,
    fresh: bool = False,
    wake_kind: str | None = None,
    window_sid: str | None = None,
    consume_kick: bool = False,
    claim_ct_notes: bool = True,
    settle: bool = False,
    shell: str | None = None,
) -> dict:
    """Assemble the wakeup note data dict. conn must use sqlite3.Row factory.
    `fresh`/`wake_kind` are accepted for caller compatibility; the handoff
    now injects at SessionStart, not here.

    `window_sid` (caller-supplied) overrides the wake_state transcript for the
    Window line — the caller's own transcript stem is correct even after a
    rotation, whereas wake_state.transcript was cleared at lie_down and is only
    re-set after this note is written. awake_since still comes from wake_state.

    `settle` (default False): kick reasons / receipts / ct notes are pending-
    visible — a claim/read does NOT consume them; ONLY a real payload injection
    settles them (clear/stamp/mark-sent). settle=True is passed by injection
    paths (free-round ear write). The wake-assemble path leaves settle=False (its
    frozen note may be discarded — the marrow hook is the real injector). A
    passive re-render (consume_kick=False) PEEKS all three read-only so an
    un-injected payload surfaces again — never consumes, never settles.

    `shell` (default None = the unqualified/cli render): the shell this note is
    rendered for. It scopes the wake ledger read (ct_wake_log.shell) AND the
    last-active read (ct_activity.channel), so one shell's page never shows
    another shell's rows."""
    from cortex import wake_state

    last_wake = _safe(_last_wake, conn, now, shell)
    last_active = _safe(_last_active, conn, cfg, now, shell)

    ws = {}
    try:
        ws = wake_state.load(cfg)
    except Exception:
        pass
    awake_since_hm = None
    if not window_sid:
        transcript_raw = ws.get("transcript")
        if transcript_raw:
            try:
                window_sid = Path(str(transcript_raw)).stem[:8]
            except Exception:
                pass
    since_raw = ws.get("awake_since")
    if since_raw:
        try:
            since_dt = _parse_utc(since_raw)
            awake_since_hm = since_dt.astimezone(_tz(cfg)).strftime("%H:%M")
        except (TypeError, ValueError):
            pass
    # Kick reason flags (cortex.kick): plain lines (no header). Pending-visible:
    # peek (render, no clear) on any read; settle (clear) only when an injection
    # path passes consume_kick+settle. A passive re-render (consume_kick=False)
    # always peeks so an un-injected kick reason surfaces again — never lost to a
    # discarded frozen note.
    kick_reasons = _consume_kick_reasons(cfg, ws, settle=consume_kick and settle)
    # Reply receipts (C11): same pending-visible rule — only an injection stamps
    # receipt_seen, so a passive re-render peeks without stamping.
    receipts = _consume_receipts(cfg, conn, now, settle=consume_kick and settle)
    # ct-targeted outbox notes (F9): claimed + rendered here only on a VISIBLE
    # round — the wake-bell payload (claim_ct_notes default True). The background
    # free-round render passes claim_ct_notes=False and claims separately AFTER
    # its ear write commits (see watchdog._deliver_ct_notes_to_ear), so a tick that
    # renders off-screen never swallows a note no turn will show.
    if consume_kick and claim_ct_notes:
        # Wake-bell payload: claim pending ct notes into the pending-visible
        # 'claimed' state; settle to 'sent' only when the caller injects
        # (settle=True). Re-renders own un-settled claims too.
        ct_notes = _consume_ct_notes(cfg, conn, settle=settle)
    else:
        # Passive re-render (marrow render_module / --print-note / off-screen
        # tick): render already-claimed-but-unsettled ct notes read-only so an
        # un-injected payload surfaces again — never claims, never settles.
        ct_notes = _peek_ct_notes(cfg, conn)

    return {
        "kick_reasons": kick_reasons,
        "receipts": receipts,
        "ct_notes": ct_notes,
        "last_wake": last_wake,
        "last_active": last_active,
        "active_app": _safe(_frontmost_app),
        "pending": _safe(_pending, cfg, now, default=[]),
        "window_sid": window_sid,
        "awake_since_hm": awake_since_hm,
    }


def render(cfg: dict, now: datetime, data: dict) -> str:
    """Pure assembly: data dict -> wakeup note text. No DB / no I/O.

    Layout (plan §一): a header block (Now / Active), then
    `---`-separated blocks for pending self-schedule, then a final
    turn-end reminder line (note.turn_end_text, every render; "" omits it).
    Handoff no longer lives here — it is injected at SessionStart on a
    fresh window."""
    header: list[str] = []

    now_seg = f"Now: {now.strftime('%H:%M %a')}"
    last = data.get("last_wake")
    # Minutes from the cortex session's last reply (ct_activity); fall back to
    # the prior wake row's age so the line never disappears when activity is
    # missing. force_slept marker always sourced from the wake row.
    active = data.get("last_active") or last
    if active:
        seg = f"Last active: {active['minutes_ago']}min ago"
        # "auto" = routine proxy sleep on the silence path -> render neutrally,
        # never as a force incident. Only real force incidents get the tag, and
        # the tag names the raw reason (ct-pause / fuse / stale).
        reason = last.get("force_slept") if last else None
        if reason and reason != "auto":
            seg += f" (force-slept: {reason})"
        now_seg += f" | {seg}"
    header.append(now_seg)

    app = data.get("active_app")
    if app:
        header.append(f"Active (Mac): {app}")

    blocks: list[str] = ["\n".join(header)]

    # External-wake (cortex.kick) reasons: plain lines, one per reason, NO
    # section header (rejected — the line speaks for itself).
    kick_reasons = data.get("kick_reasons") or []
    if kick_reasons:
        blocks.append("\n".join(str(r) for r in kick_reasons))

    # Reply receipts (C11): plain lines, one per note she replied to since the
    # last note. No section header — each line speaks for itself.
    receipts = data.get("receipts") or []
    if receipts:
        blocks.append("\n".join(str(r) for r in receipts))

    # ct-targeted outbox notes (covert session->cortex drops): each carries its
    # own header, rendered as its own block.
    ct_notes = data.get("ct_notes") or []
    for cn in ct_notes:
        if cn:
            blocks.append(str(cn))

    pending = data.get("pending") or []
    if pending:
        segs = [f"due {p['hm']} {p['intent']}".rstrip() for p in pending]
        blocks.append("Pending self-schedule: " + " · ".join(segs))

    note_text = "\n\n---\n\n".join(blocks)
    turn_end = _note_cfg(cfg).get("turn_end_text", "")
    if turn_end:
        note_text += "\n\n" + turn_end.format_map(_ClampDefaults(config.wake_clamps(cfg)))

    # Title and machine tag join the header block with a single newline (no
    # blank line): the approved note opens with tag / "Now:" / "Active (Mac):"
    # on three consecutive lines, in every shape.
    title = _note_cfg(cfg).get("title", "")
    if title:
        note_text = title + "\n" + note_text
    # Fix 5: machine-origin tag as the very first line, so the model reads the
    # ☀️ bell/wake-prompt turn that carried this note as an automated scheduler
    # signal, not a user message. Config-driven ("" omits it); note copy only,
    # never touches the bell text or receipt matching.
    machine_tag = _note_cfg(cfg).get("wake_machine_tag", "")
    if machine_tag:
        note_text = machine_tag + "\n" + note_text
    return note_text


if __name__ == "__main__":  # pragma: no cover - eyeball a real note
    from cortex import db

    _cfg = config.load()
    _conn = db.connect(_cfg)
    _now = datetime.now(_tz(_cfg))
    _data = gather(_conn, _cfg, _now, fresh=True, wake_kind="rotate")
    print(render(_cfg, _now, _data))
    _conn.close()
