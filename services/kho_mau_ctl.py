"""kho_mau_ctl.py — Scheduled CTL overdue milestone watcher.

Scans ``stock.picking`` for CTL (kho mẫu) return transfers whose lending
duration has crossed the D30 / D60 / D90 milestones and creates a
``mail.activity`` for the responsible user (Ngô Trọng Cả) so the overdue
loan is followed up. Only the highest applicable milestone is fired per
picking; already-fired milestones are skipped via summary deduplication.

Runs once per day at 11:00 ICT (04:00 UTC) in a daemon thread and exposes
its last run + result for status inspection. Converted from
``kho_mau_ctl_overdue.py``.
"""

import json
import logging
import threading
import time
from datetime import date, datetime, timezone, timedelta

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler configuration
# ---------------------------------------------------------------------------
TARGET_HOUR_UTC = 4
TARGET_MINUTE_UTC = 0  # 04:00 UTC == 11:00 ICT

# ---------------------------------------------------------------------------
# Scheduler state
# ---------------------------------------------------------------------------
_scheduler_running = False
_last_run = None
_last_result = None

# ---------------------------------------------------------------------------
# Business configuration
# ---------------------------------------------------------------------------
MILESTONES = [30, 60, 90]

PICKING_TYPE_ID = Config.CTL_PICKING_TYPE_ID
LOCATION_CTL_ID = Config.CTL_LOCATION_ID
USER_NGO_TRONG_CA = Config.CTL_USER_ID
ACTIVITY_TYPE_NAME = Config.CTL_ACTIVITY_TYPE_NAME
LENDING_MIN_DATE = Config.CTL_LENDING_MIN_DATE

# ---------------------------------------------------------------------------
# Lookup caches
# ---------------------------------------------------------------------------
_stock_picking_model_id = None
_activity_type_id = None


def _parse_date(val):
    """Parse an Odoo date/datetime value into a ``date`` object."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _ensure_lookups():
    """Resolve and cache the ir.model id for stock.picking and the activity type id."""
    global _stock_picking_model_id, _activity_type_id

    if _stock_picking_model_id is None:
        rows = odoo.search_read(
            "ir.model",
            [("model", "=", "stock.picking")],
            ["id", "model"],
        )
        if rows:
            _stock_picking_model_id = rows[0]["id"]
            logger.info("[CTL] Cached stock.picking ir.model id=%s", _stock_picking_model_id)
        else:
            logger.error("[CTL] ir.model for stock.picking not found")
            raise RuntimeError("ir.model stock.picking not found")

    if _activity_type_id is None:
        rows = odoo.search_read(
            "mail.activity.type",
            [("name", "=", ACTIVITY_TYPE_NAME)],
            ["id", "name"],
        )
        if rows:
            _activity_type_id = rows[0]["id"]
            logger.info("[CTL] Cached mail.activity.type id=%s (%s)", _activity_type_id, ACTIVITY_TYPE_NAME)
        else:
            logger.error("[CTL] mail.activity.type '%s' not found", ACTIVITY_TYPE_NAME)
            raise RuntimeError(f"mail.activity.type '{ACTIVITY_TYPE_NAME}' not found")


def fetch_return_pickings():
    """Fetch assigned CTL return pickings that have lending + send-back dates.

    Ordered by ``x_studio_lending_date`` ascending. Date fields are parsed
    into ``date`` objects in place.
    """
    domain = [
        ("picking_type_id", "=", PICKING_TYPE_ID),
        ("location_dest_id", "=", LOCATION_CTL_ID),
        ("state", "=", "assigned"),
        ("x_studio_lending_date", ">=", LENDING_MIN_DATE),
        ("x_studio_send_back_date_1", "!=", False),
    ]
    fields = [
        "id", "name", "state", "partner_id", "scheduled_date",
        "x_studio_lending_date", "x_studio_send_back_date_1", "origin", "note",
    ]
    rows = odoo.execute(
        "stock.picking",
        "search_read",
        [domain],
        {"fields": fields, "order": "x_studio_lending_date asc"},
    )
    pickings = []
    for r in rows or []:
        r["x_studio_lending_date"] = _parse_date(r.get("x_studio_lending_date"))
        r["x_studio_send_back_date_1"] = _parse_date(r.get("x_studio_send_back_date_1"))
        pickings.append(r)
    logger.info("[CTL] Fetched %d assigned CTL return pickings", len(pickings))
    return pickings


def existing_milestone_activities(picking_ids):
    """Return ``{picking_id: set(milestones)}`` already fired for the pickings."""
    if not picking_ids:
        return {}
    domain = [
        ("res_model", "=", "stock.picking"),
        ("res_id", "in", picking_ids),
        ("activity_type_id", "=", _activity_type_id),
        ("active", "=", True),
    ]
    rows = odoo.search_read("mail.activity", domain, ["res_id", "summary"])
    result = {}
    for r in rows or []:
        pid = r.get("res_id")
        summary = r.get("summary") or ""
        fired = {m for m in MILESTONES if f"D{m}" in summary}
        if pid is not None:
            result.setdefault(pid, set()).update(fired)
    return result


def _partner_name(partner_id):
    """Extract a display name from a many2one value (list/tuple/scalar/False)."""
    if isinstance(partner_id, (list, tuple)) and len(partner_id) >= 2:
        return partner_id[1]
    if partner_id:
        return str(partner_id)
    return "—"


def create_activity(picking, milestone, dry_run=False):
    """Create a ``mail.activity`` for a single picking/milestone.

    Returns the new activity id, or ``None`` when dry-running.
    """
    lending_date = picking.get("x_studio_lending_date")
    send_back_date = picking.get("x_studio_send_back_date_1")
    gap = (send_back_date - lending_date).days if lending_date and send_back_date else 0

    summary = f"CTL - Quá hạn D{milestone}: {picking.get('name')}"
    partner_name = _partner_name(picking.get("partner_id"))
    note = (
        "<div>"
        f"<p><strong>CTL - Quá hạn D{milestone}</strong></p>"
        f"<p>Phiếu: {picking.get('name')}</p>"
        f"<p>Khách hàng: {partner_name}</p>"
        f"<p>Ngày mượn: {lending_date.isoformat() if lending_date else ''}</p>"
        f"<p>Ngày hẹn trả: {send_back_date.isoformat() if send_back_date else ''}</p>"
        f"<p>Số ngày (gap): {gap}</p>"
        f"<p>Origin: {picking.get('origin') or ''}</p>"
        "</div>"
    )

    vals = {
        "res_model": "stock.picking",
        "res_model_id": _stock_picking_model_id,
        "res_id": picking["id"],
        "activity_type_id": _activity_type_id,
        "summary": summary,
        "note": note,
        "date_deadline": date.today().isoformat(),
        "user_id": USER_NGO_TRONG_CA,
    }

    if dry_run:
        logger.info("[CTL] DRY-RUN create_activity %s | %s | gap=%d", picking.get("name"), summary, gap)
        return None

    new_id = odoo.create("mail.activity", vals)
    logger.info("[CTL] Created mail.activity id=%s for %s (%s)", new_id, picking.get("name"), summary)
    return new_id


def run_ctl_overdue_check(dry_run=False):
    """Scan CTL return pickings and fire the highest overdue milestone activity."""
    global _last_run, _last_result

    logger.info("[CTL] run_ctl_overdue_check start (dry_run=%s)", dry_run)
    _ensure_lookups()
    pickings = fetch_return_pickings()

    result = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "total_pickings": len(pickings),
        "created": 0,
        "skipped_existing": 0,
        "skipped_gap": 0,
    }

    if not pickings:
        _last_run = result["ran_at"]
        _last_result = result
        logger.info("[CTL] No CTL return pickings found — nothing to do")
        return result

    picking_ids = [p["id"] for p in pickings]
    existing = existing_milestone_activities(picking_ids)

    for picking in pickings:
        lending_date = picking.get("x_studio_lending_date")
        send_back_date = picking.get("x_studio_send_back_date_1")
        if not lending_date or not send_back_date:
            result["skipped_gap"] += 1
            continue
        gap = (send_back_date - lending_date).days
        highest_due = max((m for m in MILESTONES if gap >= m), default=None)
        if highest_due is None:
            result["skipped_gap"] += 1
            continue
        if highest_due in existing.get(picking["id"], set()):
            result["skipped_existing"] += 1
            continue
        new_id = create_activity(picking, highest_due, dry_run=dry_run)
        if new_id:
            result["created"] += 1

    _last_run = result["ran_at"]
    _last_result = result
    logger.info("[CTL] run_ctl_overdue_check done — %s", json.dumps(result))
    return result


# ---------------------------------------------------------------------------
# Daily scheduler (11:00 ICT / 04:00 UTC)
# ---------------------------------------------------------------------------
def _seconds_until_next_run():
    """Seconds remaining until the next 04:00 UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=TARGET_HOUR_UTC, minute=TARGET_MINUTE_UTC, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _scheduler_loop():
    """Main scheduler loop — waits for 04:00 UTC then runs the check daily."""
    global _scheduler_running
    logger.info("[CTL] Scheduler loop started")
    time.sleep(30)
    while _scheduler_running:
        wait = _seconds_until_next_run()
        logger.info("[CTL] Next run in %.1fh", wait / 3600.0)
        # Interruptible sleep so stop requests are responsive.
        end = time.time() + wait
        while time.time() < end and _scheduler_running:
            time.sleep(min(30, max(0.0, end - time.time())))
        if not _scheduler_running:
            break
        try:
            run_ctl_overdue_check(dry_run=False)
        except Exception:
            logger.exception("[CTL] run_ctl_overdue_check failed")
        time.sleep(120)  # safety pause between daily cycles
    logger.info("[CTL] Scheduler loop stopped")


def start_ctl_scheduler():
    """Start the CTL overdue scheduler as a daemon thread (if enabled)."""
    global _scheduler_running
    if not getattr(Config, "CTL_ENABLED", False):
        logger.info("[CTL] Scheduler disabled (Config.CTL_ENABLED is False) — not starting")
        return
    if not (getattr(Config, "ODOO_URL", None) and getattr(Config, "ODOO_DB", None)
            and getattr(Config, "ODOO_UID", None)):
        logger.warning("[CTL] Missing Odoo credentials — not starting scheduler")
        return
    if _scheduler_running:
        logger.info("[CTL] Scheduler already running")
        return
    _scheduler_running = True
    thread = threading.Thread(target=_scheduler_loop, name="ctl-scheduler", daemon=True)
    thread.start()
    logger.info("[CTL] Scheduler started — target: 11:00 ICT (04:00 UTC) daily")


def get_ctl_status():
    """Return a snapshot of the scheduler state for inspection."""
    return {
        "scheduler_active": _scheduler_running,
        "target_time": f"{TARGET_HOUR_UTC:02d}:{TARGET_MINUTE_UTC:02d} UTC (11:00 ICT)",
        "last_run": _last_run,
        "last_result_summary": _last_result,
    }