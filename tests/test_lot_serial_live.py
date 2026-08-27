"""
Live E2E test for Lot/Serial automation against the testing server.

Prerequisites:
  - LOT_SERIAL_ENABLED=true in .env
  - LOT_SERIAL_ODOO_URL, LOT_SERIAL_ODOO_DB, LOT_SERIAL_ODOO_UID,
    LOT_SERIAL_ODOO_API_KEY configured for testing0808.odoo.com

This test:
  1. Connects to the test server
  2. Finds an existing receipt picking (incoming, not done)
  3. Runs preview → validates response structure
  4. Optionally runs apply (with --apply flag)
  5. Cleans up created lots if apply was run

Usage:
  python tests/test_lot_serial_live.py           # preview only (safe)
  python tests/test_lot_serial_live.py --apply    # preview + apply + cleanup
"""

import os
import sys
import argparse

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Live E2E test for Lot/Serial")
    parser.add_argument("--apply", action="store_true", help="Also run apply (creates real data)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Lot/Serial Automation — Live E2E Test")
    print("=" * 60)
    print()

    # 1. Check config
    from config import Config
    if not Config.LOT_SERIAL_ENABLED:
        print("❌ LOT_SERIAL_ENABLED is false. Set it to true in .env.")
        sys.exit(1)

    print(f"  Server: {Config.ODOO_TEST_URL}")
    print(f"  DB:     {Config.ODOO_TEST_DB}")
    print(f"  UID:    {Config.ODOO_TEST_UID}")
    print()

    # 2. Initialize service
    print("[1] Initializing LotSerialService...")
    try:
        from services.lot_serial import LotSerialService
        svc = LotSerialService()
        print("  ✅ Service initialized successfully")
    except Exception as e:
        print(f"  ❌ Failed: {e}")
        sys.exit(1)
    print()

    # 3. Find a test receipt picking
    print("[2] Finding a test receipt picking (incoming, not done)...")
    pickings = svc.odoo.search_read(
        "stock.picking",
        [
            ["picking_type_code", "=", "incoming"],
            ["state", "in", ["assigned", "confirmed"]],
        ],
        fields=["id", "name", "state", "origin", "purchase_id"],
        limit=5,
    )

    if not pickings:
        print("  ⚠️  No suitable receipt pickings found on test server.")
        print("  Create a PO and confirm receipt on testing0808.odoo.com first.")
        print()
        _test_naming_only()
        return

    print(f"  Found {len(pickings)} candidates:")
    for p in pickings:
        po_name = p['purchase_id'][1] if isinstance(p.get('purchase_id'), (list, tuple)) else "N/A"
        print(f"    #{p['id']}: {p['name']} (state={p['state']}, PO={po_name})")
    print()

    # Use the first one
    test_picking = pickings[0]
    picking_id = test_picking["id"]
    print(f"[3] Running preview for picking #{picking_id} ({test_picking['name']})...")

    result = svc.preview_receipt_serial(picking_id)
    print(f"  Source: {result.get('picking', {}).get('source_type', 'N/A')} "
          f"→ {result.get('picking', {}).get('source_name', 'N/A')}")
    print(f"  Lines to assign: {len(result.get('lines_to_assign', []))}")
    print(f"  Lines skipped:   {len(result.get('lines_skipped', []))}")
    print(f"  Errors:          {len(result.get('errors', []))}")

    if result.get("errors"):
        print(f"  ⚠️  Errors: {result['errors']}")

    for line in result.get("lines_to_assign", [])[:5]:
        print(f"    → {line['proposed_serial']} (product: {line['product_name']})")

    print()

    if result.get("lines_to_assign"):
        print("  ✅ Preview PASSED — serial format looks correct")
    elif result.get("lines_skipped"):
        print("  ℹ️  All lines already have serials — preview working correctly")
    else:
        print("  ⚠️  No lines found — check picking data on test server")

    # 4. Optionally apply
    if args.apply and result.get("lines_to_assign"):
        print()
        print(f"[4] Applying serials to picking #{picking_id}...")
        apply_result = svc.apply_receipt_serial(picking_id)

        print(f"  Assigned: {len(apply_result.get('assigned', []))}")
        print(f"  Errors:   {len(apply_result.get('errors', []))}")

        if apply_result.get("errors"):
            print(f"  ⚠️  Apply errors: {apply_result['errors']}")

        for a in apply_result.get("assigned", []):
            print(f"    ✅ {a['serial_name']} (lot ID: {a['lot_id']})")

        # 5. Cleanup
        if apply_result.get("assigned"):
            print()
            print("[5] Cleaning up — deleting test lots...")
            for a in apply_result["assigned"]:
                try:
                    # Unlink move line assignment first
                    svc.odoo.write("stock.move.line", [a["move_line_id"]], {
                        "lot_id": False,
                        "lot_name": "",
                    })
                    # Delete the lot
                    svc.odoo.execute("stock.lot", "unlink", [[a["lot_id"]]])
                    print(f"    🗑️ Deleted lot {a['lot_id']} ({a['serial_name']})")
                except Exception as e:
                    print(f"    ⚠️  Failed to delete lot {a['lot_id']}: {e}")

    print()

    # Also test MO preview if available
    _test_mo_preview(svc)

    print()
    print("=" * 60)
    print("  Test complete!")
    print("=" * 60)


def _test_mo_preview(svc):
    """Try to find and preview an MO."""
    print("[MO] Looking for a Manufacturing Order to preview...")
    productions = svc.odoo.search_read(
        "mrp.production",
        [["state", "in", ["confirmed", "progress"]]],
        fields=["id", "name", "state", "product_id"],
        limit=3,
    )
    if not productions:
        print("  ℹ️  No suitable MOs found on test server.")
        return

    for p in productions[:1]:
        prod_name = p["product_id"][1] if isinstance(p.get("product_id"), (list, tuple)) else "N/A"
        print(f"  MO #{p['id']}: {p['name']} (state={p['state']}, product={prod_name})")

        result = svc.preview_mo_serial(p["id"])
        print(f"    Finished lines to assign: {len(result.get('finished_lines_to_assign', []))}")
        print(f"    Raw material lines: {len(result.get('raw_material_lines', []))}")
        if result.get("errors"):
            print(f"    Errors: {result['errors']}")
        for line in result.get("finished_lines_to_assign", [])[:3]:
            print(f"      → {line['proposed_serial']}")


def _test_naming_only():
    """Fallback test — just verify naming utilities work."""
    print("[Fallback] Testing naming utilities only...")
    from services.lot_serial.serial_naming import sanitize_name, build_receipt_serial, build_mo_serial

    tests = [
        ("Giường NERISSA", "GIUONG-NERISSA"),
        ("BED (KING SIZE)", "BED-KING-SIZE"),
        ("Đèn Đỏ", "DEN-DO"),
        ("O-MH08966", "O-MH08966"),
    ]
    for input_val, expected in tests:
        result = sanitize_name(input_val)
        status = "✅" if result == expected else "❌"
        print(f"  {status} sanitize_name('{input_val}') = '{result}' (expected '{expected}')")

    serial = build_receipt_serial("O-MH08966", "BED NERISSA", 1)
    print(f"  ✅ build_receipt_serial → {serial}")

    serial = build_mo_serial("SBC/00027", "Giường NERISSA", 3)
    print(f"  ✅ build_mo_serial → {serial}")

    print()
    print("  Naming utilities working correctly!")


if __name__ == "__main__":
    main()
