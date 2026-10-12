"""
One-shot backfill / recalculation script:
Recalculates commission revenue (x_studio_commission_revenue and x_studio_commission_revenue_sp2)
on all sale.order records with x_studio_ratio > 0 using current exclusion logic
(excluded product categories and excluded product name prefixes).

Directly computes and writes values back to Odoo for any records where the values differ.

Run from project root:
    python -m scripts.recalc_all_commission
"""

import logging
import sys
import time

from config import Config
from services.odoo_client import odoo
from services.commission_revenue import (
    _calculate_commission,
    _fetch_order_line_categories,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    if not Config.ODOO_URL or not Config.ODOO_DB:
        logger.error("ODOO_URL / ODOO_DB not set in .env — abort")
        sys.exit(1)

    logger.info("Starting commission recalculation...")
    logger.info(f"Excluded categories: {sorted(Config.COMMISSION_REVENUE_EXCLUDED_CATEGORIES)}")
    logger.info(f"Excluded name prefixes: {Config.COMMISSION_REVENUE_EXCLUDED_NAME_PREFIXES}")

    domain = [
        ("x_studio_ratio", "!=", False),
        ("x_studio_ratio", ">", 0),
    ]
    so_ids = odoo.search("sale.order", domain, limit=0)
    logger.info(f"Found {len(so_ids)} SO(s) with ratio set")

    if not so_ids:
        logger.info("Nothing to recalculate")
        return

    BATCH = 200
    total_checked = 0
    total_updated = 0
    total_skipped = 0
    start_time = time.time()

    for i in range(0, len(so_ids), BATCH):
        chunk = so_ids[i:i + BATCH]
        sos = odoo.read("sale.order", chunk, fields=[
            "id", "name", "amount_untaxed", "x_studio_ratio",
            "x_studio_commission_revenue",
            "x_studio_commission_revenue_sp2",
        ])
        so_lines_map = _fetch_order_line_categories(chunk)

        writes_by_value = {}
        for so in sos:
            lines_data = so_lines_map.get(so["id"], [])
            new_sp1, new_sp2 = _calculate_commission(so, lines_data)

            curr_sp1 = round(float(so.get("x_studio_commission_revenue") or 0), 2)
            curr_sp2 = round(float(so.get("x_studio_commission_revenue_sp2") or 0), 2)

            if curr_sp1 == new_sp1 and curr_sp2 == new_sp2:
                total_skipped += 1
                continue

            key = (new_sp1, new_sp2)
            writes_by_value.setdefault(key, []).append({
                "id": so["id"],
                "name": so["name"],
                "old": (curr_sp1, curr_sp2),
            })

        for (new_sp1, new_sp2), records in writes_by_value.items():
            ids = [r["id"] for r in records]
            try:
                odoo.write("sale.order", ids, {
                    "x_studio_commission_revenue": new_sp1,
                    "x_studio_commission_revenue_sp2": new_sp2,
                })
                total_updated += len(records)
                for r in records:
                    logger.info(
                        f"  ✅ {r['name']}: updated commission "
                        f"sp1 {r['old'][0]}→{new_sp1}, "
                        f"sp2 {r['old'][1]}→{new_sp2}"
                    )
            except Exception as e:
                for r in records:
                    logger.error(f"  ❌ {r['name']} (id={r['id']}): write failed: {e}")

        total_checked += len(chunk)
        logger.info(
            f"Progress: {total_checked}/{len(so_ids)} checked | "
            f"{total_updated} updated | {total_skipped} unchanged"
        )

    logger.info(
        f"Recalculation complete in {time.time() - start_time:.1f}s. "
        f"Total checked: {total_checked}, Total updated: {total_updated}, "
        f"Total unchanged: {total_skipped}"
    )


if __name__ == "__main__":
    main()
