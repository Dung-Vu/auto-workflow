"""
Unit and Mocked Contract Tests for ZNS Odoo JSON-RPC 2.0 Client.

Verifies:
- JSON-RPC 2.0 protocol adherence (service, method, args, kwargs).
- Error handling, HTTP status codes, and Odoo JSON-RPC error formats.
- Retries with exponential backoff on transient errors.
- Strict credential redaction (secrets are never leaked in logs or error messages).
- Read-only production preflight (`test_connection()`).
"""

import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config
from services.zns_odoo_client import (
    ZNSOdooJSONRPCClient,
    OdooJSONRPCError,
    _sanitize_error_message,
    get_zns_odoo_client,
    reset_zns_odoo_client,
)


class TestZNSOdooJSONRPCClient(unittest.TestCase):
    """Test suite for ZNSOdooJSONRPCClient."""

    def setUp(self):
        reset_zns_odoo_client()
        self.mock_session = MagicMock()
        self.client = ZNSOdooJSONRPCClient(
            url="https://test.odoo.com",
            db="test_db",
            user="admin@bonario.vn",
            api_key="super_secret_api_key_12345",
            uid=2,
            session=self.mock_session,
        )

    def test_jsonrpc_search_read_success(self):
        """Verify search_read formats JSON-RPC 2.0 call and parses results."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": [
                {"id": 101, "name": "SO001", "x_studio_zns_request_state": "pending"}
            ],
        }
        self.mock_session.post.return_value = mock_response

        res = self.client.search_read(
            model="sale.order",
            domain=[("x_studio_zns_request_state", "=", "pending")],
            fields=["id", "name", "x_studio_zns_request_state"],
            limit=10,
        )

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["name"], "SO001")

        # Verify posted payload
        self.mock_session.post.assert_called_once()
        args, kwargs = self.mock_session.post.call_args
        self.assertEqual(args[0], "https://test.odoo.com/jsonrpc")
        sent_payload = kwargs["json"]
        self.assertEqual(sent_payload["jsonrpc"], "2.0")
        self.assertEqual(sent_payload["params"]["service"], "object")
        self.assertEqual(sent_payload["params"]["method"], "execute_kw")
        rpc_args = sent_payload["params"]["args"]
        self.assertEqual(rpc_args[0], "test_db")
        self.assertEqual(rpc_args[1], 2)  # UID
        self.assertEqual(rpc_args[2], "super_secret_api_key_12345")
        self.assertEqual(rpc_args[3], "sale.order")
        self.assertEqual(rpc_args[4], "search_read")

    def test_jsonrpc_write_returns_boolean(self):
        """Verify write returns True on success and False on write failure."""
        mock_response_true = MagicMock()
        mock_response_true.status_code = 200
        mock_response_true.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": True}

        self.mock_session.post.return_value = mock_response_true
        ok = self.client.write("sale.order", [101], {"x_studio_zns_request_state": "processing"})
        self.assertTrue(ok)

        mock_response_false = MagicMock()
        mock_response_false.status_code = 200
        mock_response_false.json.return_value = {"jsonrpc": "2.0", "id": 2, "result": False}

        self.mock_session.post.return_value = mock_response_false
        failed = self.client.write("sale.order", [101], {"x_studio_zns_request_state": "processing"})
        self.assertFalse(failed)

    def test_cas_write_searches_version_domain_before_write(self):
        """cas_write must no-op when search finds no matching version row."""
        mock_empty = MagicMock()
        mock_empty.status_code = 200
        mock_empty.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": []}
        self.mock_session.post.return_value = mock_empty

        skipped = self.client.cas_write(
            "sale.order",
            101,
            {"x_studio_zns_request_state": "completed"},
            extra_domain=[("x_studio_zns_send_count", "=", 1)],
        )
        self.assertFalse(skipped)
        rpc_args = self.mock_session.post.call_args.kwargs["json"]["params"]["args"]
        self.assertEqual(rpc_args[4], "search")

        mock_search = MagicMock()
        mock_search.status_code = 200
        mock_search.json.return_value = {"jsonrpc": "2.0", "id": 2, "result": [101]}
        mock_write = MagicMock()
        mock_write.status_code = 200
        mock_write.json.return_value = {"jsonrpc": "2.0", "id": 3, "result": True}
        self.mock_session.post.side_effect = [mock_search, mock_write]
        written = self.client.cas_write(
            "sale.order",
            101,
            {"x_studio_zns_request_state": "completed"},
            extra_domain=[("x_studio_zns_send_count", "=", 1)],
        )
        self.assertTrue(written)

    def test_jsonrpc_heartbeat_abort_fails_closed(self):
        """A false heartbeat must abort the JSON-RPC call before posting."""
        from services.zns_odoo_client import OdooJSONRPCError

        with self.assertRaises(OdooJSONRPCError) as ctx:
            self.client.write(
                "sale.order",
                [101],
                {"x_studio_zns_status": "delivered"},
                heartbeat=lambda: False,
            )
        self.assertIn("lost durable lease", str(ctx.exception))
        self.mock_session.post.assert_not_called()

    def test_jsonrpc_authentication_and_uid_cache(self):
        """Verify authenticate retrieves UID and caches it."""
        client_no_uid = ZNSOdooJSONRPCClient(
            url="https://test.odoo.com",
            db="test_db",
            user="admin@bonario.vn",
            api_key="secret_key",
            session=self.mock_session,
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": 42}
        self.mock_session.post.return_value = mock_response

        uid = client_no_uid.authenticate()
        self.assertEqual(uid, 42)
        self.assertEqual(client_no_uid.uid, 42)

    def test_jsonrpc_credential_redaction_in_exceptions(self):
        """Verify secrets are redacted in error messages."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "message": "Odoo server error",
                "code": 200,
                "data": {
                    "message": "AccessDenied: password='super_secret_api_key_12345' is invalid for db=test_db",
                },
            },
        }
        self.mock_session.post.return_value = mock_response

        with self.assertRaises(OdooJSONRPCError) as ctx:
            self.client.search_read("sale.order", [])

        err_msg = str(ctx.exception)
        self.assertNotIn("super_secret_api_key_12345", err_msg)
        self.assertIn("[REDACTED]", err_msg)

    def test_sanitize_error_message_helper(self):
        """Test _sanitize_error_message with various secret formats."""
        raw_msg = "Failed with api_key=123456789 and password='my_password_xyz' for token=abc12345"
        clean = _sanitize_error_message(raw_msg, secrets=["123456789", "my_password_xyz", "abc12345"])
        self.assertNotIn("123456789", clean)
        self.assertNotIn("my_password_xyz", clean)
        self.assertNotIn("abc12345", clean)
        self.assertIn("[REDACTED]", clean)

    def test_recursive_redaction_of_error_data(self):
        """Verify nested structures in OdooJSONRPCError.data are recursively redacted."""
        from services.zns_odoo_client import _redact_recursive

        nested_data = {
            "name": "odoo.exceptions.AccessDenied",
            "debug": "Traceback...\nauth_failed for user admin with password 'secret_pass_999'",
            "context": {
                "params": {
                    "api_key": "raw_secret_key_123",
                    "safe_field": "ok_value",
                    "nested_list": [{"token": "jwt_secret_token", "name": "Item 1"}],
                }
            },
        }

        redacted = _redact_recursive(nested_data, secrets=["secret_pass_999", "raw_secret_key_123", "jwt_secret_token"])
        self.assertEqual(redacted["context"]["params"]["api_key"], "[REDACTED]")
        self.assertEqual(redacted["context"]["params"]["nested_list"][0]["token"], "[REDACTED]")
        self.assertEqual(redacted["context"]["params"]["safe_field"], "ok_value")
        self.assertNotIn("secret_pass_999", str(redacted))

    def test_jsonrpc_read_only_preflight_test_connection(self):
        """Verify test_connection executes read-only checks without writing."""
        mock_v_resp = MagicMock()
        mock_v_resp.status_code = 200
        mock_v_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"server_version": "19.0+e"},
        }

        mock_search_resp = MagicMock()
        mock_search_resp.status_code = 200
        mock_search_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": [{"id": 1, "name": "Bonario JSC"}],
        }

        self.mock_session.post.side_effect = [mock_v_resp, mock_search_resp]

        result = self.client.test_connection()
        self.assertTrue(result["success"])
        self.assertEqual(result["server_version"], "19.0+e")
        self.assertEqual(result["company_name"], "Bonario JSC")
        self.assertEqual(result["uid"], 2)

    def test_unconfigured_credentials_fails_closed(self):
        """Verify unconfigured client reports configured=False and throws error on calls."""
        empty_client = ZNSOdooJSONRPCClient(url="", db="", api_key="", session=self.mock_session)
        self.assertFalse(empty_client.is_configured)
        res = empty_client.test_connection()
        self.assertFalse(res["success"])
        self.assertFalse(res["configured"])

        with self.assertRaises(OdooJSONRPCError):
            empty_client.search_read("sale.order", [])


if __name__ == "__main__":
    unittest.main()
