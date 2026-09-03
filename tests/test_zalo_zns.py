"""
Unit tests for Zalo ZNS Service.

Covers:
  - R1: Concurrency Control (threading.Lock per app, multi-threaded refresh race conditions)
  - R2: Smart Token Caching (30-minute buffer, expiry calculation, token reuse, force refresh)
  - R3: Auto-Refresh Daemon (retry on failure, exponential backoff, graceful stop)
  - R4: Full test coverage for send_zns, authorization callbacks, get_token_status, and Flask webhook routes
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import services.zalo_zns as zalo_svc
from config import Config


class BaseZaloTestCase(unittest.TestCase):
    """Base test case setting up isolated temp token directory."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.temp_dir.name
        self.ord_token_file = os.path.join(self.data_dir, "zalo_tokens.json")
        self.bon_token_file = os.path.join(self.data_dir, "zalo_tokens_bon.json")

        # Patch DATA_DIR, TOKEN_FILES, and APP_CREDENTIALS in module
        self.orig_data_dir = zalo_svc.DATA_DIR
        self.orig_token_files = zalo_svc.TOKEN_FILES
        self.orig_app_creds = zalo_svc.APP_CREDENTIALS

        zalo_svc.DATA_DIR = self.data_dir
        zalo_svc.TOKEN_FILES = {
            "ord": self.ord_token_file,
            "bon": self.bon_token_file,
        }
        zalo_svc.APP_CREDENTIALS = {
            "ord": {"app_id": "ord_app_test_id", "secret_key": "ord_secret_test"},
            "bon": {"app_id": "bon_app_test_id", "secret_key": "bon_secret_test"},
        }

        # Reset locks and stop event
        zalo_svc._APP_LOCKS = {
            "ord": threading.Lock(),
            "bon": threading.Lock(),
        }
        zalo_svc._stop_event.clear()
        zalo_svc._auto_refresh_running = False

    def tearDown(self):
        zalo_svc.stop_auto_refresh()
        zalo_svc.DATA_DIR = self.orig_data_dir
        zalo_svc.TOKEN_FILES = self.orig_token_files
        zalo_svc.APP_CREDENTIALS = self.orig_app_creds
        self.temp_dir.cleanup()

    def _write_token_file(self, app: str, tokens: dict):
        tf = zalo_svc._token_file(app)
        with open(tf, "w", encoding="utf-8") as f:
            json.dump(tokens, f)


# ═══════════════════════════════════════════
#  R2: TOKEN CACHING TESTS
# ═══════════════════════════════════════════

class TestTokenCaching(BaseZaloTestCase):
    """Tests for R2: Smart Token Caching & Reuse."""

    def test_token_validity_fresh_token(self):
        """Token with >30m remaining is valid."""
        tokens = {
            "access_token": "valid_token_123",
            "expires_at": time.time() + 3600,  # 1 hour remaining
        }
        self.assertTrue(zalo_svc._is_token_valid(tokens, buffer_seconds=1800))

    def test_token_validity_within_buffer(self):
        """Token within 30m buffer is treated as invalid (needs refresh)."""
        tokens = {
            "access_token": "near_expiry_token",
            "expires_at": time.time() + 1000,  # Only ~16.6m remaining (< 1800s buffer)
        }
        self.assertFalse(zalo_svc._is_token_valid(tokens, buffer_seconds=1800))

    def test_token_validity_expired(self):
        """Token past expiry is invalid."""
        tokens = {
            "access_token": "expired_token",
            "expires_at": time.time() - 60,
        }
        self.assertFalse(zalo_svc._is_token_valid(tokens, buffer_seconds=1800))

    def test_token_validity_missing_fields(self):
        """Token without access_token or expires_at is invalid."""
        self.assertFalse(zalo_svc._is_token_valid({}))
        self.assertFalse(zalo_svc._is_token_valid({"access_token": "abc"}))
        self.assertFalse(zalo_svc._is_token_valid({"expires_at": time.time() + 5000}))
        self.assertFalse(zalo_svc._is_token_valid({"access_token": "abc", "expires_at": "invalid_ts"}))

    def test_token_validity_whitespace_and_invalid_types(self):
        """Whitespace-only, non-string, or invalid tokens are invalid."""
        self.assertFalse(zalo_svc._is_token_valid({"access_token": "   ", "expires_at": time.time() + 5000}))
        self.assertFalse(zalo_svc._is_token_valid({"access_token": 12345, "expires_at": time.time() + 5000}))
        self.assertFalse(zalo_svc._is_token_valid({"access_token": None, "expires_at": time.time() + 5000}))
        # Negative buffer handled gracefully (clamped to 0)
        self.assertTrue(zalo_svc._is_token_valid({"access_token": "tok", "expires_at": time.time() + 100}, buffer_seconds=-50))

    @patch("services.zalo_zns.requests.post")
    def test_get_access_token_reuses_valid_token(self, mock_post):
        """When valid token exists, get_access_token does NOT call Zalo API."""
        now = time.time()
        self._write_token_file("ord", {
            "access_token": "cached_access_token_abc",
            "refresh_token": "valid_refresh_token",
            "expires_at": now + 7200,  # 2 hours left
            "expires_in": 90000,
        })

        token = zalo_svc.get_access_token("ord")
        self.assertEqual(token, "cached_access_token_abc")
        mock_post.assert_not_called()

    @patch("services.zalo_zns.requests.post")
    def test_get_access_token_refreshes_when_near_expiry(self, mock_post):
        """When token has <30m left, get_access_token calls refresh API."""
        now = time.time()
        self._write_token_file("ord", {
            "access_token": "old_token",
            "refresh_token": "existing_refresh_token",
            "expires_at": now + 600,  # 10 min left (< 30 min buffer)
        })

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "new_refreshed_token_xyz",
            "refresh_token": "new_refresh_token_xyz",
            "expires_in": 90000,
        }
        mock_post.return_value = mock_resp

        token = zalo_svc.get_access_token("ord")
        self.assertEqual(token, "new_refreshed_token_xyz")
        mock_post.assert_called_once()

        # Check token was saved to file with updated expires_at
        saved = zalo_svc._load_tokens("ord")
        self.assertEqual(saved["access_token"], "new_refreshed_token_xyz")
        self.assertEqual(saved["refresh_token"], "new_refresh_token_xyz")
        self.assertGreater(saved["expires_at"], now + 80000)

    @patch("services.zalo_zns.requests.post")
    def test_get_access_token_force_refresh(self, mock_post):
        """force_refresh=True forces API call even if token is still valid."""
        now = time.time()
        self._write_token_file("ord", {
            "access_token": "valid_but_force_refresh",
            "refresh_token": "existing_refresh_token",
            "expires_at": now + 7200,
        })

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "forced_token_123",
            "refresh_token": "new_refresh_token",
            "expires_in": 90000,
        }
        mock_post.return_value = mock_resp

        token = zalo_svc.get_access_token("ord", force_refresh=True)
        self.assertEqual(token, "forced_token_123")
        mock_post.assert_called_once()


# ═══════════════════════════════════════════
#  R1: CONCURRENCY CONTROL TESTS
# ═══════════════════════════════════════════

class TestConcurrencyControl(BaseZaloTestCase):
    """Tests for R1: Concurrency Control & Thread Safety."""

    @patch("services.zalo_zns.requests.post")
    def test_concurrent_refresh_calls_single_api(self, mock_post):
        """
        When 20 threads call get_access_token concurrently on expired token,
        only 1 refresh API call is made; all threads receive the valid token.
        """
        self._write_token_file("ord", {
            "access_token": "expired_token",
            "refresh_token": "valid_refresh_token",
            "expires_at": time.time() - 100,  # Expired
        })

        api_call_count = 0
        call_lock = threading.Lock()

        def slow_refresh_post(*args, **kwargs):
            nonlocal api_call_count
            with call_lock:
                api_call_count += 1
            time.sleep(0.05)  # Simulate network latency
            resp = MagicMock()
            resp.json.return_value = {
                "access_token": "concurrent_fresh_token",
                "refresh_token": "concurrent_new_refresh",
                "expires_in": 90000,
            }
            return resp

        mock_post.side_effect = slow_refresh_post

        results = []
        threads = []

        def worker():
            t = zalo_svc.get_access_token("ord")
            results.append(t)

        for _ in range(20):
            th = threading.Thread(target=worker)
            threads.append(th)
            th.start()

        for th in threads:
            th.join()

        # All 20 workers should have received the fresh token
        self.assertEqual(len(results), 20)
        self.assertTrue(all(t == "concurrent_fresh_token" for t in results))

        # Exactly 1 API call should have been made due to locking and double-check
        self.assertEqual(api_call_count, 1)

    @patch("services.zalo_zns.requests.post")
    def test_dual_app_concurrent_independence(self, mock_post):
        """
        ORD and BON app locks are independent; threads accessing ORD and BON
        simultaneously execute without blocking each other or corrupting tokens.
        """
        now = time.time()
        self._write_token_file("ord", {
            "access_token": "ord_old",
            "refresh_token": "ord_refresh",
            "expires_at": now - 10,
        })
        self._write_token_file("bon", {
            "access_token": "bon_old",
            "refresh_token": "bon_refresh",
            "expires_at": now - 10,
        })

        def fake_post(url, *args, **kwargs):
            data = kwargs.get("data", {})
            app_id = data.get("app_id")
            time.sleep(0.02)
            resp = MagicMock()
            if app_id == "ord_app_test_id":
                resp.json.return_value = {
                    "access_token": "ord_new_token",
                    "refresh_token": "ord_new_rf",
                    "expires_in": 90000,
                }
            else:
                resp.json.return_value = {
                    "access_token": "bon_new_token",
                    "refresh_token": "bon_new_rf",
                    "expires_in": 90000,
                }
            return resp

        mock_post.side_effect = fake_post

        ord_results = []
        bon_results = []
        threads = []

        for _ in range(10):
            th_ord = threading.Thread(target=lambda: ord_results.append(zalo_svc.get_access_token("ord")))
            th_bon = threading.Thread(target=lambda: bon_results.append(zalo_svc.get_access_token("bon")))
            threads.extend([th_ord, th_bon])
            th_ord.start()
            th_bon.start()

        for th in threads:
            th.join()

        self.assertEqual(len(ord_results), 10)
        self.assertEqual(len(bon_results), 10)
        self.assertTrue(all(t == "ord_new_token" for t in ord_results))
        self.assertTrue(all(t == "bon_new_token" for t in bon_results))

        # Token files contain correct data
        ord_saved = zalo_svc._load_tokens("ord")
        bon_saved = zalo_svc._load_tokens("bon")
        self.assertEqual(ord_saved["access_token"], "ord_new_token")
        self.assertEqual(bon_saved["access_token"], "bon_new_token")


# ═══════════════════════════════════════════
#  R3: AUTO-REFRESH RETRY & BACKOFF TESTS
# ═══════════════════════════════════════════

class TestAutoRefreshRetry(BaseZaloTestCase):
    """Tests for R3: Auto-Refresh Daemon with Retry & Exponential Backoff."""

    @patch("services.zalo_zns.requests.post")
    def test_refresh_app_with_retry_succeeds_first_try(self, mock_post):
        """Auto-refresh succeeds on first attempt."""
        self._write_token_file("ord", {
            "access_token": "old_token",
            "refresh_token": "valid_refresh",
            "expires_at": time.time() - 100,
        })

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "fresh_token_try1",
            "refresh_token": "fresh_rf_try1",
            "expires_in": 90000,
        }
        mock_post.return_value = mock_resp

        success = zalo_svc._refresh_app_with_retry("ord", max_retries=3, backoffs=[0.01, 0.02, 0.03])
        self.assertTrue(success)
        self.assertEqual(mock_post.call_count, 1)

    @patch("services.zalo_zns.requests.post")
    def test_refresh_app_with_retry_succeeds_after_failures(self, mock_post):
        """Auto-refresh retries on network/API failure and succeeds on 3rd attempt."""
        self._write_token_file("ord", {
            "access_token": "old_token",
            "refresh_token": "valid_refresh",
            "expires_at": time.time() - 100,
        })

        fail_resp = MagicMock()
        fail_resp.json.return_value = {"error": -1, "message": "Zalo OA internal error"}

        success_resp = MagicMock()
        success_resp.json.return_value = {
            "access_token": "fresh_token_try3",
            "refresh_token": "fresh_rf_try3",
            "expires_in": 90000,
        }

        mock_post.side_effect = [
            Exception("Connection timed out"),
            fail_resp,
            success_resp,
        ]

        success = zalo_svc._refresh_app_with_retry("ord", max_retries=3, backoffs=[0.01, 0.02, 0.03])
        self.assertTrue(success)
        self.assertEqual(mock_post.call_count, 3)

        # Check token updated
        saved = zalo_svc._load_tokens("ord")
        self.assertEqual(saved["access_token"], "fresh_token_try3")

    @patch("services.zalo_zns.requests.post")
    def test_refresh_app_with_retry_fails_all_attempts(self, mock_post):
        """Auto-refresh returns False and logs error when all retries fail."""
        self._write_token_file("ord", {
            "access_token": "old_token",
            "refresh_token": "valid_refresh",
            "expires_at": time.time() - 100,
        })

        mock_post.side_effect = Exception("Permanent network failure")

        success = zalo_svc._refresh_app_with_retry("ord", max_retries=3, backoffs=[0.01, 0.02, 0.03])
        self.assertFalse(success)
        self.assertEqual(mock_post.call_count, 3)

    def test_refresh_app_skips_when_no_refresh_token(self):
        """When app has no refresh token configured, skips without retrying."""
        self._write_token_file("ord", {"access_token": "", "refresh_token": ""})
        success = zalo_svc._refresh_app_with_retry("ord", max_retries=3)
        self.assertFalse(success)

    def test_refresh_app_skips_whitespace_refresh_token(self):
        """When app has whitespace refresh token, skips without retrying."""
        self._write_token_file("ord", {"access_token": "a", "refresh_token": "   "})
        success = zalo_svc._refresh_app_with_retry("ord", max_retries=3)
        self.assertFalse(success)

    @patch("services.zalo_zns.requests.post")
    def test_refresh_app_with_retry_empty_backoffs_list(self, mock_post):
        """Passing backoffs=[] or None defaults gracefully without crashing."""
        self._write_token_file("ord", {
            "access_token": "old",
            "refresh_token": "rf",
            "expires_at": time.time() - 100,
        })
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "ok_tok", "refresh_token": "rf", "expires_in": 90000}
        mock_post.return_value = mock_resp

        success = zalo_svc._refresh_app_with_retry("ord", max_retries=1, backoffs=[])
        self.assertTrue(success)

    def test_start_and_stop_auto_refresh(self):
        """start_auto_refresh and stop_auto_refresh lifecycle test."""
        zalo_svc.start_auto_refresh()
        self.assertTrue(zalo_svc._auto_refresh_running)
        zalo_svc.stop_auto_refresh()
        self.assertFalse(zalo_svc._auto_refresh_running)

    def test_stop_auto_refresh_from_within_thread(self):
        """Calling stop_auto_refresh from within the running thread does not deadlock."""
        def run_inner():
            zalo_svc.stop_auto_refresh()

        zalo_svc.start_auto_refresh()
        th = threading.Thread(target=run_inner)
        th.start()
        th.join(timeout=2.0)
        self.assertFalse(th.is_alive())
        self.assertFalse(zalo_svc._auto_refresh_running)


# ═══════════════════════════════════════════
#  R4: ZNS SENDING & INTEGRATION TESTS
# ═══════════════════════════════════════════

class TestZNSFunctions(BaseZaloTestCase):
    """Tests for send_zns, authorization callback, and token status."""

    @patch("services.zalo_zns.requests.post")
    def test_send_zns_ord_routes_correctly(self, mock_post):
        """send_zns with 'hdsd-vie' routes to ORD app and includes formatted payload."""
        now = time.time()
        self._write_token_file("ord", {
            "access_token": "ord_valid_access_token",
            "refresh_token": "ord_rf",
            "expires_at": now + 7200,
        })

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": 0, "message": "Success", "data": {"msg_id": "12345"}}
        mock_post.return_value = mock_resp

        result = zalo_svc.send_zns(
            template_type="hdsd-vie",
            phone="+84987654321",
            order_code="BG-202508-001",
            order_date="27/08/2026",
            customer_name="Nguyen Van A",
        )

        self.assertEqual(result["error"], 0)
        self.assertEqual(mock_post.call_count, 1)

        # Check call arguments
        call_args = mock_post.call_args
        self.assertEqual(call_args[0][0], "https://business.openapi.zalo.me/message/template")
        self.assertEqual(call_args[1]["headers"]["access_token"], "ord_valid_access_token")
        self.assertEqual(call_args[1]["json"]["phone"], "+84987654321")
        self.assertEqual(call_args[1]["json"]["template_id"], Config.ZNS_TEMPLATES["hdsd-vie"])
        self.assertEqual(call_args[1]["json"]["template_data"]["order_code"], "BG-202508-001")
        self.assertEqual(call_args[1]["json"]["template_data"]["name"], "Nguyen Van A")
        self.assertEqual(call_args[1]["json"]["template_data"]["order_date"], "27/08/2026")

    @patch("services.zalo_zns.requests.post")
    def test_send_zns_bon_rating_routes_correctly(self, mock_post):
        """send_zns with 'rating' routes to BON app and uses customer_name key."""
        now = time.time()
        self._write_token_file("bon", {
            "access_token": "bon_valid_access_token",
            "refresh_token": "bon_rf",
            "expires_at": now + 7200,
        })

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": 0, "message": "Success"}
        mock_post.return_value = mock_resp

        result = zalo_svc.send_zns(
            template_type="rating",
            phone="+84912345678",
            order_code="BON-001",
            order_date="27/08/2026",
            customer_name="Tran Thi B",
        )

        self.assertEqual(result["error"], 0)
        call_args = mock_post.call_args
        self.assertEqual(call_args[1]["headers"]["access_token"], "bon_valid_access_token")
        self.assertEqual(call_args[1]["json"]["template_data"]["customer_name"], "Tran Thi B")

    def test_send_zns_invalid_template(self):
        """send_zns raises ValueError for unknown template."""
        with self.assertRaises(ValueError):
            zalo_svc.send_zns(template_type="unknown-template", phone="+84987654321")

    @patch("services.zalo_zns.requests.post")
    def test_handle_authorization_callback(self, mock_post):
        """OAuth authorization callback saves tokens and expiration timestamp."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "auth_access_token_123",
            "refresh_token": "auth_refresh_token_123",
            "expires_in": 90000,
        }
        mock_post.return_value = mock_resp

        result = zalo_svc.handle_authorization_callback(
            code="test_code_123",
            code_verifier="test_verifier",
            app="ord",
        )

        self.assertIn("access_token", result)
        saved = zalo_svc._load_tokens("ord")
        self.assertEqual(saved["access_token"], "auth_access_token_123")
        self.assertEqual(saved["refresh_token"], "auth_refresh_token_123")
        self.assertEqual(saved["expires_in"], 90000)
        self.assertIn("expires_at", saved)
        self.assertIn("updated_at", saved)

    def test_get_token_status(self):
        """get_token_status reports accurate metadata for both apps."""
        now = time.time()
        self._write_token_file("ord", {
            "access_token": "ord_access",
            "refresh_token": "ord_refresh",
            "expires_at": now + 7200,
            "updated_at": "2026-08-27T10:00:00",
        })
        self._write_token_file("bon", {
            "access_token": "",
            "refresh_token": "",
        })

        status = zalo_svc.get_token_status()
        self.assertIn("ord", status)
        self.assertIn("bon", status)
        self.assertIn("auto_refresh_active", status)

        self.assertTrue(status["ord"]["has_access_token"])
        self.assertTrue(status["ord"]["has_refresh_token"])
        self.assertTrue(status["ord"]["is_token_valid"])

        self.assertFalse(status["bon"]["has_access_token"])
        self.assertFalse(status["bon"]["has_refresh_token"])
        self.assertFalse(status["bon"]["is_token_valid"])


# ═══════════════════════════════════════════
#  ROBUSTNESS & EDGE CASE TESTS
# ═══════════════════════════════════════════

class TestTokenStorageAndIO(BaseZaloTestCase):
    """Tests for file I/O robustness, atomic saves, and corruption recovery."""

    def test_atomic_save_and_clean_cleanup(self):
        """Saving tokens writes atomically and leaves no lingering temp files."""
        tokens = {
            "access_token": "atom_access_1",
            "refresh_token": "atom_refresh_1",
            "expires_in": 90000,
            "expires_at": time.time() + 90000,
        }
        zalo_svc._save_tokens(tokens, "ord")

        # Main file exists and is valid JSON
        loaded = zalo_svc._load_tokens("ord")
        self.assertEqual(loaded["access_token"], "atom_access_1")

        # No temp files remaining in directory
        tmp_files = [f for f in os.listdir(self.data_dir) if ".tmp." in f]
        self.assertEqual(len(tmp_files), 0)

    def test_load_tokens_corrupt_json_returns_default(self):
        """Corrupt JSON in token file returns default empty tokens without throwing."""
        with open(self.ord_token_file, "w", encoding="utf-8") as f:
            f.write("{invalid_json: true, unterminated")

        tokens = zalo_svc._load_tokens("ord")
        self.assertEqual(tokens, {"access_token": "", "refresh_token": ""})
        self.assertFalse(zalo_svc._is_token_valid(tokens))

    def test_load_tokens_non_dict_json_returns_default(self):
        """JSON file containing array or null returns default dict without throwing."""
        with open(self.ord_token_file, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")

        tokens = zalo_svc._load_tokens("ord")
        self.assertEqual(tokens, {"access_token": "", "refresh_token": ""})

        with open(self.ord_token_file, "w", encoding="utf-8") as f:
            f.write("null")

        tokens = zalo_svc._load_tokens("ord")
        self.assertEqual(tokens, {"access_token": "", "refresh_token": ""})

    def test_load_tokens_empty_file_zero_bytes(self):
        """Empty 0-byte token file returns default empty dict cleanly."""
        with open(self.ord_token_file, "w", encoding="utf-8") as f:
            f.write("")

        tokens = zalo_svc._load_tokens("ord")
        self.assertEqual(tokens, {"access_token": "", "refresh_token": ""})

    def test_save_tokens_relative_path_without_dir(self):
        """Saving tokens to a filename with no dirname works properly."""
        orig_ord_path = zalo_svc.TOKEN_FILES["ord"]
        try:
            temp_filename = f"temp_ord_token_{os.getpid()}.json"
            zalo_svc.TOKEN_FILES["ord"] = temp_filename
            zalo_svc._save_tokens({"access_token": "rel_tok", "refresh_token": "rel_rf"}, "ord")
            self.assertTrue(os.path.exists(temp_filename))
            loaded = zalo_svc._load_tokens("ord")
            self.assertEqual(loaded["access_token"], "rel_tok")
        finally:
            zalo_svc.TOKEN_FILES["ord"] = orig_ord_path
            if os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except OSError:
                    pass

    def test_save_tokens_invalid_type_raises_type_error(self):
        """_save_tokens raises TypeError if tokens is not a dictionary."""
        with self.assertRaises(TypeError):
            zalo_svc._save_tokens("invalid_type", "ord")

    @patch("os.replace")
    def test_save_tokens_failure_cleans_up_tmp_file(self, mock_replace):
        """If os.replace fails during save, temporary file is cleaned up."""
        mock_replace.side_effect = OSError("Disk write failure")
        with self.assertRaises(OSError):
            zalo_svc._save_tokens({"access_token": "a", "refresh_token": "r"}, "ord")

        tmp_files = [f for f in os.listdir(self.data_dir) if ".tmp." in f]
        self.assertEqual(len(tmp_files), 0)


class TestZaloAPIEdgeCases(BaseZaloTestCase):
    """Tests for HTTP errors, non-JSON responses, and malformed payload edge cases."""

    def test_refresh_token_whitespace_refresh_token_raises_value_error(self):
        """Whitespace refresh token raises ValueError asking for OAuth setup without making API call."""
        self._write_token_file("ord", {"access_token": "", "refresh_token": "   "})
        with self.assertRaises(ValueError) as ctx:
            zalo_svc.get_access_token("ord", force_refresh=True)
        self.assertIn("No refresh_token found", str(ctx.exception))

    @patch("services.zalo_zns.requests.post")
    def test_refresh_token_zalo_error_response_with_name_field(self, mock_post):
        """Extracts error message from Zalo error responses with 'name' or 'message' field."""
        self._write_token_file("ord", {
            "access_token": "old",
            "refresh_token": "valid_rf",
            "expires_at": time.time() - 100,
        })
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": -14014, "name": "Invalid refresh token"}
        mock_post.return_value = mock_resp

        with self.assertRaises(ValueError) as ctx:
            zalo_svc.get_access_token("ord", force_refresh=True)
        self.assertIn("Invalid refresh token", str(ctx.exception))

    @patch("services.zalo_zns.requests.post")
    def test_refresh_token_non_json_502_bad_gateway(self, mock_post):
        """When Zalo OAuth returns HTTP 502 with HTML body, raises clear ValueError."""
        self._write_token_file("ord", {
            "access_token": "old_token",
            "refresh_token": "valid_refresh",
            "expires_at": time.time() - 100,
        })

        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.text = "<html><head><title>502 Bad Gateway</title></head><body>502 Bad Gateway</body></html>"
        mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
        mock_post.return_value = mock_resp

        with self.assertRaises(ValueError) as ctx:
            zalo_svc.get_access_token("ord", force_refresh=True)

        self.assertIn("non-JSON response (HTTP 502)", str(ctx.exception))

    @patch("services.zalo_zns.requests.post")
    def test_refresh_token_string_and_null_expires_in(self, mock_post):
        """Handles string or null expires_in from Zalo API without crashing."""
        self._write_token_file("ord", {
            "access_token": "old_token",
            "refresh_token": "valid_refresh",
            "expires_at": time.time() - 100,
        })

        # Test string expires_in
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "fresh_token_str_exp",
            "refresh_token": "new_rf",
            "expires_in": "7200",
        }
        mock_post.return_value = mock_resp

        token = zalo_svc.get_access_token("ord", force_refresh=True)
        self.assertEqual(token, "fresh_token_str_exp")
        saved = zalo_svc._load_tokens("ord")
        self.assertEqual(saved["expires_in"], 7200)

        # Test null expires_in fallback to default
        mock_resp.json.return_value = {
            "access_token": "fresh_token_null_exp",
            "refresh_token": "new_rf",
            "expires_in": None,
        }
        token2 = zalo_svc.get_access_token("ord", force_refresh=True)
        self.assertEqual(token2, "fresh_token_null_exp")
        saved2 = zalo_svc._load_tokens("ord")
        self.assertEqual(saved2["expires_in"], zalo_svc.DEFAULT_EXPIRES_IN)

    @patch("services.zalo_zns.requests.post")
    def test_refresh_token_retains_existing_refresh_token_if_omitted(self, mock_post):
        """If Zalo API response does not include a new refresh_token, keeps the old one."""
        self._write_token_file("ord", {
            "access_token": "old_token",
            "refresh_token": "original_refresh_token_keep_me",
            "expires_at": time.time() - 100,
        })

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "fresh_token_no_rf_returned",
            "expires_in": 90000,
        }
        mock_post.return_value = mock_resp

        token = zalo_svc.get_access_token("ord", force_refresh=True)
        self.assertEqual(token, "fresh_token_no_rf_returned")
        saved = zalo_svc._load_tokens("ord")
        self.assertEqual(saved["refresh_token"], "original_refresh_token_keep_me")

    @patch("services.zalo_zns.requests.post")
    def test_send_zns_non_json_504_gateway_timeout(self, mock_post):
        """send_zns raises descriptive ValueError if ZNS OpenAPI returns non-JSON HTTP 504."""
        now = time.time()
        self._write_token_file("ord", {
            "access_token": "valid_token",
            "refresh_token": "rf",
            "expires_at": now + 7200,
        })

        mock_resp = MagicMock()
        mock_resp.status_code = 504
        mock_resp.text = "<html>504 Gateway Timeout</html>"
        mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
        mock_post.return_value = mock_resp

        with self.assertRaises(ValueError) as ctx:
            zalo_svc.send_zns(
                template_type="hdsd-vie",
                phone="+84987654321",
                order_code="BG-001",
            )

        self.assertIn("non-JSON response (HTTP 504)", str(ctx.exception))

    @patch("services.zalo_zns.requests.post")
    def test_handle_authorization_callback_non_json_500(self, mock_post):
        """handle_authorization_callback raises ValueError on non-JSON response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "Internal", 0)
        mock_post.return_value = mock_resp

        with self.assertRaises(ValueError) as ctx:
            zalo_svc.handle_authorization_callback(code="code_xyz", app="ord")

        self.assertIn("non-JSON response (HTTP 500)", str(ctx.exception))

    def test_start_auto_refresh_idempotency(self):
        """Calling start_auto_refresh multiple times does not spawn multiple threads."""
        zalo_svc.start_auto_refresh()
        thread1 = zalo_svc._auto_refresh_thread
        self.assertIsNotNone(thread1)
        self.assertTrue(thread1.is_alive())

        # Second call should be a no-op
        zalo_svc.start_auto_refresh()
        thread2 = zalo_svc._auto_refresh_thread
        self.assertIs(thread1, thread2)

        zalo_svc.stop_auto_refresh()
        self.assertFalse(zalo_svc._auto_refresh_running)

    def test_auto_refresh_multiple_start_stop_cycles(self):
        """Auto-refresh can be cleanly started and stopped multiple times across cycles."""
        for _ in range(3):
            zalo_svc.start_auto_refresh()
            self.assertTrue(zalo_svc._auto_refresh_running)
            self.assertIsNotNone(zalo_svc._auto_refresh_thread)
            zalo_svc.stop_auto_refresh()
            self.assertFalse(zalo_svc._auto_refresh_running)
            self.assertIsNone(zalo_svc._auto_refresh_thread)

    def test_get_token_status_whitespace_and_invalid_tokens(self):
        """get_token_status correctly reports False for whitespace or non-string tokens."""
        self._write_token_file("ord", {
            "access_token": "   ",
            "refresh_token": "   ",
            "expires_at": time.time() + 7200,
        })
        self._write_token_file("bon", {
            "access_token": 12345,
            "refresh_token": None,
        })

        status = zalo_svc.get_token_status()
        self.assertFalse(status["ord"]["has_access_token"])
        self.assertFalse(status["ord"]["has_refresh_token"])
        self.assertFalse(status["ord"]["is_token_valid"])

        self.assertFalse(status["bon"]["has_access_token"])
        self.assertFalse(status["bon"]["has_refresh_token"])
        self.assertFalse(status["bon"]["is_token_valid"])

    def test_save_tokens_does_not_mutate_input_dict(self):
        """_save_tokens does not mutate the dictionary passed in by the caller."""
        input_tokens = {"access_token": "immutable_test", "refresh_token": "rf_test"}
        zalo_svc._save_tokens(input_tokens, "ord")
        self.assertNotIn("updated_at", input_tokens)

    def test_token_helpers_with_none_or_non_string_app(self):
        """Token helper functions safely default to 'ord' when app is None or non-string."""
        lock = zalo_svc._get_app_lock(None)
        self.assertIsNotNone(lock)
        tf = zalo_svc._token_file(None)
        self.assertEqual(tf, zalo_svc.TOKEN_FILES["ord"])
        tokens = zalo_svc._load_tokens(None)
        self.assertIsInstance(tokens, dict)

    @patch("services.zalo_zns.requests.post")
    def test_refresh_token_zero_and_negative_expires_in_fallback(self, mock_post):
        """Zero or negative expires_in from Zalo API falls back to DEFAULT_EXPIRES_IN."""
        self._write_token_file("ord", {
            "access_token": "old",
            "refresh_token": "valid_rf",
            "expires_at": time.time() - 100,
        })
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "fresh_token_zero_exp",
            "refresh_token": "valid_rf",
            "expires_in": 0,
        }
        mock_post.return_value = mock_resp

        token = zalo_svc.get_access_token("ord", force_refresh=True)
        self.assertEqual(token, "fresh_token_zero_exp")
        saved = zalo_svc._load_tokens("ord")
        self.assertEqual(saved["expires_in"], zalo_svc.DEFAULT_EXPIRES_IN)
        self.assertTrue(zalo_svc._is_token_valid(saved))

    @patch("services.zalo_zns.requests.post")
    def test_handle_authorization_callback_zero_expires_in_fallback(self, mock_post):
        """Authorization callback with zero expires_in falls back to DEFAULT_EXPIRES_IN."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "auth_tok_zero",
            "refresh_token": "auth_rf_zero",
            "expires_in": -50,
        }
        mock_post.return_value = mock_resp

        zalo_svc.handle_authorization_callback(code="auth_code_zero", app="ord")
        saved = zalo_svc._load_tokens("ord")
        self.assertEqual(saved["expires_in"], zalo_svc.DEFAULT_EXPIRES_IN)
        self.assertTrue(zalo_svc._is_token_valid(saved))


# ═══════════════════════════════════════════
#  FLASK ENDPOINTS INTEGRATION TESTS
# ═══════════════════════════════════════════

class TestFlaskEndpointsIntegration(unittest.TestCase):
    """Verify Flask endpoints /webhook/hdsd-*, /webhook/rating*, /webhook/zns-done, /health."""

    def setUp(self):
        from app import app
        self.orig_allow_dev = Config.ZNS_ALLOW_INSECURE_DEV
        self.orig_inbound_auth = Config.ZNS_INBOUND_AUTH_REQUIRED
        Config.ZNS_ALLOW_INSECURE_DEV = True
        Config.ZNS_INBOUND_AUTH_REQUIRED = False
        self.client = app.test_client()

    def tearDown(self):
        Config.ZNS_ALLOW_INSECURE_DEV = self.orig_allow_dev
        Config.ZNS_INBOUND_AUTH_REQUIRED = self.orig_inbound_auth

    def test_health_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("zns_tokens", data)

    @patch("services.zns_tracking.send_zns")
    def test_webhook_zns_routes(self, mock_send_zns):
        mock_send_zns.side_effect = lambda **kwargs: {
            "error": 0,
            "message": "Success",
            "data": {"msg_id": f"msg_{time.time_ns()}_{kwargs.get('template_type')}"}
        }
        routes = ["hdsd-eng", "hdsd-vie", "rating-ord-eng", "rating-ord-vie", "rating"]


        for route in routes:
            resp = self.client.post(f"/webhook/{route}", json={
                "x_studio_phone": "0987654321",
                "name": f"BG-{route}-001",
                "x_studio_tn_khch_hng": "Khach Hang Test",
                "date_order": "2026-08-27",
            })
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()["status"], "accepted")
            self.assertEqual(resp.get_json()["delivery_status"], "pending")

        self.assertEqual(mock_send_zns.call_count, len(routes))


    @patch("app.handle_authorization_callback")
    def test_webhook_zns_done_oauth_callback(self, mock_auth_cb):
        mock_auth_cb.return_value = {"access_token": "oauth_token_123", "refresh_token": "oauth_rf_123"}

        # ORD callback
        resp = self.client.get("/webhook/zns-done?code=test_code_ord&state=ord")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        self.assertEqual(resp.get_json()["app"], "ord")

        # BON callback
        resp_bon = self.client.get("/webhook/zns-done?code=test_code_bon&state=bon")
        self.assertEqual(resp_bon.status_code, 200)
        self.assertEqual(resp_bon.get_json()["status"], "ok")
        self.assertEqual(resp_bon.get_json()["app"], "bon")


if __name__ == "__main__":
    unittest.main()
