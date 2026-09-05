"""
Deterministic Concurrency & State Machine Integrity Tests for ZNS Tracking.

Uses threading.Barrier and threading.Event to mathematically prove race-condition
immunity between Webhooks, Reconciliation, and Send dispatchers.
"""

import os
import sys
import time
import json
import sqlite3
import threading
import tempfile
import uuid
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config
from services.zns_repository import ZNSRepository, reset_repository


def _mp_migration_worker(path):
    """Top-level worker function for multiprocessing tests on macOS."""
    import sqlite3
    from services.zns_repository import ZNSRepository
    repo_inst = ZNSRepository(db_path=path)
    c = repo_inst.get_connection()
    rows = c.execute("SELECT version FROM schema_migrations ORDER BY version ASC;").fetchall()
    c.close()
    assert len(rows) >= 4
from services.zns_tracking import (
    ZNSTrackingService,
    STATE_QUEUED,
    STATE_SUBMITTING,
    STATE_ACCEPTED,
    STATE_DELIVERED,
    STATE_DELIVERY_UNKNOWN,
    STATE_SUBMISSION_UNKNOWN,
    STATE_REJECTED,
)


class TestDeterministicConcurrency(unittest.TestCase):
    """
    Deterministic concurrency test suite using threading.Barrier & Events
    to mathematically verify race condition immunity and exact-once execution.
    """

    def setUp(self):
        reset_repository()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_concurrency_cas.sqlite3")
        Config.ZNS_TRACKING_DB_PATH = self.db_path
        Config.ZNS_ALLOW_INSECURE_DEV = True
        self.repo = ZNSRepository(db_path=self.db_path)
        self.service = ZNSTrackingService(repo=self.repo)

    def tearDown(self):
        reset_repository()
        self.temp_dir.cleanup()

    # 1. Deterministic Webhook vs Reconciliation Race (Barrier sync)
    def test_deterministic_reconciliation_vs_webhook_race(self):
        """
        Verify that when Reconciliation fetches an ACCEPTED message,
        and Webhook delivers it before Reconciliation updates DB,
        the CAS mechanism PREVENTS downgrade to DELIVERY_UNKNOWN.
        """
        created = self.repo.create_message({
            "tracking_id": "trk_race_001",
            "zalo_msg_id": "zmsg_race_001",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_race_1",
            "status": STATE_ACCEPTED,
            "accepted_at": "2026-08-28T00:00:00+00:00",
        })
        msg_id = created["id"]

        read_stale_done = threading.Event()
        webhook_delivered = threading.Event()
        reconcile_result = []

        def worker_reconciliation():
            # 1. Fetch stale messages
            stale_list = self.repo.get_stale_accepted_messages(threshold_seconds=0)
            target = next(m for m in stale_list if m["id"] == msg_id)

            # 2. Signal that stale list has been read
            read_stale_done.set()

            # 3. Wait until Webhook finishes delivering before attempting CAS transition
            webhook_delivered.wait(timeout=5.0)

            # 4. Resume and attempt CAS transition to DELIVERY_UNKNOWN
            ok, record = self.repo.transition_message(
                message_id=target["id"],
                expected_statuses=[STATE_ACCEPTED],
                new_status=STATE_DELIVERY_UNKNOWN,
                event_type="DELIVERY_SLA_EXCEEDED",
                event_source="reconciliation",
            )
            reconcile_result.append((ok, record))

        def worker_webhook():
            # 1. Wait until reconciliation thread has read the stale list
            read_stale_done.wait(timeout=5.0)

            # 2. Webhook delivers message
            ok, record = self.repo.transition_message(
                message_id=msg_id,
                expected_statuses=[STATE_ACCEPTED, STATE_SUBMITTING],
                new_status=STATE_DELIVERED,
                event_type="DELIVERY_CONFIRMED",
                event_source="zalo_webhook",
            )
            self.assertTrue(ok)
            self.assertEqual(record["status"], STATE_DELIVERED)

            # 3. Signal delivery completed
            webhook_delivered.set()

        t_recon = threading.Thread(target=worker_reconciliation)
        t_hook = threading.Thread(target=worker_webhook)

        t_recon.start()
        t_hook.start()
        t_recon.join()
        t_hook.join()

        # Reconciliation CAS must fail (ok=False) because status was no longer ACCEPTED
        self.assertFalse(reconcile_result[0][0])

        # DB status must strictly stay DELIVERED
        final_record = self.repo.get_message_by_id(msg_id)
        self.assertEqual(final_record["status"], STATE_DELIVERED)

        # Timeline must have DELIVERY_CONFIRMED and NO DELIVERY_SLA_EXCEEDED
        events = self.repo.query_events_for_message(msg_id)
        event_types = [e["event_type"] for e in events]
        self.assertIn("DELIVERY_CONFIRMED", event_types)
        self.assertNotIn("DELIVERY_SLA_EXCEEDED", event_types)

    # 2. Deterministic Webhook vs Send-Return Race (Barrier sync)
    def test_deterministic_webhook_vs_send_return_race(self):
        """
        Verify that if a delivery webhook arrives while send_zns is in-flight,
        the delayed response returning ACCEPTED does NOT downgrade DELIVERED back to ACCEPTED.
        """
        created = self.repo.create_message({
            "tracking_id": "trk_race_002",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_race_2",
            "status": STATE_SUBMITTING,
        })
        msg_id = created["id"]

        barrier = threading.Barrier(2)

        def worker_send_return():
            # Send API returned error=0 (ACCEPTED)
            barrier.wait()
            # Try to transition SUBMITTING -> ACCEPTED
            ok, record = self.repo.transition_message(
                message_id=msg_id,
                expected_statuses=[STATE_SUBMITTING],
                new_status=STATE_ACCEPTED,
                event_type="ZALO_ACCEPTED",
                event_source="zalo_api",
            )
            # CAS must fail because status was already changed to DELIVERED
            self.assertFalse(ok)

        def worker_webhook_arrival():
            # Webhook arrives first
            ok, record = self.repo.transition_message(
                message_id=msg_id,
                expected_statuses=[STATE_SUBMITTING],
                new_status=STATE_DELIVERED,
                event_type="DELIVERY_CONFIRMED",
                event_source="zalo_webhook",
            )
            self.assertTrue(ok)
            barrier.wait()

        t1 = threading.Thread(target=worker_send_return)
        t2 = threading.Thread(target=worker_webhook_arrival)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        final = self.repo.get_message_by_id(msg_id)
        self.assertEqual(final["status"], STATE_DELIVERED)

    # 3. Strict SQLite CHECK Constraint Enforcement Test
    def test_sqlite_check_constraints(self):
        """Ensure invalid status or app_key raises IntegrityError at SQLite level."""
        conn = self.repo.get_connection()
        try:
            # 1. Invalid status
            with self.assertRaises(sqlite3.IntegrityError):
                with conn:
                    conn.execute(
                        "INSERT INTO zns_messages (id, tracking_id, app_key, template_type, template_id, phone_masked, phone_hash, status, requested_at, created_at, updated_at) "
                        "VALUES ('id_bad_1', 'trk_bad_1', 'ord', 'tpl', '123', '***', 'h', 'INVALID_STATUS', 'now', 'now', 'now');"
                    )

            # 2. Invalid app_key
            with self.assertRaises(sqlite3.IntegrityError):
                with conn:
                    conn.execute(
                        "INSERT INTO zns_messages (id, tracking_id, app_key, template_type, template_id, phone_masked, phone_hash, status, requested_at, created_at, updated_at) "
                        "VALUES ('id_bad_2', 'trk_bad_2', 'invalid_app', 'tpl', '123', '***', 'h', 'QUEUED', 'now', 'now', 'now');"
                    )
        finally:
            conn.close()

    # 4. Crash Reaper Recovery Test
    def test_crash_reaper_stale_in_flight(self):
        """Ensure orphan QUEUED and dead SUBMITTING records are cleanly recovered."""
        # Create an orphan QUEUED message (age: 10 mins)
        self.repo.create_message({
            "tracking_id": "trk_orphan_queued",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_q",
            "status": STATE_QUEUED,
            "requested_at": "2026-08-28T00:00:00+00:00",
        })
        # Create a dead SUBMITTING message (age: 10 mins)
        self.repo.create_message({
            "tracking_id": "trk_dead_submitting",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "h_s",
            "status": STATE_SUBMITTING,
            "requested_at": "2026-08-28T00:00:00+00:00",
            "submitted_at": "2026-08-28T00:00:00+00:00",
        })

        reap_stats = self.service.reap_stale_in_flight_messages(queued_timeout_seconds=60, submitting_timeout_seconds=60)
        self.assertEqual(reap_stats["reaped_queued"], 1)
        self.assertEqual(reap_stats["reaped_submitting"], 1)

        rec_q = self.repo.get_message_by_tracking_id("trk_orphan_queued")
        rec_s = self.repo.get_message_by_tracking_id("trk_dead_submitting")
        self.assertEqual(rec_q["status"], STATE_SUBMISSION_UNKNOWN)
        self.assertEqual(rec_s["status"], STATE_SUBMISSION_UNKNOWN)

    # 5. Strict 20 Concurrent Threads Same Idempotency Key -> Exactly 1 Send
    @patch("services.zns_tracking.send_zns")
    def test_strict_exact_once_concurrency(self, mock_send_zns):
        mock_send_zns.return_value = {
            "error": 0,
            "message": "Success",
            "data": {"msg_id": "zmsg_exact_once", "sent_time": "1626926349000"},
        }
        idem_key = "so_exact_once_lock_test"

        results = []
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            res = self.service.dispatch_zns(
                template_type="hdsd-vie",
                phone_raw="0987654321",
                idempotency_key=idem_key,
                order_code="SO-EXACT-ONCE",
            )
            results.append(res)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 20)
        # MUST BE EXACTLY 1 CALL
        self.assertEqual(mock_send_zns.call_count, 1)

        # All 20 returned tracking_id must match
        tracking_ids = {r["tracking_id"] for r in results}
        self.assertEqual(len(tracking_ids), 1)

        # Exactly 1 primary response, 19 marked as duplicate
        duplicates = [r for r in results if r.get("is_duplicate")]
        self.assertEqual(len(duplicates), 19)

    # 6. Concurrency 100 Iterations Stress Test (Zero Flakiness)
    @patch("services.zns_tracking.send_zns")
    def test_concurrency_100_iterations_exact_once(self, mock_send_zns):
        """Run 100 iterations of 10 concurrent threads to ensure zero race flakiness."""
        mock_send_zns.side_effect = lambda **kwargs: {
            "error": 0,
            "message": "Success",
            "data": {"msg_id": f"zmsg_100_rounds_{uuid.uuid4().hex}", "sent_time": "1626926349000"},
        }

        for i in range(100):
            idem_key = f"so_stress_100_round_{i}"
            results = []
            barrier = threading.Barrier(10)

            def worker():
                barrier.wait()
                res = self.service.dispatch_zns(
                    template_type="hdsd-vie",
                    phone_raw="0987654321",
                    idempotency_key=idem_key,
                    order_code=f"SO-STRESS-{i}",
                )
                results.append(res)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(results), 10, f"Round {i} failed: expected 10 results")
            self.assertEqual(mock_send_zns.call_count, i + 1, f"Round {i} upstream calls mismatch")

            # All 10 threads must receive valid responses
            for r in results:
                self.assertEqual(r["status"], "accepted")

    # 7. Raw SQL DB Trigger Invariant Test
    def test_raw_sql_db_trigger_invariant_enforcement(self):
        """Verify SQLite triggers abort illegal raw SQL updates bypassing the application layer."""
        created = self.repo.create_message({
            "tracking_id": "trk_trigger_test_01",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_trg_1",
            "status": STATE_DELIVERED,
        })
        msg_id = created["id"]

        conn = self.repo.get_connection()
        try:
            # DELIVERED -> ACCEPTED must fail
            with self.assertRaises(sqlite3.DatabaseError) as cm:
                with conn:
                    conn.execute("UPDATE zns_messages SET status = 'ACCEPTED' WHERE id = ?;", (msg_id,))
            self.assertIn("Illegal status transition", str(cm.exception))

            # REJECTED -> DELIVERED must fail
            created_rej = self.repo.create_message({
                "tracking_id": "trk_trigger_test_02",
                "app_key": "ord",
                "template_type": "hdsd-vie",
                "template_id": "497198",
                "phone_masked": "+849****321",
                "phone_hash": "hash_trg_2",
                "status": STATE_REJECTED,
            })
            with self.assertRaises(sqlite3.DatabaseError) as cm:
                with conn:
                    conn.execute("UPDATE zns_messages SET status = 'DELIVERED' WHERE id = ?;", (created_rej["id"],))
            self.assertIn("Illegal status transition", str(cm.exception))

            # CANCELLED -> DELIVERED must fail
            created_can = self.repo.create_message({
                "tracking_id": "trk_trigger_test_03",
                "app_key": "ord",
                "template_type": "hdsd-vie",
                "template_id": "497198",
                "phone_masked": "+849****321",
                "phone_hash": "hash_trg_3",
                "status": "CANCELLED",
            })
            with self.assertRaises(sqlite3.DatabaseError) as cm:
                with conn:
                    conn.execute("UPDATE zns_messages SET status = 'DELIVERED' WHERE id = ?;", (created_can["id"],))
            self.assertIn("Illegal status transition", str(cm.exception))
        finally:
            conn.close()

    # 8. Outbox Lease Contention & Worker Isolation
    def test_outbox_dual_worker_lease_contention(self):
        """Verify atomic lease claiming prevents dual workers from executing the same outbox task."""
        created = self.repo.create_message({
            "tracking_id": "trk_outbox_lease_01",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_ob_1",
            "source_model": "sale.order",
            "source_record_id": 999,
            "status": STATE_ACCEPTED,
        })
        msg_id = created["id"]

        # Transition to DELIVERED queues outbox task
        self.repo.transition_message(
            message_id=msg_id,
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        # Worker 1 claims task
        worker_1_tasks = self.repo.claim_outbox_tasks(worker_id="worker-A", batch_size=10, lease_duration_seconds=60)
        self.assertEqual(len(worker_1_tasks), 1)
        self.assertEqual(worker_1_tasks[0]["lease_owner"], "worker-A")

        # Worker 2 attempts to claim same task concurrently -> gets 0 tasks
        worker_2_tasks = self.repo.claim_outbox_tasks(worker_id="worker-B", batch_size=10, lease_duration_seconds=60)
        self.assertEqual(len(worker_2_tasks), 0)

        # Worker 2 attempts to complete worker 1's task -> rejected
        task_id = worker_1_tasks[0]["id"]
        rejected = self.repo.complete_outbox_task(task_id=task_id, worker_id="worker-B", success=True)
        self.assertFalse(rejected)

        # Worker 1 completes task -> succeeded
        completed = self.repo.complete_outbox_task(task_id=task_id, worker_id="worker-A", success=True)
        self.assertTrue(completed)

        task_after = self.repo.get_outbox_task_by_id(task_id)
        self.assertEqual(task_after["status"], "SUCCEEDED")
        self.assertIsNone(task_after["lease_owner"])

    # 9. Bounded TTL Cache Hard-Cap Capacity Test
    def test_bounded_ttl_cache_capacity_hard_cap(self):
        """Verify inserting 2,000 items into BoundedTTLCache strictly caps size at maxsize."""
        from services.zns_tracking import BoundedTTLCache

        cache = BoundedTTLCache(maxsize=500, ttl_seconds=60.0)
        for i in range(2000):
            cache.set(f"key_{i}", f"value_{i}")

        self.assertLessEqual(cache.size(), 500)
        self.assertLessEqual(len(cache), 500)
        # Recent keys must be present
        self.assertEqual(cache.get("key_1999"), "value_1999")
        # Oldest keys must be evicted
        self.assertIsNone(cache.get("key_0"))

    # 10. Data Retention Preserves Messages with Active Outbox Tasks
    def test_retention_preserves_messages_with_pending_outbox(self):
        """Verify cleanup_old_records does not delete messages that still have pending or processing outbox tasks."""
        # Create message with past date
        created = self.repo.create_message({
            "tracking_id": "trk_retention_pending_outbox",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_ret_1",
            "source_model": "sale.order",
            "source_record_id": 888,
            "status": STATE_ACCEPTED,
            "created_at": "2020-01-01T00:00:00+00:00",
        })
        msg_id = created["id"]

        self.repo.transition_message(
            message_id=msg_id,
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        # Manually backdate message created_at in DB
        conn = self.repo.get_connection()
        try:
            with conn:
                conn.execute("UPDATE zns_messages SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?;", (msg_id,))
        finally:
            conn.close()

        # Run retention cleanup
        res = self.repo.cleanup_old_records(retention_days=90)
        self.assertEqual(res["deleted_messages"], 0)

        # Message must still exist
        msg = self.repo.get_message_by_id(msg_id)
        self.assertIsNotNone(msg)

        # Once outbox is marked SUCCEEDED and backdated, cleanup removes it
        tasks = self.repo.claim_outbox_tasks("worker-test", batch_size=10)
        self.repo.complete_outbox_task(tasks[0]["id"], "worker-test", success=True)

        res2 = self.repo.cleanup_old_records(retention_days=90)
        self.assertEqual(res2["deleted_messages"], 1)
        self.assertIsNone(self.repo.get_message_by_id(msg_id))


    # 11. Outbox Lease Expiry Recovery (Crash Reaper for Outbox)
    def test_outbox_expired_lease_reclaimed_by_new_worker(self):
        """Verify that if a worker crashes holding a lease, another worker can reclaim the expired lease."""
        created = self.repo.create_message({
            "tracking_id": "trk_outbox_crash_reclaim",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_ob_crash",
            "source_model": "sale.order",
            "source_record_id": 111,
            "status": STATE_ACCEPTED,
        })
        msg_id = created["id"]

        self.repo.transition_message(
            message_id=msg_id,
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        # Worker 1 claims task with 0 second lease (immediately expired)
        tasks_w1 = self.repo.claim_outbox_tasks(worker_id="crashed-worker-1", batch_size=10, lease_duration_seconds=0)
        self.assertEqual(len(tasks_w1), 1)
        self.assertEqual(tasks_w1[0]["lease_owner"], "crashed-worker-1")

        # Worker 2 attempts to claim after lease expiry -> successfully reclaims expired task
        tasks_w2 = self.repo.claim_outbox_tasks(worker_id="healthy-worker-2", batch_size=10, lease_duration_seconds=60)
        self.assertEqual(len(tasks_w2), 1)
        self.assertEqual(tasks_w2[0]["lease_owner"], "healthy-worker-2")
        self.assertEqual(tasks_w2[0]["id"], tasks_w1[0]["id"])

    # 12. Concurrent Multi-Process Database Migration Safety
    def test_concurrent_multi_process_migrations(self):
        """Verify multiple threads / processes initializing the repository simultaneously all succeed without locking errors."""
        results = []
        barrier = threading.Barrier(5)

        def worker_init():
            barrier.wait()
            try:
                repo_inst = ZNSRepository(db_path=self.db_path)
                conn = repo_inst.get_connection()
                cur = conn.execute("SELECT MAX(version) FROM schema_migrations;")
                row = cur.fetchone()
                conn.close()
                results.append(row[0] >= 4)
            except Exception as e:
                results.append(e)

        threads = [threading.Thread(target=worker_init) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 5)
        self.assertTrue(all(r is True for r in results), f"Migration concurrency results: {results}")

    # 13. Odoo Poller Lifecycle & Claim Verification
    def test_odoo_poller_daemon_lifecycle(self):
        """Verify starting, status check, and graceful stopping of Odoo Poller daemon."""
        from services.zns_odoo_poller import start_odoo_poller, stop_odoo_poller, is_odoo_poller_running, get_odoo_poller_status

        # Configure mock Odoo credentials so poller can start
        Config.ODOO_URL = "https://odoo-mock.test"
        Config.ODOO_DB = "mock_db"
        Config.ODOO_API_KEY = "mock_key"

        # Initially stopped
        stop_odoo_poller()
        self.assertFalse(is_odoo_poller_running())

        # Start daemon
        started = start_odoo_poller(interval=0.1)
        self.assertTrue(started)
        self.assertTrue(is_odoo_poller_running())

        # Status check
        status = get_odoo_poller_status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["running"])

        # Starting again is idempotent
        started_again = start_odoo_poller(interval=0.1)
        self.assertFalse(started_again)

        # Stop daemon
        stopped = stop_odoo_poller()
        self.assertTrue(stopped)
        self.assertFalse(is_odoo_poller_running())


    # 14. True Multi-Process Migration Starting at Schema v1
    def test_true_multiprocessing_migration_from_schema_v1(self):
        """
        Verify real OS processes (multiprocessing.Process) migrating concurrently
        from schema v1 to the latest version all exit with code 0, without lock conflicts or duplicates.
        """
        import multiprocessing

        # Setup isolated DB at schema v1 only
        mp_db_path = os.path.join(self.temp_dir.name, "test_mp_migration.sqlite3")
        conn = sqlite3.connect(mp_db_path)
        try:
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);")
            conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-08-28T00:00:00+00:00');")
            conn.execute("""
                CREATE TABLE zns_messages (
                    id TEXT PRIMARY KEY, tracking_id TEXT UNIQUE NOT NULL, idempotency_key TEXT UNIQUE,
                    zalo_msg_id TEXT UNIQUE, app_key TEXT NOT NULL, template_type TEXT NOT NULL,
                    template_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'odoo', source_model TEXT,
                    source_record_id INTEGER, business_reference TEXT, customer_id TEXT, customer_name TEXT,
                    phone_masked TEXT NOT NULL, phone_hash TEXT NOT NULL, sent_by_user_id INTEGER,
                    sent_by_user_name TEXT, company_id INTEGER, status TEXT NOT NULL, error_code INTEGER,
                    error_message TEXT, sending_mode TEXT, quota_daily INTEGER, quota_remaining INTEGER,
                    requested_at TEXT NOT NULL, submitted_at TEXT, accepted_at TEXT, delivered_at TEXT,
                    last_webhook_at TEXT, unknown_at TEXT, retry_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE zns_message_events (
                    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    previous_status TEXT, new_status TEXT NOT NULL, payload_sanitized TEXT,
                    source TEXT NOT NULL, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE zns_webhook_diagnostics (
                    id TEXT PRIMARY KEY, raw_event_name TEXT, app_id TEXT, sender_id TEXT,
                    zalo_msg_id TEXT, tracking_id TEXT, signature_valid INTEGER, reason TEXT,
                    payload_sanitized TEXT, created_at TEXT NOT NULL
                );
            """)
            conn.commit()
        finally:
            conn.close()

        processes = [
            multiprocessing.Process(target=_mp_migration_worker, args=(mp_db_path,))
            for _ in range(4)
        ]
        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=10.0)

        # All processes must exit with code 0
        for p in processes:
            self.assertEqual(p.exitcode, 0, f"Process {p.pid} failed with exitcode {p.exitcode}")

        # Verify DB integrity: versions 1..4 present exactly once, no temporary tables
        conn_check = sqlite3.connect(mp_db_path)
        try:
            versions = [r[0] for r in conn_check.execute("SELECT version FROM schema_migrations ORDER BY version ASC;").fetchall()]
            self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7])
            temp_tables = conn_check.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_v2';").fetchall()
            self.assertEqual(len(temp_tables), 0)
        finally:
            conn_check.close()

    # 15. Poller Claim Failure Guard: NEVER Dispatches ZNS
    def test_odoo_poller_claim_failure_does_not_call_dispatch(self):
        """Verify that when claiming an Odoo record fails, dispatch_zns is NEVER called."""
        from services.zns_odoo_poller import ZNSOdooPoller

        mock_tracking = MagicMock()
        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.search_read.return_value = [{
            "id": 555,
            "name": "SO555",
            "partner_id": [1, "Test Partner"],
            "x_studio_phone": "0987654321",
            "x_studio_tn_khch_hng": "Test Customer",
            "x_studio_zns_last_template": "hdsd-vie",
            "x_studio_zns_send_count": 1,
            "date_order": "2026-08-28",
            "company_id": [1, "Ordinaire"],
        }]
        # Claim write fails
        mock_odoo.write.side_effect = Exception("Odoo concurrent write conflict / Record locked")

        poller = ZNSOdooPoller(tracking_service=mock_tracking, odoo_client=mock_odoo)
        dispatched = poller.poll_and_dispatch()

        self.assertEqual(dispatched, 0)
        mock_tracking.dispatch_zns.assert_not_called()

    # 16. Outbox Slow Worker Exceeding Lease Cannot Complete Task
    def test_outbox_slow_worker_exceeding_lease_cannot_complete_task(self):
        """Verify that a worker whose lease expired cannot complete the task after another worker claimed it."""
        created = self.repo.create_message({
            "tracking_id": "trk_slow_worker_lease",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_ob_slow",
            "source_model": "sale.order",
            "source_record_id": 444,
            "status": STATE_ACCEPTED,
        })
        msg_id = created["id"]

        self.repo.transition_message(
            message_id=msg_id,
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        # Worker 1 claims task with 0 second lease (immediately expired)
        tasks_w1 = self.repo.claim_outbox_tasks(worker_id="slow-worker-1", batch_size=10, lease_duration_seconds=0)
        self.assertEqual(len(tasks_w1), 1)

        # Worker 2 reclaims expired task
        tasks_w2 = self.repo.claim_outbox_tasks(worker_id="fast-worker-2", batch_size=10, lease_duration_seconds=60)
        self.assertEqual(len(tasks_w2), 1)
        self.assertEqual(tasks_w2[0]["lease_owner"], "fast-worker-2")

        # Worker 1 attempts to complete task after losing ownership -> REJECTED
        completed_w1 = self.repo.complete_outbox_task(tasks_w1[0]["id"], worker_id="slow-worker-1", success=True)
        self.assertFalse(completed_w1)

        # Worker 2 completes task -> SUCCEEDED
        completed_w2 = self.repo.complete_outbox_task(tasks_w2[0]["id"], worker_id="fast-worker-2", success=True)
        self.assertTrue(completed_w2)

        task_final = self.repo.get_outbox_task_by_id(tasks_w1[0]["id"])
        self.assertEqual(task_final["status"], "SUCCEEDED")

    # 17. Deterministic Two-Poller Claim Race: Exactly 1 Dispatch Invocation
    def test_two_pollers_concurrent_claim_race_exact_once_dispatch(self):
        """
        Verify that when 2 pollers concurrently attempt to claim the exact same pending record
        (where Poller B holds a stale snapshot), the SQLite durable queue guarantees
        dispatch_zns is called EXACTLY ONCE and upstream Zalo is called EXACTLY ONCE.
        """
        from services.zns_odoo_poller import ZNSOdooPoller

        mock_tracking = MagicMock()
        mock_tracking.dispatch_zns.return_value = {
            "status": "ACCEPTED",
            "msg_id": "zalo_msg_777",
            "tracking_id": "trk_777",
            "message_id": "msg_db_777",
        }

        # Shared mock Odoo database state for record SO777
        record_state = {
            "id": 777,
            "name": "SO777",
            "partner_id": [1, "Customer 777"],
            "x_studio_phone": "0912345678",
            "x_studio_tn_khch_hng": "Customer 777",
            "x_studio_zns_last_template": "hdsd-vie",
            "x_studio_zns_send_count": 1,
            "x_studio_zns_sent_by_user_id": 208,
            "x_studio_zns_sent_by_user_name": "Vũ Đình Dũng",
            "date_order": "2026-08-28",
            "company_id": [1, "Ordinaire"],
            "x_studio_zns_request_state": "pending",
            "x_studio_zns_claim_token": None,
            "x_studio_zns_claim_owner": None,
            "x_studio_zns_processing_started_at": None,
        }

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.search_read.return_value = [dict(record_state)]

        def mock_write(model, ids, vals):
            for k, v in vals.items():
                record_state[k] = v
            return True

        def mock_read(model, ids, fields):
            return [dict(record_state)]

        mock_odoo.write.side_effect = mock_write
        mock_odoo.read.side_effect = mock_read

        # Two pollers sharing the same real SQLite repository
        poller_a = ZNSOdooPoller(tracking_service=mock_tracking, odoo_client=mock_odoo, repo=self.repo, worker_id="poller-A")
        poller_b = ZNSOdooPoller(tracking_service=mock_tracking, odoo_client=mock_odoo, repo=self.repo, worker_id="poller-B")

        # Step 1: Poller A polls and dispatches (wins claim in SQLite queue)
        dispatched_a = poller_a.poll_and_dispatch(model="sale.order", limit=10)
        self.assertEqual(dispatched_a, 1)
        self.assertEqual(mock_tracking.dispatch_zns.call_count, 1)
        dispatch_kwargs = mock_tracking.dispatch_zns.call_args.kwargs
        self.assertEqual(dispatch_kwargs["sent_by_user_id"], 208)
        self.assertEqual(dispatch_kwargs["sent_by_user_name"], "Vũ Đình Dũng")
        self.assertEqual(dispatch_kwargs["order_date"], "28/08/2026")

        # Step 2: Poller B polls using stale snapshot (where search_read still returned SO777 pending)
        # Poller B must detect that SO777 v1 is already COMPLETED in SQLite, writeback to Odoo, and NOT dispatch!
        dispatched_b = poller_b.poll_and_dispatch(model="sale.order", limit=10)
        self.assertEqual(dispatched_b, 0)
        self.assertEqual(mock_tracking.dispatch_zns.call_count, 1)  # Still exactly 1!

        # Verify SQLite queue record is completed
        queue_req = self.repo.get_dispatch_request(
            source_model="sale.order",
            source_record_id=777,
            template_type="hdsd-vie",
            send_version=1,
        )
        self.assertIsNotNone(queue_req)
        self.assertEqual(queue_req["status"], "COMPLETED")
        self.assertEqual(queue_req["zalo_msg_id"], "zalo_msg_777")

    # 18. Stale Processing Recovery Test
    def test_odoo_poller_stale_processing_recovery(self):
        """Verify that records stuck in 'processing' older than 5 minutes are recovered back to 'pending'."""
        from services.zns_odoo_poller import ZNSOdooPoller

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True

        # Simulate 1 stale processing record (started 10 minutes ago) and 1 fresh processing record (started 1 min ago)
        now_dt = datetime.now(timezone.utc)
        stale_ts = (now_dt - timedelta(minutes=10)).isoformat()
        fresh_ts = (now_dt - timedelta(seconds=60)).isoformat()
        mock_odoo.search_read.return_value = [
            {
                "id": 881,
                "x_studio_zns_processing_started_at": stale_ts,
                "x_studio_zns_claim_token": "token_stale",
            },
            {
                "id": 882,
                "x_studio_zns_processing_started_at": fresh_ts,
                "x_studio_zns_claim_token": "token_fresh",
            }
        ]
        mock_odoo.write.return_value = True

        poller = ZNSOdooPoller(odoo_client=mock_odoo)
        # Recover with cutoff
        reclaimed = poller.recover_stale_processing_records(model="sale.order", stale_seconds=300)

        # 881 is stale (older than 300s) and gets recovered
        self.assertEqual(reclaimed, 1)
        mock_odoo.write.assert_called_once_with("sale.order", [881], {
            "x_studio_zns_request_state": "pending",
            "x_studio_zns_claim_token": False,
            "x_studio_zns_claim_owner": False,
            "x_studio_zns_processing_started_at": False,
        })

    # 19. Outbox Worker Heartbeat Lease Renewal
    def test_outbox_worker_heartbeat_lease_renewal(self):
        """Verify that renew_outbox_lease extends the lease expiration of an active task."""
        created = self.repo.create_message({
            "tracking_id": "trk_renew_lease_test",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_renew",
            "source_model": "sale.order",
            "source_record_id": 999,
            "status": STATE_ACCEPTED,
        })
        msg_id = created["id"]

        self.repo.transition_message(
            message_id=msg_id,
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        claimed = self.repo.claim_outbox_tasks(worker_id="heartbeat-worker", batch_size=1, lease_duration_seconds=30)
        self.assertEqual(len(claimed), 1)
        task_id = claimed[0]["id"]
        initial_expiry = claimed[0]["lease_expires_at"]

        # Heartbeat renew lease with 120s duration
        time.sleep(0.05)
        renewed = self.repo.renew_outbox_lease(task_id, worker_id="heartbeat-worker", lease_duration_seconds=120)
        self.assertTrue(renewed)

        task_after = self.repo.get_outbox_task_by_id(task_id)
        self.assertGreater(task_after["lease_expires_at"], initial_expiry)

        # Another worker cannot renew this task
        unauthorized_renew = self.repo.renew_outbox_lease(task_id, worker_id="imposter-worker", lease_duration_seconds=120)
        self.assertFalse(unauthorized_renew)

    def test_external_automation_bootstrap_does_not_backfill_and_new_signal_queues(self):
        """External detector snapshots existing orders, then queues only later transitions."""
        from services.zns_odoo_poller import ZNSOdooPoller

        rec = {
            "id": 9901, "name": "SO-EXT-1", "state": "sale", "write_date": "2026-09-04 00:00:01",
            "write_uid": [208, "CS User"], "partner_id": [1, "Customer"],
            "x_studio_selection_field_q4_1imrcsjj8": "Done",
            "x_studio_thng_hiu": "ORDINAIRE", "x_studio_hng_dn_s_dng": "Đã gửi (Vie)",
            "x_studio_zns_nh_gi_n_hng": False, "x_studio_zns_nh_gi_n_hng_eng": False,
            "x_studio_zns_request_state": "completed", "x_studio_zns_send_count": 0,
        }
        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.search_read.side_effect = [[rec], []]
        poller = ZNSOdooPoller(odoo_client=mock_odoo, repo=self.repo)
        state_path = os.path.join(self.temp_dir.name, "external_state.json")
        old_enabled = Config.ZNS_EXTERNAL_AUTOMATION_ENABLED
        old_path = Config.ZNS_EXTERNAL_AUTOMATION_STATE_PATH
        Config.ZNS_EXTERNAL_AUTOMATION_ENABLED = True
        Config.ZNS_EXTERNAL_AUTOMATION_STATE_PATH = state_path
        try:
            self.assertEqual(poller.discover_external_automation_requests(), 0)
            mock_odoo.write.assert_not_called()
            with open(state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["signals"]["9901"], ["hdsd-vie"])

            # A later transition adds the English rating signal.
            changed = dict(rec)
            changed["x_studio_zns_nh_gi_n_hng_eng"] = True
            mock_odoo.search_read.side_effect = [[changed]]
            with patch.object(poller, "_queue_external_request", return_value=True) as queue_spy:
                self.assertEqual(poller.discover_external_automation_requests(), 1)
            queue_spy.assert_called_once_with(changed, "rating-ord-eng")
        finally:
            Config.ZNS_EXTERNAL_AUTOMATION_ENABLED = old_enabled
            Config.ZNS_EXTERNAL_AUTOMATION_STATE_PATH = old_path

    def test_external_queue_rewrites_escaped_odoo_chatter_by_marker(self):
        """Odoo message_post return shape is irrelevant; marker lookup fixes escaped HTML."""
        from services.zns_odoo_poller import ZNSOdooPoller

        rec = {
            "id": 9902, "name": "SO-EXT-HTML", "write_uid": [208, "CS User"],
            "partner_id": [1, "Customer"], "x_studio_zns_send_count": 0,
        }
        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.search_read.side_effect = [[], [{"id": 88002}]]
        mock_odoo.message_post.return_value = True
        mock_odoo.write.return_value = True
        poller = ZNSOdooPoller(odoo_client=mock_odoo, repo=self.repo)

        self.assertTrue(poller._queue_external_request(rec, "rating-ord-vie"))
        mock_odoo.message_post.assert_called_once()
        mail_writes = [call for call in mock_odoo.write.call_args_list if call.args[0] == "mail.message"]
        self.assertEqual(len(mail_writes), 1)
        self.assertIn("<div", mail_writes[0].args[2]["body"])
        self.assertIn("zns_external_request_9902_rating-ord-vie_v1", mail_writes[0].args[2]["body"])

    # 20. Delivery updates the original request note and never posts a second note
    def test_outbox_updates_original_chatter_without_posting(self):
        """Delivery retries only update the existing queue note in place."""
        from services.zns_tracking import ZNSOdooOutboxWorker

        created = self.repo.create_message({
            "tracking_id": "trk_lost_resp_chatter",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_lost_resp",
            "source_model": "sale.order",
            "source_record_id": 888,
            "status": STATE_ACCEPTED,
        })
        msg_id = created["id"]

        self.repo.transition_message(
            message_id=msg_id,
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        queue_body = [
            "<div><p><b>YÊU CẦU GỬI ZALO ZNS</b> (Lần 1)</p>"
            "<ul><li><b>Trạng thái:</b> ⏳ Chờ xử lý</li></ul></div>"
        ]

        def write_ok(model, ids, vals, **kwargs):
            if model == "mail.message" and vals.get("body"):
                queue_body[0] = vals["body"]
            return True

        def search_read_mail(model, domain, fields=None, limit=0, order=None, **kwargs):
            if model == "mail.message":
                return [{"id": 999111, "body": queue_body[0]}]
            return []

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.write.side_effect = write_ok
        mock_odoo.search_read.side_effect = search_read_mail

        worker1 = ZNSOdooOutboxWorker(repo=self.repo)

        tasks_w1 = self.repo.claim_outbox_tasks(worker_id="w1", batch_size=1, lease_duration_seconds=0)
        self.assertEqual(len(tasks_w1), 1)
        task_id = tasks_w1[0]["id"]

        with patch("services.zns_odoo_client.get_zns_odoo_client", return_value=mock_odoo):
            Config.ODOO_URL = "https://test.odoo.com"
            Config.ODOO_DB = "test_db"
            Config.ODOO_API_KEY = "test_key"
            ok, err = worker1._sync_task_to_odoo(tasks_w1[0])
            self.assertTrue(ok, err)
            self.assertIn("DELIVERED", queue_body[0])
            mock_odoo.message_post.assert_not_called()

            tasks_w2 = self.repo.claim_outbox_tasks(worker_id="w2", batch_size=1, lease_duration_seconds=60)
            self.assertEqual(len(tasks_w2), 1)

            completed_w1 = self.repo.complete_outbox_task(task_id, worker_id="w1", success=True)
            self.assertFalse(completed_w1)

            ok2, err2 = worker1._sync_task_to_odoo(tasks_w2[0], worker_id="w2")
            self.assertTrue(ok2, err2)
            mock_odoo.message_post.assert_not_called()

            completed_w2 = self.repo.complete_outbox_task(task_id, worker_id="w2", success=True)
            self.assertTrue(completed_w2)

    def test_outbox_marker_lookup_failure_is_fail_closed(self):
        """Marker search errors must not fall through to a second message_post."""
        from services.zns_tracking import ZNSOdooOutboxWorker

        created = self.repo.create_message({
            "tracking_id": "trk_marker_fail_closed",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_marker_fail",
            "source_model": "sale.order",
            "source_record_id": 889,
            "status": STATE_ACCEPTED,
        })
        self.repo.transition_message(
            message_id=created["id"],
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.write.return_value = True
        mock_odoo.search_read.side_effect = RuntimeError("mail.message search unavailable")
        mock_odoo.message_post.return_value = 1

        worker = ZNSOdooOutboxWorker(repo=self.repo)
        tasks = self.repo.claim_outbox_tasks(worker_id="w-fail-closed", batch_size=1, lease_duration_seconds=60)
        self.assertEqual(len(tasks), 1)

        with patch("services.zns_odoo_client.get_zns_odoo_client", return_value=mock_odoo):
            Config.ODOO_URL = "https://test.odoo.com"
            Config.ODOO_DB = "test_db"
            Config.ODOO_API_KEY = "test_key"
            ok, err = worker._sync_task_to_odoo(tasks[0], worker_id="w-fail-closed")

        self.assertFalse(ok)
        self.assertIn("original queue chatter update failed", err or "")
        mock_odoo.message_post.assert_not_called()

    def test_outbox_sync_heartbeats_around_each_external_call(self):
        """Lease is renewed around the Odoo field write and task completion."""
        from services.zns_tracking import ZNSOdooOutboxWorker

        created = self.repo.create_message({
            "tracking_id": "trk_outbox_heartbeat_calls",
            "app_key": "ord",
            "template_type": "hdsd-vie",
            "template_id": "497198",
            "phone_masked": "+849****321",
            "phone_hash": "hash_hb_calls",
            "source_model": "sale.order",
            "source_record_id": 890,
            "status": STATE_ACCEPTED,
        })
        self.repo.transition_message(
            message_id=created["id"],
            expected_statuses=[STATE_ACCEPTED],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
        )

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.write.return_value = True
        mock_odoo.search_read.return_value = [{
            "id": 89001,
            "body": "<div><b>YÊU CẦU GỬI ZALO ZNS</b> (Lần 1)<li><b>Trạng thái:</b> ⏳ Chờ xử lý</li></div>",
        }]

        worker = ZNSOdooOutboxWorker(repo=self.repo)
        with patch("services.zns_odoo_client.get_zns_odoo_client", return_value=mock_odoo):
            Config.ODOO_URL = "https://test.odoo.com"
            Config.ODOO_DB = "test_db"
            Config.ODOO_API_KEY = "test_key"
            with patch.object(self.repo, "renew_outbox_lease", wraps=self.repo.renew_outbox_lease) as spy:
                processed = worker.process_pending_tasks(worker_id="hb-multi", limit=1)

        self.assertEqual(processed, 1)
        self.assertGreaterEqual(spy.call_count, 3)

    # 21. Real WSGI Chunked Payload Limit (No Content-Length)
    def test_flask_wsgi_chunked_stream_payload_cap(self):
        """
        Verify that chunked requests without Content-Length exceeding 100KB are rejected with 413
        using bounded stream reading to prevent memory explosion.
        """
        import io
        from app import app

        client = app.test_client()
        oversized_data = b"C" * 150000

        # Simulate chunked transfer encoding (no Content-Length header, stream input)
        env = {
            "wsgi.input": io.BytesIO(oversized_data),
            "CONTENT_TYPE": "application/json",
            "wsgi.input_terminated": True,
        }
        # In werkzeug test client, remove CONTENT_LENGTH to simulate chunked
        resp = client.post(
            "/webhook/hdsd-vie",
            headers={"X-ZNS-API-Key": Config.ZNS_INBOUND_API_KEY or "test_key"},
            environ_overrides=env,
        )
        self.assertEqual(resp.status_code, 413)

    def test_failed_odoo_telemetry_write_is_retried_on_next_poll(self):
        """FAILED durable queue rows must be reclaimed; a transient Odoo write must not strand the version."""
        from services.zns_odoo_poller import ZNSOdooPoller

        mock_tracking = MagicMock()
        mock_tracking.dispatch_zns.return_value = {
            "status": "ACCEPTED",
            "msg_id": "zalo_retry_901",
            "tracking_id": "trk_retry_901",
            "message_id": "msg_retry_901",
        }
        record = {
            "id": 901,
            "name": "SO901",
            "partner_id": [1, "Retry Customer"],
            "x_studio_phone": "0912345678",
            "x_studio_tn_khch_hng": "Retry Customer",
            "x_studio_zns_last_template": "hdsd-vie",
            "x_studio_zns_send_count": 1,
            "date_order": "2026-08-28",
            "company_id": [1, "Ordinaire"],
            "x_studio_zns_request_state": "pending",
            "x_studio_zns_claim_token": None,
            "x_studio_zns_claim_owner": None,
            "x_studio_zns_processing_started_at": None,
            "x_studio_zns_msg_id": None,
        }
        write_calls = []

        def search_read(model, domain, fields=None, limit=0, order=None, **kwargs):
            wanted = None
            for term in domain or []:
                if isinstance(term, (list, tuple)) and term and term[0] == "x_studio_zns_request_state":
                    wanted = term[2]
            if wanted == "processing":
                return []
            if wanted == "pending" and record.get("x_studio_zns_request_state") == "pending":
                return [dict(record)]
            return []

        def mock_write(model, ids, vals, **kwargs):
            write_calls.append(dict(vals))
            if vals.get("x_studio_zns_request_state") == "processing" and len(
                [c for c in write_calls if c.get("x_studio_zns_request_state") == "processing"]
            ) == 1:
                return False
            record.update(vals)
            return True

        def mock_read(model, ids, fields=None, **kwargs):
            data = dict(record)
            if fields:
                data = {k: data.get(k) for k in fields}
                data["id"] = record["id"]
            return [data]

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.search_read.side_effect = search_read
        mock_odoo.write.side_effect = mock_write
        mock_odoo.read.side_effect = mock_read

        poller = ZNSOdooPoller(
            tracking_service=mock_tracking,
            odoo_client=mock_odoo,
            repo=self.repo,
            worker_id="poller-retry",
        )
        self.assertEqual(poller.poll_and_dispatch(model="sale.order", limit=10), 0)
        self.assertEqual(mock_tracking.dispatch_zns.call_count, 0)
        queued = self.repo.get_dispatch_request("sale.order", 901, "hdsd-vie", 1)
        self.assertIsNotNone(queued)
        self.assertEqual(queued["status"], "FAILED")

        self.assertEqual(poller.poll_and_dispatch(model="sale.order", limit=10), 1)
        self.assertEqual(mock_tracking.dispatch_zns.call_count, 1)
        queued2 = self.repo.get_dispatch_request("sale.order", 901, "hdsd-vie", 1)
        self.assertEqual(queued2["status"], "COMPLETED")
        self.assertEqual(queued2["zalo_msg_id"], "zalo_retry_901")

    def test_stale_claimed_lease_is_reclaimed_with_same_idempotency_key(self):
        """Crash after INSERT must not leave CLAIMED forever; reclaim uses the versioned idempotency key."""
        from services.zns_odoo_poller import ZNSOdooPoller

        crashed = self.repo.claim_dispatch_request(
            source_model="sale.order",
            source_record_id=902,
            template_type="hdsd-vie",
            send_version=1,
            worker_id="dead-worker",
            claim_token="stale-token",
            lease_duration_seconds=0,
        )
        self.assertTrue(crashed["is_new_claim"])
        time.sleep(0.02)

        mock_tracking = MagicMock()
        mock_tracking.dispatch_zns.return_value = {
            "status": "ACCEPTED",
            "msg_id": "zalo_reclaim_902",
            "tracking_id": "trk_reclaim_902",
            "message_id": "msg_reclaim_902",
        }
        record = {
            "id": 902,
            "name": "SO902",
            "partner_id": [1, "Reclaim Customer"],
            "x_studio_phone": "0912345678",
            "x_studio_tn_khch_hng": "Reclaim Customer",
            "x_studio_zns_last_template": "hdsd-vie",
            "x_studio_zns_send_count": 1,
            "date_order": "2026-08-28",
            "company_id": [1, "Ordinaire"],
            "x_studio_zns_request_state": "pending",
        }

        def search_read(model, domain, fields=None, limit=0, order=None, **kwargs):
            wanted = None
            for term in domain or []:
                if isinstance(term, (list, tuple)) and term and term[0] == "x_studio_zns_request_state":
                    wanted = term[2]
            if wanted == "processing":
                return []
            if wanted == "pending":
                return [dict(record)]
            return []

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.search_read.side_effect = search_read
        mock_odoo.write.return_value = True
        mock_odoo.read.side_effect = lambda model, ids, fields=None, **kwargs: [dict(record)]

        poller = ZNSOdooPoller(
            tracking_service=mock_tracking,
            odoo_client=mock_odoo,
            repo=self.repo,
            worker_id="poller-reclaim",
        )
        self.assertEqual(poller.poll_and_dispatch(model="sale.order", limit=10), 1)
        self.assertEqual(mock_tracking.dispatch_zns.call_count, 1)
        kwargs = mock_tracking.dispatch_zns.call_args.kwargs
        self.assertEqual(kwargs["idempotency_key"], "sale.order:902:hdsd-vie:v1")
        queued = self.repo.get_dispatch_request("sale.order", 902, "hdsd-vie", 1)
        self.assertEqual(queued["status"], "COMPLETED")
        self.assertEqual(queued["claimed_by"], "poller-reclaim")

    def test_dispatch_lease_fences_stale_owner_completion_and_failure(self):
        """After B reclaims A's expired lease, stale A cannot complete or fail B's request."""
        claimed_a = self.repo.claim_dispatch_request(
            "sale.order", 903, "hdsd-vie", 1, "worker-A", "token-A",
            lease_duration_seconds=0,
        )
        self.assertTrue(claimed_a["is_new_claim"])
        time.sleep(0.01)

        claimed_b = self.repo.claim_dispatch_request(
            "sale.order", 903, "hdsd-vie", 1, "worker-B", "token-B",
            lease_duration_seconds=60,
        )
        self.assertTrue(claimed_b["is_reclaim"])
        self.assertEqual(claimed_b["record"]["lease_owner"], "worker-B")

        self.assertFalse(self.repo.complete_dispatch_request(
            claimed_a["id"], "worker-A", "token-A",
            zalo_msg_id="stale-msg", result_status="ACCEPTED",
        ))
        self.assertFalse(self.repo.fail_dispatch_request(
            claimed_a["id"], "worker-A", "token-A", "stale failure",
        ))
        self.assertTrue(self.repo.complete_dispatch_request(
            claimed_b["id"], "worker-B", "token-B",
            zalo_msg_id="owner-msg", result_status="ACCEPTED",
        ))

        final = self.repo.get_dispatch_request("sale.order", 903, "hdsd-vie", 1)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["zalo_msg_id"], "owner-msg")

    def test_stale_dispatch_reclaim_respects_max_attempts(self):
        """Expired CLAIMED/PROCESSING rows stop reclaiming after max_attempts."""
        claim = self.repo.claim_dispatch_request(
            "sale.order", 904, "hdsd-vie", 1, "worker-1", "token-1",
            lease_duration_seconds=0,
        )
        self.assertTrue(claim["is_new_claim"])

        for attempt in range(2, 9):
            time.sleep(0.002)
            claim = self.repo.claim_dispatch_request(
                "sale.order", 904, "hdsd-vie", 1,
                f"worker-{attempt}", f"token-{attempt}",
                lease_duration_seconds=0,
            )
            self.assertTrue(claim["is_reclaim"])
            self.assertEqual(claim["record"]["attempt_count"], attempt)

        time.sleep(0.002)
        exhausted = self.repo.claim_dispatch_request(
            "sale.order", 904, "hdsd-vie", 1, "worker-9", "token-9",
            lease_duration_seconds=60,
        )
        self.assertFalse(exhausted["is_new_claim"])
        self.assertEqual(exhausted["record"]["attempt_count"], 8)
        self.assertEqual(exhausted["record"]["lease_owner"], "worker-8")

    def test_writeback_cas_skips_when_version_already_bumped(self):
        """If resend happens before writeback search, v1 must not overwrite v2 pending."""
        from services.zns_odoo_poller import ZNSOdooPoller

        rec = {
            "id": 910,
            "x_studio_zns_send_count": 2,
            "x_studio_zns_request_state": "pending",
            "x_studio_zns_status": "queued",
            "x_studio_zns_msg_id": None,
            "x_studio_zns_claim_token": False,
            "x_studio_zns_claim_owner": False,
            "x_studio_zns_processing_started_at": False,
        }
        fake = _InMemoryOdoo(rec)
        poller = ZNSOdooPoller(odoo_client=fake, repo=self.repo, worker_id="wb-skip")
        ok = poller._writeback_completed_to_odoo(
            model="sale.order",
            rec_id=910,
            send_version=1,
            backend_status="ACCEPTED",
            zalo_msg_id="old-msg",
        )
        self.assertTrue(ok)
        self.assertEqual(rec["x_studio_zns_send_count"], 2)
        self.assertEqual(rec["x_studio_zns_request_state"], "pending")
        self.assertEqual(rec["x_studio_zns_status"], "queued")
        self.assertNotEqual(rec.get("x_studio_zns_msg_id"), "old-msg")

    def test_writeback_compensates_resend_between_search_and_write(self):
        """Deterministic read/resend/write interleaving: v1 writeback must restore v2 pending."""
        from services.zns_odoo_poller import ZNSOdooPoller

        rec = {
            "id": 911,
            "x_studio_zns_send_count": 1,
            "x_studio_zns_request_state": "processing",
            "x_studio_zns_status": "queued",
            "x_studio_zns_msg_id": None,
            "x_studio_zns_claim_token": "tok-v1",
            "x_studio_zns_claim_owner": "poller-v1",
            "x_studio_zns_processing_started_at": "2026-08-28T00:00:00+00:00",
        }
        fake = _InMemoryOdoo(rec)

        def resend_v2():
            rec["x_studio_zns_send_count"] = 2
            rec["x_studio_zns_request_state"] = "pending"
            rec["x_studio_zns_status"] = "queued"
            rec["x_studio_zns_claim_token"] = False
            rec["x_studio_zns_claim_owner"] = False
            rec["x_studio_zns_processing_started_at"] = False

        poller = ZNSOdooPoller(odoo_client=fake, repo=self.repo, worker_id="wb-race")
        ok = poller._writeback_completed_to_odoo(
            model="sale.order",
            rec_id=911,
            send_version=1,
            backend_status="ACCEPTED",
            zalo_msg_id="old-msg",
            before_write=resend_v2,
        )
        self.assertTrue(ok)
        self.assertEqual(rec["x_studio_zns_send_count"], 2)
        self.assertEqual(rec["x_studio_zns_request_state"], "pending")
        self.assertEqual(rec["x_studio_zns_status"], "queued")

    def test_chatter_queue_note_is_updated_to_gateway_result_in_place(self):
        """ACCEPTED writeback replaces the stale queue status without posting a second note."""
        from services.zns_odoo_poller import ZNSOdooPoller

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.search_read.return_value = [{
            "id": 4242,
            "body": (
                "<div><p>YÊU CẦU GỬI ZALO ZNS: HDSD (Lần 1)</p><ul>"
                "<li><b>Trạng thái:</b> Đã ghi nhận vào hàng đợi gửi tin (Chờ xử lý).</li>"
                "</ul></div>"
            ),
        }]
        mock_odoo.write.return_value = True
        poller = ZNSOdooPoller(odoo_client=mock_odoo, repo=self.repo)

        ok = poller._update_request_chatter_status(
            "sale.order", 5274, 1, "ACCEPTED", zalo_msg_id='<unsafe>42'
        )

        self.assertTrue(ok)
        model, ids, values = mock_odoo.write.call_args.args
        self.assertEqual((model, ids), ("mail.message", [4242]))
        self.assertIn("Zalo đã tiếp nhận tin nhắn (ACCEPTED)", values["body"])
        self.assertIn("&lt;unsafe&gt;42", values["body"])
        self.assertNotIn("Chờ xử lý", values["body"])
        mock_odoo.message_post.assert_not_called()

    def test_chatter_result_update_is_idempotent(self):
        """An already-updated queue note does not cause another Odoo write."""
        from services.zns_odoo_poller import ZNSOdooPoller

        mock_odoo = MagicMock()
        mock_odoo.is_configured = True
        mock_odoo.search_read.return_value = [{
            "id": 4242,
            "body": (
                "<p>YÊU CẦU GỬI ZALO ZNS (Lần 1)</p>"
                "<li><b>Trạng thái:</b> ✅ Zalo đã tiếp nhận tin nhắn (ACCEPTED). "
                "<b>Zalo Msg ID:</b> msg-1</li>"
            ),
        }]
        poller = ZNSOdooPoller(odoo_client=mock_odoo, repo=self.repo)

        self.assertTrue(poller._update_request_chatter_status(
            "sale.order", 5274, 1, "ACCEPTED", zalo_msg_id="msg-1"
        ))
        mock_odoo.write.assert_not_called()

    # 22. Hermetic subprocess startup: no inherited credentials, no daemons, Waitress socket probe
    def test_app_subprocess_docker_command_smoke_and_sigterm(self):
        """
        Start `python app.py` in a credential-free env with daemons disabled,
        probe Waitress until /health accepts connections, then SIGTERM -> exit 0.
        """
        import socket
        import subprocess
        import signal
        import urllib.request
        import urllib.error

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        smoke_dir = tempfile.TemporaryDirectory()
        try:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            db_path = os.path.join(smoke_dir.name, "zns_tracking.sqlite3")
            test_env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": smoke_dir.name,
                "TMPDIR": smoke_dir.name,
                "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", ""),
                "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", ""),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "FLASK_PORT": str(port),
                "FLASK_DEBUG": "false",
                "ENVIRONMENT": "test",
                "ZNS_DISABLE_BACKGROUND_DAEMONS": "1",
                "ZNS_RECONCILIATION_ENABLED": "false",
                "ZNS_ALLOW_INSECURE_DEV": "true",
                "ZNS_TRACKING_DB_PATH": db_path,
                "DATA_DIR": smoke_dir.name,
                "ODOO_URL": "",
                "ODOO_DB": "",
                "ODOO_API_KEY": "",
                "ODOO_UID": "",
                "ODOO_USER": "",
                "ODOO_TEST_URL": "",
                "ODOO_TEST_DB": "",
                "ODOO_TEST_API_KEY": "",
                "ZALO_APP_ID": "",
                "ZALO_SECRET_KEY": "",
                "ZALO_BON_APP_ID": "",
                "ZALO_BON_SECRET_KEY": "",
                "SHOPIFY_STORE": "",
                "SHOPIFY_ACCESS_TOKEN": "",
                "BONARIO_SHOPIFY_STORE": "",
                "BONARIO_SHOPIFY_ACCESS_TOKEN": "",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHAT_ID": "",
                "CONDUCTED_ODOO_API_KEY": "",
                "DASHBOARD_259_ODOO_API_KEY": "",
            }
            if os.environ.get("PYTHONPATH"):
                test_env["PYTHONPATH"] = os.environ["PYTHONPATH"]
            for win_var in ("SystemRoot", "SYSTEMROOT", "SystemDrive", "WINDIR", "ComSpec", "TEMP", "TMP"):
                if os.environ.get(win_var):
                    test_env[win_var] = os.environ[win_var]

            proc = subprocess.Popen(
                [sys.executable, "app.py"],
                cwd=repo_root,
                env=test_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            try:
                health_url = f"http://127.0.0.1:{port}/health"
                deadline = time.time() + 8.0
                health_ok = False
                last_exc = None
                while time.time() < deadline:
                    if proc.poll() is not None:
                        stdout, _ = proc.communicate(timeout=1.0)
                        self.fail(f"Process exited before Waitress listen. Output:\n{stdout}")
                    try:
                        with urllib.request.urlopen(health_url, timeout=0.4) as resp:
                            if resp.status == 200:
                                health_ok = True
                                break
                    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                        last_exc = e
                        time.sleep(0.1)
                self.assertTrue(health_ok, f"Waitress did not accept /health on port {port}: {last_exc}")

                proc.send_signal(signal.SIGTERM)
                stdout, _ = proc.communicate(timeout=5.0)
                if os.name == "nt":
                    self.assertIn(proc.returncode, (0, 1, 15), f"Process exited with unexpected code! Output:\n{stdout}")
                else:
                    self.assertEqual(proc.returncode, 0, f"Process exited with non-zero code! Output:\n{stdout}")
                self.assertNotIn("ImportError", stdout)
                self.assertIn("Background daemons disabled", stdout)
            except Exception:
                proc.kill()
                raise
        finally:
            smoke_dir.cleanup()


class _InMemoryOdoo:
    """Minimal Odoo stand-in with real search/write for CAS interleaving tests."""

    def __init__(self, rec):
        self.rec = rec
        self.is_configured = True
        self.write_calls = []

    def _matches(self, domain):
        rec = self.rec
        for term in domain or []:
            if not isinstance(term, (list, tuple)) or len(term) < 3:
                continue
            field, op, val = term[0], term[1], term[2]
            cur = rec.get("id") if field == "id" else rec.get(field)
            if op == "=":
                if val is False:
                    if cur not in (False, None, ""):
                        return False
                elif cur != val:
                    return False
            elif op == "!=":
                if cur == val:
                    return False
        return True

    def search(self, model, domain, limit=0, heartbeat=None):
        return [self.rec["id"]] if self._matches(domain) else []

    def search_read(self, model, domain, fields=None, limit=0, order=None, heartbeat=None):
        if not self._matches(domain):
            return []
        data = dict(self.rec)
        if fields:
            data = {k: data.get(k) for k in fields}
            data["id"] = self.rec["id"]
        return [data]

    def read(self, model, ids, fields=None, heartbeat=None):
        data = dict(self.rec)
        if fields:
            data = {k: data.get(k) for k in fields}
            data["id"] = self.rec["id"]
        return [data]

    def write(self, model, ids, vals, heartbeat=None):
        self.write_calls.append(dict(vals))
        self.rec.update(vals)
        return True


if __name__ == "__main__":
    unittest.main()
