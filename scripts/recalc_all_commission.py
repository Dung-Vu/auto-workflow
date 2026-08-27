"""
One-shot backfill: touch write_date on all sale.order with x_studio_ratio,
so the commission revenue watcher will re-poll and recalculate them with
the new exclusion logic (name prefix [KH]- + excluded categories).

Run from project root:
    python -m scripts.recalc_all_commission

Idempotent — touching write_date to the same value still bumps it via Odoo
onwrite trigger, which is enough to make the watcher pick them up.
"""

import logging
import sys

from config import Config
from services.odoo_client import odoo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    if not Config.ODOO_URL or not Config.ODOO_DB:
        logger.error("ODOO_URL / ODOO_DB not set in .env — abort")
        sys.exit(1)

    domain = [
        ("x_studio_ratio", "!=", False),
        ("x_studio_ratio", ">", 0),
    ]
    so_ids = odoo.search("sale.order", domain, limit=0)
    logger.info(f"Found {len(so_ids)} SO(s) with ratio set")

    if not so_ids:
        logger.info("Nothing to backfill")
        return

    BATCH = 200
    total = 0
    for i in range(0, len(so_ids), BATCH):
        chunk = so_ids[i:i + BATCH]
        # Bump write_date by assigning client_order_ref back to its current
        # value (read once, then write to all). This guarantees a real write
        # event that updates write_date regardless of Odoo's onwrite flags.
        ref_vals = odoo.read(
            "sale.order", chunk, fields=["client_order_ref"]
        )
        ref_by_id = {r["id"]: r.get("client_order_ref") or "" for r in ref_vals}
        for sid in chunk:
            current_ref = ref_by_id.get(sid, "")
            odoo.write("sale.order", [sid], {
                "client_order_ref": current_ref,
            })
        total += len(chunk)
        logger.info(
            f"  touched {total}/{len(so_ids)} (batch {i // BATCH + 1})"
        )

    logger.info(
        f"Done. Touched {total} SO(s). The watcher will pick them up on the "
        f"next poll (within {Config.COMMISSION_REVENUE_POLL_INTERVAL}s)."
    )


if __name__ == "__main__":
    main()
