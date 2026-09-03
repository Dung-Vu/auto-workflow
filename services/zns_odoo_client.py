"""
ZNS Odoo JSON-RPC Client — High-reliability, production-compatible JSON-RPC 2.0 client for Odoo 19 Online.
Replaces XML-RPC for all ZNS poller and outbox operations.

Features:
- Pure JSON-RPC 2.0 over HTTP (requests) to `/jsonrpc` endpoint.
- Automatic authentication caching with fallback to direct `Config.ODOO_UID`.
- Strict credential redaction (secrets are never logged or leaked in exceptions).
- Exponential backoff retry on transient transport errors (429, 502, 503, timeouts).
- Read-only production preflight (`test_connection()`).
"""

import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import requests
from config import Config

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRY_BACKOFF_FACTOR = 2.0

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_SUBSTRINGS = [
    "429",
    "too many requests",
    "connection refused",
    "timed out",
    "timeout",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "connection reset",
    "remote disconnected",
]


def _sanitize_error_message(msg: str, secrets: Optional[List[str]] = None) -> str:
    """Remove credentials and secrets from error messages."""
    if not msg:
        return ""
    sanitized = str(msg)
    # Redact known config secrets
    for secret in (secrets or []):
        if secret and len(secret) >= 4:
            sanitized = sanitized.replace(secret, "[REDACTED]")

    if Config.ODOO_API_KEY and len(Config.ODOO_API_KEY) >= 4:
        sanitized = sanitized.replace(Config.ODOO_API_KEY, "[REDACTED]")

    # Redact pattern matches (api_key=..., password=...)
    sanitized = re.sub(r"(api_key|password|secret|token)=([^\s&,]+)", r"\1=[REDACTED]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"(api_key|password|secret|token)\s*[:=]\s*['\"][^'\"]+['\"]", r"\1: '[REDACTED]'", sanitized, flags=re.IGNORECASE)
    return sanitized


def _redact_recursive(obj: Any, secrets: Optional[List[str]] = None) -> Any:
    """Recursively redact sensitive keys and values in nested dictionaries, lists, tuples, and strings."""
    if isinstance(obj, str):
        return _sanitize_error_message(obj, secrets)
    elif isinstance(obj, dict):
        redacted = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("password", "api_key", "secret", "token", "access_token", "refresh_token", "auth")):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = _redact_recursive(v, secrets)
        return redacted
    elif isinstance(obj, list):
        return [_redact_recursive(item, secrets) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(_redact_recursive(item, secrets) for item in obj)
    return obj


class OdooJSONRPCError(Exception):
    """Custom exception raised when Odoo JSON-RPC endpoint returns an application error."""

    def __init__(self, message: str, code: Optional[int] = None, data: Optional[Dict[str, Any]] = None):
        sanitized_msg = _sanitize_error_message(message)
        super().__init__(sanitized_msg)
        self.message = sanitized_msg
        self.code = code
        self.data = _redact_recursive(data or {})


class ZNSOdooJSONRPCClient:
    """Production-grade JSON-RPC 2.0 client for Odoo operations."""

    def __init__(
        self,
        url: Optional[str] = None,
        db: Optional[str] = None,
        user: Optional[str] = None,
        api_key: Optional[str] = None,
        uid: Optional[int] = None,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ):
        self.url = (url if url is not None else (Config.ODOO_URL or "")).rstrip("/")
        self.db = db if db is not None else (Config.ODOO_DB or "")
        self.user = user if user is not None else (Config.ODOO_USER or "")
        self.api_key = api_key if api_key is not None else (Config.ODOO_API_KEY or "")
        self._uid = uid if uid is not None else (Config.ODOO_UID or None)
        self.timeout = timeout
        self.session = session or requests.Session()
        self._req_id = 0

    @property
    def is_configured(self) -> bool:
        """Check if essential Odoo credentials are configured."""
        return bool(self.url and self.db and self.api_key)

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _call_jsonrpc(
        self,
        service: str,
        method: str,
        args: List[Any],
        kwargs: Optional[Dict[str, Any]] = None,
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> Any:
        """
        Execute raw JSON-RPC 2.0 call with retry and error redaction.
        Endpoint: `{url}/jsonrpc`

        `heartbeat` is invoked before every attempt (including retries). If it
        returns False the call fails closed instead of continuing after the
        caller has lost its durable lease.
        """
        if not self.is_configured:
            raise OdooJSONRPCError("Odoo credentials unconfigured (missing URL, DB, or API Key)")

        endpoint = f"{self.url}/jsonrpc"
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": service,
                "method": method,
                "args": args,
            },
            "id": self._next_id(),
        }
        if kwargs:
            payload["params"]["kwargs"] = kwargs

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Bonario-ZNS-Tracking/2.0 (JSON-RPC)",
        }

        last_exc: Optional[Exception] = None

        for attempt in range(MAX_RETRIES + 1):
            if heartbeat is not None:
                try:
                    hb_ok = heartbeat()
                except Exception as e:
                    raise OdooJSONRPCError(f"JSON-RPC heartbeat failed: {_sanitize_error_message(str(e))}") from e
                if not hb_ok:
                    raise OdooJSONRPCError("lost durable lease during JSON-RPC call")
            try:
                resp = self.session.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )

                if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
                    logger.warning(
                        f"[ZNS-ODOO-JSONRPC] Transient HTTP {resp.status_code} on {service}.{method} "
                        f"(attempt {attempt + 1}/{MAX_RETRIES + 1}) — retrying in {delay}s"
                    )
                    time.sleep(delay)
                    continue

                resp.raise_for_status()

                try:
                    data = resp.json()
                except Exception as e:
                    raise OdooJSONRPCError(f"Invalid JSON-RPC response from Odoo: {e}")

                if "error" in data:
                    err_info = data["error"]
                    err_msg = err_info.get("message") or "Unknown Odoo error"
                    err_code = err_info.get("code")
                    err_data = err_info.get("data") or {}
                    detailed_msg = err_data.get("message") or err_msg

                    # Check if error message is transient (e.g. concurrent update lock)
                    sanitized = _sanitize_error_message(detailed_msg)
                    if attempt < MAX_RETRIES and any(sub in sanitized.lower() for sub in ["concurrency", "lock", "busy"]):
                        delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
                        logger.warning(f"[ZNS-ODOO-JSONRPC] Transient Odoo lock: {sanitized} — retrying in {delay}s")
                        time.sleep(delay)
                        continue

                    raise OdooJSONRPCError(
                        message=f"Odoo JSON-RPC Error: {sanitized}",
                        code=err_code,
                        data=err_data,
                    )

                return data.get("result")

            except (requests.exceptions.RequestException, requests.exceptions.Timeout) as e:
                sanitized = _sanitize_error_message(str(e))
                last_exc = e
                is_retryable = any(sub in sanitized.lower() for sub in _RETRYABLE_SUBSTRINGS)
                if attempt < MAX_RETRIES and is_retryable:
                    delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
                    logger.warning(
                        f"[ZNS-ODOO-JSONRPC] Connection error on {service}.{method} "
                        f"(attempt {attempt + 1}/{MAX_RETRIES + 1}): {sanitized} — retrying in {delay}s"
                    )
                    time.sleep(delay)
                else:
                    raise OdooJSONRPCError(f"Odoo transport failure: {sanitized}") from e

        if last_exc:
            raise OdooJSONRPCError(f"Odoo JSON-RPC call failed after {MAX_RETRIES + 1} attempts: {_sanitize_error_message(str(last_exc))}")

    def authenticate(self) -> int:
        """Authenticate with Odoo and cache UID."""
        if self._uid is not None:
            return self._uid

        if Config.ODOO_UID:
            self._uid = Config.ODOO_UID
            logger.info(f"[ZNS-ODOO-JSONRPC] Using direct ODOO_UID from configuration: {self._uid}")
            return self._uid

        if not self.user:
            logger.warning("[ZNS-ODOO-JSONRPC] ODOO_USER not set, attempting authentication with empty username")

        res = self._call_jsonrpc(
            service="common",
            method="authenticate",
            args=[self.db, self.user, self.api_key, {}],
        )

        if not res or not isinstance(res, int):
            raise OdooJSONRPCError("Authentication failed: Invalid credentials or database name")

        self._uid = res
        logger.info(f"[ZNS-ODOO-JSONRPC] Authenticated successfully with Odoo (UID: {self._uid})")
        return self._uid

    @property
    def uid(self) -> int:
        """Get authenticated UID."""
        if self._uid is None:
            return self.authenticate()
        return self._uid

    def execute_kw(
        self,
        model: str,
        method: str,
        args: Optional[List[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> Any:
        """
        Execute an Odoo model method via JSON-RPC `object.execute_kw`.
        """
        args_list = args if args is not None else []
        kwargs_dict = kwargs if kwargs is not None else {}
        uid = self.uid

        return self._call_jsonrpc(
            service="object",
            method="execute_kw",
            args=[
                self.db,
                uid,
                self.api_key,
                model,
                method,
                args_list,
                kwargs_dict,
            ],
            heartbeat=heartbeat,
        )

    def search(
        self,
        model: str,
        domain: List[Any],
        limit: int = 0,
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> List[int]:
        """Search record IDs matching domain (CAS-equivalent helper)."""
        kwargs: Dict[str, Any] = {}
        if limit:
            kwargs["limit"] = limit
        res = self.execute_kw(model, "search", [domain], kwargs, heartbeat=heartbeat)
        if isinstance(res, list):
            return res
        return []

    def search_read(
        self,
        model: str,
        domain: List[Any],
        fields: Optional[List[str]] = None,
        limit: int = 0,
        order: Optional[str] = None,
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> List[Dict[str, Any]]:
        """Search and read records from Odoo."""
        kwargs: Dict[str, Any] = {}
        if fields:
            kwargs["fields"] = fields
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order

        res = self.execute_kw(model, "search_read", [domain], kwargs, heartbeat=heartbeat)
        return res if isinstance(res, list) else []

    def read(
        self,
        model: str,
        ids: List[int],
        fields: Optional[List[str]] = None,
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> List[Dict[str, Any]]:
        """Read specific fields for a list of record IDs."""
        kwargs: Dict[str, Any] = {}
        if fields:
            kwargs["fields"] = fields

        res = self.execute_kw(model, "read", [ids], kwargs, heartbeat=heartbeat)
        return res if isinstance(res, list) else []

    def write(
        self,
        model: str,
        ids: List[int],
        values: Dict[str, Any],
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """
        Write field values to specific record IDs.
        Returns True if successful, False if write failed.
        """
        res = self.execute_kw(model, "write", [ids, values], heartbeat=heartbeat)
        return bool(res)

    def cas_write(
        self,
        model: str,
        rec_id: int,
        values: Dict[str, Any],
        extra_domain: Optional[List[Any]] = None,
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """
        Version-aware CAS-equivalent write: search with domain
        `[('id', '=', rec_id), ...extra_domain]` then write only those IDs.

        Returns False when no matching row exists (lost compare), True when
        write was issued against the matched recordset.
        """
        domain: List[Any] = [("id", "=", rec_id)]
        if extra_domain:
            domain.extend(extra_domain)
        ids = self.search(model, domain, limit=1, heartbeat=heartbeat)
        if not ids:
            return False
        return self.write(model, ids, values, heartbeat=heartbeat)

    def execute(self, model: str, method: str, ids: List[int], *args, **kwargs) -> Any:
        """Execute a custom model method on record IDs."""
        heartbeat = kwargs.pop("heartbeat", None)
        call_args = [ids] + list(args)
        return self.execute_kw(model, method, call_args, kwargs, heartbeat=heartbeat)

    def message_post(
        self,
        model: str,
        record_id: int,
        body: str,
        message_type: str = "comment",
        subtype_xmlid: str = "mail.mt_note",
        heartbeat: Optional[Callable[[], bool]] = None,
    ) -> Any:
        """Post a chatter note to a record."""
        return self.execute_kw(
            model,
            "message_post",
            [[record_id]],
            {
                "body": body,
                "message_type": message_type,
                "subtype_xmlid": subtype_xmlid,
            },
            heartbeat=heartbeat,
        )

    def test_connection(self) -> Dict[str, Any]:
        """
        Read-only production preflight to verify JSON-RPC connectivity and authentication
        without modifying any business data.
        """
        if not self.is_configured:
            return {
                "success": False,
                "error": "Odoo credentials not configured",
                "configured": False,
            }

        try:
            # 1. Check version endpoint
            version_info = self._call_jsonrpc("common", "version", [])

            # 2. Check authentication
            uid = self.authenticate()

            # 3. Read-only search on res.company (read company name)
            company_data = self.search_read("res.company", [], fields=["id", "name"], limit=1)

            return {
                "success": True,
                "configured": True,
                "uid": uid,
                "server_version": (version_info or {}).get("server_version", "unknown"),
                "company_name": company_data[0]["name"] if company_data else "Unknown",
            }
        except Exception as e:
            sanitized = _sanitize_error_message(str(e))
            return {
                "success": False,
                "configured": True,
                "error": sanitized,
            }


# Global singleton instance for easy reuse
_zns_odoo_client: Optional[ZNSOdooJSONRPCClient] = None


def get_zns_odoo_client() -> ZNSOdooJSONRPCClient:
    """Get or create singleton ZNS Odoo JSON-RPC client."""
    global _zns_odoo_client
    if _zns_odoo_client is None:
        _zns_odoo_client = ZNSOdooJSONRPCClient()
    return _zns_odoo_client


def reset_zns_odoo_client():
    """Reset singleton instance (useful for testing)."""
    global _zns_odoo_client
    _zns_odoo_client = None
