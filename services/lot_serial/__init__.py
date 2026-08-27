"""
Lot/Serial Automation Service — Facade.

Composes all mixins into a single service class following the
Facade + Mixin pattern used throughout the auto-workflow project.

Usage:
    from services.lot_serial import LotSerialService

    svc = LotSerialService()
    result = svc.preview_receipt_serial(picking_id=123)
    result = svc.apply_receipt_serial(picking_id=123)
    result = svc.preview_mo_serial(production_id=456)
    result = svc.apply_mo_serial(production_id=456)
    result = svc.preview_rename_serial(picking_id=789, rename_map={"OLD": "NEW"})
    result = svc.apply_rename_serial(picking_id=789, rename_map={"OLD": "NEW"})
"""

from __future__ import annotations

import logging

from .config import LotSerialConfig, get_test_odoo
from .receipt_serial import ReceiptSerialMixin
from .mo_serial import MOSerialMixin
from .serial_rename import SerialRenameMixin

logger = logging.getLogger(__name__)

__all__ = ["LotSerialService", "LotSerialConfig"]


class LotSerialService(ReceiptSerialMixin, MOSerialMixin, SerialRenameMixin):
    """Unified service for lot/serial automation.

    Connects to the TEST SERVER only (testing0808.odoo.com).
    Never touches the production Odoo instance.
    """

    def __init__(self):
        self.odoo = get_test_odoo()
        logger.info(
            f"[LotSerial] Service initialized — "
            f"connected to {LotSerialConfig.ODOO_URL}"
        )

    def get_status(self) -> dict:
        """Return module status for health check endpoint."""
        return {
            "configured": True,
            "enabled": LotSerialConfig.ENABLED,
            "test_server_only": True,
            "whitelist_warehouses": LotSerialConfig.WHITELIST_WAREHOUSES,
            "source_warehouses": LotSerialConfig.SOURCE_WAREHOUSES,
            "audit_field": LotSerialConfig.AUDIT_FIELD,
        }
