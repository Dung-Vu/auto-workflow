"""
Commission Revenue — Background Polling Service.
Replaces Odoo base.automation ID 64 ("Commission Revenue") on sale.order.

Original Odoo automation triggered on_create_or_write when x_studio_ratio
or x_studio_selection_field_q4_1imrcsjj8 (Order State) changed, then ran:

    dv = sum(order_line filtered by categ_id in [19,998,999,1097]).price_subtotal
    comm = amount_untaxed - dv
    write x_studio_commission_revenue = comm * x_studio_ratio
    write x_studio_commission_revenue_sp2 = comm * (1 - x_studio_ratio)

This service polls sale.order every N seconds for recently-modified records,
recalculates commission, and only writes when values actually change.

Snapshot (last_poll timestamp) is persisted to {DATA_DIR}/commission_snapshot.json
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
_ODDO_URL = Config.ODOO_URL
_ODDO_DB = Config.ODOO_DB
_ODDO_UID = Config.ODOO_UID
_ODDO_API_KEY = Config.ODOO_API_KEY

# ─── Config ───
_EXCLUDED_CATEGORIES = set(Config.COMMISSION_REVENUE_EXCLUDED_CATEGORIES)
_POLL_INTERVAL = Config.COMMISSION_REVENUE_POLL_INTERVAL

# ─── Runtime state ───
_watcher_running = False
_last_poll = None
_total_polls = 0
_total_recalculated = 0
_total_skipped = 0

# ─── Snapshot persistence ───
SNAPSHOT_FILE = os.path.join(
    os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")),
    "commission_snapshot.json",
)


def _load_snapshot() -> dict:
    """Load snapshot from disk. Returns dict with 'last_poll' key."""
    if not os.path.exists(SNAPSHOT_FILE):
        logger.info("[COMMISSION] No snapshot file — starting fresh")
        return {"last_poll": None}
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"[COMMISSION] Loaded snapshot: last_poll={data.get('last_poll')}")
        return data
    except Exception as e:
        logger.warning(f"[COMMISSION] Failed to load snapshot: {e} — starting fresh")
        return {"last_poll": None}


def _save_snapshot(snapshot: dict):
    """Persist snapshot to disk."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_FILE) or ".", exist_ok=True)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[COMMISSION] Failed to save snapshot: {e}")


# ═══════════════════════════════════════════
#  CORE LOGIC
# ═══════════════════════════════════════════

def _fetch_modified_so(last_poll_str: str) -> list:
    """Fetch sale.order IDs modified since last_poll, with x_studio_ratio set."""
    domain = [
        ("x_studio_ratio", "!=", False),
        ("x_studio_ratio", ">", 0),
    ]
    if last_poll_str:
        domain.append(("write_date", ">", last_poll_str))

    so_ids = odoo.search(domain, limit=200)
    if not so_ids:
        return []

    # Fetch SO data with current commission values
    sos = odoo.read("sale.order", so_ids, fields=[
        "id", "name", "amount_untaxed", "x_studio_ratio",
        "x_studio_commission_revenue",
        "x_studio_commission_revenue_sp2",
    ])
    return sos


def _fetch_order_line_categories(so_ids: list) -> dict:
    """Fetch order lines with product category for given SO IDs.
    Returns {so_id: [(categ_id, price_subtotal), ...]}.
    """
    if not so_ids:
        return {}

    lines = odoo.search_read(
        "sale.order.line",
        [["order_id", "in", so_ids]],
        fields=["order_id", "product_id", "price_subtotal"],
    )

    # Extract product IDs to fetch categories
    product_ids = list({
        l["product_id"][0] for l in lines
        if l.get("product_id")
    })

    product_categ = {}
    if product_ids:
        # Batch read product categories
        batch = 500
        for i in range(0, len(product_ids), batch):
            batch_ids = product_ids[i:i + batch]
            prods = odoo.read("product.product", batch_ids, fields=["id", "categ_id"])
            for p in prods:
                categ = p.get("categ_id")
                product_categ[p["id"]] = categ[0] if categ else None

    # Build {so_id: [(categ_id, price_subtotal), ...]}
    so_lines = {}
    for l in lines:
        if not l.get("product_id") or not l.get("order_id"):
            continue
        so_id = l["order_id"][0]
        pid = l["product_id"][0]
        categ_id = product_categ.get(pid)
        so_lines.setdefault(so_id, []).append((categ_id, l["price_subtotal"]))

    return so_lines


def _calculate_commission(so: dict, lines_data: list) -> tuple:
    """Calculate commission values for a single SO.
    Returns (commission_revenue, commission_revenue_sp2).
    """
    amount_untaxed = so.get("amount_untaxed", 0) or 0
    ratio = so.get("x_studio_ratio", 0) or 0

    # Sum price_subtotal for excluded categories
    dv = sum(
        subtotal for categ_id, subtotal in lines_data
        if categ_id in _EXCLUDED_CATEGORIES
    )

    comm = amount_untaxed - dv
    comm_sp1 = round(comm * ratio, 2)
    comm_sp2 = round(comm * (1 - ratio), 2)
    return comm_sp1, comm_sp2


def _poll_and_recalculate(snapshot: dict) -> dict:
    """Poll for modified SOs and recalculate commission. Returns updated snapshot."""
    global _total_recalculated, _total_skipped

    last_poll_str = snapshot.get("last_poll")
    poll_start = datetime.now(timezone.utc)
    poll_start_str = poll_start.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Fetch modified SOs with ratio set
    sos = _fetch_modified_so(last_poll_str)
    if not sos:
        logger.debug(f"[COMMISSION] Poll #{_total_polls}: no modified SOs")
        snapshot["last_poll"] = poll_start_str
        return snapshot

    logger.info(f"[COMMISSION] Poll #{_total_polls}: {len(sos)} modified SO(s) to check")

    # 2. Fetch order line categories for all SOs
    so_ids = [s["id"] for s in sos]
    so_lines_map = _fetch_order_line_categories(so_ids)

    # 3. Recalculate and write if changed
    to_write = []
    for so in sos:
        lines_data = so_lines_map.get(so["id"], [])
        new_sp1, new_sp2 = _calculate_commission(so, lines_data)

        current_sp1 = so.get("x_studio_commission_revenue")
        current_sp2 = so.get("x_studio_commission_revenue_sp2")

        # Normalize current values for comparison
        curr_sp1 = round(float(current_sp1), 2) if current_sp1 is not None else None
        curr_sp2 = round(float(current_sp2), 2) if current_sp2 is not None else None

        if curr_sp1 == new_sp1 and curr_sp2 == new_sp2:
            _total_skipped += 1
            logger.debug(
                f"[COMMISSION]   {so['name']}: values unchanged "
                f"(sp1={new_sp1}, sp2={new_sp2})"
            )
            continue

        to_write.append((so["id"], so["name"], new_sp1, new_sp2, curr_sp1, curr_sp2))

    # 4. Batch write changed SOs
    for so_id, name, new_sp1, new_sp2, old_sp1, old_sp2 in to_write:
        try:
            odoo.write("sale.order", [so_id], {
                "x_studio_commission_revenue": new_sp1,
                "x_studio_commission_revenue_sp2": new_sp2,
            })
            _total_recalculated += 1
            logger.info(
                f"[COMMISSION]   ✅ {name}: commission updated "
                f"sp1 {old_sp1}→{new_sp1}, sp2 {old_sp2}→{new_sp2}"
            )
        except Exception as e:
            logger.error(f"[COMMISSION]   ❌ {name}: write failed: {e}")

    if to_write:
        logger.info(
            f"[COMMISSION] Poll #{_total_polls}: "
            f"recalculated={len(to_write)}, skipped={len(sos) - len(to_write)}"
        )

    snapshot["last_poll"] = poll_start_str
    return snapshot


# ═══════════════════════════════════════════
#  BACKGROUND THREAD
# ═══════════════════════════════════════════

def _watcher_loop():
    """Background loop: poll every COMMISSION_REVENUE_POLL_INTERVAL seconds."""
    global _watcher_running, _total_polls
    _watcher_running = True

    interval = _POLL_INTERVAL
    logger.info(f"[COMMISSION] Watcher starting — interval: {interval}s, "
                f"excluded categories: {sorted(_EXCLUDED_CATEGORIES)}")

    time.sleep(10)  # Let server finish starting

    snapshot = _load_snapshot()

    consecutive_errors = 0
    max_backoff = max(interval * 5, 600)

    while _watcher_running:
        _total_polls += 1
        try:
            snapshot = _poll_and_recalculate(snapshot)
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
            # Add jitter
            import random
            backoff += random.uniform(0, max(1, backoff * 0.2))
            log_level = logging.WARNING if is_rate_limited else logging.ERROR
            logger.log(
                log_level,
                f"[COMMISSION] Poll error (#{consecutive_errors}): {e} "
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


def start_commission_revenue_watcher():
    """Start the commission revenue watcher background thread."""
    if not Config.COMMISSION_REVENUE_ENABLED:
        logger.warning("[COMMISSION] COMMISSION_REVENUE_ENABLED not true — "
                       "watcher disabled")
        return
    if not _ODOO_UID and not Config.ODOO_USER:
        logger.warning("[COMMISSION] Odoo credentials not set — watcher disabled")
        return

    thread = threading.Thread(
        target=_watcher_loop, daemon=True, name="commission-revenue-watcher"
    )
    thread.start()
    logger.info(f"[COMMISSION] Watcher started — every {_POLL_INTERVAL}s")


def get_commission_revenue_status() -> dict:
    """Health check status."""
    return {
        "active": _watcher_running,
        "poll_interval_seconds": _POLL_INTERVAL,
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_recalculated": _total_recalculated,
        "total_skipped_unchanged": _total_skipped,
        "excluded_categories": sorted(_EXCLUDED_CATEGORIES),
    }
