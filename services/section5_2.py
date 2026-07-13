"""Scheduled service: refresh Section 5.2 (SO-to-DVHT timing violations)
table on individual dashboards 265 / 268 / 269.

Runs monthly on the 1st at 06:00 ICT, i.e. 23:00 UTC on the previous calendar
day. For each dashboard owner the service pulls confirmed sale.orders assigned
to the user (x_studio_op_user_id), links them to their FSM project tasks, and
computes per-month SO counts plus timing violations — an SO counts as a
violation when its earliest FSM task was created more than 2 days after the
SO's date_order. The values are written back into the spreadsheet dashboard
cells:

    rows data_start..data_start+11 (months 1-12):
        A{row}=T{m}/26   B{row}=total   C{row}=violation   D{row}=pct

A background scheduler thread waits until the next 1st-of-month 23:00 UTC and
triggers run_section5_2_refresh() automatically. Manual refreshes are possible
via run_section5_2_refresh(dry_run=True|False).
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
    265: {"user_id": 122, "name": "TRANG", "data_start": 128},
    268: {"user_id": 249, "name": "MAI HỒNG", "data_start": 94},
    269: {"user_id": 276, "name": "MAI THU", "data_start": 94},
}

TARGET_YEAR = 2026


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def fetch_user_so_fsm(user_id):
    """Return confirmed SOs for ``user_id`` and their linked FSM tasks.

    Uses ``active_test=False`` so archived records are included. Reads are
    batched (500 records) to stay well under XML-RPC response limits.
    """
    ctx = {"active_test": False}
    so_domain = [
        ["x_studio_op_user_id", "=", user_id],
        ["state", "=", "sale"],
        ["date_order", ">=", "2026-01-01"],
        ["date_order", "<", "2027-01-01"],
    ]
    so_ids = odoo.execute("sale.order", "search", [so_domain], {"context": ctx})
    if not so_ids:
        return {"sos": [], "tasks": []}

    batch = 500
    sos = []
    for i in range(0, len(so_ids), batch):
        recs = odoo.execute(
            "sale.order",
            "search_read",
            [[["id", "in", so_ids[i:i + batch]]]],
            {"fields": ["id", "date_order", "name"], "context": ctx},
        )
        sos.extend(recs)

    so_map = {so["id"]: so for so in sos}

    task_ids = odoo.execute(
        "project.task",
        "search",
        [[["sale_order_id", "in", list(so_map.keys())], ["is_fsm", "=", True]]],
        {"context": ctx},
    )
    if not task_ids:
        return {"sos": sos, "tasks": []}

    tasks = []
    for i in range(0, len(task_ids), batch):
        recs = odoo.execute(
            "project.task",
            "search_read",
            [[["id", "in", task_ids[i:i + batch]]]],
            {"fields": ["id", "sale_order_id", "create_date"], "context": ctx},
        )
        tasks.extend(recs)

    return {"sos": sos, "tasks": tasks}


def compute_monthly_metrics(data):
    """Aggregate per-month SO totals and >2-day DVHT timing violations.

    For each SO we keep only the earliest FSM task create_date; the delay
    (task_date - so_date).days is a violation when strictly greater than 2.
    Only SOs whose date_order falls in TARGET_YEAR are counted.
    """
    monthly = defaultdict(lambda: {"total": 0, "violation": 0})

    so_dates = {
        so["id"]: date.fromisoformat(so["date_order"][:10])
        for so in data["sos"]
        if so.get("date_order")
    }

    so_first_task = {}
    for task in data["tasks"]:
        so_id = task.get("sale_order_id")
        if not so_id or so_id[0] not in so_dates:
            continue
        tc = task.get("create_date")
        if not tc:
            continue
        task_dt = date.fromisoformat(tc[:10])
        sid = so_id[0]
        if sid not in so_first_task or task_dt < so_first_task[sid]:
            so_first_task[sid] = task_dt

    for so_id, task_dt in so_first_task.items():
        so_date = so_dates[so_id]
        if so_date.year != TARGET_YEAR:
            continue
        delay = (task_dt - so_date).days
        monthly[so_date.month]["total"] += 1
        if delay > 2:
            monthly[so_date.month]["violation"] += 1

    return dict(monthly)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------
def run_section5_2_refresh(dry_run=False):
    """Refresh Section 5.2 SO->DVHT timing tables on all configured dashboards."""
    result = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "dashboards": [],
    }

    for dash_id, cfg in DASHBOARDS.items():
        user_id = cfg["user_id"]
        name = cfg["name"]
        data_start = cfg["data_start"]

        raw = fetch_user_so_fsm(user_id)
        monthly = compute_monthly_metrics(raw)

        recs = odoo.read(
            "spreadsheet.dashboard",
            [dash_id],
            ["name", "spreadsheet_data"],
        )
        if not recs:
            logger.warning("[S5.2] dashboard %s not found; skipping", dash_id)
            result["dashboards"].append({
                "dash_id": dash_id,
                "user_id": user_id,
                "name": name,
                "so_count": len(raw["sos"]),
                "task_count": len(raw["tasks"]),
                "total_violations": 0,
                "error": "dashboard not found",
            })
            continue

        data = json.loads(recs[0]["spreadsheet_data"])
        sheet = data["sheets"][0]
        cells = sheet["cells"]

        total_violations = 0
        for m in range(1, 13):
            row = data_start + m - 1
            m_metric = monthly.get(m, {"total": 0, "violation": 0})
            total = m_metric["total"]
            violation = m_metric["violation"]
            total_violations += violation
            pct = round(violation / total, 4) if total > 0 else 0
            cells[f"A{row}"] = f"T{m}/26"
            cells[f"B{row}"] = str(total) if total > 0 else "—"
            cells[f"C{row}"] = str(violation)
            cells[f"D{row}"] = str(pct)

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
            "so_count": len(raw["sos"]),
            "task_count": len(raw["tasks"]),
            "total_violations": total_violations,
        })
        logger.info(
            "[S5.2] %s (dash %s, user %s): sos=%s tasks=%s violations=%s%s",
            name, dash_id, user_id, len(raw["sos"]), len(raw["tasks"]),
            total_violations, " [dry-run]" if dry_run else "",
        )

    global _last_run, _last_result
    _last_run = datetime.now(timezone.utc)
    _last_result = result
    logger.info(
        "[S5.2] refresh complete: dashboards=%s dry_run=%s",
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
    """Background loop that runs Section 5.2 refresh on the 1st at 23:00 UTC."""
    global _scheduler_running
    logger.info("[S5.2] scheduler loop started")
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
            run_section5_2_refresh(dry_run=False)
        except Exception:
            logger.exception("[S5.2] scheduled refresh failed")

        time.sleep(120)
    logger.info("[S5.2] scheduler loop stopped")


def start_section5_2_scheduler():
    """Start the background scheduler thread (idempotent)."""
    global _scheduler_running
    if _scheduler_running:
        logger.warning("[S5.2] scheduler already running")
        return

    if not getattr(Config, "SECTION5_2_ENABLED", False):
        logger.info("[S5.2] scheduler disabled (Config.SECTION5_2_ENABLED not set)")
        return

    if not (
        getattr(Config, "ODOO_URL", None)
        and getattr(Config, "ODOO_DB", None)
        and getattr(Config, "ODOO_API_KEY", None)
        and (getattr(Config, "ODOO_UID", None) or getattr(Config, "ODOO_USER", None))
    ):
        logger.warning("[S5.2] missing Odoo credentials; not starting scheduler")
        return

    _scheduler_running = True
    thread = threading.Thread(
        target=_scheduler_loop,
        name="section5-2-scheduler",
        daemon=True,
    )
    thread.start()
    logger.info("[S5.2] scheduler thread started: section5-2-scheduler")


def get_section5_2_status():
    """Return the current scheduler status for health checks."""
    return {
        "running": _scheduler_running,
        "enabled": getattr(Config, "SECTION5_2_ENABLED", False),
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_result": _last_result,
        "target_time_utc": f"{TARGET_HOUR_UTC:02d}:{TARGET_MINUTE_UTC:02d}",
        "target_year": TARGET_YEAR,
        "dashboards": DASHBOARDS,
    }
