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
import random
import threading
import time
from datetime import datetime, timezone

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ─── Odoo credentials ───
_ODOO_URL = Config.ODOO_URL
_ODOO_DB = Config.ODOO_DB
_ODOO_UID = Config.ODOO_UID
_ODOO_API_KEY = Config.ODOO_API_KEY

# ─── Config ───
_EXCLUDED_CATEGORIES = set(Config.COMMISSION_REVENUE_EXCLUDED_CATEGORIES)
_EXCLUDED_NAME_PREFIXES = tuple(Config.COMMISSION_REVENUE_EXCLUDED_NAME_PREFIXES)
_POLL_INTERVAL = Config.COMMISSION_REVENUE_POLL_INTERVAL

# ─── Runtime state ───
_watcher_running = False
_stop_event = threading.Event()
_last_poll = None
_total_polls = 0
_total_recalculated = 0
_total_skipped = 0

# Snapshot persistence throttle (seconds since epoch)
_LAST_SNAPSHOT_SAVE = 0.0
_SNAPSHOT_SAVE_INTERVAL = 60.0  # save at most once per 60s (writes still every poll)

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
        global _LAST_SNAPSHOT_SAVE
        _LAST_SNAPSHOT_SAVE = time.time()
    except Exception as e:
        logger.error(f"[COMMISSION] Failed to save snapshot: {e}")


def _maybe_save_snapshot(snapshot: dict):
    """Throttled snapshot save — at most once per _SNAPSHOT_SAVE_INTERVAL seconds.
    Always saves on shutdown (when _watcher_running is False).
    """
    now = time.time()
    if (
        not _watcher_running
        or (now - _LAST_SNAPSHOT_SAVE) >= _SNAPSHOT_SAVE_INTERVAL
    ):
        _save_snapshot(snapshot)


# ═══════════════════════════════════════════
#  CORE LOGIC
# ═══════════════════════════════════════════

def _fetch_modified_so(last_poll_str: str) -> list:
    """Fetch sale.order IDs modified since last_poll."""
    if last_poll_str:
        domain = [("write_date", ">", last_poll_str)]
    else:
        # Initial boot: only fetch orders with ratios to avoid scanning everything
        domain = [
            ("x_studio_ratio", "!=", False),
            ("x_studio_ratio", ">", 0),
        ]

    so_ids = odoo.search("sale.order", domain, limit=200)
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
    """Fetch order lines with product category + display_name for given SO IDs.
    Returns {so_id: [(categ_id, price_subtotal, product_name), ...]}.

    We have to call product.product separately for categ_id because Odoo's
    XML-RPC rejects dotted-notation fields like `product_id.categ_id` on
    `sale.order.line` (KeyError → ValueError on this server). Falls back to
    a separate read_product call, batched 500 IDs at a time.

    product_name (display_name) is needed to filter out lines whose name
    starts with any configured prefix — used when a shared category
    contains both revenue-counting products and individual products to
    exclude.
    """
    if not so_ids:
        return {}

    lines = odoo.search_read(
        "sale.order.line",
        [["order_id", "in", so_ids]],
        fields=["order_id", "product_id", "price_subtotal"],
    )

    # Extract unique product IDs
    product_ids = list({
        l["product_id"][0] for l in lines
        if l.get("product_id")
    })

    product_info: dict = {}  # pid -> (categ_id, display_name)
    if product_ids:
        batch = 500
        for i in range(0, len(product_ids), batch):
            batch_ids = product_ids[i:i + batch]
            prods = odoo.read(
                "product.product", batch_ids,
                fields=["id", "categ_id", "display_name"],
            )
            for p in prods:
                categ = p.get("categ_id")
                categ_id = categ[0] if categ else None
                product_info[p["id"]] = (categ_id, p.get("display_name") or "")

    # Build {so_id: [(categ_id, price_subtotal, product_name), ...]}
    so_lines = {}
    for l in lines:
        if not l.get("product_id") or not l.get("order_id"):
            continue
        so_id = l["order_id"][0]
        pid = l["product_id"][0]
        categ_id, product_name = product_info.get(pid, (None, ""))
        so_lines.setdefault(so_id, []).append(
            (categ_id, l["price_subtotal"], product_name)
        )

    return so_lines


def _calculate_commission(so: dict, lines_data: list) -> tuple:
    """Calculate commission values for a single SO.
    Returns (commission_revenue, commission_revenue_sp2).

    A line is excluded from the commission base (added to `dv`) if EITHER:
      - its product category is in _EXCLUDED_CATEGORIES, OR
      - its product display_name starts with any prefix in
        _EXCLUDED_NAME_PREFIXES.
    """
    ratio = so.get("x_studio_ratio")
    if ratio is None or ratio is False:
        return None, None

    amount_untaxed = so.get("amount_untaxed", 0) or 0
    ratio = float(ratio)

    def _is_excluded(categ_id, subtotal_unused, product_name):
        if categ_id in _EXCLUDED_CATEGORIES:
            return True
        if _EXCLUDED_NAME_PREFIXES and any(
            product_name.startswith(p) for p in _EXCLUDED_NAME_PREFIXES
        ):
            return True
        return False

    dv = sum(
        subtotal for categ_id, subtotal, product_name in lines_data
        if _is_excluded(categ_id, subtotal, product_name)
    )

    comm = amount_untaxed - dv
    comm_sp1 = round(comm * ratio, 2)
    comm_sp2 = round(comm * (1 - ratio), 2)
    return comm_sp1, comm_sp2


def _poll_and_recalculate(snapshot: dict) -> dict:
    """Poll for modified SOs and recalculate commission. Returns updated snapshot."""
    global _total_recalculated, _total_skipped, _last_poll

    last_poll_str = snapshot.get("last_poll")
    poll_start = datetime.now(timezone.utc)
    poll_start_str = poll_start.strftime("%Y-%m-%d %H:%M:%S")
    _last_poll = poll_start_str + " UTC"

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

    # 3. Recalculate and group writes by (new_sp1, new_sp2).
    # Odoo XML-RPC write() applies one value dict to all record IDs, so the
    # only way to send multiple records per RPC is to share the same value
    # pair. We group by that pair to minimise round-trips.
    writes_by_value: dict = {}
    skipped = 0
    for so in sos:
        lines_data = so_lines_map.get(so["id"], [])
        new_sp1, new_sp2 = _calculate_commission(so, lines_data)

        current_sp1 = so.get("x_studio_commission_revenue")
        current_sp2 = so.get("x_studio_commission_revenue_sp2")

        curr_sp1 = round(float(current_sp1), 2) if current_sp1 is not None else None
        curr_sp2 = round(float(current_sp2), 2) if current_sp2 is not None else None

        if curr_sp1 == new_sp1 and curr_sp2 == new_sp2:
            skipped += 1
            _total_skipped += 1
            logger.debug(
                f"[COMMISSION]   {so['name']}: values unchanged "
                f"(sp1={new_sp1}, sp2={new_sp2})"
            )
            continue

        key = (new_sp1, new_sp2)
        writes_by_value.setdefault(key, []).append({
            "id": so["id"],
            "name": so["name"],
            "old_sp1": curr_sp1,
            "old_sp2": curr_sp2,
        })

    # 4. Batch write — one RPC per unique (sp1, sp2) group.
    total_written = 0
    for (new_sp1, new_sp2), records in writes_by_value.items():
        ids = [r["id"] for r in records]
        try:
            odoo.write("sale.order", ids, {
                "x_studio_commission_revenue": new_sp1,
                "x_studio_commission_revenue_sp2": new_sp2,
            })
            total_written += len(records)
            _total_recalculated += len(records)
            for r in records:
                logger.info(
                    f"[COMMISSION]   ✅ {r['name']}: commission updated "
                    f"sp1 {r['old_sp1']}→{new_sp1}, "
                    f"sp2 {r['old_sp2']}→{new_sp2} "
                    f"(batch of {len(records)})"
                )
        except Exception as e:
            for r in records:
                logger.error(
                    f"[COMMISSION]   ❌ {r['name']} (id={r['id']}): "
                    f"write failed: {e}"
                )

    if writes_by_value:
        logger.info(
            f"[COMMISSION] Poll #{_total_polls}: "
            f"recalculated={total_written} in {len(writes_by_value)} batch(es), "
            f"skipped={skipped}"
        )

    snapshot["last_poll"] = poll_start_str
    return snapshot


# ═══════════════════════════════════════════
#  BACKGROUND THREAD
# ═══════════════════════════════════════════

def _watcher_loop():
    """Background loop: poll every COMMISSION_REVENUE_POLL_INTERVAL seconds."""
    global _watcher_running, _total_polls
    _stop_event.clear()
    _watcher_running = True

    interval = _POLL_INTERVAL
    logger.info(f"[COMMISSION] Watcher starting — interval: {interval}s, "
                f"excluded categories: {sorted(_EXCLUDED_CATEGORIES)}, "
                f"excluded name prefixes: {list(_EXCLUDED_NAME_PREFIXES)}")

    time.sleep(10)  # Let server finish starting

    snapshot = _load_snapshot()

    consecutive_errors = 0
    max_backoff = max(interval * 5, 600)

    while _watcher_running:
        _total_polls += 1
        try:
            snapshot = _poll_and_recalculate(snapshot)
            _maybe_save_snapshot(snapshot)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            err_str = str(e)
            is_rate_limited = "429" in err_str or "Too Many Requests" in err_str
            if is_rate_limited:
                backoff = min(max_backoff, max(90, 45 * (2 ** min(consecutive_errors, 4))))
            else:
                backoff = min(max_backoff, 10 * (2 ** min(consecutive_errors, 5)))
            backoff += random.uniform(0, max(1, backoff * 0.2))
            log_level = logging.WARNING if is_rate_limited else logging.ERROR
            logger.log(
                log_level,
                f"[COMMISSION] Poll error (#{consecutive_errors}): {e} "
                f"— retrying in {backoff:.0f}s"
            )
            # Interruptible backoff sleep via Event
            _stop_event.wait(timeout=min(backoff, max_backoff))
            continue

        # Interruptible jitter sleep via Event.wait
        jitter = random.uniform(0, min(15, interval * 0.25))
        _stop_event.wait(timeout=interval + jitter)


def start_commission_revenue_watcher():
    """Start the commission revenue watcher background thread."""
    global _watcher_running
    if not Config.COMMISSION_REVENUE_ENABLED:
        logger.warning("[COMMISSION] COMMISSION_REVENUE_ENABLED not true — "
                       "watcher disabled")
        return
    if not _ODOO_UID and not Config.ODOO_USER:
        logger.warning("[COMMISSION] Odoo credentials not set — watcher disabled")
        return
    if _watcher_running:
        logger.warning("[COMMISSION] Watcher already running — skip")
        return

    thread = threading.Thread(
        target=_watcher_loop, daemon=True, name="commission-revenue-watcher"
    )
    thread.start()
    logger.info(f"[COMMISSION] Watcher started — every {_POLL_INTERVAL}s")


def stop_commission_revenue_watcher():
    """Signal the watcher loop to exit and persist current snapshot."""
    global _watcher_running
    if not _watcher_running:
        return
    _watcher_running = False
    _stop_event.set()
    logger.info("[COMMISSION] Watcher stop signal sent")


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
        "excluded_name_prefixes": list(_EXCLUDED_NAME_PREFIXES),
    }
