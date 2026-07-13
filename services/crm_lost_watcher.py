"""
CRM Lost Watcher — Background Polling Service.

Monitors crm.lead for opportunities marked as lost. When a lead's
won_status becomes 'lost' (or its stage is one of the lost stages),
all pending activities on that lead are automatically cancelled.

Detects lost leads via:
  1. won_status == 'lost'
  2. stage_id in lost stage IDs (stages named "BỎ - ..." or with fold=True)

For each newly lost lead:
  - Find all mail.activity where res_model='crm.lead', res_id=<lead_id>
  - Skip activities where date_done is set (already cancelled/done)
  - Cancel each pending activity via action_done

Processed lead IDs are persisted to {DATA_DIR}/crm_lost_processed.json
so restarts don't re-process old leads.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

ICT = timezone(timedelta(hours=7))

# ─── Lost stage detection ───
# Stages that represent a lost/dead opportunity.
# Detection rules:
#   1. fold=True (folded stages are hidden/lost in Odoo pipeline)
#   2. Name starts with "BỎ -" (Vietnamese "abandoned" stages)
LOST_STAGE_PREFIX = "BỎ"  # Prefix for abandoned/lost stages

PROCESSED_FILE = os.path.join(
    os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")),
    "crm_lost_processed.json",
)

# ─── Runtime state ───
_watcher_running = False
_last_poll = None
_total_polls = 0
_total_cancelled = 0
_lost_stage_ids = None   # cached set of lost stage IDs
_processed_ids = set()   # in-memory set, synced with disk


def _load_processed() -> set[int]:
    if not os.path.exists(PROCESSED_FILE):
        return set()
    try:
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data)
    except Exception as e:
        logger.warning(f"[CRM-LOST] Failed to load processed IDs: {e}")
        return set()


def _save_processed():
    try:
        os.makedirs(os.path.dirname(PROCESSED_FILE) or ".", exist_ok=True)
        with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(_processed_ids)), f)
    except Exception as e:
        logger.error(f"[CRM-LOST] Failed to save processed IDs: {e}")


def _get_lost_stage_ids() -> set[int]:
    """Cache and return Odoo stage IDs that represent a lost opportunity."""
    global _lost_stage_ids
    if _lost_stage_ids is not None:
        return _lost_stage_ids

    try:
        all_stages = odoo.search_read("crm.stage", [], fields=["name", "fold"])
        lost = set()
        for s in all_stages:
            name = s["name"]
            if name.startswith(LOST_STAGE_PREFIX):
                lost.add(s["id"])
            elif s.get("fold"):
                lost.add(s["id"])
        _lost_stage_ids = lost
        logger.info(f"[CRM-LOST] Lost stages: {sorted(lost)} ({len(lost)} total)")
    except Exception as e:
        logger.error(f"[CRM-LOST] Failed to fetch lost stages: {e}")
        _lost_stage_ids = set()
    return _lost_stage_ids


def _cancel_activities(lead_id: int, lead_name: str) -> int:
    """Cancel all pending activities on a lead. Returns count cancelled."""
    try:
        acts = odoo.search_read(
            "mail.activity",
            [
                ["res_model", "=", "crm.lead"],
                ["res_id", "=", lead_id],
                ["date_done", "=", False],
            ],
            fields=["id", "summary", "user_id"],
        )
    except Exception as e:
        logger.error(f"[CRM-LOST] Failed to search activities for lead #{lead_id}: {e}")
        return 0

    if not acts:
        logger.debug(f"[CRM-LOST] Lead #{lead_id} ({lead_name}): no pending activities")
        return 0

    cancelled = 0
    for act in acts:
        try:
            odoo._execute_with_retry(
                "mail.activity", "action_done",
                [act["id"]],
                {},
            )
            user_info = act.get("user_id", ["", ""])
            user_name = user_info[1] if isinstance(user_info, (list, tuple)) and len(user_info) > 1 else "?"
            logger.info(
                f"[CRM-LOST] Cancelled activity #{act['id']} "
                f"'{act.get('summary', '')}' (user: {user_name}) "
                f"on lost lead #{lead_id} ({lead_name})"
            )
            cancelled += 1
        except Exception as e:
            logger.error(
                f"[CRM-LOST] Failed to cancel activity #{act['id']} "
                f"on lead #{lead_id}: {e}"
            )

    return cancelled


def _poll_and_check():
    """Single poll cycle. Detect newly lost leads and cancel their activities.

    Strategy: Find all pending activities on crm.lead, then batch-check
    which of those leads are lost. Much faster than scanning all lost leads
    (DB may have tens of thousands of historical lost leads).
    """
    global _last_poll, _total_polls, _total_cancelled

    _total_polls += 1
    _last_poll = datetime.now(ICT).strftime("%Y-%m-%d %H:%M:%S ICT")

    lost_stages = _get_lost_stage_ids()
    if not lost_stages:
        logger.warning("[CRM-LOST] No lost stages found — skipping poll")
        return

    # Step 1: Find all pending activities on CRM leads
    try:
        pending_acts = odoo.search_read(
            "mail.activity",
            [
                ["res_model", "=", "crm.lead"],
                ["date_done", "=", False],
            ],
            fields=["res_id", "id", "summary", "user_id"],
        )
    except Exception as e:
        logger.error(f"[CRM-LOST] Failed to search pending activities: {e}")
        return

    if not pending_acts:
        logger.debug("[CRM-LOST] No pending activities on any lead — nothing to do")
        return

    # Group activities by lead_id
    by_lead = {}
    for act in pending_acts:
        lid = act["res_id"]
        if lid in _processed_ids:
            continue
        by_lead.setdefault(lid, []).append(act)

    if not by_lead:
        return

    lead_ids = list(by_lead.keys())
    logger.debug(f"[CRM-LOST] Checking {len(lead_ids)} lead(s) with pending activities")

    # Step 2: Batch-check which leads are lost (including archived)
    try:
        lost_leads = odoo._execute_with_retry(
            "crm.lead", "search_read",
            [
                [
                    "&",
                    ["id", "in", lead_ids],
                    "|",
                    ["won_status", "=", "lost"],
                    ["stage_id", "in", list(lost_stages)],
                ],
            ],
            {
                "fields": ["name", "stage_id", "won_status"],
                "context": {"active_test": False},
            },
        )
        if lost_leads is None:
            lost_leads = []
    except Exception as e:
        logger.error(f"[CRM-LOST] Failed to search lost leads: {e}")
        return

    # Step 3: Cancel activities on each lost lead
    new_lost = 0
    for lead in lost_leads:
        lead_id = lead["id"]
        if lead_id in _processed_ids:
            continue

        lead_name = lead["name"]
        stage_info = lead.get("stage_id", ["", ""])
        stage_name = stage_info[1] if isinstance(stage_info, (list, tuple)) else "?"
        ws = lead.get("won_status", "?")

        logger.info(
            f"[CRM-LOST] New lost lead #{lead_id}: {lead_name} "
            f"(stage={stage_name}, won_status={ws})"
        )

        # Cancel activities for this lead
        acts = by_lead.get(lead_id, [])
        cancelled = 0
        for act in acts:
            try:
                odoo._execute_with_retry(
                    "mail.activity", "action_done",
                    [act["id"]],
                    {},
                )
                user_info = act.get("user_id", ["", ""])
                user_name = user_info[1] if isinstance(user_info, (list, tuple)) and len(user_info) > 1 else "?"
                logger.info(
                    f"[CRM-LOST] Cancelled activity #{act['id']} "
                    f"'{act.get('summary', '')}' (user: {user_name}) "
                    f"on lost lead #{lead_id} ({lead_name})"
                )
                cancelled += 1
            except Exception as e:
                logger.error(
                    f"[CRM-LOST] Failed to cancel activity #{act['id']} "
                    f"on lead #{lead_id}: {e}"
                )

        _total_cancelled += cancelled
        _processed_ids.add(lead_id)
        new_lost += 1

    if new_lost:
        _save_processed()
        logger.info(
            f"[CRM-LOST] Poll #{_total_polls}: {new_lost} new lost lead(s), "
            f"{_total_cancelled} total activities cancelled"
        )


def _watcher_loop():
    global _watcher_running, _processed_ids
    _watcher_running = True

    interval = Config.CRM_POLL_INTERVAL
    logger.info(f"[CRM-LOST] Watcher starting — interval: {interval}s")

    time.sleep(15)  # Let server finish starting

    _processed_ids = _load_processed()
    logger.info(f"[CRM-LOST] Loaded {len(_processed_ids)} previously processed lead IDs")

    while _watcher_running:
        try:
            _poll_and_check()
        except Exception as e:
            logger.error(f"[CRM-LOST] Poll error: {e}")

        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and _watcher_running:
            time.sleep(min(5, max(0, deadline - time.monotonic())))


def start_crm_lost_watcher():
    if not Config.ODOO_UID or not Config.ODOO_API_KEY:
        logger.warning("[CRM-LOST] Credentials not set — watcher disabled")
        return

    thread = threading.Thread(
        target=_watcher_loop, daemon=True, name="crm-lost-watcher"
    )
    thread.start()
    poll_interval = Config.CRM_POLL_INTERVAL
    logger.info(f"[CRM-LOST] Watcher started — every {poll_interval}s")


def get_crm_lost_status() -> dict:
    return {
        "active": _watcher_running,
        "poll_interval_seconds": Config.CRM_POLL_INTERVAL,
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_activities_cancelled": _total_cancelled,
        "processed_lead_count": len(_processed_ids),
        "lost_stage_ids": sorted(list(_get_lost_stage_ids())),
    }
