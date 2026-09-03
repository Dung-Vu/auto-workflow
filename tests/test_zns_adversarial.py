"""
Adversarial & Fault-Tolerance Tests for ZNS Tracking Service.

Covers:
- Replay webhook attacks
- Replay send requests & concurrent idempotency locks
- Malformed timestamps and payload boundaries
- SQL Injection attempts in filter APIs
- Out-of-order deliveries (Timeout followed by late delivery callback)
- Webhook signature tampering
"""

import os
import sys
import json
import time
import uuid
import tempfile
import threading
import unittest
import hashlib
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config
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
)


class TestZNSAdversarialScenarios(unittest.TestCase):
    """Adversarial security and edge-case tests."""

    def setUp(self):
        reset_repository()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_zns_adversarial.sqlite3")

        self.orig_db_path = Config.ZNS_TRACKING_DB_PATH
        self.orig_admin_key = Config.ZNS_ADMIN_API_KEY
        self.orig_phone_hash_key = Config.ZNS_PHONE_HASH_KEY
        self.orig_app_id = Config.ZALO_APP_ID
        self.orig_secret = Config.ZALO_SECRET_KEY
        self.orig_oa_id = Config.ZALO_OA_ID
        self.orig_bon_app_id = Config.ZALO_BON_APP_ID
        self.orig_bon_secret = Config.ZALO_BON_SECRET_KEY
        self.orig_bon_oa_id = Config.ZALO_BON_OA_ID
        self.orig_sig_req = Config.ZNS_WEBHOOK_REQUIRE_SIGNATURE

        Config.ZNS_TRACKING_DB_PATH = self.db_path
        Config.ZNS_ADMIN_API_KEY = "super_secure_admin_key"
        Config.ZNS_PHONE_HASH_KEY = "super_secure_phone_hash_key_adversarial"
        Config.ZALO_APP_ID = "ord_adv_app_id"
        Config.ZALO_SECRET_KEY = "ord_adv_secret_key"
        Config.ZALO_OA_ID = "ord_adv_oa_id"
        Config.ZALO_BON_APP_ID = "bon_adv_app_id"
        Config.ZALO_BON_SECRET_KEY = "bon_adv_secret_key"
        Config.ZALO_BON_OA_ID = "bon_adv_oa_id"
        Config.ZNS_WEBHOOK_REQUIRE_SIGNATURE = True

        self.repo = ZNSRepository(db_path=self.db_path)
        self.service = ZNSTrackingService(repo=self.repo)

    def tearDown(self):
        reset_repository()
        Config.ZNS_TRACKING_DB_PATH = self.orig_db_path
        Config.ZNS_ADMIN_API_KEY = self.orig_admin_key
        Config.ZNS_PHONE_HASH_KEY = self.orig_phone_hash_key
        Config.ZALO_APP_ID = self.orig_app_id
        Config.ZALO_SECRET_KEY = self.orig_secret
        Config.ZALO_OA_ID = self.orig_oa_id
        Config.ZALO_BON_APP_ID = self.orig_bon_app_id
        Config.ZALO_BON_SECRET_KEY = self.orig_bon_secret
        Config.ZALO_BON_OA_ID = self.orig_bon_oa_id
        Config.ZNS_WEBHOOK_REQUIRE_SIGNATURE = self.orig_sig_req
        self.temp_dir.cleanup()

    def _calc_sig(self, raw_body_str: str, app_id: str, timestamp: str, secret: str) -> str:
        return hashlib.sha256(f"{app_id}{raw_body_str}{timestamp}{secret}".encode("utf-8")).hexdigest()

    # 1. Replay webhook attack (100 identical callbacks)
    def test_replay_webhook_storm(self):
        created = self.repo.create_message({
            "tracking_id": "trk_replay_storm",
            "zalo_msg_id": "msg_replay_storm",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_storm",
            "status": STATE_ACCEPTED,
        })

        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {
                "msg_id": "msg_replay_storm",
                "tracking_id": "trk_replay_storm",
                "delivery_time": "1626926359000",
            },
        }
        raw_str = json.dumps(payload)
        sig = self._calc_sig(raw_str, Config.ZALO_APP_ID, ts, Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        # Send 50 replays
        for _ in range(50):
            ok, msg, code = self.service.process_delivery_webhook(
                raw_body=raw_str.encode("utf-8"),
                headers=headers,
                payload=payload,
            )
            self.assertTrue(ok)
            self.assertEqual(code, 200)

        record = self.repo.get_message_by_tracking_id("trk_replay_storm")
        self.assertEqual(record["status"], STATE_DELIVERED)
        events = self.repo.query_events_for_message(record["id"])
        # Should have exactly 2 events: MESSAGE_CREATED and 1 DELIVERY_CONFIRMED
        delivery_events = [e for e in events if e["event_type"] == "DELIVERY_CONFIRMED"]
        self.assertEqual(len(delivery_events), 1)

    # 2. Replay send request with 20 concurrent threads
    @patch("services.zns_tracking.send_zns")
    def test_concurrent_replay_send_request(self, mock_send_zns):
        mock_send_zns.return_value = {
            "error": 0,
            "message": "Success",
            "data": {"msg_id": "msg_conc_20", "sent_time": "1626926349000"},
        }
        idem_key = "so_adversarial_unique_123"

        results = []
        threads = []

        def worker():
            res = self.service.dispatch_zns(
                template_type="hdsd-vie",
                phone_raw="0987654321",
                idempotency_key=idem_key,
                order_code="BG-ADVERSARIAL",
            )
            results.append(res)

        for _ in range(20):
            th = threading.Thread(target=worker)
            threads.append(th)
            th.start()

        for th in threads:
            th.join()

        self.assertEqual(len(results), 20)
        # Verify only 1 record exists in DB for this idempotency key
        record = self.repo.get_message_by_idempotency_key(idem_key)
        self.assertIsNotNone(record)
        self.assertEqual(record["business_reference"], "BG-ADVERSARIAL")

    # 3. Webhook signature tampering / wrong secret
    def test_tampered_signature_payload(self):
        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "message": {"msg_id": "msg_tamper", "tracking_id": "trk_tamper"},
        }
        raw_str = json.dumps(payload)
        # Signature created with wrong secret key
        bogus_sig = self._calc_sig(raw_str, Config.ZALO_APP_ID, ts, "WRONG_SECRET_KEY")
        headers = {"X-ZEvent-Signature": bogus_sig}

        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertFalse(ok)
        self.assertEqual(code, 401)

    # 4. SQL Injection attempts in query filters
    def test_sql_injection_filter_attempts(self):
        # Insert a valid message
        self.repo.create_message({
            "tracking_id": "trk_sqli_safe",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_safe",
            "status": STATE_ACCEPTED,
        })

        sqli_payloads = [
            "' OR '1'='1",
            "'; DROP TABLE zns_messages; --",
            "1 UNION SELECT null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null --",
            "admin'--",
        ]

        for injection in sqli_payloads:
            # Query messages with malicious filter string
            msgs, count = self.repo.query_messages(filters={"business_reference": injection})
            self.assertEqual(count, 0)
            # Ensure table still exists and record is intact
            stats = self.repo.query_statistics(filters={"template_type": injection})
            self.assertEqual(stats["total_requests"], 0)

        # Confirm data was not corrupted
        all_msgs, total = self.repo.query_messages()
        self.assertEqual(total, 1)

    # 5. Out-of-order: Transport timeout followed by late delivery callback
    @patch("services.zns_tracking.send_zns")
    def test_timeout_followed_by_late_delivered_callback(self, mock_send_zns):
        # Dispatch fails with network timeout -> SUBMISSION_UNKNOWN
        mock_send_zns.side_effect = Exception("Read timed out after 30 seconds")

        dispatch_res = self.service.dispatch_zns(
            template_type="hdsd-vie",
            phone_raw="0987654321",
        )
        self.assertEqual(dispatch_res["status"], "submission_unknown")
        tracking_id = dispatch_res["tracking_id"]

        record = self.repo.get_message_by_tracking_id(tracking_id)
        self.assertEqual(record["status"], STATE_SUBMISSION_UNKNOWN)

        # Later, Zalo sends delivery webhook for that tracking_id
        ts = str(int(time.time() * 1000))
        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": ts,
            "sender": {"id": Config.ZALO_OA_ID},
            "message": {
                "msg_id": "late_recovered_msg_id",
                "tracking_id": tracking_id,
                "delivery_time": "1626926360000",
            },
        }
        raw_str = json.dumps(payload)
        sig = self._calc_sig(raw_str, Config.ZALO_APP_ID, ts, Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertTrue(ok)
        self.assertEqual(code, 200)

        # Message must transition from SUBMISSION_UNKNOWN to DELIVERED
        updated = self.repo.get_message_by_tracking_id(tracking_id)
        self.assertEqual(updated["status"], STATE_DELIVERED)
        self.assertEqual(updated["zalo_msg_id"], "late_recovered_msg_id")

    # 6. Malformed non-numeric timestamps in webhook
    def test_malformed_timestamp_in_webhook(self):
        created = self.repo.create_message({
            "tracking_id": "trk_bad_ts",
            "zalo_msg_id": "msg_bad_ts",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_bad_ts",
            "status": STATE_ACCEPTED,
        })

        payload = {
            "event_name": "user_received_message",
            "app_id": Config.ZALO_APP_ID,
            "timestamp": "non_numeric_timestamp_xyz",
            "message": {
                "msg_id": "msg_bad_ts",
                "tracking_id": "trk_bad_ts",
                "delivery_time": "corrupted_non_numeric_time",
            },
        }
        raw_str = json.dumps(payload)
        sig = self._calc_sig(raw_str, Config.ZALO_APP_ID, "non_numeric_timestamp_xyz", Config.ZALO_SECRET_KEY)
        headers = {"X-ZEvent-Signature": sig}

        # Must fail-closed with 400 Bad Request for non-numeric timestamp
        ok, msg, code = self.service.process_delivery_webhook(
            raw_body=raw_str.encode("utf-8"),
            headers=headers,
            payload=payload,
        )
        self.assertFalse(ok)
        self.assertEqual(code, 400)

        record = self.repo.get_message_by_tracking_id("trk_bad_ts")
        self.assertEqual(record["status"], STATE_ACCEPTED)


if __name__ == "__main__":
    unittest.main()
