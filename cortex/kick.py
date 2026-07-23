"""cortex.kick — bridge -> cortex wake relay.

Spawned as a detached subprocess by synapse cortex_kick.kick() (and by
marrow cortex_bridge.kick_cortex()) when a watch event fires.  Wakes
the resident cortex session via the standard run_wake pipeline.

  python -m cortex.kick --kind reply   [--note-id N] [--text "..."]
  python -m cortex.kick --kind timeout [--note-id N] [--minutes N]
  python -m cortex.kick --kind morning
  python -m cortex.kick --kind note    [--note-id N]
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(message)s",
)
log = logging.getLogger("cortex.kick")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cortex.kick")
    parser.add_argument("--kind", required=True,
                        choices=["reply", "timeout", "morning", "note"])
    parser.add_argument("--note-id", type=int, default=None)
    parser.add_argument("--minutes", type=int, default=None)
    parser.add_argument("--text", default="")
    args = parser.parse_args(argv)

    from cortex import config, db, wake_state

    cfg = config.load()
    tz = ZoneInfo(cfg["core"]["timezone"])
    now = datetime.now(tz)

    state = wake_state.load(cfg)

    if args.kind != "reply" and state.get("paused"):
        log.info("kick %s: DND on, skipped", args.kind)
        return 0

    from cortex.wake import run_wake, _window_alive

    if _window_alive(cfg) and wake_state.is_awake(cfg):
        log.info("kick %s: already awake on duty, no-op", args.kind)
        return 0

    conn = db.connect(cfg)
    try:
        detail = args.kind
        if args.note_id:
            detail += f" note_id={args.note_id}"
        if args.text:
            detail += f" text={args.text[:60]}"
        decision = {
            "wake": True,
            "reasons": [],
            "gated_by": [],
            "wake_reasons": f"kick:{args.kind}",
            "explanation": f"{now.strftime('%H:%M')} bridge kick ({detail})",
        }
        result = run_wake(conn, cfg, decision, now=now)
        mode = result.get("mode", "?")
        log.info("kick %s: woke (mode=%s)", args.kind, mode)
        if mode != "window":
            from cortex.pacemaker import integration
            next_floor = integration.lie_down(conn, cfg)
            wake_state.set_next_wake_at(
                cfg, next_floor.isoformat() if next_floor else None)
    except Exception:
        log.exception("kick %s: run_wake failed", args.kind)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
