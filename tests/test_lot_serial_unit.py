"""
Unit tests for Lot/Serial automation — mocked Odoo calls.

Covers:
  - serial_naming: sanitize_name, build_receipt_serial, build_mo_serial, is_temp_serial
  - receipt_serial: preview/apply for PO receipts, skip existing lots, qty!=1, done picking
  - mo_serial: preview/apply for MO finished products, raw material preservation
  - serial_rename: preview/apply with whitelist, temp detection, duplicate check, audit trail
  - concurrency: threading lock prevents double-apply
"""

import os
import sys
import threading
import time
import unittest

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ═══════════════════════════════════════════
#  MOCK ODOO CLIENT
# ═══════════════════════════════════════════

class MockOdooClient:
    """In-memory mock of OdooClient for unit testing."""

    def __init__(self):
        self.lots = {}         # {id: {name, product_id, company_id, x_original_lot_name, messages}}
        self.pickings = {}     # {id: {name, state, origin, picking_type_code, purchase_id, ...}}
        self.move_lines = {}   # {id: {picking_id, product_id, quantity, lot_id, lot_name, move_id}}
        self.products = {}     # {id: {display_name, tracking}}
        self.purchase_orders = {}  # {id: {name}}
        self.productions = {}  # {id: {name, state, product_id, company_id}}
        self.moves = {}        # {id: {production_id, raw_material_production_id, product_id}}
        self.warehouses = {}   # {id: {code, name, lot_stock_id}}
        self.locations = {}    # {id: {name, warehouse_id, location_id}}
        self._next_id = 1000
        self._lot_next_id = 5000

    def _match_domain(self, record: dict, domain: list) -> bool:
        """Simple domain matcher for unit tests."""
        for clause in domain:
            if isinstance(clause, str):  # '|', '&' operators — skip for simplicity
                continue
            if not isinstance(clause, (list, tuple)) or len(clause) != 3:
                continue
            field, op, value = clause
            rec_val = record.get(field)

            # Handle Many2one fields stored as [id, name]
            if isinstance(rec_val, (list, tuple)) and len(rec_val) == 2:
                rec_val_id = rec_val[0]
            else:
                rec_val_id = rec_val

            if op == "=":
                if rec_val != value and rec_val_id != value:
                    return False
            elif op == "in":
                if rec_val not in value and rec_val_id not in value:
                    return False
            elif op == "!=":
                if rec_val == value or rec_val_id == value:
                    return False
        return True

    def _get_store(self, model: str) -> dict:
        stores = {
            "stock.picking": self.pickings,
            "stock.move.line": self.move_lines,
            "product.product": self.products,
            "purchase.order": self.purchase_orders,
            "mrp.production": self.productions,
            "stock.move": self.moves,
            "stock.lot": self.lots,
            "stock.warehouse": self.warehouses,
            "stock.location": self.locations,
        }
        return stores.get(model, {})

    def search_read(self, model, domain, fields=None, limit=0, order=None):
        store = self._get_store(model)
        results = []
        for rid, rec in store.items():
            full_rec = {"id": rid, **rec}
            if self._match_domain(full_rec, domain):
                if fields:
                    filtered = {"id": rid}
                    for f in fields:
                        if f in full_rec:
                            filtered[f] = full_rec[f]
                    results.append(filtered)
                else:
                    results.append(full_rec)
        if limit:
            results = results[:limit]
        return results

    def search(self, model, domain, limit=0):
        store = self._get_store(model)
        results = []
        for rid, rec in store.items():
            full_rec = {"id": rid, **rec}
            if self._match_domain(full_rec, domain):
                results.append(rid)
        if limit:
            results = results[:limit]
        return results

    def create(self, model, values, context=None):
        store = self._get_store(model)
        if model == "stock.lot":
            new_id = self._lot_next_id
            self._lot_next_id += 1
        else:
            new_id = self._next_id
            self._next_id += 1
        store[new_id] = dict(values)
        return new_id

    def write(self, model, record_ids, values):
        store = self._get_store(model)
        for rid in record_ids:
            if rid in store:
                store[rid].update(values)
        return True

    def unlink(self, model, record_ids):
        store = self._get_store(model)
        for rid in record_ids:
            store.pop(rid, None)
        return True

    def fields_get(self, model):
        if model == "stock.lot":
            return {
                "x_lot_serial_workflow_generated": {"type": "boolean"},
                "x_original_lot_name": {"type": "char"},
            }
        return {}

    def read(self, model, record_ids, fields=None):
        store = self._get_store(model)
        results = []
        for rid in record_ids:
            if rid in store:
                rec = {"id": rid, **store[rid]}
                if fields:
                    rec = {k: v for k, v in rec.items() if k in fields or k == "id"}
                results.append(rec)
        return results

    def execute(self, model, method, args=None, kwargs=None):
        if method == "message_post":
            # Track chatter messages
            lot_id = args[0] if args else None
            if lot_id and lot_id in self.lots:
                if "messages" not in self.lots[lot_id]:
                    self.lots[lot_id]["messages"] = []
                self.lots[lot_id]["messages"].append(kwargs or {})
            return True
        return True


# ═══════════════════════════════════════════
#  HELPER: Build a service with mocked Odoo
# ═══════════════════════════════════════════

def _make_service(odoo_mock=None):
    """Create a LotSerialService with a mocked OdooClient."""
    from services.lot_serial.receipt_serial import ReceiptSerialMixin
    from services.lot_serial.mo_serial import MOSerialMixin
    from services.lot_serial.serial_rename import SerialRenameMixin

    class TestService(ReceiptSerialMixin, MOSerialMixin, SerialRenameMixin):
        def __init__(self, odoo):
            self.odoo = odoo

    mock = odoo_mock or MockOdooClient()
    return TestService(mock), mock


# ═══════════════════════════════════════════
#  TEST: serial_naming utilities
# ═══════════════════════════════════════════

class TestSerialNaming(unittest.TestCase):
    """Tests for serial_naming.py utilities."""

    def test_sanitize_name_ascii(self):
        from services.lot_serial.serial_naming import sanitize_name
        self.assertEqual(sanitize_name("BED NERISSA"), "BED-NERISSA")

    def test_sanitize_name_uppercase(self):
        from services.lot_serial.serial_naming import sanitize_name
        self.assertEqual(sanitize_name("bed nerissa"), "BED-NERISSA")

    def test_sanitize_name_vietnamese(self):
        from services.lot_serial.serial_naming import sanitize_name
        result = sanitize_name("Giường NERISSA")
        self.assertEqual(result, "GIUONG-NERISSA")

    def test_sanitize_name_vietnamese_complex(self):
        from services.lot_serial.serial_naming import sanitize_name
        result = sanitize_name("Bàn Ăn Gỗ Óc Chó")
        self.assertEqual(result, "BAN-AN-GO-OC-CHO")

    def test_sanitize_name_d_stroke(self):
        from services.lot_serial.serial_naming import sanitize_name
        result = sanitize_name("Đèn Đỏ")
        self.assertEqual(result, "DEN-DO")

    def test_sanitize_name_special_chars(self):
        from services.lot_serial.serial_naming import sanitize_name
        result = sanitize_name("BED (KING SIZE)")
        self.assertEqual(result, "BED-KING-SIZE")

    def test_sanitize_name_double_dash(self):
        from services.lot_serial.serial_naming import sanitize_name
        result = sanitize_name("BED--NERISSA")
        self.assertEqual(result, "BED-NERISSA")

    def test_sanitize_name_preserves_slash(self):
        from services.lot_serial.serial_naming import sanitize_name
        result = sanitize_name("SBC/00027")
        self.assertEqual(result, "SBC/00027")

    def test_sanitize_name_preserves_dash(self):
        from services.lot_serial.serial_naming import sanitize_name
        result = sanitize_name("O-MH08966")
        self.assertEqual(result, "O-MH08966")

    def test_sanitize_name_empty(self):
        from services.lot_serial.serial_naming import sanitize_name
        self.assertEqual(sanitize_name(""), "")

    def test_sanitize_name_strip_leading_trailing_dash(self):
        from services.lot_serial.serial_naming import sanitize_name
        result = sanitize_name("-test-")
        self.assertEqual(result, "TEST")

    def test_build_receipt_serial(self):
        from services.lot_serial.serial_naming import build_receipt_serial
        result = build_receipt_serial("O-MH08966", "BED NERISSA", 1)
        self.assertEqual(result, "O-MH08966-BED-NERISSA-001")

    def test_build_receipt_serial_index_padding(self):
        from services.lot_serial.serial_naming import build_receipt_serial
        result = build_receipt_serial("O-MH08966", "BED NERISSA", 99)
        self.assertEqual(result, "O-MH08966-BED-NERISSA-099")

    def test_build_receipt_serial_uses_base_product_name(self):
        from services.lot_serial.serial_naming import build_receipt_serial
        result = build_receipt_serial(
            "O-MH08966", "ORD-BED-NERISSA (W1800, LENO 800)", 1
        )
        self.assertEqual(result, "O-MH08966-BED-NERISSA-001")

    def test_build_mo_serial(self):
        from services.lot_serial.serial_naming import build_mo_serial
        result = build_mo_serial("SBC/00027", "BED NERISSA", 1)
        self.assertEqual(result, "SBC/00027-BED-NERISSA-001")

    def test_build_mo_serial_vietnamese_product(self):
        from services.lot_serial.serial_naming import build_mo_serial
        result = build_mo_serial("SBC/00027", "Giường NERISSA", 3)
        self.assertEqual(result, "SBC/00027-GIUONG-NERISSA-003")

    def test_is_temp_serial_true(self):
        from services.lot_serial.serial_naming import is_temp_serial
        self.assertTrue(is_temp_serial("O-MH08966-BED-NERISSA-001"))
        self.assertTrue(is_temp_serial("SBC/00027-BED-NERISSA-001"))

    def test_is_temp_serial_false(self):
        from services.lot_serial.serial_naming import is_temp_serial
        self.assertFalse(is_temp_serial("REAL-SERIAL-XYZ"))
        self.assertFalse(is_temp_serial(""))
        self.assertFalse(is_temp_serial("SIMPLE"))


# ═══════════════════════════════════════════
#  TEST: Receipt Serial
# ═══════════════════════════════════════════

class TestReceiptSerial(unittest.TestCase):
    """Tests for receipt_serial.py — preview/apply logic."""

    def _setup_po_receipt(self, mock: MockOdooClient, state="assigned",
                          tracking="serial", qty=1.0,
                          existing_lot=None, num_lines=3):
        """Set up a standard PO receipt scenario."""
        # Product
        mock.products[10] = {
            "display_name": "BED NERISSA",
            "tracking": tracking,
        }
        # Purchase Order
        mock.purchase_orders[20] = {"name": "O-MH08966"}
        # Picking
        mock.pickings[100] = {
            "name": "WH/IN/00001",
            "state": state,
            "origin": "O-MH08966",
            "picking_type_code": "incoming",
            "purchase_id": [20, "O-MH08966"],
            "location_dest_id": [5, "MID"],
            "company_id": [1, "Test Company"],
        }
        # Move Lines
        for i in range(num_lines):
            ml_id = 200 + i
            mock.move_lines[ml_id] = {
                "picking_id": 100,
                "product_id": [10, "BED NERISSA"],
                "quantity": qty,
                "lot_id": existing_lot,
                "lot_name": "",
            }

    def test_receipt_preview_happy_path(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock, num_lines=3)

        result = svc.preview_receipt_serial(100)
        self.assertEqual(len(result["errors"]), 0)
        self.assertEqual(len(result["lines_to_assign"]), 3)
        self.assertEqual(result["picking"]["source_type"], "po")
        self.assertEqual(result["picking"]["source_name"], "O-MH08966")

        # Check serial format
        serials = [l["proposed_serial"] for l in result["lines_to_assign"]]
        self.assertEqual(serials[0], "O-MH08966-BED-NERISSA-001")
        self.assertEqual(serials[1], "O-MH08966-BED-NERISSA-002")
        self.assertEqual(serials[2], "O-MH08966-BED-NERISSA-003")

    def test_receipt_preview_skip_existing_lot(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock, num_lines=1, existing_lot=[99, "EXISTING-LOT"])

        result = svc.preview_receipt_serial(100)
        self.assertEqual(len(result["lines_to_assign"]), 0)
        self.assertEqual(len(result["lines_skipped"]), 1)
        self.assertIn("Already has lot/serial", result["lines_skipped"][0]["reason"])

    def test_receipt_preview_qty_not_one(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock, qty=2.0)

        result = svc.preview_receipt_serial(100)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("quantity", result["errors"][0].lower())

    def test_receipt_preview_done_picking(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock, state="done")

        result = svc.preview_receipt_serial(100)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("done", result["errors"][0].lower())

    def test_receipt_preview_cancelled_picking(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock, state="cancel")

        result = svc.preview_receipt_serial(100)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("cancel", result["errors"][0].lower())

    def test_receipt_preview_not_incoming(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock)
        mock.pickings[100]["picking_type_code"] = "outgoing"

        result = svc.preview_receipt_serial(100)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("incoming", result["errors"][0].lower())

    def test_receipt_preview_ambiguous_source(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock)
        # Remove PO link and origin
        mock.pickings[100]["purchase_id"] = False
        mock.pickings[100]["origin"] = ""

        result = svc.preview_receipt_serial(100)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("cannot determine source", result["errors"][0].lower())

    def test_receipt_preview_non_serial_product(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock, tracking="lot")

        result = svc.preview_receipt_serial(100)
        self.assertEqual(len(result["lines_to_assign"]), 0)
        self.assertEqual(len(result["lines_skipped"]), 3)

    def test_receipt_preview_picking_not_found(self):
        svc, mock = _make_service()
        result = svc.preview_receipt_serial(999)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("not found", result["errors"][0].lower())

    def test_receipt_apply_creates_lots(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock, num_lines=2)

        result = svc.apply_receipt_serial(100)
        self.assertEqual(len(result["errors"]), 0)
        self.assertEqual(len(result["assigned"]), 2)

        # Verify lots were created
        self.assertEqual(len(mock.lots), 2)
        lot_names = [lot["name"] for lot in mock.lots.values()]
        self.assertIn("O-MH08966-BED-NERISSA-001", lot_names)
        self.assertIn("O-MH08966-BED-NERISSA-002", lot_names)

        # Verify move lines were updated
        for ml in mock.move_lines.values():
            self.assertIsNotNone(ml.get("lot_id"))
            self.assertIn("O-MH08966-BED-NERISSA-", ml.get("lot_name", ""))

    def test_receipt_apply_continues_existing_sequence(self):
        svc, mock = _make_service()
        self._setup_po_receipt(mock, num_lines=1)

        # Pre-create a lot with the same name
        mock.lots[9999] = {
            "name": "O-MH08966-BED-NERISSA-001",
            "product_id": 10,
            "company_id": 1,
        }

        result = svc.apply_receipt_serial(100)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["assigned"][0]["serial_name"], "O-MH08966-BED-NERISSA-002")

    def test_receipt_apply_mo_source(self):
        """Receipt originating from Manufacturing Order."""
        svc, mock = _make_service()
        mock.products[10] = {"display_name": "BED NERISSA", "tracking": "serial"}
        mock.productions[30] = {"name": "SBC/00027", "state": "done", "product_id": [10, "BED NERISSA"]}
        mock.pickings[100] = {
            "name": "WH/IN/00002",
            "state": "assigned",
            "origin": "SBC/00027",
            "picking_type_code": "incoming",
            "purchase_id": False,
            "location_dest_id": [5, "MID"],
            "company_id": [1, "Test Company"],
        }
        mock.move_lines[200] = {
            "picking_id": 100,
            "product_id": [10, "BED NERISSA"],
            "quantity": 1.0,
            "lot_id": False,
            "lot_name": "",
        }

        result = svc.preview_receipt_serial(100)
        self.assertEqual(result["picking"]["source_type"], "mo")
        self.assertEqual(result["picking"]["source_name"], "SBC/00027")
        self.assertEqual(
            result["lines_to_assign"][0]["proposed_serial"],
            "SBC/00027-BED-NERISSA-001"
        )


# ═══════════════════════════════════════════
#  TEST: MO Serial
# ═══════════════════════════════════════════

class TestMOSerial(unittest.TestCase):
    """Tests for mo_serial.py — preview/apply for MO finished products."""

    def _setup_mo(self, mock: MockOdooClient, state="confirmed",
                  tracking="serial", qty=1.0,
                  existing_lot=None, num_finished=2, num_raw=1):
        """Set up a Manufacturing Order scenario."""
        # Finished product
        mock.products[10] = {
            "display_name": "BED NERISSA",
            "tracking": tracking,
        }
        # Raw material
        mock.products[11] = {
            "display_name": "WOOD PLANK",
            "tracking": "serial",
        }
        # Production
        mock.productions[50] = {
            "name": "SBC/00027",
            "state": state,
            "product_id": [10, "BED NERISSA"],
            "company_id": [1, "Test Company"],
        }
        # Finished moves
        for i in range(num_finished):
            move_id = 300 + i
            mock.moves[move_id] = {
                "production_id": 50,
                "raw_material_production_id": False,
                "product_id": [10, "BED NERISSA"],
            }
            ml_id = 400 + i
            mock.move_lines[ml_id] = {
                "move_id": move_id,
                "picking_id": False,
                "product_id": [10, "BED NERISSA"],
                "quantity": qty,
                "lot_id": existing_lot,
                "lot_name": "",
            }
        # Raw material moves
        for i in range(num_raw):
            raw_move_id = 350 + i
            mock.moves[raw_move_id] = {
                "production_id": False,
                "raw_material_production_id": 50,
                "product_id": [11, "WOOD PLANK"],
            }
            raw_ml_id = 450 + i
            mock.move_lines[raw_ml_id] = {
                "move_id": raw_move_id,
                "picking_id": False,
                "product_id": [11, "WOOD PLANK"],
                "quantity": 1.0,
                "lot_id": [88, "RAW-SERIAL-001"],
                "lot_name": "RAW-SERIAL-001",
            }

    def test_mo_preview_happy_path(self):
        svc, mock = _make_service()
        self._setup_mo(mock, num_finished=2)

        result = svc.preview_mo_serial(50)
        self.assertEqual(len(result["errors"]), 0)
        self.assertEqual(len(result["finished_lines_to_assign"]), 2)
        serials = [l["proposed_serial"] for l in result["finished_lines_to_assign"]]
        self.assertEqual(serials[0], "SBC/00027-BED-NERISSA-001")
        self.assertEqual(serials[1], "SBC/00027-BED-NERISSA-002")

    def test_mo_preview_done_state(self):
        svc, mock = _make_service()
        self._setup_mo(mock, state="done")

        result = svc.preview_mo_serial(50)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("done", result["errors"][0].lower())

    def test_mo_preview_skip_existing_lot(self):
        svc, mock = _make_service()
        self._setup_mo(mock, existing_lot=[77, "EXISTING-001"], num_finished=1)

        result = svc.preview_mo_serial(50)
        self.assertEqual(len(result["finished_lines_to_assign"]), 0)
        self.assertEqual(len(result["finished_lines_skipped"]), 1)

    def test_mo_preview_qty_not_one(self):
        svc, mock = _make_service()
        self._setup_mo(mock, qty=5.0, num_finished=1)

        result = svc.preview_mo_serial(50)
        self.assertTrue(len(result["errors"]) > 0)

    def test_mo_preview_non_serial_product(self):
        svc, mock = _make_service()
        self._setup_mo(mock, tracking="none")

        result = svc.preview_mo_serial(50)
        self.assertEqual(len(result["finished_lines_to_assign"]), 0)

    def test_mo_apply_creates_lots(self):
        svc, mock = _make_service()
        self._setup_mo(mock, num_finished=2)

        result = svc.apply_mo_serial(50)
        self.assertEqual(len(result["errors"]), 0)
        self.assertEqual(len(result["assigned"]), 2)

        lot_names = [lot["name"] for lot in mock.lots.values()]
        self.assertIn("SBC/00027-BED-NERISSA-001", lot_names)
        self.assertIn("SBC/00027-BED-NERISSA-002", lot_names)

    def test_mo_apply_preserves_raw_materials(self):
        """Raw material move lines must NOT be modified."""
        svc, mock = _make_service()
        self._setup_mo(mock, num_finished=1, num_raw=2)

        # Record raw material state before
        raw_before = {
            ml_id: dict(ml) for ml_id, ml in mock.move_lines.items()
            if ml_id >= 450
        }

        svc.apply_mo_serial(50)

        # Verify raw material lines unchanged
        for ml_id, before in raw_before.items():
            after = mock.move_lines[ml_id]
            self.assertEqual(before["lot_id"], after["lot_id"])
            self.assertEqual(before["lot_name"], after["lot_name"])

    def test_mo_apply_continues_existing_sequence(self):
        svc, mock = _make_service()
        self._setup_mo(mock, num_finished=1)

        mock.lots[9999] = {
            "name": "SBC/00027-BED-NERISSA-001",
            "product_id": 10,
            "company_id": 1,
        }

        result = svc.apply_mo_serial(50)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["assigned"][0]["serial_name"], "SBC/00027-BED-NERISSA-002")

    def test_mo_not_found(self):
        svc, mock = _make_service()
        result = svc.preview_mo_serial(999)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("not found", result["errors"][0].lower())


# ═══════════════════════════════════════════
#  TEST: Serial Rename
# ═══════════════════════════════════════════

class TestSerialRename(unittest.TestCase):
    """Tests for serial_rename.py — preview/apply with whitelist + audit."""

    def _setup_rename(self, mock: MockOdooClient, dest_wh_code="ORDST",
                      state="done", lot_name="O-MH08966-BED-NERISSA-001"):
        """Set up a completed picking for rename testing."""
        # Warehouse
        mock.warehouses[1] = {"code": dest_wh_code, "name": "ORDST", "lot_stock_id": 5}
        mock.locations[5] = {"name": dest_wh_code, "warehouse_id": [1, dest_wh_code], "location_id": False}

        # Lot
        mock.lots[500] = {
            "name": lot_name,
            "product_id": 10,
            "company_id": 1,
            "x_lot_serial_workflow_generated": True,
        }
        mock.products[10] = {"display_name": "BED NERISSA", "tracking": "serial"}

        # Picking (done)
        mock.pickings[100] = {
            "name": "WH/INT/00001",
            "state": state,
            "origin": "",
            "picking_type_code": "internal",
            "purchase_id": False,
            "location_dest_id": [5, dest_wh_code],
            "location_id": [6, "MID"],
            "company_id": [1, "Test Company"],
        }
        mock.warehouses[2] = {"code": "MID", "name": "MID", "lot_stock_id": 6}
        mock.locations[6] = {"name": "MID", "warehouse_id": [2, "MID"], "location_id": False}
        # Move line
        mock.move_lines[200] = {
            "picking_id": 100,
            "product_id": [10, "BED NERISSA"],
            "quantity": 1.0,
            "lot_id": [500, lot_name],
            "lot_name": lot_name,
        }

    def test_rename_preview_happy_path(self):
        svc, mock = _make_service()
        self._setup_rename(mock)

        result = svc.preview_rename_serial(100, {
            "O-MH08966-BED-NERISSA-001": "REAL-SERIAL-XYZ"
        })
        self.assertEqual(len(result["errors"]), 0)
        self.assertEqual(len(result["will_rename"]), 1)
        self.assertEqual(result["will_rename"][0]["old_name"], "O-MH08966-BED-NERISSA-001")
        self.assertEqual(result["will_rename"][0]["new_name"], "REAL-SERIAL-XYZ")

    def test_rename_preview_wrong_warehouse(self):
        svc, mock = _make_service()
        self._setup_rename(mock, dest_wh_code="RANDOM-WH")

        result = svc.preview_rename_serial(100, {
            "O-MH08966-BED-NERISSA-001": "REAL-SERIAL-XYZ"
        })
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("whitelist", result["errors"][0].lower())

    def test_rename_preview_wrong_source_warehouse(self):
        svc, mock = _make_service()
        self._setup_rename(mock)
        mock.warehouses[2]["code"] = "ORDST"
        mock.locations[6]["name"] = "ORDST"

        result = svc.preview_rename_serial(100, {
            "O-MH08966-BED-NERISSA-001": "REAL-SERIAL-XYZ"
        })
        self.assertTrue(result["errors"])
        self.assertIn("source warehouse", result["errors"][0].lower())

    def test_rename_preview_not_done(self):
        svc, mock = _make_service()
        self._setup_rename(mock, state="assigned")

        result = svc.preview_rename_serial(100, {
            "O-MH08966-BED-NERISSA-001": "REAL-SERIAL-XYZ"
        })
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("done", result["errors"][0].lower())

    def test_rename_preview_not_temp_serial(self):
        svc, mock = _make_service()
        self._setup_rename(mock, lot_name="REAL-SERIAL")

        result = svc.preview_rename_serial(100, {
            "REAL-SERIAL": "NEW-SERIAL"
        })
        self.assertEqual(len(result["will_rename"]), 0)
        self.assertEqual(len(result["will_skip"]), 1)
        self.assertIn("not a temp serial", result["will_skip"][0]["reason"].lower())

    def test_rename_preview_duplicate_new_serial(self):
        svc, mock = _make_service()
        self._setup_rename(mock)

        # Pre-create a lot with the target name
        mock.lots[600] = {
            "name": "REAL-SERIAL-XYZ",
            "product_id": 10,
            "company_id": 1,
        }

        result = svc.preview_rename_serial(100, {
            "O-MH08966-BED-NERISSA-001": "REAL-SERIAL-XYZ"
        })
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("already exists", result["errors"][0].lower())

    def test_rename_preview_empty_rename_map(self):
        svc, mock = _make_service()
        self._setup_rename(mock)

        result = svc.preview_rename_serial(100, {})
        self.assertTrue(len(result["errors"]) > 0)
        self.assertIn("empty", result["errors"][0].lower())

    def test_rename_preview_serial_not_in_picking(self):
        svc, mock = _make_service()
        self._setup_rename(mock)

        result = svc.preview_rename_serial(100, {
            "NONEXISTENT-SERIAL-001": "NEW-SERIAL"
        })
        self.assertEqual(len(result["will_skip"]), 1)
        self.assertIn("not found", result["will_skip"][0]["reason"].lower())

    def test_rename_apply_writes_audit(self):
        svc, mock = _make_service()
        self._setup_rename(mock)

        result = svc.apply_rename_serial(100, {
            "O-MH08966-BED-NERISSA-001": "REAL-SERIAL-XYZ"
        })
        self.assertEqual(len(result["errors"]), 0)
        self.assertEqual(len(result["renamed"]), 1)

        # Verify lot was renamed
        lot = mock.lots[500]
        self.assertEqual(lot["name"], "REAL-SERIAL-XYZ")
        # Verify audit field
        self.assertEqual(lot.get("x_original_lot_name"), "O-MH08966-BED-NERISSA-001")

    def test_rename_apply_posts_chatter(self):
        svc, mock = _make_service()
        self._setup_rename(mock)

        svc.apply_rename_serial(100, {
            "O-MH08966-BED-NERISSA-001": "REAL-SERIAL-XYZ"
        })

        # Verify chatter message
        lot = mock.lots[500]
        self.assertTrue(len(lot.get("messages", [])) > 0)
        msg_body = lot["messages"][0].get("body", "")
        self.assertIn("O-MH08966-BED-NERISSA-001", msg_body)
        self.assertIn("REAL-SERIAL-XYZ", msg_body)

    def test_rename_picking_not_found(self):
        svc, mock = _make_service()
        result = svc.preview_rename_serial(999, {"A": "B"})
        self.assertTrue(len(result["errors"]) > 0)


# ═══════════════════════════════════════════
#  TEST: Concurrency Lock
# ═══════════════════════════════════════════

class TestConcurrency(unittest.TestCase):
    """Tests for thread-safety of apply operations."""

    def test_concurrent_receipt_apply_blocked(self):
        """Second concurrent apply for same picking should return error."""
        svc, mock = _make_service()

        # Setup a valid PO receipt
        mock.products[10] = {"display_name": "BED NERISSA", "tracking": "serial"}
        mock.purchase_orders[20] = {"name": "O-MH08966"}
        mock.pickings[100] = {
            "name": "WH/IN/00001", "state": "assigned",
            "origin": "O-MH08966", "picking_type_code": "incoming",
            "purchase_id": [20, "O-MH08966"], "location_dest_id": [5, "MID"],
            "company_id": [1, "Test"],
        }
        mock.move_lines[200] = {
            "picking_id": 100, "product_id": [10, "BED NERISSA"],
            "quantity": 1.0, "lot_id": False, "lot_name": "",
        }

        # Manually acquire the lock to simulate a concurrent apply
        from services.lot_serial.receipt_serial import _get_picking_lock
        lock = _get_picking_lock(100)
        lock.acquire()

        try:
            result = svc.apply_receipt_serial(100)
            self.assertTrue(len(result["errors"]) > 0)
            self.assertIn("already being processed", result["errors"][0].lower())
        finally:
            lock.release()


# ═══════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
