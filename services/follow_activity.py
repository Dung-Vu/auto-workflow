"""
Follow Activity on Order State Change — Background Polling Service.

Monitors sale.order for changes to x_studio_selection_field_q4_1imrcsjj8 (Order State).
When the field changes to a trigger value (e.g. 'Hàng về/Chờ thi công'),
creates Follow activities (mail.activity type=Follow) for:
  - Fixed users (configurable: Ngô Trọng Cả, Hoàng Thị Ngọc Bích, Nguyễn Quỳnh Như)
  - The SO's salesperson (dynamic — user_id field)

Snapshot {so_id: last_known_state} is persisted to {DATA_DIR}/follow_activity_snapshot.json
so the service only triggers on actual STATE CHANGES, not every poll cycle.
State survives container restarts.
"""

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ─── Config ───
_TRIGGER_VALUES = set(Config.FOLLOW_ACTIVITY_TRIGGER_VALUES)
_POLL_INTERVAL = Config.FOLLOW_ACTIVITY_POLL_INTERVAL
_ACTIVITY_TYPE_ID = Config.FOLLOW_ACTIVITY_TYPE_ID
_FIXED_USER_IDS = set(Config.FOLLOW_ACTIVITY_FIXED_USER_IDS)
_DEADLINE_DAYS = Config.FOLLOW_ACTIVITY_DEADLINE_DAYS

# ─── Runtime state ───
_watcher_running = False
_last_poll = None
_total_polls = 0
_total_activities_created = 0
_total_so_triggered = 0

# ─── Cached ir.model id for sale.order ───
_so_model_id = None

# ─── Snapshot persistence ───
SNAPSHOT_FILE = os.path.join(
    os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")),
    "follow_activity_snapshot.json",
)


def _load_snapshot() -> dict:
    """Load snapshot from disk. Returns {so_id_str: last_known_state}."""
    if not os.path.exists(SNAPSHOT_FILE):
        logger.info("[FOLLOW] No snapshot file — starting fresh")
        return {}
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"[FOLLOW] Loaded snapshot: {len(data)} SO states tracked")
        return data
    except Exception as e:
        logger.warning(f"[FOLLOW] Failed to load snapshot: {e} — starting fresh")
        return {}


def _save_snapshot(snapshot: dict):
    """Persist snapshot to disk."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_FILE) or ".", exist_ok=True)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[FOLLOW] Failed to save snapshot: {e}")


def _ensure_model_id():
    """Cache ir.model id for sale.order (needed for mail.activity res_model_id)."""
    global _so_model_id
    if _so_model_id is not None:
        return _so_model_id
    ir = odoo.search_read("ir.model", [["model", "=", "sale.order"]],
                          fields=["id", "model"], limit=1)
    if ir:
        _so_model_id = ir[0]["id"]
        logger.info(f"[FOLLOW] Cached ir.model id for sale.order: {_so_model_id}")
    else:
        raise RuntimeError("ir.model 'sale.order' not found")
    return _so_model_id


# ═══════════════════════════════════════════
#  CORE LOGIC
# ═══════════════════════════════════════════

def _fetch_sos_with_trigger_state() -> list:
    """Fetch sale.order records where Order State is in trigger values."""
    so_ids = odoo.search("sale.order", [
        ["x_studio_selection_field_q4_1imrcsjj8", "in", list(_TRIGGER_VALUES)],
    ], limit=200)
    if not so_ids:
        return []
    return odoo.read("sale.order", so_ids, fields=[
        "id", "name", "partner_id", "user_id",
        "x_studio_selection_field_q4_1imrcsjj8",
    ])


def _create_follow_activity(so: dict, user_id: int, user_name: str) -> int:
    """Create a single Follow activity for a user on a sale.order. Returns activity ID."""
    model_id = _ensure_model_id()
    so_name = so.get("name", "?")
    order_state = so.get("x_studio_selection_field_q4_1imrcsjj8", "?")

    partner = so.get("partner_id")
    partner_name = partner[1] if isinstance(partner, (list, tuple)) and len(partner) > 1 else "?"

    summary = f"Follow up: {so_name} — {order_state}"
    note = (
        f"<p><strong>📋 Follow-up Activity</strong></p>"
        f"<ul>"
        f"<li>SO: <strong>{so_name}</strong></li>"
        f"<li>Customer: {partner_name}</li>"
        f"<li>Order State: <strong>{order_state}</strong></li>"
        f"</ul>"
    )

    deadline = (date.today() + timedelta(days=_DEADLINE_DAYS)).isoformat()

    vals = {
        "res_model": "sale.order",
        "res_model_id": model_id,
        "res_id": so["id"],
        "activity_type_id": _ACTIVITY_TYPE_ID,
        "summary": summary,
        "note": note,
        "date_deadline": deadline,
        "user_id": user_id,
    }

    activity_id = odoo.create("mail.activity", vals)
    logger.info(f"[FOLLOW]   ✅ Activity #{activity_id} → {user_name} (uid={user_id}) "
                f"on {so_name}")
    return activity_id


def _poll_and_create_activities(snapshot: dict) -> dict:
    """Poll for SOs with trigger state and create activities for state changes."""
    global _total_activities_created, _total_so_triggered

    # 1. Fetch SOs currently in trigger state
    sos = _fetch_sos_with_trigger_state()
    if not sos:
        logger.debug(f"[FOLLOW] Poll #{_total_polls}: no SOs in trigger state")
        return snapshot

    logger.info(f"[FOLLOW] Poll #{_total_polls}: {len(sos)} SO(s) in trigger state")

    # 2. For each SO, check if state CHANGED to trigger value
    for so in sos:
        so_id = so["id"]
        so_id_str = str(so_id)
        current_state = so.get("x_studio_selection_field_q4_1imrcsjj8", "")
        last_state = snapshot.get(so_id_str)

        # Skip if state hasn't changed (already in trigger state on previous poll)
        if last_state == current_state:
            continue

        # State changed TO a trigger value → create activities
        if current_state in _TRIGGER_VALUES:
            logger.info(f"[FOLLOW]   {so.get('name','?')}: state changed "
                        f"'{last_state}' → '{current_state}' — creating activities")

            # Build target user list: fixed users + salesperson (dedup)
            salesperson = so.get("user_id")
            sp_id = salesperson[0] if isinstance(salesperson, (list, tuple)) else None
            sp_name = salesperson[1] if isinstance(salesperson, (list, tuple)) and len(salesperson) > 1 else "?"

            target_users = set(_FIXED_USER_IDS)
            if sp_id:
                target_users.add(sp_id)

            # Fetch user names for fixed users
            user_names = {}
            fixed_to_lookup = list(_FIXED_USER_IDS)
            if fixed_to_lookup:
                users = odoo.read("res.users", fixed_to_lookup, fields=["id", "name"])
                user_names = {u["id"]: u["name"] for u in users}
            if sp_id:
                user_names[sp_id] = sp_name

            # Create activity for each target user
            created = 0
            for uid in sorted(target_users):
                uname = user_names.get(uid, f"User {uid}")
                try:
                    _create_follow_activity(so, uid, uname)
                    created += 1
                except Exception as e:
                    logger.error(f"[FOLLOW]   ❌ Failed to create activity for "
                                 f"{uname} (uid={uid}) on {so.get('name','?')}: {e}")

            _total_activities_created += created
            _total_so_triggered += 1
            logger.info(f"[FOLLOW]   {so.get('name','?')}: {created} activity(ies) created")

        # Update snapshot with current state
        snapshot[so_id_str] = current_state

    return snapshot


# ═══════════════════════════════════════════
#  BACKGROUND THREAD
# ═══════════════════════════════════════════

def _watcher_loop():
    """Background loop: poll every FOLLOW_ACTIVITY_POLL_INTERVAL seconds."""
    global _watcher_running, _total_polls
    _watcher_running = True

    interval = _POLL_INTERVAL
    logger.info(f"[FOLLOW] Watcher starting — interval: {interval}s, "
                f"trigger values: {_TRIGGER_VALUES}, "
                f"fixed users: {sorted(_FIXED_USER_IDS)}")

    time.sleep(15)  # Stagger start to avoid burst

    snapshot = _load_snapshot()

    consecutive_errors = 0
    max_backoff = max(interval * 5, 600)

    while _watcher_running:
        _total_polls += 1
        try:
            snapshot = _poll_and_create_activities(snapshot)
            _save_snapshot(snapshot)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            err_str = str(e)
            is_rate_limited = "429" in err_str or "Too Many Requests" in err_str
            if is_rate_limited:
                backoff = min(max_backoff, max(90, 45 * (2 ** min(consecutive_errors, 4))))
            else:
                backoff = min(max_backoff, 10 * (2 ** min(consecutive_errors, 5)))
            import random
            backoff += random.uniform(0, max(1, backoff * 0.2))
            log_level = logging.WARNING if is_rate_limited else logging.ERROR
            logger.log(
                log_level,
                f"[FOLLOW] Poll error (#{consecutive_errors}): {e} "
                f"— retrying in {backoff:.0f}s"
            )
            time.sleep(min(backoff, max_backoff))
            continue

        # Interruptible sleep with jitter
        import random
        jitter = random.uniform(0, min(15, interval * 0.25))
        deadline = time.monotonic() + interval + jitter
        while time.monotonic() < deadline and _watcher_running:
            time.sleep(min(5, max(0, deadline - time.monotonic())))


def start_follow_activity_watcher():
    """Start the follow activity watcher background thread."""
    if not Config.FOLLOW_ACTIVITY_ENABLED:
        logger.warning("[FOLLOW] FOLLOW_ACTIVITY_ENABLED not true — watcher disabled")
        return
    if not Config.ODOO_UID and not Config.ODOO_USER:
        logger.warning("[FOLLOW] Odoo credentials not set — watcher disabled")
        return

    thread = threading.Thread(
        target=_watcher_loop, daemon=True, name="follow-activity-watcher"
    )
    thread.start()
    logger.info(f"[FOLLOW] Watcher started — every {_POLL_INTERVAL}s")


def get_follow_activity_status() -> dict:
    """Health check status."""
    return {
        "active": _watcher_running,
        "poll_interval_seconds": _POLL_INTERVAL,
        "trigger_values": list(_TRIGGER_VALUES),
        "activity_type_id": _ACTIVITY_TYPE_ID,
        "fixed_user_ids": sorted(_FIXED_USER_IDS),
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_activities_created": _total_activities_created,
        "total_so_triggered": _total_so_triggered,
    }
