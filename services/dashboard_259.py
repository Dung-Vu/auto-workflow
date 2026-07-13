"""
Dashboard 259 "Approval" refresh service.

Scheduled service that refreshes Dashboard 259 "Approval" columns:
  - Column F (Chờ approve) — count of *planned* approval activities per
    purchase-order owner.
  - Column G (Đã approve chưa TT) — count of *done* approval activities
    whose purchase order is not yet fully paid (no invoice, or any invoice
    in ``not_paid`` / ``partial`` payment state).

The scheduler triggers monthly on the 1st at 06:00 ICT, which is 23:00 UTC
the previous day.  All Odoo interactions go through the ``_odoo_call``
JSON-RPC helper, which provides automatic retry (3 attempts, exponential
back-off) on transient errors.
"""

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests

from config import Config
from services.odoo_client import odoo  # noqa: F401  — kept for pattern parity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler state globals
# ---------------------------------------------------------------------------
_scheduler_running = False
_last_run = None
_last_result = None

# ---------------------------------------------------------------------------
# Target time — 23:00 UTC == 06:00 ICT (next day) == 1st of month
# ---------------------------------------------------------------------------
TARGET_HOUR_UTC = 23
TARGET_MINUTE_UTC = 0

# ---------------------------------------------------------------------------
# Dashboard 259 business constants
# ---------------------------------------------------------------------------
USER_TO_ROW = {220: 4, 258: 5, 262: 6, 249: 7, 122: 8, 120: 9, 276: 10, 270: 11}
TOTAL_ROW = 12

APPROVAL_TYPE_ID = getattr(Config, "DASHBOARD_259_APPROVAL_TYPE_ID", 13)
DASHBOARD_ID = getattr(Config, "DASHBOARD_259_ID", 259)

_RETRYABLE_ERRORS = [
    "429", "Too Many Requests", "Connection refused", "timed out",
    "Read timed out", "IncompleteRead", "Bad Gateway", "502", "503",
    "Connection reset", "No address associated", "Name or service not known",
]

# ---------------------------------------------------------------------------
# Odoo JSON-RPC helper
# ---------------------------------------------------------------------------

def _odoo_call(model, method, args=None, kwargs=None):
    """Execute an Odoo ``execute_kw`` JSON-RPC call with retry logic.

    Uses Dashboard-259-specific credentials when available, falling back to
    the generic ``Config.ODOO_UID`` / ``Config.ODOO_API_KEY``.  Retries up to
    3 times with exponential back-off (1 s, 2 s, 4 s) on transient errors.
    """
    args = args if args is not None else []
    kwargs = kwargs if kwargs is not None else {}

    uid = getattr(Config, "DASHBOARD_259_ODOO_UID", None) or Config.ODOO_UID
    api_key = getattr(Config, "DASHBOARD_259_ODOO_API_KEY", None) or Config.ODOO_API_KEY
    db = Config.ODOO_DB

    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [db, uid, api_key, model, method, args, kwargs],
        },
        "id": 1,
    }

    url = f"{Config.ODOO_URL}/jsonrpc"
    last_exc = None

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=60)
            data = response.json()
            if "error" in data:
                err_msg = json.dumps(data["error"])
                if any(token in err_msg for token in _RETRYABLE_ERRORS):
                    last_exc = RuntimeError(f"Odoo RPC error: {err_msg}")
                    logger.warning("[D259] Transient Odoo error (attempt %d/3): %s",
                                   attempt + 1, err_msg)
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Odoo RPC error: {err_msg}")
            return data.get("result")
        except Exception as exc:
            err_msg = str(exc)
            if any(token in err_msg for token in _RETRYABLE_ERRORS):
                last_exc = exc
                logger.warning("[D259] Transient error (attempt %d/3): %s",
                               attempt + 1, err_msg)
                time.sleep(2 ** attempt)
                continue
            raise

    raise last_exc

# ---------------------------------------------------------------------------
# Core refresh logic
# ---------------------------------------------------------------------------

def run_dashboard_259_refresh(dry_run=False):
    """Refresh Dashboard 259 approval columns F and G.

    Returns a result dict and updates the ``_last_run`` / ``_last_result``
    module-level globals.
    """
    global _last_run, _last_result

    ran_at = datetime.now(timezone.utc).isoformat()
    logger.info("[D259] Starting refresh (dry_run=%s)", dry_run)

    # --- Column F: planned approval activities (Chờ approve) ---
    planned_ids = _odoo_call(
        "mail.activity", "search",
        [[
            ("res_model", "=", "purchase.order"),
            ("state", "=", "planned"),
            ("activity_type_id", "=", APPROVAL_TYPE_ID),
        ]],
    )
    logger.info("[D259] Planned approval activities: %d", len(planned_ids))

    f_counts = defaultdict(int)
    if planned_ids:
        planned_records = _odoo_call(
            "mail.activity", "read", [planned_ids, ["res_id"]],
        )
        f_po_ids = [r["res_id"] for r in planned_records]
        if f_po_ids:
            f_pos = _odoo_call(
                "purchase.order", "read", [f_po_ids, ["id", "user_id"]],
            )
            for po in f_pos:
                uid = po.get("user_id")
                if isinstance(uid, (list, tuple)):
                    uid = uid[0]
                if uid in USER_TO_ROW:
                    f_counts[uid] += 1

    f_total = sum(f_counts.values())

    # --- Column G: done approval activities (Đã approve chưa TT) ---
    done_ids = _odoo_call(
        "mail.activity", "search",
        [[
            ("res_model", "=", "purchase.order"),
            ("state", "=", "done"),
            ("activity_type_id", "=", APPROVAL_TYPE_ID),
        ]],
    )
    logger.info("[D259] Done approval activities: %d", len(done_ids))

    g_counts = defaultdict(int)
    if done_ids:
        done_records = _odoo_call(
            "mail.activity", "read", [done_ids, ["res_id"]],
        )
        g_po_ids = [r["res_id"] for r in done_records]
        if g_po_ids:
            g_pos = _odoo_call(
                "purchase.order", "read",
                [g_po_ids, ["id", "user_id", "invoice_ids"]],
            )

            # Batch-fetch payment_state for every invoice across all POs
            all_invoice_ids = []
            for po in g_pos:
                all_invoice_ids.extend(po.get("invoice_ids", []))

            invoice_states = {}
            if all_invoice_ids:
                inv_records = _odoo_call(
                    "account.move", "read",
                    [all_invoice_ids, ["id", "payment_state"]],
                )
                for inv in inv_records:
                    invoice_states[inv["id"]] = inv.get("payment_state")

            for po in g_pos:
                uid = po.get("user_id")
                if isinstance(uid, (list, tuple)):
                    uid = uid[0]
                if uid not in USER_TO_ROW:
                    continue
                inv_ids = po.get("invoice_ids", [])
                if not inv_ids:
                    g_counts[uid] += 1
                else:
                    states = [invoice_states.get(iid) for iid in inv_ids]
                    if any(s in ("not_paid", "partial") for s in states):
                        g_counts[uid] += 1

    g_total = sum(g_counts.values())

    # --- Build cell updates ---
    cell_updates = {}
    for uid, row in USER_TO_ROW.items():
        cell_updates[f"F{row}"] = f_counts.get(uid, 0)
        cell_updates[f"G{row}"] = g_counts.get(uid, 0)
    cell_updates[f"F{TOTAL_ROW}"] = f_total
    cell_updates[f"G{TOTAL_ROW}"] = g_total

    n_cell_updates = len(cell_updates)
    logger.info("[D259] Cell updates: %s", cell_updates)

    # --- Apply to spreadsheet ---
    if not dry_run:
        dash_records = _odoo_call(
            "spreadsheet.dashboard", "read",
            [DASHBOARD_ID, ["spreadsheet_data"]],
        )
        if dash_records:
            raw = dash_records[0].get("spreadsheet_data") or "{}"
            sheet = json.loads(raw)

            sheets = sheet.get("sheets", [])
            if sheets:
                cells = sheets[0].setdefault("cells", {})
                for cell_ref, value in cell_updates.items():
                    if cell_ref in cells:
                        cells[cell_ref]["content"] = str(value)
                    else:
                        cells[cell_ref] = {"content": str(value)}

            sheet["revisionId"] = str(uuid.uuid4())

            _odoo_call(
                "spreadsheet.dashboard", "write",
                [DASHBOARD_ID, {"spreadsheet_data": json.dumps(sheet)}],
            )
            logger.info("[D259] Wrote %d cells to dashboard %s",
                        n_cell_updates, DASHBOARD_ID)
        else:
            logger.warning("[D259] Dashboard %s not found", DASHBOARD_ID)

    # --- Build result ---
    result = {
        "ran_at": ran_at,
        "dashboard_id": DASHBOARD_ID,
        "dry_run": dry_run,
        "f_counts": dict(f_counts),
        "g_counts": dict(g_counts),
        "f_total": f_total,
        "g_total": g_total,
        "n_cell_updates": n_cell_updates,
    }

    _last_run = ran_at
    _last_result = result
    logger.info("[D259] Refresh complete — F total=%d, G total=%d",
                f_total, g_total)
    return result

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def _seconds_until_next_run():
    """Return seconds until the next 1st-of-month 23:00 UTC."""
    now = datetime.now(timezone.utc)

    today_target = now.replace(
        hour=TARGET_HOUR_UTC, minute=TARGET_MINUTE_UTC, second=0, microsecond=0,
    )

    if now.day == 1 and now < today_target:
        return (today_target - now).total_seconds()

    # First of next month at 23:00 UTC
    year, month = now.year, now.month + 1
    if month > 12:
        month = 1
        year += 1
    next_target = datetime(
        year, month, 1, TARGET_HOUR_UTC, TARGET_MINUTE_UTC, 0, tzinfo=timezone.utc,
    )
    return (next_target - now).total_seconds()


def _scheduler_loop():
    """Main scheduler loop — sleep until target, run refresh, repeat."""
    global _scheduler_running
    time.sleep(30)
    while _scheduler_running:
        wait = _seconds_until_next_run()
        logger.info("[D259] Next run in %.1f hours", wait / 3600)

        # Interruptible sleep — check _scheduler_running every 30 s
        end_time = time.time() + wait
        while _scheduler_running and time.time() < end_time:
            time.sleep(min(30, max(0, end_time - time.time())))

        if not _scheduler_running:
            break

        try:
            run_dashboard_259_refresh(dry_run=False)
        except Exception:
            logger.exception("[D259] Refresh failed")

        time.sleep(120)


def start_dashboard_259_scheduler():
    """Start the Dashboard 259 refresh scheduler as a daemon thread."""
    global _scheduler_running

    if not getattr(Config, "DASHBOARD_259_ENABLED", False):
        logger.info("[D259] Scheduler disabled (DASHBOARD_259_ENABLED not set)")
        return

    uid = getattr(Config, "DASHBOARD_259_ODOO_UID", None) or getattr(Config, "ODOO_UID", None)
    api_key = getattr(Config, "DASHBOARD_259_ODOO_API_KEY", None) or getattr(Config, "ODOO_API_KEY", None)
    if not uid or not api_key:
        logger.warning("[D259] Missing Odoo credentials — scheduler not started")
        return

    if _scheduler_running:
        logger.info("[D259] Scheduler already running")
        return

    _scheduler_running = True
    thread = threading.Thread(
        target=_scheduler_loop, name="dashboard-259-scheduler", daemon=True,
    )
    thread.start()
    logger.info("[D259] Scheduler started — target: 1st of month "
                "06:00 ICT (23:00 UTC prev day)")


def get_dashboard_259_status():
    """Return current scheduler status and last-run summary."""
    summary = None
    if _last_result:
        summary = {
            "f_total": _last_result.get("f_total"),
            "g_total": _last_result.get("g_total"),
            "n_cell_updates": _last_result.get("n_cell_updates"),
            "dry_run": _last_result.get("dry_run"),
        }
    return {
        "scheduler_active": _scheduler_running,
        "target_time": "1st of month 06:00 ICT (23:00 UTC prev day)",
        "dashboard_id": DASHBOARD_ID,
        "last_run": _last_run,
        "last_result_summary": summary,
    }


