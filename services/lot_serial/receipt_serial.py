"""
Receipt Serial Mixin — auto-generate serial numbers for incoming receipts.

Handles both Purchase Order receipts and Manufacturing Order receipts
into the MID warehouse. Only applies to products with tracking="serial".

Key rules:
  - Each move_line must have quantity == 1 (serial tracking)
  - PO name is resolved from purchase.order, not from picking.origin
  - MO name is resolved from mrp.production if receipt originates from MO
  - Existing lot/serial assignments are never overwritten
  - Serial format: {PO_NAME}-{PRODUCT_NAME}-{INDEX:03d}
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .serial_naming import (
    build_plan_hash,
    build_receipt_from_mo_serial,
    build_receipt_serial,
    serial_prefix,
)
from .workflow_fields import WORKFLOW_CREATED_FIELD, assert_workflow_fields

logger = logging.getLogger(__name__)

# Lock registry: one lock per picking_id to prevent concurrent apply
_apply_locks: dict[int, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_picking_lock(picking_id: int) -> threading.Lock:
    with _locks_lock:
        if picking_id not in _apply_locks:
            _apply_locks[picking_id] = threading.Lock()
        return _apply_locks[picking_id]


class ReceiptSerialMixin:
    """Mixin for receipt serial generation.

    Expects self.odoo to be an OdooClient instance (test server).
    """

    def preview_receipt_serial(self, picking_id: int) -> dict:
        """Preview serial assignments for a receipt picking.

        Returns:
            {
                "picking": {id, name, state, origin, source_type, source_name},
                "lines_to_assign": [
                    {move_line_id, product_id, product_name, proposed_serial, ...}
                ],
                "lines_skipped": [
                    {move_line_id, product_id, product_name, reason, ...}
                ],
                "errors": [str]
            }
        """
        return self._do_receipt_preview(picking_id)

    def apply_receipt_serial(self, picking_id: int, expected_plan_hash: str | None = None) -> dict:
        """Create and assign serial numbers to receipt move lines.

        Thread-safe per picking_id — concurrent calls for the same
        picking will block until the first completes.

        Returns:
            {
                "picking_id": int,
                "assigned": [{move_line_id, serial_name, lot_id}],
                "skipped": [{move_line_id, reason}],
                "errors": [str]
            }
        """
        lock = _get_picking_lock(picking_id)
        if not lock.acquire(blocking=False):
            return {
                "picking_id": picking_id,
                "assigned": [],
                "skipped": [],
                "errors": ["Picking is already being processed. Please wait."],
            }
        try:
            return self._do_receipt_apply(picking_id, expected_plan_hash)
        finally:
            lock.release()

    # ─── Internal Implementation ────────────────────────────────

    def _do_receipt_preview(self, picking_id: int) -> dict:
        """Core preview logic — shared between preview and apply."""
        errors: list[str] = []
        lines_to_assign: list[dict] = []
        lines_skipped: list[dict] = []

        # 1. Fetch picking
        pickings = self.odoo.search_read(
            "stock.picking",
            [["id", "=", picking_id]],
            fields=[
                "id", "name", "state", "origin", "picking_type_code",
                "purchase_id", "location_dest_id", "company_id",
            ],
            limit=1,
        )
        if not pickings:
            return self._error_result(picking_id, f"Picking ID {picking_id} not found")

        picking = pickings[0]

        # Validate state
        if picking["state"] in ("done", "cancel"):
            return self._error_result(
                picking_id,
                f"Picking {picking['name']} is '{picking['state']}' — cannot assign serials"
            )

        # Validate picking type
        if picking.get("picking_type_code") != "incoming":
            return self._error_result(
                picking_id,
                f"Picking {picking['name']} is '{picking.get('picking_type_code')}', "
                f"not 'incoming'. Receipt serial only applies to incoming receipts."
            )

        # 2. Resolve source (PO or MO)
        source_type, source_name = self._resolve_receipt_source(picking)
        if not source_type:
            return self._error_result(
                picking_id,
                f"Cannot determine source for picking {picking['name']}. "
                f"Origin: '{picking.get('origin', '')}'. "
                f"Expected a linked purchase.order or mrp.production."
            )

        # 3. Fetch move lines
        move_lines = self.odoo.search_read(
            "stock.move.line",
            [["picking_id", "=", picking_id]],
            fields=[
                "id", "product_id", "quantity", "lot_id", "lot_name",
            ],
        )

        if not move_lines:
            return self._error_result(
                picking_id,
                f"Picking {picking['name']} has no move lines (Detailed Operations)"
            )

        # 4. Fetch product tracking info for all products in move lines
        product_ids = list({
            ml["product_id"][0] if isinstance(ml["product_id"], (list, tuple))
            else ml["product_id"]
            for ml in move_lines if ml.get("product_id")
        })
        products_data = {}
        if product_ids:
            products = self.odoo.search_read(
                "product.product",
                [["id", "in", product_ids]],
                fields=["id", "tracking", "display_name"],
            )
            products_data = {p["id"]: p for p in products}

        # 5. Continue the source/product sequence across backorders.
        company_id = self._to_id(picking.get("company_id")) or 1
        serial_counters: dict[int, int] = {}

        for ml in move_lines:
            pid = (
                ml["product_id"][0]
                if isinstance(ml["product_id"], (list, tuple))
                else ml["product_id"]
            )
            product = products_data.get(pid, {})
            product_name = product.get("display_name", "UNKNOWN")
            tracking = product.get("tracking", "none")

            # Skip non-serial products
            if tracking != "serial":
                lines_skipped.append({
                    "move_line_id": ml["id"],
                    "product_id": pid,
                    "product_name": product_name,
                    "reason": f"Product tracking is '{tracking}', not 'serial'",
                })
                continue

            # Skip if already has lot
            if ml.get("lot_id"):
                lot_display = (
                    ml["lot_id"][1]
                    if isinstance(ml["lot_id"], (list, tuple))
                    else str(ml["lot_id"])
                )
                lines_skipped.append({
                    "move_line_id": ml["id"],
                    "product_id": pid,
                    "product_name": product_name,
                    "reason": f"Already has lot/serial: {lot_display}",
                })
                continue

            # Skip if lot_name already filled (manually entered but not yet saved as lot)
            if ml.get("lot_name"):
                lines_skipped.append({
                    "move_line_id": ml["id"],
                    "product_id": pid,
                    "product_name": product_name,
                    "reason": f"Already has lot_name: {ml['lot_name']}",
                })
                continue

            # Validate quantity == 1
            qty = ml.get("quantity", 0)
            if qty != 1.0:
                errors.append(
                    f"Move line {ml['id']} (product: {product_name}) has quantity "
                    f"{qty}, expected 1.0 for serial tracking"
                )
                continue

            # Build serial name
            if pid not in serial_counters:
                serial_counters[pid] = self._next_sequence(
                    pid, company_id, serial_prefix(source_name, product_name)
                )
            idx = serial_counters[pid]
            serial_counters[pid] += 1

            if source_type == "po":
                serial = build_receipt_serial(source_name, product_name, idx)
            else:  # mo
                serial = build_receipt_from_mo_serial(source_name, product_name, idx)

            lines_to_assign.append({
                "move_line_id": ml["id"],
                "product_id": pid,
                "product_name": product_name,
                "proposed_serial": serial,
                "quantity": qty,
            })

        result = {
            "picking": {
                "id": picking["id"],
                "name": picking["name"],
                "state": picking["state"],
                "origin": picking.get("origin", ""),
                "source_type": source_type,
                "source_name": source_name,
            },
            "lines_to_assign": lines_to_assign,
            "lines_skipped": lines_skipped,
            "errors": errors,
        }
        result["plan_hash"] = build_plan_hash("receipt", picking_id, lines_to_assign)
        return result

    def _do_receipt_apply(self, picking_id: int, expected_plan_hash: str | None) -> dict:
        """Create lots and assign to move lines."""
        preview = self._do_receipt_preview(picking_id)

        if preview.get("errors"):
            return {
                "picking_id": picking_id,
                "assigned": [],
                "skipped": preview.get("lines_skipped", []),
                "errors": preview["errors"],
            }

        if expected_plan_hash is not None and expected_plan_hash != preview.get("plan_hash"):
            return {
                "picking_id": picking_id,
                "assigned": [],
                "skipped": preview.get("lines_skipped", []),
                "errors": ["Preview is missing or stale. Preview again and send its plan_hash to Apply."],
            }

        try:
            assert_workflow_fields(self.odoo)
        except Exception as error:
            return {
                "picking_id": picking_id,
                "assigned": [],
                "skipped": preview.get("lines_skipped", []),
                "errors": [str(error)],
            }

        if not preview.get("lines_to_assign"):
            return {
                "picking_id": picking_id,
                "assigned": [],
                "skipped": preview.get("lines_skipped", []),
                "errors": ["No lines to assign — all lines already have serials or are skipped"],
            }

        assigned = []
        errors = []

        # Get company_id from picking's destination location
        picking_info = preview["picking"]
        # Fetch company from picking
        picking_records = self.odoo.search_read(
            "stock.picking",
            [["id", "=", picking_id]],
            fields=["company_id"],
            limit=1,
        )
        company_id = (
            picking_records[0]["company_id"][0]
            if picking_records and isinstance(picking_records[0].get("company_id"), (list, tuple))
            else picking_records[0].get("company_id", 1) if picking_records
            else 1
        )

        created: list[tuple[int, int]] = []
        for line in preview["lines_to_assign"]:
            serial_name = line["proposed_serial"]
            product_id = line["product_id"]
            move_line_id = line["move_line_id"]

            # Check duplicate serial
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
                # Abort: don't create partial data
                return {
                    "picking_id": picking_id,
                    "assigned": [],
                    "skipped": preview.get("lines_skipped", []),
                    "errors": errors,
                }

        # All checks passed — create lots and assign
        for line in preview["lines_to_assign"]:
            serial_name = line["proposed_serial"]
            product_id = line["product_id"]
            move_line_id = line["move_line_id"]

            try:
                # Create stock.lot
                lot_id = self.odoo.create("stock.lot", {
                    "name": serial_name,
                    "product_id": product_id,
                    "company_id": company_id,
                    WORKFLOW_CREATED_FIELD: True,
                })
                created.append((move_line_id, lot_id))

                # Assign to move line
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
                    f"[LotSerial] Assigned serial '{serial_name}' to "
                    f"move_line {move_line_id} (lot ID: {lot_id})"
                )

            except Exception as e:
                errors.append(f"Failed to create/assign serial '{serial_name}' for move_line {move_line_id}: {e}")
                errors.extend(self._rollback_created(created))
                logger.exception(f"[LotSerial] Error assigning serial: {e}")
                return {
                    "picking_id": picking_id,
                    "assigned": [],
                    "skipped": preview.get("lines_skipped", []),
                    "errors": errors,
                }

        return {
            "picking_id": picking_id,
            "assigned": assigned,
            "skipped": preview.get("lines_skipped", []),
            "errors": errors,
        }

    # ─── Helpers ─────────────────────────────────────────────────

    def _resolve_receipt_source(self, picking: dict) -> tuple[str | None, str | None]:
        """Resolve the source document (PO or MO) for a receipt.

        Returns:
            (source_type, source_name) — e.g. ("po", "O-MH08966")
            or (None, None) if ambiguous.
        """
        # Priority 1: Direct purchase_id link
        if picking.get("purchase_id"):
            po_id = (
                picking["purchase_id"][0]
                if isinstance(picking["purchase_id"], (list, tuple))
                else picking["purchase_id"]
            )
            po_records = self.odoo.search_read(
                "purchase.order",
                [["id", "=", po_id]],
                fields=["name"],
                limit=1,
            )
            if po_records:
                return ("po", po_records[0]["name"])

        # Priority 2: resolve the actual production link on stock moves.
        moves = self.odoo.search_read(
            "stock.move",
            [["picking_id", "=", picking["id"]], ["production_id", "!=", False]],
            fields=["production_id"],
        )
        production_ids = {self._to_id(move.get("production_id")) for move in moves}
        production_ids.discard(None)
        if len(production_ids) == 1:
            production_id = next(iter(production_ids))
            records = self.odoo.search_read(
                "mrp.production", [["id", "=", production_id]], fields=["name"], limit=1
            )
            if records:
                return ("mo", records[0]["name"])
        if len(production_ids) > 1:
            return (None, None)

        # Priority 3: exact origin reference, rejecting ambiguous candidates.
        origin = picking.get("origin", "") or ""
        if origin:
            candidates = []
            for part in origin.split(","):
                part = part.strip()
                if part:
                    for model, source_type in (("mrp.production", "mo"), ("purchase.order", "po")):
                        records = self.odoo.search_read(
                            model, [["name", "=", part]], fields=["name"], limit=2
                        )
                        candidates.extend((source_type, row["name"]) for row in records)
            if len(candidates) == 1:
                return candidates[0]

        return (None, None)

    def _next_sequence(self, product_id: int, company_id: int, prefix: str) -> int:
        rows = self.odoo.search_read(
            "stock.lot",
            [["product_id", "=", product_id], ["company_id", "=", company_id], ["name", "=ilike", f"{prefix}%"]],
            fields=["name"],
        )
        values = []
        for row in rows:
            name = str(row.get("name", ""))
            suffix = name[len(prefix):] if name.startswith(prefix) else ""
            if suffix.isdigit():
                values.append(int(suffix))
        return max(values, default=0) + 1

    def _rollback_created(self, created: list[tuple[int, int]]) -> list[str]:
        errors = []
        for move_line_id, lot_id in reversed(created):
            try:
                self.odoo.write("stock.move.line", [move_line_id], {"lot_id": False, "lot_name": False})
                self.odoo.unlink("stock.lot", [lot_id])
            except Exception as error:
                errors.append(f"Rollback failed for lot {lot_id}: {error}")
        return errors

    @staticmethod
    def _to_id(value):
        return value[0] if isinstance(value, (list, tuple)) else value

    @staticmethod
    def _error_result(picking_id: int, error: str) -> dict:
        return {
            "picking": {"id": picking_id},
            "lines_to_assign": [],
            "lines_skipped": [],
            "errors": [error],
        }
