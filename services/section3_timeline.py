"""Scheduled service: refresh Section 3 (Timeline) "Tỷ lệ request xử lý trễ hạn"
table (rows 52-64) on individual dashboards 265 / 268 / 269.

Runs monthly on the 1st at 06:00 ICT, i.e. 23:00 UTC on the previous calendar
day. For each dashboard owner the service pulls completed mail.activity records
(date_done set) of the configured service-call activity types, aggregates
per-month done / late counts for TARGET_YEAR, computes a late percentage, and
writes the values back into the spreadsheet dashboard cells:

    rows 52-63 (months 1-12):  B{row}=done   C{row}=late   D{row}=pct
    row 64 (TOTAL):            B64=total_done C64=total_late D64=total_pct

A background scheduler thread waits until the next 1st-of-month 23:00 UTC and
triggers run_section3_timeline_refresh() automatically. Manual refreshes are
possible via run_section3_timeline_refresh(dry_run=True|False).
"""

import json
import logging
import threading
import time
from datetime import date, datetime, timezone, timedelta
from collections import defaultdict

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler state
# ---------------------------------------------------------------------------
_scheduler_running = False
_last_run = None
_last_result = None

# 23:00 UTC == 06:00 ICT the following day == 1st of the month.
TARGET_HOUR_UTC = 23
TARGET_MINUTE_UTC = 0

# ---------------------------------------------------------------------------
# Business-logic configuration
# ---------------------------------------------------------------------------
DASHBOARDS = {
    265: {"user_id": 122, "name": "TRANG"},
    268: {"user_id": 249, "name": "MAI HỒNG"},
    269: {"user_id": 276, "name": "MAI THU"},
}

SC_TYPE_IDS = [
    58, 78, 59, 67, 68, 60, 61, 62, 108, 112,
    63, 100, 101, 65, 91, 92, 114, 102, 103, 104,
]

TARGET_YEAR = 2026


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def fetch_user_done_activities(user_id):
    """Return completed mail.activity records for ``user_id`` (incl. archived)."""
    ctx = {"active_test": False}
    domain = [
        ("activity_type_id", "in", SC_TYPE_IDS),
        ("user_id", "=", user_id),
        ("date_done", "!=", False),
    ]
    ids = odoo.execute("mail.activity", "search", [domain], {"context": ctx})
    if not ids:
        return []

    activities = []
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        recs = odoo.execute(
            "mail.activity",
            "read",
            [batch, ["id", "date_done", "date_deadline"]],
            {"context": ctx},
        )
        activities.extend(recs)
    return activities


def compute_monthly_metrics(activities):
    """Aggregate per-month done / late counts for TARGET_YEAR."""
    monthly = defaultdict(lambda: {"done": 0, "late": 0})
    for act in activities:
        done = act.get("date_done")
        if not done:
            continue
        dt = date.fromisoformat(done)
        if dt.year != TARGET_YEAR:
            continue
        month = dt.month
        monthly[month]["done"] += 1
        deadline = act.get("date_deadline")
        if deadline:
            if dt > date.fromisoformat(deadline):
                monthly[month]["late"] += 1
    return dict(monthly)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------
def run_section3_timeline_refresh(dry_run=False):
    """Refresh Section 3 timeline tables on all configured dashboards."""
    result = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "dashboards": [],
    }

    for dash_id, cfg in DASHBOARDS.items():
        user_id = cfg["user_id"]
        name = cfg["name"]

        activities = fetch_user_done_activities(user_id)
        metrics = compute_monthly_metrics(activities)

        recs = odoo.read(
            "spreadsheet.dashboard",
            [dash_id],
            ["name", "spreadsheet_data"],
        )
        if not recs:
            logger.warning("[S3] dashboard %s not found; skipping", dash_id)
            result["dashboards"].append({
                "dash_id": dash_id,
                "user_id": user_id,
                "name": name,
                "total_done": 0,
                "total_late": 0,
                "total_pct": 0,
                "activities_count": len(activities),
                "error": "dashboard not found",
            })
            continue

        data = json.loads(recs[0]["spreadsheet_data"])
        sheet = data["sheets"][0]
        cells = sheet["cells"]

        total_done = 0
        total_late = 0
        for m in range(1, 13):
            row = 51 + m
            m_metric = metrics.get(m, {"done": 0, "late": 0})
            done = m_metric["done"]
            late = m_metric["late"]
            total_done += done
            total_late += late
            cells[f"B{row}"] = str(done) if done > 0 else "—"
            cells[f"C{row}"] = str(late)
            pct = round(late / done, 4) if done > 0 else 0
            cells[f"D{row}"] = str(pct)

        total_pct = round(total_late / total_done, 4) if total_done > 0 else 0
        cells["B64"] = str(total_done)
        cells["C64"] = str(total_late)
        cells["D64"] = str(total_pct)

        if not dry_run:
            odoo.write(
                "spreadsheet.dashboard",
                dash_id,
                {"spreadsheet_data": json.dumps(data)},
            )

        result["dashboards"].append({
            "dash_id": dash_id,
            "user_id": user_id,
            "name": name,
            "total_done": total_done,
            "total_late": total_late,
            "total_pct": total_pct,
            "activities_count": len(activities),
        })
        logger.info(
            "[S3] %s (dash %s, user %s): done=%s late=%s pct=%s activities=%s%s",
            name, dash_id, user_id, total_done, total_late, total_pct,
            len(activities), " [dry-run]" if dry_run else "",
        )

    global _last_run, _last_result
    _last_run = datetime.now(timezone.utc)
    _last_result = result
    logger.info(
        "[S3] refresh complete: dashboards=%s dry_run=%s",
        len(result["dashboards"]), dry_run,
    )
    return result


# ---------------------------------------------------------------------------
# Scheduler (monthly 1st @ 23:00 UTC)
# ---------------------------------------------------------------------------
def _seconds_until_next_run():
    """Seconds until the next 1st-of-month 23:00 UTC."""
    now = datetime.now(timezone.utc)
    candidate = now.replace(
        day=1, hour=TARGET_HOUR_UTC, minute=TARGET_MINUTE_UTC,
        second=0, microsecond=0,
    )
    if candidate <= now:
        if candidate.month == 12:
            candidate = candidate.replace(year=candidate.year + 1, month=1)
        else:
            candidate = candidate.replace(month=candidate.month + 1)
    return (candidate - now).total_seconds()


def _scheduler_loop():
    global _scheduler_running
    logger.info("[S3] scheduler loop started")
    while _scheduler_running:
        time.sleep(30)
        if not _scheduler_running:
            break

        wait = _seconds_until_next_run()
        deadline_ts = time.time() + wait
        # Interruptible sleep in <=60s chunks so a stop request is responsive.
        while time.time() < deadline_ts and _scheduler_running:
            time.sleep(min(60.0, max(0.0, deadline_ts - time.time())))
        if not _scheduler_running:
            break

        try:
            run_section3_timeline_refresh(dry_run=False)
        except Exception:
            logger.exception("[S3] scheduled refresh failed")

        time.sleep(120)
    logger.info("[S3] scheduler loop stopped")


def start_section3_timeline_scheduler():
    """Start the background scheduler thread (idempotent)."""
    global _scheduler_running
    if _scheduler_running:
        logger.warning("[S3] scheduler already running")
        return

    if not getattr(Config, "SECTION3_TIMELINE_ENABLED", False):
        logger.info("[S3] scheduler disabled (Config.SECTION3_TIMELINE_ENABLED not set)")
        return

    creds = [
        getattr(Config, "ODOO_URL", None),
        getattr(Config, "ODOO_DB", None),
        getattr(Config, "ODOO_UID", None) or getattr(Config, "ODOO_USER", None),
        getattr(Config, "ODOO_API_KEY", None),
    ]
    if not all(creds):
        logger.warning("[S3] missing Odoo credentials; not starting scheduler")
        return

    _scheduler_running = True
    thread = threading.Thread(
        target=_scheduler_loop,
        name="section3-timeline-scheduler",
        daemon=True,
    )
    thread.start()
    logger.info("[S3] scheduler thread started: section3-timeline-scheduler")


def stop_section3_timeline_scheduler():
    """Signal the scheduler loop to stop."""
    global _scheduler_running
    _scheduler_running = False
    logger.info("[S3] scheduler stop requested")


def get_section3_timeline_status():
    """Return the current scheduler status."""
    return {
        "running": _scheduler_running,
        "enabled": getattr(Config, "SECTION3_TIMELINE_ENABLED", False),
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_result": _last_result,
        "target_time_utc": f"{TARGET_HOUR_UTC:02d}:{TARGET_MINUTE_UTC:02d}",
        "target_year": TARGET_YEAR,
        "dashboards": DASHBOARDS,
    }