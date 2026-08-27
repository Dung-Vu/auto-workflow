"""
MO Serial Mixin — auto-generate serial numbers for Manufacturing Order
finished products.

Key rules:
  - Only applies to finished product (product_id == production.product_id)
  - Only for products with tracking="serial"
  - Each move_line must have quantity == 1
  - Raw material/component move lines are NOT modified (preserves traceability)
  - Serial format: {MO_NAME}-{PRODUCT_NAME}-{INDEX:03d}
  - Example: SBC/00027-BED-NERISSA-001
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .serial_naming import build_mo_serial, build_plan_hash, serial_prefix
from .workflow_fields import WORKFLOW_CREATED_FIELD, assert_workflow_fields

logger = logging.getLogger(__name__)

# Lock registry for MO apply operations
_mo_apply_locks: dict[int, threading.Lock] = {}
_mo_locks_lock = threading.Lock()


def _get_mo_lock(production_id: int) -> threading.Lock:
    with _mo_locks_lock:
        if production_id not in _mo_apply_locks:
            _mo_apply_locks[production_id] = threading.Lock()
        return _mo_apply_locks[production_id]


class MOSerialMixin:
    """Mixin for Manufacturing Order serial generation.

    Expects self.odoo to be an OdooClient instance (test server).
    """

    def preview_mo_serial(self, production_id: int) -> dict:
        """Preview serial assignments for a Manufacturing Order's finished product.

        Returns:
            {
                "production": {id, name, state, product_name},
                "finished_lines_to_assign": [...],
                "finished_lines_skipped": [...],
                "raw_material_lines": [...],  # read-only, for info
                "errors": [str]
            }
        """
        return self._do_mo_preview(production_id)

    def apply_mo_serial(self, production_id: int, expected_plan_hash: str | None = None) -> dict:
        """Create and assign serial numbers to MO finished product move lines.

        Thread-safe per production_id.

        Returns:
            {
                "production_id": int,
                "assigned": [{move_line_id, serial_name, lot_id}],
                "skipped": [...],
                "errors": [str]
            }
        """
        lock = _get_mo_lock(production_id)
        if not lock.acquire(blocking=False):
            return {
                "production_id": production_id,
                "assigned": [],
                "skipped": [],
                "errors": ["Manufacturing Order is already being processed. Please wait."],
            }
        try:
            return self._do_mo_apply(production_id, expected_plan_hash)
        finally:
            lock.release()

    # ─── Internal Implementation ────────────────────────────────

    def _do_mo_preview(self, production_id: int) -> dict:
        errors: list[str] = []
        finished_to_assign: list[dict] = []
        finished_skipped: list[dict] = []
        raw_material_lines: list[dict] = []

        # 1. Fetch production order
        productions = self.odoo.search_read(
            "mrp.production",
            [["id", "=", production_id]],
            fields=["id", "name", "state", "product_id", "company_id"],
            limit=1,
        )
        if not productions:
            return self._mo_error_result(
                production_id,
                f"Manufacturing Order ID {production_id} not found"
            )

        production = productions[0]

        if production["state"] in ("done", "cancel"):
            return self._mo_error_result(
                production_id,
                f"MO {production['name']} is '{production['state']}' — "
                f"cannot assign serials"
            )

        mo_product_id = (
            production["product_id"][0]
            if isinstance(production["product_id"], (list, tuple))
            else production["product_id"]
        )

        # 2. Fetch the finished product info
        finished_products = self.odoo.search_read(
            "product.product",
            [["id", "=", mo_product_id]],
            fields=["id", "tracking", "display_name"],
            limit=1,
        )
        if not finished_products:
            return self._mo_error_result(
                production_id,
                f"Product ID {mo_product_id} not found"
            )

        finished_product = finished_products[0]

        # 3. Fetch all stock.move for this production
        moves = self.odoo.search_read(
            "stock.move",
            [["production_id", "=", production_id]],
            fields=["id", "product_id", "state"],
        )

        # Also fetch moves where the MO is the origin (finished moves)
        finished_moves = self.odoo.search_read(
            "stock.move",
            [
                ["production_id", "=", production_id],
                ["product_id", "=", mo_product_id],
            ],
            fields=["id", "product_id"],
        )

        # Combine and get unique finished move IDs
        finished_move_ids = {m["id"] for m in finished_moves}

        # If no finished moves found via production_id, try raw_material_production_id
        if not finished_move_ids:
            # In Odoo, finished product moves have production_id set
            # and product_id == production.product_id
            all_moves = self.odoo.search_read(
                "stock.move",
                [
                    "|",
                    ["production_id", "=", production_id],
                    ["raw_material_production_id", "=", production_id],
                ],
                fields=["id", "product_id", "raw_material_production_id", "production_id"],
            )
            finished_move_ids = {
                m["id"] for m in all_moves
                if (
                    (isinstance(m["product_id"], (list, tuple)) and m["product_id"][0] == mo_product_id)
                    or m["product_id"] == mo_product_id
                ) and m.get("production_id")
            }
            raw_move_ids = {
                m["id"] for m in all_moves
                if m["id"] not in finished_move_ids
            }
        else:
            all_move_ids = {m["id"] for m in moves}
            raw_move_ids = all_move_ids - finished_move_ids

        # 4. Fetch move lines for finished product moves
        if finished_move_ids:
            finished_mls = self.odoo.search_read(
                "stock.move.line",
                [["move_id", "in", list(finished_move_ids)]],
                fields=["id", "product_id", "quantity", "lot_id", "lot_name", "move_id"],
            )
        else:
            finished_mls = []
            errors.append(
                f"No finished product moves found for MO {production['name']}"
            )

        # 5. Process finished product lines
        company_id = self._to_id(production.get("company_id")) or 1
        serial_counter = None
        for ml in finished_mls:
            pid = (
                ml["product_id"][0]
                if isinstance(ml["product_id"], (list, tuple))
                else ml["product_id"]
            )

            # Only process the MO's finished product
            if pid != mo_product_id:
                finished_skipped.append({
                    "move_line_id": ml["id"],
                    "product_id": pid,
                    "product_name": "by-product",
                    "reason": "Not the finished product of this MO",
                })
                continue

            # Check tracking
            if finished_product.get("tracking") != "serial":
                finished_skipped.append({
                    "move_line_id": ml["id"],
                    "product_id": pid,
                    "product_name": finished_product["display_name"],
                    "reason": f"Product tracking is '{finished_product.get('tracking')}', not 'serial'",
                })
                continue

            # Skip if already has lot
            if ml.get("lot_id"):
                lot_display = (
                    ml["lot_id"][1]
                    if isinstance(ml["lot_id"], (list, tuple))
                    else str(ml["lot_id"])
                )
                finished_skipped.append({
                    "move_line_id": ml["id"],
                    "product_id": pid,
                    "product_name": finished_product["display_name"],
                    "reason": f"Already has lot/serial: {lot_display}",
                })
                continue

            if ml.get("lot_name"):
                finished_skipped.append({
                    "move_line_id": ml["id"],
                    "product_id": pid,
                    "product_name": finished_product["display_name"],
                    "reason": f"Already has lot_name: {ml['lot_name']}",
                })
                continue

            # Validate quantity
            qty = ml.get("quantity", 0)
            if qty != 1.0:
                errors.append(
                    f"Move line {ml['id']} has quantity {qty}, expected 1.0 "
                    f"for serial tracking"
                )
                continue

            # Build serial
            if serial_counter is None:
                serial_counter = self._next_sequence(
                    mo_product_id,
                    company_id,
                    serial_prefix(production["name"], finished_product["display_name"]),
                )
            serial = build_mo_serial(
                production["name"],
                finished_product["display_name"],
            serial_counter,
            )
            serial_counter += 1

            finished_to_assign.append({
                "move_line_id": ml["id"],
                "product_id": pid,
                "product_name": finished_product["display_name"],
                "proposed_serial": serial,
                "quantity": qty,
            })

        # 6. Fetch raw material lines (read-only, for traceability info)
        if raw_move_ids:
            raw_mls = self.odoo.search_read(
                "stock.move.line",
                [["move_id", "in", list(raw_move_ids)]],
                fields=["id", "product_id", "quantity", "lot_id", "lot_name"],
            )
            for rml in raw_mls:
                rpid = (
                    rml["product_id"][0]
                    if isinstance(rml["product_id"], (list, tuple))
                    else rml["product_id"]
                )
                raw_material_lines.append({
                    "move_line_id": rml["id"],
                    "product_id": rpid,
                    "product_name": (
                        rml["product_id"][1]
                        if isinstance(rml["product_id"], (list, tuple)) and len(rml["product_id"]) > 1
                        else str(rpid)
                    ),
                    "has_lot": bool(rml.get("lot_id")),
                    "lot_name": (
                        rml["lot_id"][1]
                        if isinstance(rml.get("lot_id"), (list, tuple))
                        else rml.get("lot_name", "")
                    ),
                })

        result = {
            "production": {
                "id": production["id"],
                "name": production["name"],
                "state": production["state"],
                "product_name": finished_product["display_name"],
            },
            "finished_lines_to_assign": finished_to_assign,
            "finished_lines_skipped": finished_skipped,
            "raw_material_lines": raw_material_lines,
            "errors": errors,
        }
        result["plan_hash"] = build_plan_hash("mo", production_id, finished_to_assign)
        return result

    def _do_mo_apply(self, production_id: int, expected_plan_hash: str | None) -> dict:
        preview = self._do_mo_preview(production_id)

        if preview.get("errors"):
            return {
                "production_id": production_id,
                "assigned": [],
                "skipped": preview.get("finished_lines_skipped", []),
                "errors": preview["errors"],
            }

        if expected_plan_hash is not None and expected_plan_hash != preview.get("plan_hash"):
            return {
                "production_id": production_id,
                "assigned": [],
                "skipped": preview.get("finished_lines_skipped", []),
                "errors": ["Preview is missing or stale. Preview again and send its plan_hash to Apply."],
            }

        try:
            assert_workflow_fields(self.odoo)
        except Exception as error:
            return {
                "production_id": production_id,
                "assigned": [],
                "skipped": preview.get("finished_lines_skipped", []),
                "errors": [str(error)],
            }

        if not preview.get("finished_lines_to_assign"):
            return {
                "production_id": production_id,
                "assigned": [],
                "skipped": preview.get("finished_lines_skipped", []),
                "errors": [
                    "No finished product lines to assign — "
                    "all lines already have serials or are skipped"
                ],
            }

        # Fetch company_id from production
        productions = self.odoo.search_read(
            "mrp.production",
            [["id", "=", production_id]],
            fields=["company_id"],
            limit=1,
        )
        company_id = (
            productions[0]["company_id"][0]
            if productions and isinstance(productions[0].get("company_id"), (list, tuple))
            else productions[0].get("company_id", 1) if productions
            else 1
        )

        assigned = []
        errors = []

        # Pre-check all serials for duplicates
        for line in preview["finished_lines_to_assign"]:
            serial_name = line["proposed_serial"]
            product_id = line["product_id"]

            existing = self.odoo.search(
                "stock.lot",
                [
                    ["name", "=", serial_name],
                    ["product_id", "=", product_id],
                    ["company_id", "=", company_id],
                ],
                limit=1,
            )
            if existing:
                errors.append(
                    f"Serial '{serial_name}' already exists for product "
                    f"{line['product_name']} (lot ID: {existing[0]}). "
                    f"Aborting entire operation."
                )
                return {
                    "production_id": production_id,
                    "assigned": [],
                    "skipped": preview.get("finished_lines_skipped", []),
                    "errors": errors,
                }

        # Create and assign, compensating every created lot if one write fails.
        created: list[tuple[int, int]] = []
        for line in preview["finished_lines_to_assign"]:
            serial_name = line["proposed_serial"]
            product_id = line["product_id"]
            move_line_id = line["move_line_id"]

            try:
                lot_id = self.odoo.create("stock.lot", {
                    "name": serial_name,
                    "product_id": product_id,
                    "company_id": company_id,
                    WORKFLOW_CREATED_FIELD: True,
                })
                created.append((move_line_id, lot_id))

                self.odoo.write("stock.move.line", [move_line_id], {
                    "lot_id": lot_id,
                    "lot_name": serial_name,
                })

                assigned.append({
                    "move_line_id": move_line_id,
                    "serial_name": serial_name,
                    "lot_id": lot_id,
                    "product_name": line["product_name"],
                })

                logger.info(
                    f"[LotSerial] MO assigned serial '{serial_name}' to "
                    f"move_line {move_line_id} (lot ID: {lot_id})"
                )

            except Exception as e:
                errors.append(f"Failed to create/assign serial '{serial_name}' for move_line {move_line_id}: {e}")
                errors.extend(self._rollback_created(created))
                logger.exception(f"[LotSerial] MO error assigning serial: {e}")
                return {
                    "production_id": production_id,
                    "assigned": [],
                    "skipped": preview.get("finished_lines_skipped", []),
                    "errors": errors,
                }

        return {
            "production_id": production_id,
            "assigned": assigned,
            "skipped": preview.get("finished_lines_skipped", []),
            "errors": errors,
        }

    @staticmethod
    def _mo_error_result(production_id: int, error: str) -> dict:
        return {
            "production": {"id": production_id},
            "finished_lines_to_assign": [],
            "finished_lines_skipped": [],
            "raw_material_lines": [],
            "errors": [error],
        }
