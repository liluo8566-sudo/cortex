# cortex

Awake-presence layer: collectors gather signals → the always-on wake daemon reconciles the alarm ledger and decides when to wake → a resident Claude Code session runs the wake.

Assumes [marrow](../marrow) + synapse already installed and a Claude Code max plan. The resident-window delivery needs macOS + iTerm2.

## Setup

1. Clone into `~/CC-Lab/cortex` and create a venv (stdlib only, no third-party deps), then link the synapse repo (scheduler engine) as an editable install:
   ```
   python3.12 -m venv .venv
   .venv/bin/pip install -e ../synapse
   ```
2. Copy the config template and edit identity/paths:
   ```
   cp config.example.toml ~/.config/marrow/cortex.toml
   ```
   Override the path with the `CORTEX_CONFIG` env var if needed.
3. Enable the marrow-side bridge: set `[cortex] enabled = true` in marrow's config.toml and list the shells that run as cortex shells in `[cortex] shells` (default `["cli"]`) — this repo reads that same key directly, no cortex.toml copy needed — then restart the marrow watcher. This installs the MCP tools (`lie_down` for every shell, `say` for the cli shell; `wish` / `first` / `goal` everywhere) and the wake hooks.
4. Seed the cortex home dir `~/.config/marrow/cortex/` (configurable via `[paths] cortex_home`) — this is the resident session's cwd and inner world. Copy [templates/](templates/) there and customise names/paths:
   ```
   cp templates/*.md ~/.config/marrow/cortex/
   ```
   - `CLAUDE.md` — world rules + house rules for the resident session
   - `playbook.md` — activity menu (what to do when awake)
   - `notebook.md` — long-term memory, self-maintained
   - `handoff_template.md` — page template for the rolling log (per shell, `handoff-<shell>.md`; a page over `handoff_max_lines` is archived and a fresh page carries the unchecked todos + last lines)
   - `wishlist.md` — created automatically on first `wish`; template optional
   Everything else under cortex_home (wakeup_note, wake_state, handoff-cli.md, logs) is generated at runtime.
5. Install the launchd jobs (collect-tick + wake-daemon):
   ```
   .venv/bin/python -m cortex.install
   ```
   `python -m cortex.install remove` unloads them (retired jobs are unloaded too, their plist file left in place).

Ships with `pacemaker.dry_run = true` — a due wake is logged and the next one re-armed, without actually waking, until you flip it.

## How it works

- Collectors (launchd, ~30 min) read macOS app-usage (plus optional geofence/health) into `ct_` tables on the shared marrow DB.
- The wake daemon (launchd, always on) holds the alarm ledger: it reconciles state every minute and fires the due wake through the daily token budget gate.
- A wake lands in a resident iTerm window running `claude` (fresh spawn, `--resume`, or a bell into the live window), with the wakeup note injected by marrow's hook. There is no windowless fallback: a failed window path raises a marrow alert and the round is given up for the next tick to retry.
- The session ends its wake itself via `lie_down(next_wake_min=N)` (0 = wake again immediately). While it stays up, every `silent_max_min` of user silence injects one free-round note + `[NEW ROUND]` line and re-arms the same timer — a perpetual cycle, never a forced sleep. A per-wake watchdog covers that cycle and the token fuses; the always-on wake daemon fires the exact-time wakes.

## Docs

- [DESIGN.md](DESIGN.md) — goals and outcomes.
- [MAP.md](MAP.md) — how each part works today.
