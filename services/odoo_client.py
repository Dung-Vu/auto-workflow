"""
Odoo XML-RPC Client — reusable wrapper for all Odoo operations.
Replaces n8n's built-in Odoo node.

Includes retry logic with exponential backoff for transient errors
(429 Too Many Requests, Connection refused, Read timed out, etc.).
"""

import time
import xmlrpc.client
import logging
from config import Config

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 4
RETRY_BASE_DELAY = 10  # seconds
RETRY_BACKOFF_FACTOR = 2  # exponential backoff multiplier

# Errors that are safe to retry
_RETRYABLE_SUBSTRINGS = [
    "429",
    "Too Many Requests",
    "Connection refused",
    "timed out",
    "Read timed out",
    "IncompleteRead",
    "Bad Gateway",
    "502",
    "503",
    "Service Unavailable",
    "Connection reset",
    "BrokenPipeError",
    "No address associated",
    "Name or service not known",
]


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is transient and safe to retry."""
    msg = str(exc)
    return any(s.lower() in msg.lower() for s in _RETRYABLE_SUBSTRINGS)


class OdooClient:
    """XML-RPC client for Odoo ERP with retry logic."""

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

    def _execute_with_retry(self, model: str, method: str, args: list, kwargs: dict):
        """Execute an Odoo XML-RPC call with retry + exponential backoff."""
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self.models.execute_kw(
                    self.db, self.uid, self.api_key,
                    model, method,
                    args,
                    kwargs,
                )
            except Exception as e:
                last_exc = e
                if attempt < MAX_RETRIES and _is_retryable(e):
                    delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
                    logger.warning(
                        f"[Odoo] {model}.{method} failed (attempt {attempt + 1}/{MAX_RETRIES + 1}): "
                        f"{e} — retrying in {delay}s"
                    )
                    time.sleep(delay)
                else:
                    raise
        raise last_exc

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

        return self._execute_with_retry(model, "search_read", [domain], kwargs)

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

        record_id = self._execute_with_retry(model, "create", args, kwargs)
        logger.info(f"Created {model} record ID: {record_id}")
        return record_id

    def write(self, model: str, record_ids: list, values: dict) -> bool:
        """Update records in Odoo."""
        return self._execute_with_retry(model, "write", [record_ids, values], {})

    def search(self, model: str, domain: list, limit: int = 0) -> list:
        """Search for record IDs."""
        kwargs = {}
        if limit:
            kwargs["limit"] = limit

        return self._execute_with_retry(model, "search", [domain], kwargs)


# Singleton instance
odoo = OdooClient()
