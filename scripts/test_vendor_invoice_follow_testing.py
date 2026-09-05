#!/usr/bin/env python3
"""Safe live canary for vendor-invoice follow assignment on testing2608.

Creates one uniquely named test activity, verifies its assignee and linkage,
then removes it in ``finally``. Existing activities are never changed.
Credentials come only from ODOO_TEST_* environment variables.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.vendor_invoice_follow import _create_activity, _resolve_assignee  # noqa: E402
from services.zns_odoo_client import ZNSOdooJSONRPCClient  # noqa: E402


class CanaryClient:
    """Expose the small client protocol used by vendor_invoice_follow."""

    def __init__(self, raw: ZNSOdooJSONRPCClient):
        self.raw = raw

    def search_read(self, model, domain, fields=None, limit=0, order=None):
        return self.raw.search_read(model, domain, fields=fields, limit=limit, order=order)

    def read(self, model, ids, fields=None):
        return self.raw.read(model, ids, fields=fields)

    def search(self, model, domain, limit=0):
        return self.raw.search(model, domain, limit=limit)

    def create(self, model, values, context=None):
        kwargs = {"context": context} if context else {}
        return self.raw.execute_kw(model, "create", [values], kwargs)

    def unlink(self, model, ids):
        return bool(self.raw.execute_kw(model, "unlink", [ids], {}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--picking-id",
        type=int,
        default=16609,
        help="testing2608 completed purchase receipt with a linked Sales Order",
    )
    parser.add_argument(
        "--expected-user-id",
        type=int,
        default=122,
        help="expected OP user; default is BON SC, NGUYỄN THỊ TRANG",
    )
    args = parser.parse_args()

    url = os.getenv("ODOO_TEST_URL", "").rstrip("/")
    db = os.getenv("ODOO_TEST_DB", "")
    uid = int(os.getenv("ODOO_TEST_UID", "0") or 0)
    api_key = os.getenv("ODOO_TEST_API_KEY", "")
    host = (urlparse(url).hostname or "").lower()
    if host != "testing2608.odoo.com" or db != "testing2608":
        raise SystemExit("Refusing live canary: ODOO_TEST_URL/DB must be testing2608")
    if not uid or not api_key:
        raise SystemExit("ODOO_TEST_UID and ODOO_TEST_API_KEY are required")

    raw = ZNSOdooJSONRPCClient(url=url, db=db, uid=uid, api_key=api_key, timeout=30)
    client = CanaryClient(raw)
    rows = client.read(
        "stock.picking",
        [args.picking_id],
        fields=[
            "id",
            "name",
            "origin",
            "partner_id",
            "company_id",
            "purchase_id",
            "sale_id",
            "x_studio_source_so",
            "move_ids",
            "date_done",
            "state",
            "picking_type_code",
            "return_id",
        ],
    )
    if not rows:
        raise SystemExit(f"Picking {args.picking_id} not found on testing2608")
    picking = rows[0]
    assert picking["state"] == "done"
    assert picking["picking_type_code"] == "incoming"
    assert picking["purchase_id"]
    assert not picking["return_id"]

    assignee = _resolve_assignee(picking, client)
    if not assignee:
        raise SystemExit("No SO OP or PO Buyer could be resolved")
    if assignee["user_id"] != args.expected_user_id:
        raise SystemExit(
            f"Unexpected assignee {assignee['user_id']}; expected {args.expected_user_id}"
        )

    before_ids = set(
        client.search(
            "mail.activity",
            [["res_model", "=", "stock.picking"], ["res_id", "=", args.picking_id]],
        )
    )
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    canary_summary = f"Follow NCC xuất hóa đơn [TEST-{stamp}]"
    created_id = None
    try:
        created_id = _create_activity(
            picking,
            assignee,
            client,
            summary=canary_summary,
            deadline_days=0,
        )
        assert created_id
        created = client.read(
            "mail.activity",
            [created_id],
            fields=["id", "summary", "user_id", "res_model", "res_id", "date_deadline"],
        )[0]
        assert created["summary"] == canary_summary
        assert created["user_id"][0] == assignee["user_id"]
        assert created["res_model"] == "stock.picking"
        assert created["res_id"] == args.picking_id
        print(
            json.dumps(
                {
                    "server": host,
                    "database": db,
                    "receipt": [picking["id"], picking["name"]],
                    "purchase_order": assignee["purchase_order"],
                    "sale_order": assignee["sale_order"],
                    "assignee": [assignee["user_id"], assignee["user_name"]],
                    "resolution_source": assignee["source"],
                    "resolution_path": assignee["resolution_path"],
                    "canary_activity_id": created_id,
                    "verified": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        if created_id:
            client.unlink("mail.activity", [created_id])
        after_ids = set(
            client.search(
                "mail.activity",
                [["res_model", "=", "stock.picking"], ["res_id", "=", args.picking_id]],
            )
        )
        if after_ids != before_ids:
            raise RuntimeError(
                f"Canary cleanup mismatch: before={sorted(before_ids)}, after={sorted(after_ids)}"
            )

    print("Cleanup verified: all pre-existing activities are unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
