"""iTerm2 window control for the resident interactive cortex session. All
control via iTerm2 AppleScript (works while the screen is locked — no keyboard
simulation). Primitives: ensure_window, respawn (fresh window with the emoji +
bell-marker wake prompt baked in as its first prompt, see fresh_initial_prompt),
type_wake_signal (the typed bell for an already-running resident window),
deliver_covert_marker, send_esc, say, hard_interrupt (process-level SIGINT
fallback when esc alone may not land, e.g. no focus). A fresh window wakes
silently — the baked-in prompt is the only trace, no notification, but carries
the same bell as a typed wake so the marrow UserPromptSubmit hook detects it
(via the wake_state receipt) and injects the full wakeup note. An alive resident
window is woken by typing that same bell line into it. The window body is one
`claude` running in cortex_home with MARROW_CORTEX=cli set explicitly (shell id
+ identity marker).
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime

from cortex import config, wake_state

_APP = "iTerm2"
_ITERM_BID = "com.googlecode.iterm2"
# Delay between typing a prompt and pressing Enter. Claude's TUI treats a
# text+newline `write text` as one bracketed paste and swallows the submit, so
# the prompt is typed first (no newline) then Enter is sent as a separate key.
_SUBMIT_DELAY_S = 0.6


class WindowError(Exception):
    pass


def _osa(script: str) -> str:
    p = subprocess.run(["osascript", "-"], input=script,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise WindowError(p.stderr.strip() or "osascript failed")
    return p.stdout.strip()


def _esc(text: str) -> str:
    # Newlines become AppleScript \n escapes: a raw LF inside a string literal
    # breaks the script, and a multi-line typed body (free-round note) must reach
    # the window as one bracketed paste, not as several submitted prompts.
    return (text.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\r", "\\r").replace("\n", "\\n"))


def is_running() -> bool:
    # Plain `application ... is running` never launches the app (unlike `tell`).
    return _osa('return (application "iTerm2" is running)') == "true"


def _frontmost_bid() -> str | None:
    """Bundle id of the current frontmost app, so window creation can restore
    focus (spawn must never steal keyboard focus)."""
    try:
        bid = _osa('tell application "System Events" to get bundle identifier '
                   'of first process whose frontmost is true')
        return bid or None
    except WindowError:
        return None


def _activate_bid(bid: str | None) -> None:
    if bid:
        try:
            _osa(f'tell application id "{bid}" to activate')
        except WindowError:
            pass


def _guard_focus(prev: str | None) -> None:
    """`write text` intermittently raises the iTerm window. If it grabbed focus
    from another app, hand focus back. Only say() is allowed to front cortex."""
    if not prev or prev == _ITERM_BID:
        return
    if _frontmost_bid() == _ITERM_BID:
        _activate_bid(prev)


def _session_alive(sid: str) -> bool:
    script = f'''
tell application "{_APP}"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "{sid}" then return "yes"
      end repeat
    end repeat
  end repeat
end tell
return "no"
'''
    try:
        return _osa(script) == "yes"
    except WindowError:
        return False


def window_model(cfg: dict) -> str:
    """Model for cortex windows. Empty -> omit the flag (inherit the
    environment default). Reused by every cortex window spawn."""
    return cfg["wake"].get("window_model", "")


def window_effort(cfg: dict) -> str:
    """Reasoning effort (low|medium|high|xhigh|max). Empty -> omit the flag."""
    return cfg["wake"].get("window_effort", "")


def wake_prompt(cfg: dict) -> str:
    """The single-line first prompt handed to a fresh cortex window: JUST the
    configured emoji (wake.wake_prompt, default '☀️') so no readable text shows
    in the user's face. The full wake instructions (read the note, choose next
    wake) are injected by marrow's UserPromptSubmit hook when this emoji is
    submitted in a cortex window."""
    return cfg["wake"].get("wake_prompt", "☀️")


def _bell_template(cfg: dict) -> str:
    """Template of the bell TYPED into an already-running resident window
    (scheduled wake on a live window, resume bell)."""
    return cfg["wake"].get("wake_bell_template", "⏰ {hm}")


def _opener_template(cfg: dict) -> str:
    """Template of the first prompt baked into a FRESHLY SPAWNED window
    (fresh_initial_prompt). Separate from the resident bell: a brand-new brain
    opens the shift, a live one just gets rung."""
    return cfg["wake"].get("spawn_opener_template", "☀️ {hm}")


def _template_prefix(tmpl: str) -> str:
    """Static text BEFORE the {hm} placeholder (whole text for a static
    template) — the shape a consumer falls back to with no receipt."""
    return tmpl.split("{hm}", 1)[0]


def bell_template_prefix(cfg: dict) -> str:
    """Static prefix of the resident bell template. Persisted into the receipt
    so the marrow side never needs cortex config."""
    return _template_prefix(_bell_template(cfg))


def opener_template_prefix(cfg: dict) -> str:
    """Static prefix of the fresh-spawn opener template — the window-lineage
    marker (every freshly spawned window's first prompt leads with it)."""
    return _template_prefix(_opener_template(cfg))


def wake_signal_line(cfg: dict, now, rearm: bool = False, token=None) -> str:
    """The VISIBLE bell line = human text only, from [wake].wake_bell_template
    with {hm} -> local HH:MM (default '☀️ 00:55'). NO machine marker, NO epoch
    token, NO rearm suffix on screen: all machine data (gen/state_id/rearm) is
    written to the wake_state receipt sidecar (write_wake_receipt) at send time.
    The marrow hook matches this exact on-screen text against the receipt. The
    `rearm`/`token` args are kept for signature compatibility — they no longer
    change the rendered text (they flow into the receipt instead)."""
    return _bell_template(cfg).replace("{hm}", now.strftime("%H:%M"))


def spawn_opener_line(cfg: dict, now) -> str:
    """The VISIBLE opener line of a freshly spawned window, from
    [wake].spawn_opener_template with {hm} -> local HH:MM. Same receipt/hook
    chain as the bell — only the wording differs."""
    return _opener_template(cfg).replace("{hm}", now.strftime("%H:%M"))


def write_wake_receipt(cfg: dict, now, token=None, rearm: bool = False,
                       opener: bool = False) -> None:
    """Persist the pending bell receipt into wake_state under the shared flock,
    at bell-send time. Records the exact visible text, gen, state_id, rearm
    flag, an ISO timestamp, and the template ACTUALLY used (so the consumer can
    shape-match without cortex config). `opener=True` = the fresh-spawn opener
    line (spawn_opener_template) instead of the resident bell. Overwrites any
    prior receipt (stale hygiene). Best-effort: a write failure never crashes
    the wake — the consumer then takes the shape fallback."""
    from datetime import timezone

    from cortex import wake_state
    tmpl = _opener_template(cfg) if opener else _bell_template(cfg)
    text = spawn_opener_line(cfg, now) if opener else wake_signal_line(cfg, now)
    gen = state_id = None
    if token:
        gen, state_id = token
    receipt = {
        "text": text,
        "gen": int(gen) if gen is not None else None,
        "state_id": str(state_id) if state_id is not None else None,
        "rearm": bool(rearm),
        "ts": datetime.now(timezone.utc).isoformat(),
        # Both persisted so the consumer shape-matches without cortex config: the
        # full template (to know whether it has an {hm} time placeholder — a fully
        # STATIC template with no placeholder is valid) and its static prefix.
        "template": tmpl,
        "template_prefix": _template_prefix(tmpl),
    }
    try:
        wake_state.update(cfg, wake_receipt=receipt)
    except Exception:
        pass


def fresh_initial_prompt(cfg: dict, now, token=None) -> str:
    """The baked first prompt for a brand-new cortex window: JUST the visible
    opener line (human text from [wake].spawn_opener_template, e.g. '☀️ 00:55').
    The machine data for it is written to the wake_state receipt via
    write_wake_receipt(opener=True) so the marrow UserPromptSubmit hook
    recognizes the on-screen line and injects the full wakeup note — the window
    gets its wake identity + note in one stroke instead of the emoji being read
    as a bare chat message. `token` (gen, state_id) is carried in the receipt,
    not the visible line."""
    return spawn_opener_line(cfg, now)


def launch_command(cfg: dict, initial_prompt: str | None = None,
                   resume_sid: str | None = None) -> str:
    # Identity + channel markers set explicitly (hooks derive channel from
    # MARROW_CHANNEL; MARROW_CORTEX=cli = shell id / kickout immunity).
    # --model/--effort only when configured; unset = inherit the environment
    # default. Reused by every cortex window spawn. A non-empty
    # initial_prompt (fresh_initial_prompt: emoji + bell marker) is baked in as
    # claude's first positional prompt so a freshly launched window starts
    # acting immediately — the marrow hook detects the marker and injects the
    # full note; near-zero readable text (one emoji + a short marker line).
    # A non-empty resume_sid adds `--resume <sid>` so a window that simply died
    # (crash / manual close, NOT a deliberate rotate) comes back as the SAME
    # session with full context — no fresh brain, no handoff catchup needed.
    home = str(config.cortex_home(cfg))
    cmd = cfg["wake"].get("launch_command", "claude")
    flags = ""
    mdl = window_model(cfg)
    if mdl:
        flags += f" --model {mdl}"
    eff = window_effort(cfg)
    if eff:
        flags += f" --effort {eff}"
    if resume_sid:
        flags += f" --resume {_shq(resume_sid)}"
    # Skip the workspace-trust dialog so the injected note lands (a fresh dir
    # otherwise blocks on the trust prompt). Mirrors marrow's headless call.
    if cfg["wake"].get("skip_permissions", True):
        flags += " --dangerously-skip-permissions"
    arg = f" {_shq(initial_prompt)}" if initial_prompt else ""
    return f"cd {home} && MARROW_CORTEX=cli MARROW_CHANNEL=ct {cmd}{flags}{arg}"


def _shq(text: str) -> str:
    """Single-quote a shell argument (the initial prompt) for the launch command."""
    return "'" + text.replace("'", "'\\''") + "'"


_launch_command = launch_command  # back-compat alias


def _spawn(cfg: dict, initial_prompt: str | None = None,
          resume_sid: str | None = None) -> str:
    # Source-level spawn barrier: the single genuine window-creation choke point
    # (both ensure_window and respawn route here). Under pytest, an unmocked test
    # reaching this would launch a REAL iTerm window + `claude` (burning credits,
    # spamming the desktop). Fail loudly instead — belt-and-braces with the
    # conftest subprocess guard, covering future tests and out-of-repo callers the
    # fixture cannot reach.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise WindowError(
            "refusing real window spawn under pytest: no test may launch a real "
            "iTerm window + claude (burns credits, spams the desktop). Your test "
            "reached window._spawn unmocked. Fix: stub the spawn boundary, e.g. "
            "monkeypatch.setattr(window, 'respawn', lambda cfg, initial_prompt="
            "None, resume_sid=None: 'test-sid'); if the wake takes the typed "
            "path also stub window.type_wake_signal + wake._signal_landed. The "
            "conftest autouse fixture _block_real_processes already blocks "
            "osascript/claude subprocess calls; this barrier catches the window-"
            "creation path that fixture cannot reach.")
    name = _esc(cfg["wake"].get("session_name", "cortex"))
    launch = _esc(launch_command(cfg, initial_prompt, resume_sid))
    # No `activate` — spawning must not steal keyboard focus. Creating a window
    # still brings iTerm forward, so capture the frontmost app and restore it.
    prev = _frontmost_bid()
    script = f'''
tell application "{_APP}"
  set w to (create window with default profile)
  tell current session of w
    set name to "{name}"
    write text "{launch}"
    return id
  end tell
end tell
'''
    sid = _osa(script)
    _activate_bid(prev)
    return sid


def ensure_window(cfg: dict) -> str:
    """Return the live cortex session id, spawning the window if iTerm is not
    running or the persisted session is gone/dead. A session that still exists
    but whose `claude` process died (SIGINT/crash/manual ctrl-C leaves a bare
    shell) is relaunched in place rather than respawned — cheaper, keeps the
    window/geometry, and the shell is otherwise idle so typing the launch
    command is safe. Either path is a new brain; wake.py's _window_rotated
    detects both cases itself (session-dead / claude-dead) BEFORE this runs,
    so no rotate flag is set here (this fn can also fire mid-wake, where
    setting it would wrongly mark the NEXT wake)."""
    sid = wake_state.get_session_id(cfg)
    if sid and is_running() and _session_alive(sid):
        if find_claude_pid(cfg) is not None:
            return sid
        _relaunch(sid, cfg)
        return sid
    sid = _spawn(cfg)
    wake_state.set_session_id(cfg, sid)
    _wait_ready(sid, cfg)  # let the TUI finish booting before the first inject
    return sid


def _relaunch(sid: str, cfg: dict) -> None:
    """Type the launch command into a session sitting at a bare shell (its
    `claude` process died) and wait for the TUI to come back up."""
    _type(sid, launch_command(cfg))
    time.sleep(_SUBMIT_DELAY_S)
    _enter(sid)
    _wait_ready(sid, cfg)


def claude_session_id(cfg: dict) -> str | None:
    """The claude conversation session UUID for --resume: the stem of a
    session jsonl (~/.claude/projects/<cwd>/<uuid>.jsonl). This is NOT the
    iTerm session id (wake_state.session_id).

    Priority: the newest WINDOW-LINEAGE session jsonl in the transcript dir
    FIRST (transcript.newest_window_lineage) — the newest jsonl whose first
    user message carries the wake signal marker, i.e. was launched as a cortex
    window (fresh_initial_prompt bakes it into every window's first prompt
    since dccb3d4). Plain newest() is NOT enough: the transcript dir also holds
    HEADLESS session archives (marrow's sessionend digest runs `claude -p`
    against this same cwd -> same projects dir), and a digest run can be the
    mtime-newest file — resuming it exposes its full worker prompt in the
    window (live-confirmed). The recorded hint is a best-effort bounded poll
    captured right after a spawn (_wait_new_transcript, ~8s) — the claude TUI
    can take 30s+ to create its session jsonl, so in real timing the poll
    routinely times out (hint None) AND, if a stale entry from a previous cycle
    was never cleared, the hint can be present but wrong (live-confirmed:
    resumed a stale recorded uuid instead of the dead window's real archive).
    The hint is now only a fallback for when no marker-bearing transcript file
    exists at all. None only when neither yields a UUID (caller falls back to
    a fresh spawn)."""
    from pathlib import Path
    from cortex import transcript as _transcript

    # Window-lineage marker = the spawn-opener template prefix (e.g. '☀️'): every
    # window's first prompt leads with it (fresh_initial_prompt).
    marker = opener_template_prefix(cfg).strip()
    lineage = _transcript.newest_window_lineage(cfg, marker)
    if lineage is not None:
        return lineage.stem
    raw = wake_state.load(cfg).get("transcript")
    if raw:
        stem = Path(str(raw)).stem
        if stem:
            return stem
    return None


def respawn(cfg: dict, initial_prompt: str | None = None,
           resume_sid: str | None = None) -> str:
    """Spawn a new resident window. The old window is left OPEN and its `claude`
    process is NOT killed — on a rotate the predecessor stays up for the user to
    close herself; on a dead-window resume there is nothing to kill anyway. A
    non-empty initial_prompt (fresh_initial_prompt: emoji + bell marker) is baked
    into the launch command so the window starts acting immediately — no arm
    prompt, no lie-down-first, no signal. A non-empty resume_sid launches
    `claude --resume <sid>` (same conversation, full context) instead of a fresh
    brain — used when the window simply died with no rotate flag. Persists and
    returns the new resident sid. Reused for rotate/rebirth (fresh) and the
    dead-window recovery (resume).

    Readiness is VERIFIED before the sid is returned (Fix 2): _wait_ready raises
    WindowError if the TUI never comes up (a bad/gone --resume sid, or `claude`
    exiting at once, leaves a bare shell). On that failure NOTHING is persisted --
    the caller (_spawn_wake) turns the WindowError into a None return so a dead
    resume falls back to a fresh spawn instead of recording a bare shell as an
    awake resident.

    codex adversarial-review Fix 2: this function no longer persists the sid
    itself. The prior version called wake_state.set_session_id() unconditionally
    right here, BEFORE the caller's epoch CAS (_spawn_wake's set_awake) had a
    chance to reject a stale spawn -- so a spawn a newer epoch went on to cancel
    had ALREADY overwritten the live resident's session pointer by the time the
    CAS ran, leaving liveness checks / the watchdog / the next injection all
    targeting the cancelled window. The verified sid is now only ever committed
    by the caller, atomically WITH the awake flip, under wake_state.set_awake's
    expected_token CAS -- a rejected CAS leaves the prior (still-live) sid
    completely untouched."""
    sid = _spawn(cfg, initial_prompt, resume_sid)
    _wait_ready(sid, cfg)  # raises WindowError on timeout -> nothing persisted
    return sid


def _read_session(sid: str) -> str:
    script = f'''
tell application "{_APP}"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "{sid}" then return (text of s)
      end repeat
    end repeat
  end repeat
end tell
return ""
'''
    try:
        return _osa(script)
    except WindowError:
        return ""


def _wait_ready(sid: str, cfg: dict) -> None:
    """Block until the freshly spawned claude TUI is ready for input (its footer
    marker appears), so the first injection never types into a booting shell.

    Readiness is VERIFIED, never assumed: the footer marker must actually appear
    within ready_timeout_sec. A timeout (a bad/gone --resume sid or an instantly
    exiting `claude` leaves a bare shell that never renders the marker) raises
    WindowError so the caller can fall back to a fresh spawn — returning
    identically on marker-found and on timeout previously let a dead resume be
    recorded as an awake resident (Fix 2)."""
    marker = cfg["wake"].get("ready_marker", "accept edits")
    timeout = float(cfg["wake"].get("ready_timeout_sec", 30))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker in _read_session(sid):
            return
        time.sleep(1.0)
    raise WindowError(
        f"session {sid} not ready (marker {marker!r} absent after {timeout}s)")


def _session_stmt(sid: str, stmt: str) -> str:
    return f'''
tell application "{_APP}"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "{sid}" then
          {stmt}
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no"
'''


def _type(sid: str, text: str) -> None:
    """Type text into the session WITHOUT a trailing newline (no submit)."""
    if _osa(_session_stmt(sid, f'tell s to write text "{_esc(text)}" newline no')) != "ok":
        raise WindowError(f"session {sid} not found for write")


def _enter(sid: str) -> None:
    """Send a bare carriage return (submit the current input)."""
    _osa(_session_stmt(sid, "tell s to write text (character id 13) newline no"))


def _submit_prompt(sid: str, text: str) -> None:
    # Type once (avoid double-typing), then Enter twice: a first-run startup
    # notice can swallow the first Enter, leaving the prompt unsubmitted; the
    # second Enter is a harmless no-op on an already-empty input line.
    _type(sid, text)
    time.sleep(_SUBMIT_DELAY_S)
    _enter(sid)
    time.sleep(0.3)
    _enter(sid)


def write_note(cfg: dict, text: str, shell: str | None = None,
               sid: str | None = None):
    """Persist the wakeup note into THIS shell's section of the note file and
    return the path. The marrow hook reads its own section to inject the full
    note when it sees the bell line. Other shells' sections are left intact
    (note_file.write_section: flock + read-modify-write)."""
    from cortex import note, note_file

    note_path = wake_state.wakeup_note_path(cfg)
    note_file.write_section(note_path, shell or note.CLI_SHELL, text, sid)
    return note_path


def inject_prompt(cfg: dict, text: str) -> bool:
    """Inject a one-line text prompt into the resident cortex window, restoring
    focus afterwards. Used by the fuse path to ask the session to write its
    handoff and lie down. Returns False if there is no resident session."""
    sid = wake_state.get_session_id(cfg)
    if not sid:
        return False
    prev = _frontmost_bid()
    try:
        _submit_prompt(sid, text)
    except WindowError:
        return False
    finally:
        _guard_focus(prev)
    return True


def type_wake_signal(cfg: dict, now, token=None) -> bool:
    """The wake bell for a live resident window: type the VISIBLE bell line
    (human text) into it and write its receipt first. It flows through the marrow
    hook (receipt matched -> note injected). `token` (gen, state_id), when given,
    is carried in the receipt so a superseded wake is suppressed by the marrow
    epoch check. Returns False if there is no resident session. Focus-guarded
    like every typing path."""
    write_wake_receipt(cfg, now, token=token, rearm=True)
    return inject_prompt(cfg, wake_signal_line(cfg, now, rearm=True))


def deliver_covert_marker(cfg: dict, marker_line: str) -> str:
    """Deliver a machine-marker line to the resident window the SAME way a wake
    bell reaches it: type ONLY the marker (the full instruction body is injected
    invisibly by the marrow UserPromptSubmit hook keyed on the marker). The
    visible round is just the short marker line — never the prompt body. Returns
    the rung used: 'typed' | 'none' (no resident window to type into)."""
    return "typed" if inject_prompt(cfg, marker_line) else "none"


def send_esc(cfg: dict) -> None:
    """Interrupt the current turn (ESC, char id 27, no trailing newline)."""
    sid = wake_state.get_session_id(cfg)
    if sid:
        prev = _frontmost_bid()
        _osa(_session_stmt(sid, "tell s to write text (character id 27) newline no"))
        _guard_focus(prev)


def _session_tty(sid: str) -> str | None:
    """tty device (e.g. /dev/ttys003) of the resident session, via iTerm2."""
    try:
        out = _osa(_session_stmt(sid, "return (tty of s)"))
    except WindowError:
        return None
    return out if out.startswith("/dev/") else None


def _ps_tty_claude_pids(ttyname: str) -> list[int]:
    """pid(s) whose exact command is `claude` on the given tty (name without
    the /dev/ prefix, e.g. ttys003)."""
    try:
        p = subprocess.run(["ps", "-t", ttyname, "-o", "pid=,comm="],
                           capture_output=True, text=True)
    except OSError:
        return []
    if p.returncode != 0:
        return []
    pids = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == "claude":
            try:
                pids.append(int(parts[0]))
            except ValueError:
                continue
    return pids


def _pgrep_claude_pids() -> list[int]:
    """`-a` is REQUIRED: BSD/macOS pgrep excludes the calling process's own
    ANCESTORS by default (see pgrep(1) `-a`) — since this is called from a
    subprocess of the very cortex claude window we need to find, plain
    `pgrep -x claude` silently drops that exact pid every time (verified
    07-20 live-incident root cause: resident_pid always recorded None)."""
    try:
        p = subprocess.run(["pgrep", "-a", "-x", "claude"], capture_output=True, text=True)
    except OSError:
        return []
    if p.returncode not in (0, 1):  # 1 = no matches, still a clean run
        return []
    return [int(x) for x in p.stdout.split() if x.isdigit()]


def _pid_cwd(pid: int) -> str | None:
    try:
        p = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                           capture_output=True, text=True)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    for line in p.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def find_claude_pid(cfg: dict) -> int | None:
    """Discover the pid of the resident cortex window's `claude` process.
    (a) iTerm session tty -> ps -t <tty> for a `claude` command on that tty.
    (b) fallback: pgrep -x claude, keep the ones whose cwd == cortex_home.
    Ambiguous (0 or >1 candidates) or undiscoverable -> None (never guess).
    Used for non-decision uses (e.g. hard_interrupt). Liveness = _window_alive."""
    sid = wake_state.get_session_id(cfg)
    if sid:
        tty = _session_tty(sid)
        if tty:
            pids = _ps_tty_claude_pids(tty.removeprefix("/dev/"))
            if len(pids) == 1:
                return pids[0]

    home = str(config.cortex_home(cfg))
    candidates = [pid for pid in _pgrep_claude_pids() if _pid_cwd(pid) == home]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _list_sessions() -> list[tuple[str, str]]:
    """Enumerate EVERY live iTerm session as (session id, tty). tty is the
    /dev/ttysNNN device backing the session (empty for a session with no live
    process). Used by the tick's auto-adopt scan to find a window the user
    opened `claude` in herself (never registered). Same repeat-over-windows/
    tabs/sessions shape as _session_alive; one AppleScript call, machine-parsed
    from `id|tty` lines. iTerm down / AppleScript error -> []."""
    script = f'''
set out to ""
tell application "{_APP}"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        set out to out & (id of s) & "|" & (tty of s) & linefeed
      end repeat
    end repeat
  end repeat
end tell
return out
'''
    try:
        raw = _osa(script)
    except WindowError:
        return []
    pairs = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        sid, tty = line.split("|", 1)
        sid, tty = sid.strip(), tty.strip()
        if sid:
            pairs.append((sid, tty))
    return pairs


def _claude_start_on_tty(ttyname: str, home: str) -> float | None:
    """Newest start time (epoch seconds) of an interactive `claude` process on
    `ttyname` (no /dev/ prefix) whose cwd is `home`. `ps -o lstart=` gives the
    wall-clock start; parsed to epoch. An INTERACTIVE tty (a real ttysNNN) is
    required by construction — headless `claude -p` runs (marrow's digest) have
    no controlling tty, so they never appear under `ps -t <tty>` and are excluded
    without a special case. None when no matching claude runs on that tty."""
    try:
        p = subprocess.run(["ps", "-t", ttyname, "-o", "pid=,comm=,lstart="],
                           capture_output=True, text=True)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    newest: float | None = None
    for line in p.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, comm, lstart = parts
        if comm != "claude":
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if _pid_cwd(pid) != home:
            continue
        ts = _parse_lstart(lstart)
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    return newest


def _parse_lstart(text: str) -> float | None:
    """Parse `ps -o lstart` output (e.g. 'Sun Jul 20 18:36:01 2026') to epoch
    seconds via the local timezone. Unparseable -> None."""
    from datetime import datetime
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %d %b %H:%M:%S %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def find_adoptable_window(cfg: dict) -> str | None:
    """Scan iTerm for a window the user opened `claude` in herself inside
    cortex_home and never registered. Returns the iTerm session id (UUID) of the
    best candidate — the one whose `claude` process start time is NEWEST — or
    None when there is no candidate. Interactive-tty-only by construction (see
    _claude_start_on_tty), so marrow's headless `claude -p` runs against the same
    cwd are never adopted. iTerm down / no sessions -> None."""
    home = str(config.cortex_home(cfg))
    best_sid: str | None = None
    best_ts = -1.0
    for sid, tty in _list_sessions():
        if not tty.startswith("/dev/"):
            continue
        ts = _claude_start_on_tty(tty.removeprefix("/dev/"), home)
        if ts is not None and ts > best_ts:
            best_ts, best_sid = ts, sid
    return best_sid


def _claude_on_session_tty(cfg: dict, sid: str) -> bool:
    """True iff a `claude` process runs on the RECORDED iTerm session's own tty.
    Per-session liveness — the cwd fallback in find_claude_pid is deliberately
    NOT used, so another claude window in cortex_home can't fake this session
    alive. No tty (session gone) -> False."""
    tty = _session_tty(sid)
    if not tty:
        return False
    return bool(_ps_tty_claude_pids(tty.removeprefix("/dev/")))


def hard_interrupt(cfg: dict) -> int | None:
    """Guaranteed esc-equivalent: SIGINT the resident window's claude process.
    Never SIGKILL. Returns the signaled pid, or None if discovery was
    ambiguous/failed (skip rather than signal an unverified pid)."""
    pid = find_claude_pid(cfg)
    if pid is None:
        return None
    try:
        os.kill(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        return None
    return pid


def say(cfg: dict, note: str | None = None) -> None:
    """开口 primitive: the attention signal. Fronts the resident cortex iTerm
    window and plays a sound (the words themselves are the normal in-window
    reply). This is the SOLE place cortex is allowed to take keyboard focus —
    every other path guards focus. `note` is accepted for CLI/API symmetry but
    the words live in the window; only the sound + front happen here."""
    _play_sound(cfg.get("wake", {}).get("say_sound", ""))
    _bring_to_front(wake_state.get_session_id(cfg))


def _play_sound(name: str) -> None:
    """Play a named macOS system sound (afplay on the .aiff under System/Library
    Sounds); empty name -> silent. Best-effort, never raises."""
    if not name:
        return
    path = f"/System/Library/Sounds/{name}.aiff"
    try:
        subprocess.Popen(["afplay", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _bring_to_front(sid: str | None) -> None:
    """Opt-in only: front the cortex window (the sole allowed activate of it)."""
    if not sid:
        return
    script = f'''
tell application "{_APP}"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "{sid}" then
          select w
          tell t to select
          tell s to select
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no"
'''
    try:
        _osa(script)
    except WindowError:
        pass
