"""
Lot/Serial Module Configuration — TEST SERVER ONLY.

Provides a dedicated OdooClient instance that connects to the
testing server (ODOO_TEST_*), completely isolated from
the production OdooClient singleton in services/odoo_client.py.

Config is simple:
  - ODOO_TEST_URL/DB/UID/API_KEY → test server credentials
  - LOT_SERIAL_WHITELIST_WAREHOUSES → allowed destination warehouses for rename
  - Audit field (x_original_lot_name) and chatter fallback are hardcoded
"""

import logging

from config import Config as AppConfig

logger = logging.getLogger(__name__)


class LotSerialConfig:
    """Configuration for the Lot/Serial automation module."""

    # Test server credentials (shared with any future test-only modules)
    ODOO_URL = AppConfig.ODOO_TEST_URL
    ODOO_DB = AppConfig.ODOO_TEST_DB
    ODOO_UID = AppConfig.ODOO_TEST_UID
    ODOO_API_KEY = AppConfig.ODOO_TEST_API_KEY

    # Warehouse whitelist for serial rename operations
    WHITELIST_WAREHOUSES = AppConfig.LOT_SERIAL_WHITELIST_WAREHOUSES
    SOURCE_WAREHOUSES = AppConfig.LOT_SERIAL_SOURCE_WAREHOUSES
    ENABLED = AppConfig.LOT_SERIAL_ENABLED
    API_TOKEN = AppConfig.LOT_SERIAL_API_TOKEN
    ALLOW_SCHEMA_SETUP = AppConfig.LOT_SERIAL_ALLOW_SCHEMA_SETUP

    # Audit trail — hardcoded, no need to configure in .env
    AUDIT_FIELD = "x_original_lot_name"
    AUDIT_FALLBACK_CHATTER = True


# ─── Dedicated OdooClient for test server ───────────────────────

from services.odoo_client import OdooClient as _BaseOdooClient


class _TestServerOdooClient(_BaseOdooClient):
    """OdooClient that connects to the test server instead of production.

    Overrides the singleton's credentials with ODOO_TEST_* values,
    ensuring zero risk of writing to the production database.
    """

    def __init__(self):
        # Skip parent __init__ — set our own credentials
        self.url = LotSerialConfig.ODOO_URL
        self.db = LotSerialConfig.ODOO_DB
        self.user = ""  # Not used when UID is set directly
        self.api_key = LotSerialConfig.ODOO_API_KEY
        self._uid = None

    @property
    def uid(self) -> int:
        if self._uid is None:
            if LotSerialConfig.ODOO_UID:
                self._uid = LotSerialConfig.ODOO_UID
                logger.info(
                    f"[LotSerial] Using direct UID {self._uid} for {self.url}"
                )
            else:
                import xmlrpc.client
                common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
                self._uid = common.authenticate(self.db, self.user, self.api_key, {})
                if not self._uid:
                    raise ConnectionError(
                        f"[LotSerial] Authentication failed for {self.url}"
                    )
                logger.info(f"[LotSerial] Authenticated — UID: {self._uid}")
        return self._uid


def get_test_odoo() -> _BaseOdooClient:
    """Get the dedicated test-server OdooClient instance.

    Validates that credentials are configured before returning.
    Raises RuntimeError if misconfigured.
    """
    if not LotSerialConfig.ODOO_URL or not LotSerialConfig.ODOO_API_KEY:
        raise RuntimeError(
            "[LotSerial] Missing test server credentials. "
            "Set ODOO_TEST_URL, ODOO_TEST_DB, "
            "ODOO_TEST_UID, ODOO_TEST_API_KEY in .env"
        )

    # This module must never be pointed to Production accidentally.
    if "testing0808.odoo.com" not in LotSerialConfig.ODOO_URL.lower():
        raise RuntimeError(
            "[LotSerial] Refusing to run: ODOO_TEST_URL must point to "
            "testing0808.odoo.com."
        )

    return _TestServerOdooClient()
