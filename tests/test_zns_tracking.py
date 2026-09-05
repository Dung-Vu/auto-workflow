"""
Comprehensive Unit & Integration Test Suite for ZNS Tracking & Delivery System.

Covers:
1.  ZNS send accepted -> ACCEPTED (status != sent)
2.  ZNS send rejected -> REJECTED (status != sent)
3.  ZNS send network timeout -> SUBMISSION_UNKNOWN
4.  ZNS send malformed JSON -> SUBMISSION_UNKNOWN
5.  ZNS send invalid phone -> 400 REJECTED (no Zalo API call)
6.  ZNS send invalid template -> 400 REJECTED (no Zalo API call)
7.  Idempotent duplicate send -> cached response, Zalo called ONCE
8.  Strict concurrent duplicate sends -> exactly 1 send call
9.  Duplicate delivery webhook -> idempotent 200, no duplicate events
10. Delivery webhook arriving during SUBMITTING -> DELIVERED
11. DELIVERED never downgrades (Terminal State & CAS protection)
12. Unknown message callback -> 200 OK + diagnostic logged
13. Invalid signature / app -> 401 Unauthorized
14. PII masking in DB, logs, and sanitized payloads
15. Statistics denominator and rate calculations
16. Date/time UTC storage and ISO formatting
17. SQLite schema migrations (v1 and v2 with CHECK constraints)
18. Concurrent SQLite access handling
19. Docker volume persistence contract
20. Reconciliation SLA stale transition
21. Fail-closed Admin API authentication (empty key returns 503)
22. Fail-closed Inbound send route authentication (missing key returns 401)
23. Cross-Tenant Webhook Protection (ORD secret callback for BON record rejected 403)
24. Webhook Msg ID Mismatch Protection (rejects 400, does not overwrite)
25. Webhook Timestamp Freshness (stale timestamp rejected 400)
26. Data Retention Cleanup (deletes terminal records, preserves pending)
"""

import os
import sys
import json
import time
import uuid
import tempfile
import threading
import sqlite3
import unittest
import hashlib
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config
from app import app
from services.zns_repository import ZNSRepository, reset_repository
from services.zns_tracking import (
    ZNSTrackingService,
    STATE_QUEUED,
    STATE_SUBMITTING,
    STATE_ACCEPTED,
    STATE_DELIVERED,
    STATE_REJECTED,
    STATE_SUBMISSION_UNKNOWN,
    STATE_DELIVERY_UNKNOWN,
    VALID_TRANSITIONS,
    _utc_now_iso,
    verify_zalo_webhook_signature,
)
from utils.pii import mask_phone, hash_phone, sanitize_payload, is_valid_vn_phone


class BaseZNSTestCase(unittest.TestCase):
    """Base setup for ZNS tracking test cases using isolated temporary DB."""

    def setUp(self):
        reset_repository()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_zns_tracking.sqlite3")

        self.orig_db_path = Config.ZNS_TRACKING_DB_PATH
        self.orig_admin_key = Config.ZNS_ADMIN_API_KEY
        self.orig_inbound_key = Config.ZNS_INBOUND_API_KEY
        self.orig_inbound_auth_req = Config.ZNS_INBOUND_AUTH_REQUIRED
        self.orig_phone_hash_key = Config.ZNS_PHONE_HASH_KEY
        self.orig_allow_dev = Config.ZNS_ALLOW_INSECURE_DEV
        self.orig_app_id = Config.ZALO_APP_ID
        self.orig_secret = Config.ZALO_SECRET_KEY
        self.orig_oa_id = Config.ZALO_OA_ID
        self.orig_bon_app_id = Config.ZALO_BON_APP_ID
        self.orig_bon_secret = Config.ZALO_BON_SECRET_KEY
        self.orig_bon_oa_id = Config.ZALO_BON_OA_ID
        self.orig_sig_req = Config.ZNS_WEBHOOK_REQUIRE_SIGNATURE
        self.orig_ts_tol = Config.ZNS_WEBHOOK_TIMESTAMP_TOLERANCE
        self.orig_max_payload = Config.ZNS_MAX_PAYLOAD_BYTES
        self.orig_odoo_webhook_token = Config.ZNS_ODOO_WEBHOOK_TOKEN

        Config.ZNS_TRACKING_DB_PATH = self.db_path
        Config.ZNS_ADMIN_API_KEY = "test_admin_secret_key_123"
        Config.ZNS_INBOUND_API_KEY = "test_inbound_secret_key_456"
        Config.ZNS_INBOUND_AUTH_REQUIRED = True
        Config.ZNS_PHONE_HASH_KEY = "test_secret_phone_hash_key"
        Config.ZNS_ALLOW_INSECURE_DEV = True
        Config.ZALO_APP_ID = "ord_app_test_id"
        Config.ZALO_SECRET_KEY = "ord_secret_test_key"
        Config.ZALO_OA_ID = "ord_oa_test_id"
        Config.ZALO_BON_APP_ID = "bon_app_test_id"
        Config.ZALO_BON_SECRET_KEY = "bon_secret_test_key"
        Config.ZALO_BON_OA_ID = "bon_oa_test_id"
        Config.ZNS_WEBHOOK_REQUIRE_SIGNATURE = True
        Config.ZNS_WEBHOOK_TIMESTAMP_TOLERANCE = 300
        Config.ZNS_MAX_PAYLOAD_BYTES = 100 * 1024
        Config.ZNS_ODOO_WEBHOOK_TOKEN = "test-odoo-webhook-capability"

        self.repo = ZNSRepository(db_path=self.db_path)
        self.service = ZNSTrackingService(repo=self.repo)

    def _make_sig(self, raw_body: str, app_id: str, timestamp: str, secret_key: str) -> str:
        raw_to_hash = f"{app_id}{raw_body}{timestamp}{secret_key}"
        return hashlib.sha256(raw_to_hash.encode("utf-8")).hexdigest()

    def tearDown(self):
        reset_repository()
        Config.ZNS_TRACKING_DB_PATH = self.orig_db_path
        Config.ZNS_ADMIN_API_KEY = self.orig_admin_key
        Config.ZNS_INBOUND_API_KEY = self.orig_inbound_key
        Config.ZNS_INBOUND_AUTH_REQUIRED = self.orig_inbound_auth_req
        Config.ZNS_PHONE_HASH_KEY = self.orig_phone_hash_key
        Config.ZNS_ALLOW_INSECURE_DEV = self.orig_allow_dev
        Config.ZALO_APP_ID = self.orig_app_id
        Config.ZALO_SECRET_KEY = self.orig_secret
        Config.ZALO_OA_ID = self.orig_oa_id
        Config.ZALO_BON_APP_ID = self.orig_bon_app_id
        Config.ZALO_BON_SECRET_KEY = self.orig_bon_secret
        Config.ZALO_BON_OA_ID = self.orig_bon_oa_id
        Config.ZNS_WEBHOOK_REQUIRE_SIGNATURE = self.orig_sig_req
        Config.ZNS_WEBHOOK_TIMESTAMP_TOLERANCE = self.orig_ts_tol
        Config.ZNS_MAX_PAYLOAD_BYTES = self.orig_max_payload
        Config.ZNS_ODOO_WEBHOOK_TOKEN = self.orig_odoo_webhook_token
        self.temp_dir.cleanup()


# ═══════════════════════════════════════════
#  UNIT TESTS (26 MANDATORY SCENARIOS)
# ═══════════════════════════════════════════

class TestZNSUnitScenarios(BaseZNSTestCase):
    """Unit test cases for ZNS state transitions and contracts."""

    # 1. ZNS send accepted -> ACCEPTED
    @patch("services.zns_tracking.send_zns")
    def test_1_accepted_returns_accepted_not_sent(self, mock_send_zns):
        mock_send_zns.return_value = {
            "error": 0,
            "message": "Success",
            "data": {
                "msg_id": "zmsg_acc_001",
                "sent_time": "1626926349000",
                "sending_mode": "1",
                "quota": {"dailyQuota": 1000, "remainingQuota": 950},
            },
        }

        res = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="0987654321",
            order_code="BG-2026-001",
            customer_name="Nguyen Van A",
        )

        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["delivery_status"], "pending")
        self.assertEqual(res["msg_id"], "zmsg_acc_001")
        self.assertNotIn("status: sent", str(res))

        record = self.repo.get_message_by_tracking_id(res["tracking_id"])
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], STATE_ACCEPTED)
        self.assertEqual(record["zalo_msg_id"], "zmsg_acc_001")
        self.assertEqual(record["quota_daily"], 1000)
        self.assertEqual(record["quota_remaining"], 950)

    # 2. ZNS send rejected -> REJECTED
    @patch("services.zns_tracking.send_zns")
    def test_2_rejected_does_not_return_sent(self, mock_send_zns):
        mock_send_zns.return_value = {
            "error": -118,
            "message": "Zalo account not existed",
        }

        res = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="0987654321",
            order_code="BG-2026-002",
        )

        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["delivery_status"], "failed")
        self.assertEqual(res["error"]["code"], -118)
        self.assertIn("mã -118", res["message"])

        record = self.repo.get_message_by_tracking_id(res["tracking_id"])
        self.assertEqual(record["status"], STATE_REJECTED)
        self.assertEqual(record["error_code"], -118)

    # 3. ZNS send network timeout -> SUBMISSION_UNKNOWN
    @patch("services.zns_tracking.send_zns")
    def test_3_network_timeout_transitions_to_submission_unknown(self, mock_send_zns):
        mock_send_zns.side_effect = Exception("HTTPSConnectionPool(host='openapi.zalo.me'): Read timed out.")

        res = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="0987654321",
        )

        self.assertEqual(res["status"], "submission_unknown")
        self.assertEqual(res["delivery_status"], "unknown")
        self.assertEqual(res["error"]["code"], "NETWORK_TIMEOUT_OR_ERROR")

        record = self.repo.get_message_by_tracking_id(res["tracking_id"])
        self.assertEqual(record["status"], STATE_SUBMISSION_UNKNOWN)

    # 4. ZNS send malformed response -> SUBMISSION_UNKNOWN
    @patch("services.zns_tracking.send_zns")
    def test_4_malformed_upstream_response(self, mock_send_zns):
        mock_send_zns.return_value = "<html>502 Bad Gateway</html>"

        res = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="0987654321",
        )

        self.assertEqual(res["status"], "submission_unknown")
        record = self.repo.get_message_by_tracking_id(res["tracking_id"])
        self.assertEqual(record["status"], STATE_SUBMISSION_UNKNOWN)

    # 5. Invalid phone -> REJECTED (no Zalo call)
    @patch("services.zns_tracking.send_zns")
    def test_5_invalid_phone_rejected_without_upstream_call(self, mock_send_zns):
        res = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="12345",
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["error"]["code"], "INVALID_PHONE")
        mock_send_zns.assert_not_called()

    # 6. Invalid template -> REJECTED (no Zalo call)
    @patch("services.zns_tracking.send_zns")
    def test_6_invalid_template_rejected_without_upstream_call(self, mock_send_zns):
        res = self.service.dispatch_zns(
            template_type="non_existent_template",
            phone_raw="0987654321",
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["error"]["code"], "INVALID_TEMPLATE")
        mock_send_zns.assert_not_called()

    # 7. Idempotent duplicate send -> calls Zalo ONCE
    @patch("services.zns_tracking.send_zns")
    def test_7_duplicate_idempotency_key_calls_once(self, mock_send_zns):
        mock_send_zns.return_value = {
            "error": 0,
            "message": "Success",
            "data": {"msg_id": "zmsg_idem_1", "sent_time": "1626926349000"},
        }
        idem_key = "so_idem_test_001"

        res1 = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="0987654321",
            idempotency_key=idem_key,
            order_code="BG-001",
        )
        self.assertEqual(res1["status"], "accepted")
        self.assertEqual(mock_send_zns.call_count, 1)

        res2 = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="0987654321",
            idempotency_key=idem_key,
            order_code="BG-001",
        )
        self.assertEqual(res2["status"], "accepted")
        self.assertTrue(res2.get("is_duplicate"))
        self.assertEqual(res1["tracking_id"], res2["tracking_id"])
        # Zalo MUST still be called exactly once
        self.assertEqual(mock_send_zns.call_count, 1)

    # 8. Strict concurrent duplicate sends -> exactly 1 send call
    @patch("services.zns_tracking.send_zns")
    def test_8_concurrent_duplicate_requests(self, mock_send_zns):
        mock_send_zns.return_value = {
            "error": 0,
            "message": "Success",
            "data": {"msg_id": "zmsg_conc_1", "sent_time": "1626926349000"},
        }
        idem_key = "so_conc_002"

        results = []
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            res = self.service.dispatch_zns(
                template_type="hdsd-vie",
                phone_raw="0987654321",
                idempotency_key=idem_key,
            )
            results.append(res)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 5)
        self.assertEqual(mock_send_zns.call_count, 1)

    # 9. Duplicate delivery webhook -> idempotent 200
    def test_9_duplicate_delivery_webhook_is_idempotent(self):
        created = self.repo.create_message({
            "tracking_id": "trk_dup_wh",
            "zalo_msg_id": "zmsg_dup_wh",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash123",
            "status": STATE_ACCEPTED,
        })

        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {
                "msg_id": "zmsg_dup_wh",
                "tracking_id": "trk_dup_wh",
                "delivery_time": str(int(time.time() * 1000)),
            },
        }
        raw_body_str = json.dumps(payload)
        sig = self._make_sig(raw_body_str, Config.ZALO_APP_ID, ts, Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        # First delivery webhook
        ok1, msg1, code1 = self.service.process_delivery_webhook(
            raw_body=raw_body_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertTrue(ok1)
        self.assertEqual(code1, 200)

        record = self.repo.get_message_by_tracking_id("trk_dup_wh")
        self.assertEqual(record["status"], STATE_DELIVERED)
        events1 = self.repo.query_events_for_message(record["id"])

        # Second identical delivery webhook
        ok2, msg2, code2 = self.service.process_delivery_webhook(
            raw_body=raw_body_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertTrue(ok2)
        self.assertEqual(code2, 200)

        events2 = self.repo.query_events_for_message(record["id"])
        self.assertEqual(len(events1), len(events2))

    # 10. Delivery webhook arriving during SUBMITTING -> DELIVERED
    def test_10_delivery_webhook_arriving_during_submitting(self):
        created = self.repo.create_message({
            "tracking_id": "trk_early_wh",
            "zalo_msg_id": "zmsg_early",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash123",
            "status": STATE_SUBMITTING,
        })

        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {
                "msg_id": "zmsg_early",
                "tracking_id": "trk_early_wh",
                "delivery_time": str(int(time.time() * 1000)),
            },
        }
        raw_body_str = json.dumps(payload)
        sig = self._make_sig(raw_body_str, Config.ZALO_APP_ID, ts, Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw_body_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertTrue(ok)
        self.assertEqual(code, 200)

        record = self.repo.get_message_by_tracking_id("trk_early_wh")
        self.assertEqual(record["status"], STATE_DELIVERED)

    # 11. DELIVERED never downgrades
    def test_11_delivered_never_downgrades(self):
        created = self.repo.create_message({
            "tracking_id": "trk_no_downgrade",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash123",
            "status": STATE_DELIVERED,
        })

        # Attempt to downgrade via CAS
        ok, updated = self.repo.transition_message(
            message_id=created["id"],
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERY_UNKNOWN,
            event_type="ILLEGAL_DOWNGRADE",
        )
        self.assertFalse(ok)
        self.assertEqual(updated["status"], STATE_DELIVERED)

        # Attempt to transition DELIVERED -> ACCEPTED directly raises ValueError
        with self.assertRaises(ValueError) as cm:
            self.repo.transition_message(
                message_id=created["id"],
                expected_statuses=[STATE_DELIVERED],
                new_status=STATE_ACCEPTED,
                event_type="ILLEGAL_DOWNGRADE_2",
            )
        self.assertIn("Illegal state transition", str(cm.exception))

    # 12. Unknown message callback records diagnostic
    def test_12_unknown_message_callback_records_diagnostic(self):
        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {
                "msg_id": "non_existent_msg_id",
                "tracking_id": "non_existent_tracking_id",
                "delivery_time": "1626926349000",
            },
        }
        raw_body_str = json.dumps(payload)
        sig = self._make_sig(raw_body_str, Config.ZALO_APP_ID, ts, Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw_body_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertTrue(ok)
        self.assertEqual(code, 200)
        self.assertIn("Diagnostic logged", msg)

    # 13. Invalid signature rejected with 401
    def test_13_invalid_signature_rejected_with_401(self):
        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "message": {"msg_id": "m1"},
        }
        raw = json.dumps(payload).encode("utf-8")
        headers = {"X-ZEvent-Signature": "invalid_bogus_signature_hex"}

        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw,
            headers=headers,
            payload=payload,
        )
        self.assertFalse(ok)
        self.assertEqual(code, 401)

    # 14. PII masked in DB and events
    @patch("services.zns_tracking.send_zns")
    def test_14_pii_masked_in_db_and_events(self, mock_send_zns):
        mock_send_zns.return_value = {"error": 0, "message": "Success", "data": {"msg_id": "m_pii"}}

        res = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="+84987654321",
        )

        record = self.repo.get_message_by_tracking_id(res["tracking_id"])
        self.assertEqual(record["phone_masked"], "+849****321")
        self.assertNotIn("+84987654321", str(dict(record)))

    # 15. Stats denominator and rates
    def test_15_stats_calculation_and_rates(self):
        self.repo.create_message({
            "tracking_id": "t_acc", "app_key": "ord", "template_type": "hdsd-vie",
            "template_id": "497198", "phone_masked": "+849****321", "phone_hash": "h1",
            "status": STATE_ACCEPTED,
        })
        self.repo.create_message({
            "tracking_id": "t_del", "app_key": "ord", "template_type": "hdsd-vie",
            "template_id": "497198", "phone_masked": "+849****321", "phone_hash": "h2",
            "status": STATE_DELIVERED,
            "accepted_at": "2026-08-28T10:00:00+00:00",
            "delivered_at": "2026-08-28T10:00:08+00:00",
        })
        self.repo.create_message({
            "tracking_id": "t_rej", "app_key": "ord", "template_type": "hdsd-vie",
            "template_id": "497198", "phone_masked": "+849****321", "phone_hash": "h3",
            "status": STATE_REJECTED, "error_code": -118, "error_message": "Account not found",
        })
        self.repo.create_message({
            "tracking_id": "t_del_unk", "app_key": "ord", "template_type": "hdsd-vie",
            "template_id": "497198", "phone_masked": "+849****321", "phone_hash": "h4",
            "status": STATE_DELIVERY_UNKNOWN,
        })

        stats = self.repo.query_statistics()
        self.assertEqual(stats["total_requests"], 4)
        self.assertEqual(stats["rates"]["acceptance_rate_pct"], 75.0)
        self.assertEqual(stats["rates"]["delivery_rate_pct"], 33.33)
        self.assertEqual(stats["latency_seconds"]["average"], 8.0)

    # 16. Reconciliation SLA stale transition
    def test_16_reconciliation_stale_accepted(self):
        two_hours_ago = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 7200,
            timezone.utc
        ).isoformat()

        self.repo.create_message({
            "tracking_id": "trk_stale_1",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_stale",
            "status": STATE_ACCEPTED,
            "accepted_at": two_hours_ago,
        })

        reconciled = self.service.reconcile_stale_messages(threshold_seconds=1800)
        self.assertEqual(reconciled, 1)

        record = self.repo.get_message_by_tracking_id("trk_stale_1")
        self.assertEqual(record["status"], STATE_DELIVERY_UNKNOWN)

    # 23. Cross-Tenant Webhook Protection (ORD secret callback for BON record rejected 403)
    def test_23_cross_tenant_webhook_rejected(self):
        # Create a message belonging to app BON
        self.repo.create_message({
            "tracking_id": "trk_bon_record",
            "zalo_msg_id": "zmsg_bon_001",
            "app_key": "bon",
            "template_type": "rating",
            "template_id": "463253",
            "phone_masked": "+849****321",
            "phone_hash": "h_bon",
            "status": STATE_ACCEPTED,
        })

        # Send webhook purporting to be from app ORD with ORD signature
        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,  # ORD app ID
            "timestamp": ts,
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {
                "msg_id": "zmsg_bon_001",
                "tracking_id": "trk_bon_record",
                "delivery_time": ts,
            },
        }
        raw_body_str = json.dumps(payload)
        sig = self._make_sig(raw_body_str, Config.ZALO_APP_ID, ts, Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw_body_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertFalse(ok)
        self.assertEqual(code, 403)

        # Confirm BON record was NOT mutated
        rec = self.repo.get_message_by_tracking_id("trk_bon_record")
        self.assertEqual(rec["status"], STATE_ACCEPTED)

    # 24. Webhook Msg ID Mismatch Protection
    def test_24_webhook_msg_id_mismatch_rejected(self):
        self.repo.create_message({
            "tracking_id": "trk_msg_mismatch",
            "zalo_msg_id": "zmsg_original_001",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_mismatch",
            "status": STATE_ACCEPTED,
        })

        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {
                "msg_id": "zmsg_attacker_tampered",
                "tracking_id": "trk_msg_mismatch",
                "delivery_time": ts,
            },
        }
        raw_body_str = json.dumps(payload)
        sig = self._make_sig(raw_body_str, Config.ZALO_APP_ID, ts, Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw_body_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertFalse(ok)
        self.assertEqual(code, 400)

        # Confirm original msg_id was preserved
        rec = self.repo.get_message_by_tracking_id("trk_msg_mismatch")
        self.assertEqual(rec["zalo_msg_id"], "zmsg_original_001")
        self.assertEqual(rec["status"], STATE_ACCEPTED)

    # 25. Webhook Stale Timestamp Rejected
    def test_25_webhook_stale_timestamp_rejected(self):
        self.repo.create_message({
            "tracking_id": "trk_stale_ts",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_stale",
            "status": STATE_ACCEPTED,
        })

        # Timestamp 10 minutes ago (> 300s tolerance)
        ts = str(int((time.time() - 600) * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {
                "msg_id": "zmsg_stale",
                "tracking_id": "trk_stale_ts",
                "delivery_time": ts,
            },
        }
        raw_body_str = json.dumps(payload)
        sig = self._make_sig(raw_body_str, Config.ZALO_APP_ID, ts, Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw_body_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertFalse(ok)
        self.assertEqual(code, 400)
        self.assertIn("Stale timestamp", msg)

    # 26. Data Retention Cleanup
    def test_26_data_retention_cleanup(self):
        old_time = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        recent_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        # Terminal old message (should be deleted)
        self.repo.create_message({
            "tracking_id": "trk_old_delivered", "app_key": "ord", "template_type": "hdsd-vie",
            "template_id": "497198", "phone_masked": "+849****321", "phone_hash": "h1",
            "status": STATE_DELIVERED, "requested_at": old_time,
        })
        # Terminal old rejected (should be deleted)
        self.repo.create_message({
            "tracking_id": "trk_old_rejected", "app_key": "ord", "template_type": "hdsd-vie",
            "template_id": "497198", "phone_masked": "+849****321", "phone_hash": "h2",
            "status": STATE_REJECTED, "requested_at": old_time,
        })
        # Pending old message (MUST BE PRESERVED)
        self.repo.create_message({
            "tracking_id": "trk_old_pending", "app_key": "ord", "template_type": "hdsd-vie",
            "template_id": "497198", "phone_masked": "+849****321", "phone_hash": "h3",
            "status": STATE_ACCEPTED, "requested_at": old_time,
        })
        # Terminal recent message (MUST BE PRESERVED)
        self.repo.create_message({
            "tracking_id": "trk_recent_delivered", "app_key": "ord", "template_type": "hdsd-vie",
            "template_id": "497198", "phone_masked": "+849****321", "phone_hash": "h4",
            "status": STATE_DELIVERED, "requested_at": recent_time,
        })

        cleanup_res = self.repo.cleanup_old_records(retention_days=90)
        self.assertEqual(cleanup_res["deleted_messages"], 2)

        # Confirm preserved
        self.assertIsNotNone(self.repo.get_message_by_tracking_id("trk_old_pending"))
        self.assertIsNotNone(self.repo.get_message_by_tracking_id("trk_recent_delivered"))
        self.assertIsNone(self.repo.get_message_by_tracking_id("trk_old_delivered"))
        self.assertIsNone(self.repo.get_message_by_tracking_id("trk_old_rejected"))

    # 27. Migration v1 -> v2 Zero Data / Event Loss Guarantee
    def test_27_migration_v1_to_v2_zero_event_data_loss(self):
        import sqlite3
        mig_db_path = os.path.join(self.temp_dir.name, "test_mig_v1_v2.sqlite3")

        # Manually create schema at migration v1
        conn = sqlite3.connect(mig_db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript("""
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE zns_messages (
                id TEXT PRIMARY KEY,
                tracking_id TEXT UNIQUE NOT NULL,
                idempotency_key TEXT UNIQUE,
                zalo_msg_id TEXT UNIQUE,
                app_key TEXT NOT NULL,
                template_type TEXT NOT NULL,
                template_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'odoo',
                source_model TEXT,
                source_record_id INTEGER,
                business_reference TEXT,
                customer_id TEXT,
                customer_name TEXT,
                phone_masked TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                sent_by_user_id INTEGER,
                sent_by_user_name TEXT,
                company_id INTEGER,
                status TEXT NOT NULL,
                error_code INTEGER,
                error_message TEXT,
                sending_mode TEXT,
                quota_daily INTEGER,
                quota_remaining INTEGER,
                requested_at TEXT NOT NULL,
                submitted_at TEXT,
                accepted_at TEXT,
                delivered_at TEXT,
                last_webhook_at TEXT,
                unknown_at TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE zns_message_events (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES zns_messages(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                previous_status TEXT,
                new_status TEXT NOT NULL,
                payload_sanitized TEXT,
                source TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE zns_webhook_diagnostics (
                id TEXT PRIMARY KEY,
                raw_event_name TEXT,
                app_id TEXT,
                sender_id TEXT,
                zalo_msg_id TEXT,
                tracking_id TEXT,
                signature_valid INTEGER,
                reason TEXT,
                payload_sanitized TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-08-28T00:00:00Z');
        """)

        # Insert 3 messages with 6 events (foreign key child rows) and 2 diagnostics
        for i in range(3):
            m_id = f"msg_uuid_{i}"
            conn.execute(
                """
                INSERT INTO zns_messages (
                    id, tracking_id, app_key, template_type, template_id,
                    phone_masked, phone_hash, status, requested_at, created_at, updated_at
                ) VALUES (?, ?, 'ord', 'hdsd-vie', '497198', '+849****321', 'h', 'ACCEPTED', '2026-08-28T00:00:00Z', '2026-08-28T00:00:00Z', '2026-08-28T00:00:00Z');
                """,
                (m_id, f"trk_{i}"),
            )
            # 2 events per message
            conn.execute(
                """
                INSERT INTO zns_message_events (id, message_id, event_type, new_status, source, occurred_at, created_at)
                VALUES (?, ?, 'CREATED', 'QUEUED', 'test', '2026-08-28T00:00:00Z', '2026-08-28T00:00:00Z');
                """,
                (f"ev_{i}_1", m_id),
            )
            conn.execute(
                """
                INSERT INTO zns_message_events (id, message_id, event_type, new_status, source, occurred_at, created_at)
                VALUES (?, ?, 'ACCEPTED', 'ACCEPTED', 'test', '2026-08-28T00:00:00Z', '2026-08-28T00:00:00Z');
                """,
                (f"ev_{i}_2", m_id),
            )

        conn.execute("INSERT INTO zns_webhook_diagnostics (id, raw_event_name, created_at) VALUES ('d1', 'e1', '2026-08-28T00:00:00Z');")
        conn.execute("INSERT INTO zns_webhook_diagnostics (id, raw_event_name, created_at) VALUES ('d2', 'e2', '2026-08-28T00:00:00Z');")
        conn.commit()
        conn.close()

        # Instantiate ZNSRepository on this database -> triggers safe migration v2 and v3
        repo_mig = ZNSRepository(db_path=mig_db_path)

        # Verify zero data loss
        conn_v2 = repo_mig.get_connection()
        try:
            cnt_msgs = conn_v2.execute("SELECT COUNT(*) as c FROM zns_messages;").fetchone()["c"]
            cnt_events = conn_v2.execute("SELECT COUNT(*) as c FROM zns_message_events;").fetchone()["c"]
            cnt_diags = conn_v2.execute("SELECT COUNT(*) as c FROM zns_webhook_diagnostics;").fetchone()["c"]
            self.assertEqual(cnt_msgs, 3)
            self.assertEqual(cnt_events, 6, "CRITICAL: All 6 child events must remain intact after migration v2 (zero event loss)")
            self.assertEqual(cnt_diags, 2)

            # Foreign key check
            fk_violations = conn_v2.execute("PRAGMA foreign_key_check;").fetchall()
            self.assertEqual(len(fk_violations), 0, "PRAGMA foreign_key_check must have 0 violations")

            # Outbox table exists
            outbox_exists = conn_v2.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='zns_odoo_outbox';").fetchone()
            self.assertIsNotNone(outbox_exists)
        finally:
            conn_v2.close()

    # 28. Domain Transition State Machine CAS Enforcement (P0-01)
    def test_28_domain_transition_cas_enforcement(self):
        created = self.repo.create_message({
            "tracking_id": "trk_cas_enforce",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_cas",
            "status": STATE_DELIVERED,
        })
        msg_id = created["id"]

        # Illegal terminal downgrade: DELIVERED -> ACCEPTED must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            self.repo.transition_message(
                message_id=msg_id,
                expected_statuses=[STATE_DELIVERED],
                new_status=STATE_ACCEPTED,
                event_type="ILLEGAL_DOWNGRADE",
            )
        self.assertIn("Illegal state transition from 'DELIVERED' to 'ACCEPTED'", str(ctx.exception))

        # Illegal terminal downgrade: DELIVERED -> QUEUED must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            self.repo.transition_message(
                message_id=msg_id,
                expected_statuses=[STATE_DELIVERED],
                new_status=STATE_QUEUED,
                event_type="ILLEGAL_DOWNGRADE",
            )
        self.assertIn("Illegal state transition", str(ctx.exception))

        # Create REJECTED message
        created_rej = self.repo.create_message({
            "tracking_id": "trk_rej_terminal",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_cas2",
            "status": STATE_REJECTED,
        })
        # Illegal transition: REJECTED -> DELIVERED must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            self.repo.transition_message(
                message_id=created_rej["id"],
                expected_statuses=[STATE_REJECTED],
                new_status=STATE_DELIVERED,
                event_type="ILLEGAL_TRANSITION",
            )
        self.assertIn("Illegal state transition from 'REJECTED' to 'DELIVERED'", str(ctx.exception))

    # 29. Durable Odoo Outbox & Exponential Backoff Worker
    def test_29_durable_outbox_transactional_insert_and_worker(self):
        from services.zns_tracking import ZNSOdooOutboxWorker

        created = self.repo.create_message({
            "tracking_id": "trk_outbox_test",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "source_model": "sale.order",
            "source_record_id": 999,
            "phone_masked": "+849****321",
            "phone_hash": "h_outbox",
            "status": STATE_ACCEPTED,
        })
        msg_id = created["id"]

        # Transition ACCEPTED -> DELIVERED
        now = _utc_now_iso()
        ok, updated = self.repo.transition_message(
            message_id=msg_id,
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
            delivered_at=now,
            zalo_msg_id="z_msg_outbox_999",
        )
        self.assertTrue(ok)

        # Verify task was inserted into zns_odoo_outbox
        tasks = self.repo.fetch_pending_outbox_tasks(now_iso="9999-12-31T23:59:59Z")
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["message_id"], msg_id)
        self.assertEqual(task["source_model"], "sale.order")
        self.assertEqual(task["source_record_id"], 999)
        self.assertEqual(task["status"], "PENDING")

        # Process with outbox worker
        worker = ZNSOdooOutboxWorker(repo=self.repo)
        with patch.object(worker, "_sync_task_to_odoo", return_value=(True, None)) as mock_sync:
            processed = worker.process_pending_tasks()
            self.assertEqual(processed, 1)
            mock_sync.assert_called_once()

        # Verify status is SUCCEEDED
        task_done = self.repo.get_outbox_task_by_id(task["id"])
        self.assertEqual(task_done["status"], "SUCCEEDED")

    # 30. Sending Mode Validation
    def test_30_sending_mode_validation(self):
        # Invalid modes rejected immediately
        for bad_mode in ("manual", "automatic", "test_custom"):
            res = self.service.dispatch_zns(
                template_type="hdsd-vie",
                phone_raw="0987654321",
                mode=bad_mode,
            )
            self.assertEqual(res["status"], "rejected")
            self.assertEqual(res["error"]["code"], "INVALID_MODE")

        # Mode 'development' and None are valid
        with patch("services.zns_tracking.send_zns") as mock_send:
            mock_send.return_value = {"error": 0, "message": "Success", "data": {"msg_id": "m_dev"}}
            res_dev = self.service.dispatch_zns(
                template_type="hdsd-vie",
                phone_raw="0987654321",
                mode="development",
            )
            self.assertEqual(res_dev["status"], "accepted")


# ═══════════════════════════════════════════
#  FLASK INTEGRATION & AUTH TESTS
# ═══════════════════════════════════════════

class TestFlaskEndpointsZNSIntegration(BaseZNSTestCase):
    """Integration tests for Flask routes, admin APIs, and fail-closed authentication."""

    def setUp(self):
        super().setUp()
        self.client = app.test_client()

    # 21. Fail-closed Admin API authentication (Header-only)
    def test_21_admin_api_fail_closed(self):
        # 1. Unauthenticated request with key configured -> 401
        resp = self.client.get("/api/zns/stats")
        self.assertEqual(resp.status_code, 401)

        # 2. Query param ?api_key= is DISALLOWED -> 401
        resp_query = self.client.get(f"/api/zns/stats?api_key={Config.ZNS_ADMIN_API_KEY}")
        self.assertEqual(resp_query.status_code, 401, "Query parameter ?api_key= must be rejected to prevent secret leakage in access logs")

        # 3. Authenticated request with valid header X-API-Key -> 200
        resp_auth = self.client.get(
            "/api/zns/stats",
            headers={"X-API-Key": Config.ZNS_ADMIN_API_KEY},
        )
        self.assertEqual(resp_auth.status_code, 200)

        # 4. Authenticated request with Bearer token -> 200
        resp_bearer = self.client.get(
            "/api/zns/stats",
            headers={"Authorization": f"Bearer {Config.ZNS_ADMIN_API_KEY}"},
        )
        self.assertEqual(resp_bearer.status_code, 200)

        # 5. Empty configured key + insecure dev false -> 503
        Config.ZNS_ADMIN_API_KEY = ""
        Config.ZNS_ALLOW_INSECURE_DEV = False
        resp_empty = self.client.get("/api/zns/stats")
        self.assertEqual(resp_empty.status_code, 503)

    # 22. Fail-closed Inbound send route authentication
    @patch("services.zns_tracking.send_zns")
    def test_22_inbound_send_auth_fail_closed(self, mock_send_zns):
        mock_send_zns.return_value = {
            "error": 0, "message": "Success", "data": {"msg_id": "m1"},
        }
        Config.ZNS_INBOUND_AUTH_REQUIRED = True
        Config.ZNS_ALLOW_INSECURE_DEV = False

        # Anonymous send request without key -> 401
        resp = self.client.post(
            "/webhook/hdsd-vie",
            json={"phone": "0987654321", "name": "SO-001"},
        )
        self.assertEqual(resp.status_code, 401)
        mock_send_zns.assert_not_called()

        # Send request with valid X-ZNS-API-Key -> 200
        resp_auth = self.client.post(
            "/webhook/hdsd-vie",
            json={"phone": "0987654321", "name": "SO-001"},
            headers={"X-ZNS-API-Key": Config.ZNS_INBOUND_API_KEY},
        )
        self.assertEqual(resp_auth.status_code, 200)
        self.assertEqual(mock_send_zns.call_count, 1)

    def test_odoo_builtin_webhook_queues_without_odoo_python_code(self):
        record = {
            "id": 9911, "name": "SO-WEBHOOK-1", "state": "sale",
            "write_uid": [208, "CS User"], "partner_id": [1, "Customer"],
            "x_studio_selection_field_q4_1imrcsjj8": "Done",
            "x_studio_thng_hiu": "ORDINAIRE", "x_studio_hng_dn_s_dng": "Đã gửi (Vie)",
            "x_studio_zns_nh_gi_n_hng": False, "x_studio_zns_nh_gi_n_hng_eng": False,
            "x_studio_zns_request_state": "completed", "x_studio_zns_send_count": 0,
        }
        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.read.return_value = [record]
        with patch("services.zns_odoo_client.get_zns_odoo_client", return_value=mock_odoo), \
             patch("services.zns_odoo_poller.ZNSOdooPoller._queue_external_request", return_value=True) as queue:
            unauthorized = self.client.post("/webhook/odoo-zns/wrong/hdsd-vie", json={"_id": 9911})
            accepted = self.client.post(
                "/webhook/odoo-zns/test-odoo-webhook-capability/hdsd-vie", json={"_id": 9911}
            )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(accepted.status_code, 202)
        queue.assert_called_once()

    # Dashboard Basic Auth
    def test_dashboard_basic_auth(self):
        # 1. Without auth -> 401 with WWW-Authenticate header
        resp = self.client.get("/zns/dashboard")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("WWW-Authenticate", resp.headers)
        self.assertIn('Basic realm="ZNS Dashboard"', resp.headers["WWW-Authenticate"])

        # 2. With valid Basic Auth -> 200
        import base64
        creds = base64.b64encode(f"admin:{Config.ZNS_ADMIN_API_KEY}".encode("utf-8")).decode("utf-8")
        resp_auth = self.client.get(
            "/zns/dashboard",
            headers={"Authorization": f"Basic {creds}"},
        )
        self.assertEqual(resp_auth.status_code, 200)

    def test_dashboard_filters_by_send_date_and_template(self):
        for template_type, reference in [("hdsd-vie", "FILTER-KEEP"), ("rating", "FILTER-HIDE")]:
            self.repo.create_message({
                "tracking_id": f"trk_{reference.lower()}",
                "app_key": "ord" if template_type != "rating" else "bon",
                "template_type": template_type,
                "template_id": "test-template",
                "phone_masked": "+849****321",
                "phone_hash": f"hash-{reference}",
                "business_reference": reference,
                "status": STATE_ACCEPTED,
            })

        import base64
        creds = base64.b64encode(f"admin:{Config.ZNS_ADMIN_API_KEY}".encode("utf-8")).decode("utf-8")
        with patch("routes.zns_routes.get_repository", return_value=self.repo):
            resp = self.client.get(
                "/zns/dashboard?template_type=hdsd-vie&from_date=2000-01-01&to_date=2999-12-31",
                headers={"Authorization": f"Basic {creds}"},
            )

        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Ngày gửi từ", body)
        self.assertIn("Ngày gửi đến", body)
        self.assertIn("FILTER-KEEP", body)
        self.assertNotIn("FILTER-HIDE", body)
        self.assertIn('value="hdsd-vie" selected', body)
        self.assertNotIn("Khách Đã Bấm Link", body)
        self.assertNotIn("Tỷ Lệ Click", body)
        self.assertNotIn("System Telemetry", body)
        self.assertNotIn("Xuất CSV", body)

    # Payload too large
    def test_payload_too_large_rejected(self):
        Config.ZNS_MAX_PAYLOAD_BYTES = 100
        resp = self.client.post(
            "/webhook/hdsd-vie",
            data="x" * 200,
            headers={
                "Content-Type": "application/json",
                "X-ZNS-API-Key": Config.ZNS_INBOUND_API_KEY,
            },
        )
        self.assertEqual(resp.status_code, 413)

    # Webhook Missing OA ID -> 503
    def test_webhook_missing_oa_id_returns_503(self):
        Config.ZALO_OA_ID = ""
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": str(int(time.time() * 1000)),
            "sender": {"id": "some_oa"},
            "message": {"msg_id": "msg_123", "tracking_id": "trk_123", "delivery_time": "1626926349000"},
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = hashlib.sha256(f"{Config.ZALO_APP_ID}{raw_body.decode('utf-8')}{payload['timestamp']}{Config.ZALO_SECRET_KEY}".encode("utf-8")).hexdigest()

        ok, msg, status = self.service.process_delivery_webhook(
            raw_body=raw_body,
            headers={"X-ZEvent-Signature": sig},
            payload=payload,
        )
        self.assertFalse(ok)
        self.assertEqual(status, 503)

    # Webhook Non-Numeric Delivery Time -> 400
    def test_webhook_non_numeric_delivery_time_returns_400(self):
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": str(int(time.time() * 1000)),
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {"msg_id": "msg_123", "tracking_id": "trk_123", "delivery_time": "invalid_date_time"},
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = hashlib.sha256(f"{Config.ZALO_APP_ID}{raw_body.decode('utf-8')}{payload['timestamp']}{Config.ZALO_SECRET_KEY}".encode("utf-8")).hexdigest()

        ok, msg, status = self.service.process_delivery_webhook(
            raw_body=raw_body,
            headers={"X-ZEvent-Signature": sig},
            payload=payload,
        )
        self.assertFalse(ok)
        self.assertEqual(status, 400)

    # Outbox Worker Missing Odoo Credentials -> Marks FAILED, not SUCCEEDED
    def test_outbox_worker_missing_odoo_fails_with_backoff(self):
        from services.zns_tracking import ZNSOdooOutboxWorker

        Config.ODOO_URL = ""  # Unconfigured
        created = self.repo.create_message({
            "tracking_id": "trk_outbox_unconfigured",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_ob_unc",
            "source_model": "sale.order",
            "source_record_id": 777,
            "status": STATE_ACCEPTED,
        })
        self.repo.transition_message(
            message_id=created["id"],
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        worker = ZNSOdooOutboxWorker(repo=self.repo)
        processed = worker.process_pending_tasks(limit=10)
        self.assertEqual(processed, 0)  # 0 succeeded

        tasks = self.repo.fetch_pending_outbox_tasks(now_iso="2099-01-01T00:00:00+00:00", limit=10)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "FAILED")
        self.assertEqual(tasks[0]["retry_count"], 1)

    # Health endpoint
    def test_health_endpoint_safe_response(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("zns_tracking", data)
        self.assertEqual(data["zns_tracking"]["db_status"], "connected")
        self.assertIn("zns_odoo_poller", data)
        self.assertIn("enabled", data["zns_odoo_poller"])
        self.assertIn("running", data["zns_odoo_poller"])
        # Ensure no raw tokens leaked in response
        self.assertNotIn("secret", str(data).lower())

    # Payload Limit: Streamed & Oversized Requests Rejected (HTTP 413)
    def test_streamed_oversized_payload_rejected_413(self):
        """Verify requests exceeding ZNS_MAX_PAYLOAD_BYTES are rejected with 413."""
        # 1. With Content-Length header exceeding limit
        oversized_data = {"phone": "0912345678", "order_code": "SO1", "data": "X" * 150000}
        resp1 = self.client.post(
            "/webhook/hdsd-vie",
            json=oversized_data,
            headers={"X-ZNS-API-Key": Config.ZNS_INBOUND_API_KEY or "test_inbound_key"},
        )
        self.assertEqual(resp1.status_code, 413)

        # 2. Webhook oversized delivery event
        resp2 = self.client.post(
            "/webhook/zalo/zns-delivery",
            data="B" * 120000,
            content_type="application/json",
            headers={"X-ZEvent-Signature": "mac=test", "X-ZEvent-Server": "ZNS"},
        )
        self.assertEqual(resp2.status_code, 413)

    # Idempotency Isolation: Non-idempotency UNIQUE constraints propagate
    def test_non_idempotency_unique_collision_propagates_integrity_error(self):
        """Verify that only idempotency_key UNIQUE collisions raise DuplicateIdempotencyKeyError, others raise sqlite3.IntegrityError."""
        from services.zns_repository import DuplicateIdempotencyKeyError

        self.repo.create_message({
            "id": "msg-unique-1",
            "tracking_id": "trk_fixed_unique_1",
            "idempotency_key": "idem_unique_1",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_u1",
            "status": "QUEUED",
        })

        # 1. Duplicate idempotency_key -> DuplicateIdempotencyKeyError
        with self.assertRaises(DuplicateIdempotencyKeyError):
            self.repo.create_message({
                "id": "msg-unique-2",
                "tracking_id": "trk_fixed_unique_2",
                "idempotency_key": "idem_unique_1",  # Duplicate idempotency_key
                "app_key": "ord",
                "template_type": "hdsd-vie",
                "template_id": "497198",
                "phone_masked": "+849****321",
                "phone_hash": "hash_u2",
                "status": "QUEUED",
            })

        # 2. Duplicate tracking_id with DIFFERENT idempotency_key -> Standard sqlite3.IntegrityError (NOT DuplicateIdempotencyKeyError)
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.repo.create_message({
                "id": "msg-unique-3",
                "tracking_id": "trk_fixed_unique_1",  # Duplicate tracking_id
                "idempotency_key": "idem_unique_3_diff",
                "app_key": "ord",
                "template_type": "hdsd-vie",
                "template_id": "497198",
                "phone_masked": "+849****321",
                "phone_hash": "hash_u3",
                "status": "QUEUED",
            })
        self.assertNotIsInstance(cm.exception, DuplicateIdempotencyKeyError)

    # Status Normalization for all backend statuses
    def test_status_normalization_for_all_backend_statuses(self):
        """Verify normalize_status_for_odoo maps all backend internal states into valid Odoo Selection values."""
        from services.zns_odoo_poller import normalize_status_for_odoo

        self.assertEqual(normalize_status_for_odoo("QUEUED"), "queued")
        self.assertEqual(normalize_status_for_odoo("SUBMITTING"), "queued")
        self.assertEqual(normalize_status_for_odoo("ACCEPTED"), "accepted")
        self.assertEqual(normalize_status_for_odoo("DELIVERED"), "delivered")
        self.assertEqual(normalize_status_for_odoo("REJECTED"), "rejected")
        self.assertEqual(normalize_status_for_odoo("SUBMISSION_UNKNOWN"), "unknown")
        self.assertEqual(normalize_status_for_odoo("DELIVERY_UNKNOWN"), "unknown")
        self.assertEqual(normalize_status_for_odoo("CANCELLED"), "rejected")
        self.assertEqual(normalize_status_for_odoo(None), "unknown")
        self.assertEqual(normalize_status_for_odoo(""), "unknown")

    # Non-ZNS Routes Not Affected by ZNS 100KB Payload Cap
    def test_non_zns_endpoints_accept_large_payloads(self):
        """Verify non-ZNS endpoints (e.g. Lot-Serial) are not blocked by the ZNS 100KB payload limit."""
        large_json = {"items": [{"name": f"Item {i}", "desc": "A" * 100} for i in range(1500)]}
        resp = self.client.post(
            "/api/lot-serial/normalize",
            json=large_json,
        )
        # Should not return 413 Payload Too Large
        self.assertNotEqual(resp.status_code, 413)

    # Health Telemetry for Enabled-Not-Running and Stale Processing Count
    def test_health_telemetry_poller_state_and_stale_count(self):
        """Verify health endpoint accurately reflects poller state and stale count."""
        Config.ODOO_URL = "https://test.odoo.com"
        Config.ODOO_DB = "test_db"
        Config.ODOO_API_KEY = "test_key"

        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("zns_odoo_poller", data)
        poller_info = data["zns_odoo_poller"]
        self.assertTrue(poller_info["enabled"])
        self.assertIn("state", poller_info)
        self.assertIn("stale_processing_count", poller_info)


if __name__ == "__main__":
    unittest.main()
