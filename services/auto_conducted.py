"""
Auto-tick Conducted — Scheduled Service.
Integrated from standalone auto_conducted.py script.

Runs daily at 08:00 ICT (01:00 UTC) to auto-tick `Conducted = True`
for Management Meetings that have ended within T+1 and have Meeting Minutes.

Logic:
  - Find calendar.event where x_studio_is_management_event = True
  - Ended (stop < now UTC)
  - Within T+1 window (stop >= now - 1 day)
  - Has Meeting Minutes → TICK Conducted ✅
  - No Minutes → Skip (Non-compliance ⚠️)
"""

import json
import logging
import threading
import time
import requests
from datetime import datetime, timedelta, timezone

from config import Config

logger = logging.getLogger(__name__)

# ─── Scheduler State ───
_scheduler_running = False
_last_run = None
_last_result = None

# Target time: 08:00 ICT = 01:00 UTC
TARGET_HOUR_UTC = 1
TARGET_MINUTE_UTC = 0


def _odoo_call(model: str, method: str, args: list = None, kwargs: dict = None):
    """Execute Odoo JSON-RPC call using Conducted-specific credentials."""
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [
                Config.ODOO_DB,
                Config.CONDUCTED_ODOO_UID,
                Config.CONDUCTED_ODOO_API_KEY,
                model,
                method,
                args or [],
                kwargs or {},
            ],
        },
        "id": 1,
    }
    r = requests.post(f"{Config.ODOO_URL}/jsonrpc", json=payload, timeout=30)
    res = r.json()
    if "error" in res:
        raise Exception(
            res["error"].get("data", {}).get("message", str(res["error"]))
        )
    return res.get("result")


def run_auto_conducted(dry_run: bool = False) -> dict:
    """
    Run the auto-conducted logic.

    Args:
        dry_run: If True, only report what would be ticked without writing.

    Returns:
        dict with run summary.
    """
    global _last_run, _last_result

    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=1)

    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    threshold_str = threshold.strftime("%Y-%m-%d %H:%M:%S")

    prefix = "[DRY RUN] " if dry_run else ""
    logger.info(f"{prefix}Auto-tick Conducted — {now.strftime('%d/%m/%Y %H:%M')} UTC")
    logger.info(f"Window: {threshold_str} → {now_str}")

    # Find eligible meetings
    domain = [
        ("x_studio_is_management_event", "=", True),
        ("stop", ">=", threshold_str),
        ("stop", "<", now_str),
        ("x_studio_conducted", "=", False),
    ]

    meetings = _odoo_call(
        "calendar.event",
        "search_read",
        [domain],
        {
            "fields": [
                "id", "name", "stop", "user_id",
                "x_studio_meeting_minutes", "x_studio_conducted",
            ]
        },
    ) or []

    logger.info(f"Found {len(meetings)} meetings in window (not yet conducted)")

    to_tick = [m for m in meetings if m.get("x_studio_meeting_minutes")]
    no_minutes = [m for m in meetings if not m.get("x_studio_meeting_minutes")]

    logger.info(f"  → {len(to_tick)} have Meeting Minutes → will tick")
    logger.info(f"  → {len(no_minutes)} have NO Minutes → Non-compliance")

    if no_minutes:
        logger.warning("Non-compliance meetings (no minutes):")
        for m in no_minutes:
            logger.warning(f"  [{m['id']}] {m['name']} | end={m['stop']}")

    ticked_count = 0
    if to_tick:
        for m in to_tick:
            logger.info(f"  ✅ [{m['id']}] {m['name']} | end={m['stop']}")

        if not dry_run:
            ids = [m["id"] for m in to_tick]
            _odoo_call("calendar.event", "write", [ids, {"x_studio_conducted": True}])
            ticked_count = len(to_tick)
            logger.info(f"Done — {ticked_count} meetings marked as Conducted")
    else:
        logger.info("Nothing to tick today.")

    result = {
        "run_at": now_str,
        "dry_run": dry_run,
        "total_found": len(meetings),
        "ticked": ticked_count,
        "no_minutes": len(no_minutes),
        "ticked_meetings": [
            {"id": m["id"], "name": m["name"], "stop": m["stop"]}
            for m in to_tick
        ] if to_tick else [],
        "non_compliance": [
            {"id": m["id"], "name": m["name"], "stop": m["stop"]}
            for m in no_minutes
        ] if no_minutes else [],
    }

    _last_run = now_str
    _last_result = result

    logger.info(f"Summary: {json.dumps(result, ensure_ascii=False)}")
    return result


# ─── Scheduler ───

def _seconds_until_next_run() -> float:
    """Calculate seconds until next 08:00 ICT (01:00 UTC)."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=TARGET_HOUR_UTC, minute=TARGET_MINUTE_UTC, second=0, microsecond=0)

    # If target time has passed today, schedule for tomorrow
    if now >= target:
        target += timedelta(days=1)

    delta = (target - now).total_seconds()
    return delta


def _scheduler_loop():
    """Background loop that runs auto-conducted at 08:00 ICT daily."""
    global _scheduler_running
    _scheduler_running = True

    # Wait 30s before calculating first run (let server finish starting)
    time.sleep(30)

    while _scheduler_running:
        wait_seconds = _seconds_until_next_run()
        hours = wait_seconds / 3600
        logger.info(f"[CONDUCTED] Next run in {hours:.1f}h ({wait_seconds:.0f}s)")

        # Sleep until target time (wake every 60s to check if still running)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and _scheduler_running:
            remaining = deadline - time.monotonic()
            chunk = min(60, max(0, remaining))
            if chunk > 0:
                time.sleep(chunk)

        if not _scheduler_running:
            break

        # Run auto-conducted
        try:
            result = run_auto_conducted(dry_run=False)
            logger.info(f"[CONDUCTED] Completed: ticked={result['ticked']}, non_compliance={result['no_minutes']}")
        except Exception as e:
            logger.exception(f"[CONDUCTED] Error: {e}")

        # Safety sleep: wait 120s before recalculating next run
        # This prevents double-execution if the job finishes within the same minute
        time.sleep(120)


def start_conducted_scheduler():
    """Start the background conducted scheduler (call once at app startup)."""
    if not Config.CONDUCTED_ODOO_UID or not Config.CONDUCTED_ODOO_API_KEY:
        logger.warning("[CONDUCTED] CONDUCTED_ODOO_UID or CONDUCTED_ODOO_API_KEY not set — scheduler disabled")
        return

    thread = threading.Thread(target=_scheduler_loop, daemon=True, name="conducted-scheduler")
    thread.start()
    logger.info("[CONDUCTED] Scheduler started — target: 08:00 ICT (01:00 UTC) daily")


def get_conducted_status() -> dict:
    """Get scheduler status for health check."""
    return {
        "scheduler_active": _scheduler_running,
        "target_time": "08:00 ICT (01:00 UTC)",
        "last_run": _last_run,
        "last_result_summary": {
            "ticked": _last_result["ticked"],
            "no_minutes": _last_result["no_minutes"],
        } if _last_result else None,
    }
