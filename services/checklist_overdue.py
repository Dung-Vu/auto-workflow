"""
checklist_overdue.py — Scheduled service that counts overdue checklist
activities (activity type 108) assigned to each SC (Sales Coordinator) user
and writes the resulting count into the D69 cell of their respective
spreadsheet dashboard.

Schedule
--------
Runs once a month on the 1st at 00:00 ICT, which is 17:00 UTC of the
previous calendar day. The scheduler computes the wait time to the next
1st-of-month 17:00 UTC boundary and sleeps (interruptibly) until then.

For each configured dashboard the service:
  1. Counts overdue activities for the dashboard's owner via mail.activity.
  2. Reads the spreadsheet.dashboard record (name + spreadsheet_data).
  3. Parses the spreadsheet JSON, sets cells['D69'] = str(overdue_count).
  4. Writes the updated payload back (with a fresh revisionId) unless dry_run.
"""

import json
import logging
import threading
import time
import uuid
from datetime import date, datetime, timezone, timedelta

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler state
# ---------------------------------------------------------------------------
_scheduler_running = False
_last_run = None
_last_result = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# 17:00 UTC == 00:00 ICT (next day) == 1st of the month.
TARGET_HOUR_UTC = 17
TARGET_MINUTE_UTC = 0

ACTIVITY_TYPE_ID = getattr(Config, "CHECKLIST_ACTIVITY_TYPE_ID", 108)

# Dashboard id -> owner mapping for the SC users whose overdue checklist
# counts should be surfaced on their personal dashboard (cell D69).
DASHBOARDS = {
    265: {"user_id": 122, "name": "TRANG"},
    268: {"user_id": 249, "name": "MAI HỒNG"},
    269: {"user_id": 276, "name": "MAI THU"},
}


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------
def count_overdue(user_id):
    """Return the number of overdue checklist activities for *user_id*.

    An activity is overdue when its deadline is before today and it has not
    been marked done (date_done is False).
    """
    today = date.today().isoformat()
    domain = [
        ["activity_type_id", "=", ACTIVITY_TYPE_ID],
        ["user_id", "=", user_id],
        ["date_done", "=", False],
        ["date_deadline", "<", today],
    ]
    return odoo.execute("mail.activity", "search_count", [domain], {})


def run_checklist_overdue_refresh(dry_run=False):
    """Refresh the D69 overdue-count cell on every configured dashboard.

    When *dry_run* is True the counts are computed and reported but the
    spreadsheet payloads are not written back to Odoo.
    """
    global _last_run, _last_result

    dashboards_result = []
    for dash_id, cfg in DASHBOARDS.items():
        user_id = cfg["user_id"]
        name = cfg["name"]
        overdue_count = count_overdue(user_id)
        updated = False

        records = odoo.read(
            "spreadsheet.dashboard", [dash_id], ["name", "spreadsheet_data"]
        )
        if records:
            rec = records[0]
            raw = rec.get("spreadsheet_data") or "{}"
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data = {}
            if not isinstance(data, dict):
                data = {}

            cells = data.get("cells", {})
            if not isinstance(cells, dict):
                cells = {}
            cells["D69"] = str(overdue_count)
            data["cells"] = cells

            if not dry_run:
                revision_id = str(uuid.uuid4())
                odoo.write(
                    "spreadsheet.dashboard",
                    [dash_id],
                    {
                        "spreadsheet_data": json.dumps(data),
                        "revisionId": revision_id,
                    },
                )
                updated = True

        dashboards_result.append(
            {
                "dash_id": dash_id,
                "user_id": user_id,
                "name": name,
                "overdue_count": overdue_count,
                "updated": updated,
            }
        )

    result = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "dashboards": dashboards_result,
    }

    _last_run = result["ran_at"]
    _last_result = result

    logger.info(
        "[CHECKLIST] Refresh complete — dry_run=%s dashboards=%d",
        dry_run,
        len(dashboards_result),
    )
    return result


# ---------------------------------------------------------------------------
# Scheduler (monthly, 1st at 17:00 UTC)
# ---------------------------------------------------------------------------
def _seconds_until_next_run():
    """Seconds until the next 1st-of-month 17:00 UTC boundary."""
    now = datetime.now(timezone.utc)

    # If today is the 1st and we are still before 17:00 UTC, the target is
    # today at 17:00 UTC. Otherwise we aim at the 1st of next month.
    if now.day == 1 and now.hour < TARGET_HOUR_UTC:
        target = datetime(
            now.year, now.month, 1,
            TARGET_HOUR_UTC, TARGET_MINUTE_UTC, 0,
            tzinfo=timezone.utc,
        )
    else:
        if now.month == 12:
            year, month = now.year + 1, 1
        else:
            year, month = now.year, now.month + 1
        target = datetime(
            year, month, 1,
            TARGET_HOUR_UTC, TARGET_MINUTE_UTC, 0,
            tzinfo=timezone.utc,
        )

    return (target - now).total_seconds()


def _interruptible_sleep(seconds):
    """Sleep for *seconds* but bail out early if the scheduler stops."""
    slept = 0.0
    while slept < seconds and _scheduler_running:
        chunk = min(60.0, seconds - slept)
        time.sleep(chunk)
        slept += chunk


def _scheduler_loop():
    """Main scheduler loop: wait until the target time then run a refresh."""
    global _scheduler_running
    while _scheduler_running:
        time.sleep(30)
        if not _scheduler_running:
            break

        wait_seconds = _seconds_until_next_run()
        logger.info("[CHECKLIST] Next run in %.1fh", wait_seconds / 3600.0)

        _interruptible_sleep(wait_seconds)
        if not _scheduler_running:
            break

        try:
            run_checklist_overdue_refresh()
        except Exception:
            logger.exception("[CHECKLIST] Refresh failed")

        # Safety pause so we never busy-loop around the boundary.
        time.sleep(120)


def start_checklist_overdue_scheduler():
    """Start the monthly checklist-overdue scheduler (daemon thread)."""
    global _scheduler_running

    if not getattr(Config, "CHECKLIST_OVERDUE_ENABLED", False):
        logger.info(
            "[CHECKLIST] Scheduler disabled "
            "(CHECKLIST_OVERDUE_ENABLED not set)"
        )
        return

    if not (
        getattr(Config, "ODOO_DB", None)
        and (getattr(Config, "ODOO_UID", None) or getattr(Config, "ODOO_USER", None))
        and getattr(Config, "ODOO_API_KEY", None)
    ):
        logger.warning(
            "[CHECKLIST] Missing Odoo credentials, skipping scheduler start"
        )
        return

    if _scheduler_running:
        logger.info("[CHECKLIST] Scheduler already running")
        return

    _scheduler_running = True
    thread = threading.Thread(
        target=_scheduler_loop,
        name="checklist-overdue-scheduler",
        daemon=True,
    )
    thread.start()
    logger.info(
        "[CHECKLIST] Scheduler started — target: "
        "1st of month 00:00 ICT (17:00 UTC prev day)"
    )


def get_checklist_overdue_status():
    """Return a snapshot of the scheduler state for monitoring/diagnostics."""
    return {
        "scheduler_active": _scheduler_running,
        "target_time": "1st of month 00:00 ICT (17:00 UTC prev day)",
        "last_run": _last_run,
        "last_result_summary": _last_result,
    }


