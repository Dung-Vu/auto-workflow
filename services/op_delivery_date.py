"""
OP Delivery Date Sync — Background Polling Service.
Replaces Odoo base.automation ID 80 ("SO OP Tiến độ dự kiến → Delivery Scheduled Date").

Original Odoo automation triggered on_create_or_write (filter: state not in [cancel]),
then ran:

    if record.x_studio_op_tin_d_kin:
        record.picking_ids.filtered(
            lambda p: p.state not in ['done', 'cancel']
            and p.picking_type_code == 'outgoing'
        ).write({'scheduled_date': record.x_studio_op_tin_d_kin})

This service polls sale.order every N seconds for recently-modified records
(non-cancelled) with x_studio_op_tin_d_kin set, and syncs that date to the
scheduled_date of all outgoing pickings that are not done/cancel.

Only writes when the picking's scheduled_date actually differs from the SO's
x_studio_op_tin_d_kin — avoids unnecessary write API calls.

Snapshot (last_poll timestamp) is persisted to {DATA_DIR}/op_delivery_snapshot.json
so state survives container restarts.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ─── Odoo credentials ───
_ODDO_UID = Config.ODOO_UID
_ODDO_API_KEY = Config.ODOO_API_KEY

# ─── Config ───
_POLL_INTERVAL = Config.OP_DELIVERY_DATE_POLL_INTERVAL

# ─── Runtime state ───
_watcher_running = False
_last_poll = None
_total_polls = 0
_total_synced = 0
_total_skipped = 0

# ─── Snapshot persistence ───
SNAPSHOT_FILE = os.path.join(
    os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")),
    "op_delivery_snapshot.json",
)


def _load_snapshot() -> dict:
    """Load snapshot from disk."""
    if not os.path.exists(SNAPSHOT_FILE):
        logger.info("[OP-DELIVERY] No snapshot file — starting fresh")
        return {"last_poll": None}
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"[OP-DELIVERY] Loaded snapshot: last_poll={data.get('last_poll')}")
        return data
    except Exception as e:
        logger.warning(f"[OP-DELIVERY] Failed to load snapshot: {e} — starting fresh")
        return {"last_poll": None}


def _save_snapshot(snapshot: dict):
    """Persist snapshot to disk."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_FILE) or ".", exist_ok=True)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[OP-DELIVERY] Failed to save snapshot: {e}")


# ═══════════════════════════════════════════
#  CORE LOGIC
# ═══════════════════════════════════════════

def _fetch_modified_so(last_poll_str: str) -> list:
    """Fetch sale.order IDs modified since last_poll, not cancelled, with delivery date set."""
    domain = [
        ("state", "!=", "cancel"),
        ("x_studio_op_tin_d_kin", "!=", False),
    ]
    if last_poll_str:
        domain.append(("write_date", ">", last_poll_str))

    so_ids = odoo.search(domain, limit=200)
    if not so_ids:
        return []

    sos = odoo.read("sale.order", so_ids, fields=[
        "id", "name", "x_studio_op_tin_d_kin", "picking_ids",
    ])
    # Filter out SOs with no pickings
    return [s for s in sos if s.get("picking_ids")]


def _fetch_pickings(picking_ids: list) -> list:
    """Fetch outgoing pickings not done/cancel with their current scheduled_date."""
    if not picking_ids:
        return []

    pickings = odoo.read("stock.picking", picking_ids, fields=[
        "id", "name", "state", "picking_type_code", "scheduled_date",
    ])
    # Filter: outgoing, not done/cancel
    return [
        p for p in pickings
        if p.get("picking_type_code") == "outgoing"
        and p.get("state") not in ("done", "cancel")
    ]


def _poll_and_sync(snapshot: dict) -> dict:
    """Poll for modified SOs and sync delivery date to pickings."""
    global _total_synced, _total_skipped

    last_poll_str = snapshot.get("last_poll")
    poll_start = datetime.now(timezone.utc)
    poll_start_str = poll_start.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Fetch modified SOs with delivery date set
    sos = _fetch_modified_so(last_poll_str)
    if not sos:
        logger.debug(f"[OP-DELIVERY] Poll #{_total_polls}: no modified SOs")
        snapshot["last_poll"] = poll_start_str
        return snapshot

    logger.info(f"[OP-DELIVERY] Poll #{_total_polls}: {len(sos)} modified SO(s) to check")

    # 2. Collect all picking IDs across SOs, fetch in one batch
    all_picking_ids = []
    for so in sos:
        all_picking_ids.extend(so.get("picking_ids", []))

    eligible_pickings = _fetch_pickings(all_picking_ids)
    if not eligible_pickings:
        logger.debug(f"[OP-DELIVERY] No eligible outgoing pickings to sync")
        snapshot["last_poll"] = poll_start_str
        return snapshot

    # Build {picking_id: picking} lookup
    picking_map = {p["id"]: p for p in eligible_pickings}

    # 3. For each SO, sync delivery date to its eligible pickings
    sync_count = 0
    skip_count = 0
    for so in sos:
        target_date = so.get("x_studio_op_tin_d_kin")
        if not target_date:
            continue

        so_name = so.get("name", "?")
        picking_ids = so.get("picking_ids", [])

        # Find pickings needing update
        to_write = []
        for pid in picking_ids:
            p = picking_map.get(pid)
            if not p:
                continue  # not eligible (done/cancel or not outgoing)
            current_scheduled = p.get("scheduled_date")
            if current_scheduled == target_date:
                skip_count += 1
                continue
            to_write.append(p)

        if not to_write:
            continue

        # Write scheduled_date to eligible pickings
        picking_write_ids = [p["id"] for p in to_write]
        try:
            odoo.write("stock.picking", picking_write_ids, {
                "scheduled_date": target_date
            })
            sync_count += len(to_write)
            for p in to_write:
                logger.info(
                    f"[OP-DELIVERY]   ✅ {so_name} → {p['name']}: "
                    f"scheduled_date {p.get('scheduled_date')} → {target_date}"
                )
        except Exception as e:
            logger.error(f"[OP-DELIVERY]   ❌ {so_name}: write failed: {e}")

    if sync_count:
        logger.info(
            f"[OP-DELIVERY] Poll #{_total_polls}: "
            f"synced={sync_count} pickings, skipped={skip_count} (unchanged)"
        )

    _total_synced += sync_count
    _total_skipped += skip_count
    snapshot["last_poll"] = poll_start_str
    return snapshot


# ═══════════════════════════════════════════
#  BACKGROUND THREAD
# ═══════════════════════════════════════════

def _watcher_loop():
    """Background loop: poll every OP_DELIVERY_DATE_POLL_INTERVAL seconds."""
    global _watcher_running, _total_polls
    _watcher_running = True

    interval = _POLL_INTERVAL
    logger.info(f"[OP-DELIVERY] Watcher starting — interval: {interval}s")

    time.sleep(12)  # Stagger start vs commission_revenue (10s) to avoid burst

    snapshot = _load_snapshot()

    consecutive_errors = 0
    max_backoff = max(interval * 5, 600)

    while _watcher_running:
        _total_polls += 1
        try:
            snapshot = _poll_and_sync(snapshot)
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
                f"[OP-DELIVERY] Poll error (#{consecutive_errors}): {e} "
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


def start_op_delivery_date_watcher():
    """Start the OP delivery date watcher background thread."""
    if not Config.OP_DELIVERY_DATE_ENABLED:
        logger.warning("[OP-DELIVERY] OP_DELIVERY_DATE_ENABLED not true — "
                       "watcher disabled")
        return
    if not _ODOO_UID and not Config.ODOO_USER:
        logger.warning("[OP-DELIVERY] Odoo credentials not set — watcher disabled")
        return

    thread = threading.Thread(
        target=_watcher_loop, daemon=True, name="op-delivery-watcher"
    )
    thread.start()
    logger.info(f"[OP-DELIVERY] Watcher started — every {_POLL_INTERVAL}s")


def get_op_delivery_date_status() -> dict:
    """Health check status."""
    return {
        "active": _watcher_running,
        "poll_interval_seconds": _POLL_INTERVAL,
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_synced": _total_synced,
        "total_skipped_unchanged": _total_skipped,
    }