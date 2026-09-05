import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from config import Config
from routes.zns_routes import zns_bp
from services.zns_click_tracking import create_click_token, verify_click_token
from services.zns_repository import ZNSRepository


class TestZNSClickTracking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ZNSRepository(os.path.join(self.tmp.name, "zns.sqlite3"))
        self.message = self.repo.create_message({
            "tracking_id": "bon-ord-click-test",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+847****286",
            "phone_hash": "hash",
            "status": "ACCEPTED",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_signed_token_rejects_tampering(self):
        with patch.object(Config, "ZNS_CLICK_SECRET", "test-secret"):
            token = create_click_token(self.message["id"])
            self.assertEqual(verify_click_token(token), self.message["id"])
            self.assertIsNone(verify_click_token(token[:-1] + ("A" if token[-1] != "A" else "B")))

    def test_status_api_reconciliation_promotes_accepted_to_delivered(self):
        from services.zns_tracking import ZNSTrackingService

        with patch.object(Config, "ZNS_STATUS_POLL_AFTER_SECONDS", 0), patch(
            "services.zns_tracking.get_zns_status",
            return_value={"error": 0, "data": {"status": 1, "delivery_time": "1788431419886"}},
        ):
            count = ZNSTrackingService(self.repo).reconcile_stale_messages(threshold_seconds=1800)
        updated = self.repo.get_message_by_id(self.message["id"])
        self.assertEqual(count, 1)
        self.assertEqual(updated["status"], "DELIVERED")
        self.assertIsNotNone(updated["delivered_at"])
        events = self.repo.query_events_for_message(self.message["id"])
        self.assertIn("DELIVERY_CONFIRMED_BY_STATUS_API", [e["event_type"] for e in events])

    def test_get_does_not_count_and_post_counts_then_redirects(self):
        app = Flask(__name__)
        app.register_blueprint(zns_bp)
        token_patch = patch.object(Config, "ZNS_CLICK_SECRET", "test-secret")
        repo_patch = patch("routes.zns_routes.get_repository", return_value=self.repo)
        sync_patch = patch("routes.zns_routes.sync_first_click_to_odoo")
        with token_patch, repo_patch, sync_patch as sync:
            token = create_click_token(self.message["id"])
            client = app.test_client()
            response = client.get(f"/zns/c/{token}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.repo.get_message_by_id(self.message["id"])["click_count"], 0)

            response = client.post(f"/zns/c/{token}", data=b"confirm")
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.location, Config.ZNS_CLICK_DESTINATIONS["hdsd-vie"])
            updated = self.repo.get_message_by_id(self.message["id"])
            self.assertEqual(updated["click_count"], 1)
            self.assertIsNotNone(updated["clicked_at"])
            sync.assert_called_once()

            client.post(f"/zns/c/{token}", data=b"confirm")
            self.assertEqual(self.repo.get_message_by_id(self.message["id"])["click_count"], 2)
            sync.assert_called_once()

            stats = self.repo.query_statistics()
            self.assertEqual(stats["counts"]["clicked"], 1)
            self.assertEqual(stats["counts"]["total_clicks"], 2)
            self.assertEqual(stats["rates"]["click_rate_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
