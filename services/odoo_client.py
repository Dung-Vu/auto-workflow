"""
Odoo XML-RPC Client — reusable wrapper for all Odoo operations.
Replaces n8n's built-in Odoo node.
"""

import xmlrpc.client
import logging
from config import Config

logger = logging.getLogger(__name__)


class OdooClient:
    """XML-RPC client for Odoo ERP."""

    def __init__(self):
        self.url = Config.ODOO_URL
        self.db = Config.ODOO_DB
        self.user = Config.ODOO_USER
        self.api_key = Config.ODOO_API_KEY
        self._uid = None

    @property
    def uid(self) -> int:
        """Authenticate and cache UID. Uses ODOO_UID from .env if set."""
        if self._uid is None:
            # If ODOO_UID is set directly in .env, use it (skip authenticate)
            if Config.ODOO_UID:
                self._uid = Config.ODOO_UID
                logger.info(f"Odoo using direct UID from .env: {self._uid}")
            else:
                common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
                self._uid = common.authenticate(self.db, self.user, self.api_key, {})
                if not self._uid:
                    raise ConnectionError("Odoo authentication failed")
                logger.info(f"Odoo authenticated — UID: {self._uid}")
        return self._uid

    @property
    def models(self):
        """Get XML-RPC models proxy."""
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def search_read(
        self,
        model: str,
        domain: list,
        fields: list = None,
        limit: int = 0,
        order: str = None,
    ) -> list:
        """Search and read records from Odoo."""
        kwargs = {}
        if fields:
            kwargs["fields"] = fields
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order

        result = self.models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "search_read",
            [domain],
            kwargs,
        )
        return result

    def create(self, model: str, values: dict, context: dict = None) -> int:
        """Create a single record in Odoo. Returns the new record ID.

        Args:
            context: Optional Odoo context dict. Use {'allowed_company_ids': [id]}
                     to force company in multi-company setups.
        """
        args = [values]
        kwargs = {}
        if context:
            kwargs["context"] = context

        record_id = self.models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "create",
            args,
            kwargs,
        )
        logger.info(f"Created {model} record ID: {record_id}")
        return record_id

    def write(self, model: str, record_ids: list, values: dict) -> bool:
        """Update records in Odoo."""
        result = self.models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "write",
            [record_ids, values],
        )
        return result

    def search(self, model: str, domain: list, limit: int = 0) -> list:
        """Search for record IDs."""
        kwargs = {}
        if limit:
            kwargs["limit"] = limit

        return self.models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "search",
            [domain],
            kwargs,
        )


# Singleton instance
odoo = OdooClient()
