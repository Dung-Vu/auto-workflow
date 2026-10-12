"""
Follow Activity on Order State Change — Background Polling Service.

Monitors sale.order for changes to x_studio_selection_field_q4_1imrcsjj8 (Order State).
Two recipient policies are supported:
  - Legacy states: fixed users + company-specific user + SO salesperson.
  - Sales-only states: SO salesperson + Salesperson 2 (if it resolves to res.users).

Snapshot {so_id: last_known_state} is persisted to {DATA_DIR}/follow_activity_snapshot.json.
Trigger metadata is persisted separately so newly-added trigger values are baselined
without historical backfill. State survives container restarts.
"""

import json
import logging
import os
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ─── Config ───
_LEGACY_TRIGGER_VALUES = set(Config.FOLLOW_ACTIVITY_TRIGGER_VALUES)
_SALES_ONLY_TRIGGER_VALUES = set(Config.FOLLOW_ACTIVITY_SALES_ONLY_TRIGGER_VALUES)
_TRIGGER_VALUES = _LEGACY_TRIGGER_VALUES | _SALES_ONLY_TRIGGER_VALUES
_POLL_INTERVAL = Config.FOLLOW_ACTIVITY_POLL_INTERVAL
_ACTIVITY_TYPE_ID = Config.FOLLOW_ACTIVITY_TYPE_ID
_FIXED_USER_IDS = set(Config.FOLLOW_ACTIVITY_FIXED_USER_IDS)
_COMPANY_USER_IDS = Config.FOLLOW_ACTIVITY_COMPANY_USER_IDS
_SALESPERSON2_USER_OVERRIDES = Config.FOLLOW_ACTIVITY_SALESPERSON2_USER_OVERRIDES
_DEADLINE_DAYS = Config.FOLLOW_ACTIVITY_DEADLINE_DAYS

_STATE_LABELS = {
    "ATTENTION!": "Đã đặt hàng",
    "NVL về/Chưa SX": "NVL về/Chờ SX",
}

# ─── Runtime state ───
_watcher_running = False
_last_poll = None
_total_polls = 0
_total_activities_created = 0
_total_so_triggered = 0

# ─── Runtime caches ───
_so_model_id = None
_salesperson2_user_cache = {}

# ─── Snapshot persistence ───
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
SNAPSHOT_FILE = os.path.join(_DATA_DIR, "follow_activity_snapshot.json")
SNAPSHOT_META_FILE = os.path.join(_DATA_DIR, "follow_activity_snapshot_meta.json")


def _load_snapshot() -> dict | None:
    """Load persisted state, or ``None`` when it is missing/invalid.

    ``None`` is deliberately different from an existing empty snapshot.  A
    missing snapshot must be baselined without creating activities; otherwise
    every old order already in a trigger state is mistaken for a new change.
    """
    if not os.path.exists(SNAPSHOT_FILE):
        logger.warning("[FOLLOW] No snapshot file — initial baseline required")
        return None
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("snapshot root must be an object")
        logger.info(f"[FOLLOW] Loaded snapshot: {len(data)} SO states tracked")
        return data
    except Exception as e:
        logger.error(f"[FOLLOW] Failed to load snapshot: {e} — rebuilding baseline safely")
        return None


def _build_initial_baseline() -> dict:
    """Capture current trigger-state orders without emitting activities."""
    sos = _fetch_sos_with_trigger_state()
    snapshot = {
        str(so["id"]): so.get("x_studio_selection_field_q4_1imrcsjj8", "")
        for so in sos
    }
    logger.warning(
        f"[FOLLOW] Initial baseline saved for {len(snapshot)} SO(s); "
        "no activities created"
    )
    return snapshot


def _save_snapshot(snapshot: dict):
    """Persist snapshot to disk."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_FILE) or ".", exist_ok=True)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[FOLLOW] Failed to save snapshot: {e}")


def _load_snapshot_trigger_values() -> set | None:
    """Return trigger values recorded at the previous successful startup."""
    if not os.path.exists(SNAPSHOT_META_FILE):
        return None
    try:
        with open(SNAPSHOT_META_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        values = data.get("trigger_values") if isinstance(data, dict) else None
        if not isinstance(values, list):
            raise ValueError("trigger_values must be a list")
        return set(values)
    except Exception as e:
        logger.error(f"[FOLLOW] Failed to load trigger metadata: {e}")
        return None


def _save_snapshot_trigger_values():
    """Persist the active trigger policy for safe future config expansion."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_META_FILE) or ".", exist_ok=True)
        with open(SNAPSHOT_META_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"version": 1, "trigger_values": sorted(_TRIGGER_VALUES)},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        logger.error(f"[FOLLOW] Failed to save trigger metadata: {e}")


def _prepare_snapshot_for_current_triggers(snapshot: dict) -> dict:
    """Baseline new trigger values without creating historical activities."""
    previous_values = _load_snapshot_trigger_values()
    if previous_values is None:
        logger.warning(
            "[FOLLOW] Trigger metadata missing/invalid — rebuilding full baseline safely"
        )
        snapshot = _build_initial_baseline()
    else:
        added_values = _TRIGGER_VALUES - previous_values
        if added_values:
            seeded = 0
            for so in _fetch_sos_with_trigger_state():
                state = so.get("x_studio_selection_field_q4_1imrcsjj8", "")
                if state in added_values:
                    snapshot[str(so["id"])] = state
                    seeded += 1
            logger.warning(
                f"[FOLLOW] Baselined {seeded} existing SO(s) for new trigger values "
                f"{sorted(added_values)}; no activities created"
            )
    _save_snapshot(snapshot)
    _save_snapshot_trigger_values()
    return snapshot


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


def _relation_id(value):
    return value[0] if isinstance(value, (list, tuple)) and value else value or None


def _normalize_person_name(value: str) -> str:
    """Normalize custom salesperson names and prefixed Odoo user names."""
    name = unicodedata.normalize("NFC", value or "")
    if "," in name:
        name = name.split(",", 1)[1]
    return " ".join(name.split()).casefold()


def _resolve_salesperson2_user(so: dict) -> tuple[int, str] | None:
    """Resolve x_salesperson_2 to one active, company-compatible res.users."""
    salesperson2_id = _relation_id(so.get("x_studio_salesperson_2"))
    if not salesperson2_id:
        return None

    company_id = _relation_id(so.get("company_id"))
    cache_key = (salesperson2_id, company_id)
    if cache_key in _salesperson2_user_cache:
        return _salesperson2_user_cache[cache_key]

    override_user_id = _SALESPERSON2_USER_OVERRIDES.get(salesperson2_id)
    if override_user_id:
        users = odoo.read(
            "res.users", [override_user_id], fields=["id", "name", "active", "company_ids"]
        )
        if users:
            user = users[0]
            company_ids = user.get("company_ids") or []
            if user.get("active") and (not company_id or not company_ids or company_id in company_ids):
                result = (user["id"], user["name"])
                _salesperson2_user_cache[cache_key] = result
                return result
        logger.warning(
            f"[FOLLOW] Invalid Salesperson 2 override: sp2_id={salesperson2_id}, "
            f"uid={override_user_id}, company_id={company_id}"
        )
        _salesperson2_user_cache[cache_key] = None
        return None

    records = odoo.read(
        "x_salesperson_2", [salesperson2_id], fields=["id", "x_name"]
    )
    if not records:
        logger.warning(f"[FOLLOW] Salesperson 2 record #{salesperson2_id} not found")
        _salesperson2_user_cache[cache_key] = None
        return None

    salesperson2_name = records[0].get("x_name") or ""
    normalized_name = _normalize_person_name(salesperson2_name)
    candidates = odoo.search_read(
        "res.users",
        [["active", "=", True], ["name", "ilike", salesperson2_name]],
        fields=["id", "name", "company_ids"],
        limit=20,
    )
    matches = []
    for user in candidates:
        company_ids = user.get("company_ids") or []
        if company_id and company_ids and company_id not in company_ids:
            continue
        if _normalize_person_name(user.get("name") or "") == normalized_name:
            matches.append(user)

    if len(matches) == 1:
        result = (matches[0]["id"], matches[0]["name"])
        _salesperson2_user_cache[cache_key] = result
        return result

    logger.warning(
        f"[FOLLOW] Salesperson 2 unresolved/ambiguous: sp2_id={salesperson2_id}, "
        f"name='{salesperson2_name}', company_id={company_id}, matches={len(matches)}"
    )
    _salesperson2_user_cache[cache_key] = None
    return None


def _resolve_target_users(so: dict) -> tuple[set[int], dict[int, str]]:
    """Return deduplicated activity recipients according to the state policy."""
    state = so.get("x_studio_selection_field_q4_1imrcsjj8", "")
    salesperson = so.get("user_id")
    salesperson_id = _relation_id(salesperson)
    salesperson_name = (
        salesperson[1]
        if isinstance(salesperson, (list, tuple)) and len(salesperson) > 1
        else f"User {salesperson_id}"
    )

    target_users = set()
    user_names = {}
    if salesperson_id:
        target_users.add(salesperson_id)
        user_names[salesperson_id] = salesperson_name

    if state in _SALES_ONLY_TRIGGER_VALUES:
        salesperson2 = _resolve_salesperson2_user(so)
        if salesperson2:
            user_id, user_name = salesperson2
            target_users.add(user_id)
            user_names[user_id] = user_name
        return target_users, user_names

    company_id = _relation_id(so.get("company_id"))
    company_user_id = _COMPANY_USER_IDS.get(company_id)
    configured_users = set(_FIXED_USER_IDS)
    if company_user_id:
        configured_users.add(company_user_id)
    target_users.update(configured_users)
    if configured_users:
        users = odoo.read("res.users", sorted(configured_users), fields=["id", "name"])
        user_names.update({user["id"]: user["name"] for user in users})
    return target_users, user_names


# ═══════════════════════════════════════════
#  CORE LOGIC
# ═══════════════════════════════════════════

def _fetch_sos_with_trigger_state() -> list:
    """Fetch sale.order records where Order State is in trigger values."""
    so_ids = odoo.search("sale.order", [
        ["x_studio_selection_field_q4_1imrcsjj8", "in", list(_TRIGGER_VALUES)],
    ])
    if not so_ids:
        return []
    return odoo.read("sale.order", so_ids, fields=[
        "id", "name", "partner_id", "user_id", "company_id",
        "x_studio_salesperson_2", "x_studio_selection_field_q4_1imrcsjj8",
    ])


def _has_existing_follow_activity(so_id: int, user_id: int, order_state: str) -> bool:
    """Idempotency guard: True if a Follow activity with the same summary already exists.

    Prevents duplicate activities when the snapshot is lost (e.g. after a service
    restart without a persisted data volume).
    """
    # Search by res_model + res_id + user_id + activity type, then inspect summary.
    existing = odoo.search_read(
        "mail.activity",
        [
            ["res_model", "=", "sale.order"],
            ["res_id", "=", so_id],
            ["user_id", "=", user_id],
            ["activity_type_id", "=", _ACTIVITY_TYPE_ID],
        ],
        fields=["id", "summary"],
        limit=10,
    )
    # Match the displayed state label to allow re-trigger on a different state.
    state_label = _STATE_LABELS.get(order_state, order_state)
    for act in existing:
        if state_label in (act.get("summary") or ""):
            return True
    return False


def _create_follow_activity(so: dict, user_id: int, user_name: str) -> int | None:
    """Create a single Follow activity for a user on a sale.order.

    Returns activity ID, or None if a duplicate already exists (idempotency).
    """
    model_id = _ensure_model_id()
    so_name = so.get("name", "?")
    order_state = so.get("x_studio_selection_field_q4_1imrcsjj8", "?")
    state_label = _STATE_LABELS.get(order_state, order_state)

    partner = so.get("partner_id")
    partner_name = partner[1] if isinstance(partner, (list, tuple)) and len(partner) > 1 else "?"

    summary = f"Follow up: {so_name} — {state_label}"
    note = (
        f"<p><strong>📋 Follow-up Activity</strong></p>"
        f"<ul>"
        f"<li>SO: <strong>{so_name}</strong></li>"
        f"<li>Customer: {partner_name}</li>"
        f"<li>Order State: <strong>{state_label}</strong></li>"
        f"</ul>"
    )

    deadline = (date.today() + timedelta(days=_DEADLINE_DAYS)).isoformat()

    # Idempotency: skip if same activity already open for this user
    if _has_existing_follow_activity(so["id"], user_id, order_state):
        logger.info(f"[FOLLOW]   ⏭ Skipped (duplicate) → {user_name} (uid={user_id}) "
                    f"on {so_name} state='{order_state}'")
        return None

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
    global _total_activities_created, _total_so_triggered, _last_poll

    poll_start = datetime.now()
    _last_poll = poll_start.strftime("%Y-%m-%d %H:%M:%S") + " ICT"

    # 1. Fetch SOs currently in trigger state
    sos = _fetch_sos_with_trigger_state()

    # ── KEY FIX ──────────────────────────────────────────────────────────────
    # Remove SOs that are NO LONGER in the trigger state from the snapshot.
    # Without this, if SO leaves trigger → re-enters trigger, snapshot still
    # holds the old trigger value so last_state == current_state → skipped.
    current_trigger_ids = {str(so["id"]) for so in sos}
    stale_keys = [k for k in snapshot if k not in current_trigger_ids]
    for k in stale_keys:
        logger.debug(f"[FOLLOW] SO #{k} left trigger state — removed from snapshot")
        del snapshot[k]
    # ─────────────────────────────────────────────────────────────────────────

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

            target_users, user_names = _resolve_target_users(so)

            # Create activity for each target user
            created = 0
            for uid in sorted(target_users):
                uname = user_names.get(uid, f"User {uid}")
                try:
                    act_id = _create_follow_activity(so, uid, uname)
                    if act_id is not None:
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
                f"legacy triggers: {_LEGACY_TRIGGER_VALUES}, "
                f"sales-only triggers: {_SALES_ONLY_TRIGGER_VALUES}, "
                f"fixed users: {sorted(_FIXED_USER_IDS)}, "
                f"company users: {_COMPANY_USER_IDS}")

    time.sleep(15)  # Stagger start to avoid burst

    snapshot = _load_snapshot()
    # Fail closed on first run, lost volume, corrupt state, or trigger-policy
    # expansion. Only transitions observed after baseline may create activity.
    while _watcher_running:
        try:
            if snapshot is None:
                snapshot = _build_initial_baseline()
                _save_snapshot(snapshot)
                _save_snapshot_trigger_values()
            else:
                snapshot = _prepare_snapshot_for_current_triggers(snapshot)
            break
        except Exception as e:
            logger.error(f"[FOLLOW] Baseline preparation failed: {e} — retrying in 60s")
            time.sleep(60)
    if snapshot is None:
        return

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
        "trigger_values": sorted(_TRIGGER_VALUES),
        "legacy_trigger_values": sorted(_LEGACY_TRIGGER_VALUES),
        "sales_only_trigger_values": sorted(_SALES_ONLY_TRIGGER_VALUES),
        "activity_type_id": _ACTIVITY_TYPE_ID,
        "fixed_user_ids": sorted(_FIXED_USER_IDS),
        "company_user_ids": _COMPANY_USER_IDS,
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_activities_created": _total_activities_created,
        "total_so_triggered": _total_so_triggered,
    }
