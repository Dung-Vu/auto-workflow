"""
ZNS Repository — SQLite database access layer for ZNS message tracking.

Features:
- Thread-safe SQLite connection manager with WAL mode, foreign keys, and busy timeout.
- Versioned, idempotent schema migrations (v1 and v2 with CHECK constraints).
- Atomic Compare-And-Set (CAS) state transitions with rowcount check and transactional event logging.
- Idempotency key lookup and anti-duplicate sending support.
- Paginated message filtering, data retention cleanup, and comprehensive statistics aggregation.
"""

import os
import json
import sqlite3
import uuid
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set
from config import Config
from utils.pii import sanitize_payload

logger = logging.getLogger(__name__)

# Valid Domain States & Allowed State Transitions (Single Source of Truth)
VALID_STATES: Set[str] = {
    "QUEUED",
    "SUBMITTING",
    "ACCEPTED",
    "DELIVERED",
    "REJECTED",
    "SUBMISSION_UNKNOWN",
    "DELIVERY_UNKNOWN",
    "CANCELLED",
}

VALID_TRANSITIONS: Dict[str, Set[str]] = {
    "QUEUED": {"SUBMITTING", "CANCELLED", "SUBMISSION_UNKNOWN"},
    "SUBMITTING": {"ACCEPTED", "REJECTED", "SUBMISSION_UNKNOWN", "DELIVERED"},
    "ACCEPTED": {"DELIVERED", "DELIVERY_UNKNOWN"},
    "DELIVERED": set(),          # Terminal state — cannot be transitioned out or downgraded
    "REJECTED": set(),           # Terminal state
    "SUBMISSION_UNKNOWN": {"ACCEPTED", "DELIVERED", "REJECTED"},
    "DELIVERY_UNKNOWN": {"DELIVERED"},  # Late delivery callback allowed
    "CANCELLED": set(),          # Terminal state
}

VALID_APP_KEYS: Set[str] = {"ord", "bon"}

# Durable dispatch-request lease / retry defaults
DISPATCH_LEASE_SECONDS = 60
DISPATCH_MAX_ATTEMPTS = 8

# Lock for migration and DB initialization
_INIT_LOCK = threading.Lock()
_MIGRATIONS_RUN = False


class DuplicateIdempotencyKeyError(Exception):
    """Raised when creating a message with an idempotency key that already exists."""

    def __init__(self, message: str, idempotency_key: str, existing_record: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.idempotency_key = idempotency_key
        self.existing_record = existing_record


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 string format."""
    return datetime.now(timezone.utc).isoformat()


class ZNSRepository:
    """Thread-safe SQLite repository for ZNS tracking."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = db_path
        else:
            self.db_path = Config.ZNS_TRACKING_DB_PATH

        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._ensure_migrations()

    def get_connection(self) -> sqlite3.Connection:
        """
        Create and configure a SQLite connection.
        Enables WAL mode, foreign keys, busy timeout, and row factory.
        """
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000;")
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
        except Exception:
            pass
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _ensure_migrations(self):
        """Run database migrations idempotently, crash-safely, and process-safely."""
        global _MIGRATIONS_RUN
        with _INIT_LOCK:
            conn = self.get_connection()
            try:
                with conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS schema_migrations (
                            version INTEGER PRIMARY KEY,
                            applied_at TEXT NOT NULL
                        );
                    """)
                    cur = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
                    applied = {row["version"] for row in cur.fetchall()}
            finally:
                conn.close()

            if 1 not in applied:
                self._run_migration_1_safe()

            if 2 not in applied:
                self._run_migration_2_safe()

            if 3 not in applied:
                self._run_migration_3_safe()

            if 4 not in applied:
                self._run_migration_4_safe()

            if 5 not in applied:
                self._run_migration_5_safe()

            if 6 not in applied:
                self._run_migration_6_safe()

            _MIGRATIONS_RUN = True

    def _run_migration_1_safe(self):
        """Execute Migration 1: Core tables for messages, events, diagnostics."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            with conn:
                cur = conn.execute("SELECT version FROM schema_migrations WHERE version = 1;")
                if cur.fetchone():
                    return

                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS zns_messages (
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

                    CREATE INDEX IF NOT EXISTS idx_zns_messages_tracking_id ON zns_messages(tracking_id);
                    CREATE INDEX IF NOT EXISTS idx_zns_messages_zalo_msg_id ON zns_messages(zalo_msg_id);
                    CREATE INDEX IF NOT EXISTS idx_zns_messages_idempotency_key ON zns_messages(idempotency_key);
                    CREATE INDEX IF NOT EXISTS idx_zns_messages_status ON zns_messages(status);
                    CREATE INDEX IF NOT EXISTS idx_zns_messages_created_at ON zns_messages(created_at);
                    CREATE INDEX IF NOT EXISTS idx_zns_messages_phone_hash ON zns_messages(phone_hash);
                    CREATE INDEX IF NOT EXISTS idx_zns_messages_source ON zns_messages(source_model, source_record_id);
                    CREATE INDEX IF NOT EXISTS idx_zns_messages_business_ref ON zns_messages(business_reference);

                    CREATE TABLE IF NOT EXISTS zns_message_events (
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

                    CREATE INDEX IF NOT EXISTS idx_zns_events_message_id ON zns_message_events(message_id);
                    CREATE INDEX IF NOT EXISTS idx_zns_events_created_at ON zns_message_events(created_at);

                    CREATE TABLE IF NOT EXISTS zns_webhook_diagnostics (
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

                    CREATE INDEX IF NOT EXISTS idx_zns_diag_created_at ON zns_webhook_diagnostics(created_at);
                    CREATE INDEX IF NOT EXISTS idx_zns_diag_tracking_id ON zns_webhook_diagnostics(tracking_id);
                """)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                    (1, _utc_now_iso()),
                )
            logger.info("Applied ZNS tracking migration v1")
        finally:
            conn.close()

    def _run_migration_2_safe(self):
        """
        Execute Migration 2 safely adhering to SQLite table recreation procedure:
        - PRAGMA foreign_keys = OFF executed outside transaction on dedicated connection.
        - Snapshot record counts before migration.
        - Copy messages intact with CHECK constraints.
        - PRAGMA foreign_key_check validation.
        - Assert counts equality (zero data/event loss).
        - Commit & record migration version.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = OFF;")
            conn.execute("PRAGMA busy_timeout = 30000;")

            conn.execute("BEGIN EXCLUSIVE TRANSACTION;")

            # Double-check inside exclusive transaction
            cur = conn.execute("SELECT version FROM schema_migrations WHERE version = 2;")
            if cur.fetchone():
                conn.execute("COMMIT;")
                return

            # Snapshot counts before migration
            cur = conn.execute("SELECT COUNT(*) as cnt FROM zns_messages;")
            cnt_messages_before = cur.fetchone()["cnt"]

            cur = conn.execute("SELECT COUNT(*) as cnt FROM zns_message_events;")
            cnt_events_before = cur.fetchone()["cnt"]

            cur = conn.execute("SELECT COUNT(*) as cnt FROM zns_webhook_diagnostics;")
            cnt_diags_before = cur.fetchone()["cnt"]

            conn.execute("""
                CREATE TABLE IF NOT EXISTS zns_messages_v2 (
                    id TEXT PRIMARY KEY,
                    tracking_id TEXT UNIQUE NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    zalo_msg_id TEXT UNIQUE,
                    app_key TEXT NOT NULL CHECK (app_key IN ('ord', 'bon')),
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
                    status TEXT NOT NULL CHECK (status IN (
                        'QUEUED', 'SUBMITTING', 'ACCEPTED', 'DELIVERED',
                        'REJECTED', 'SUBMISSION_UNKNOWN', 'DELIVERY_UNKNOWN', 'CANCELLED'
                    )),
                    error_code INTEGER,
                    error_message TEXT,
                    sending_mode TEXT,
                    sent_time_ms INTEGER,
                    quota_daily INTEGER,
                    quota_remaining INTEGER,
                    requested_at TEXT NOT NULL,
                    submitted_at TEXT,
                    accepted_at TEXT,
                    delivered_at TEXT,
                    last_webhook_at TEXT,
                    unknown_at TEXT,
                    retry_count INTEGER DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            conn.execute("""
                INSERT INTO zns_messages_v2 (
                    id, tracking_id, idempotency_key, zalo_msg_id,
                    app_key, template_type, template_id, source,
                    source_model, source_record_id, business_reference,
                    customer_id, customer_name, phone_masked, phone_hash,
                    sent_by_user_id, sent_by_user_name, company_id,
                    status, error_code, error_message, sending_mode,
                    quota_daily, quota_remaining, requested_at,
                    submitted_at, accepted_at, delivered_at,
                    last_webhook_at, unknown_at, retry_count,
                    created_at, updated_at
                )
                SELECT
                    id, tracking_id, idempotency_key, zalo_msg_id,
                    app_key, template_type, template_id, source,
                    source_model, source_record_id, business_reference,
                    customer_id, customer_name, phone_masked, phone_hash,
                    sent_by_user_id, sent_by_user_name, company_id,
                    status, error_code, error_message, sending_mode,
                    quota_daily, quota_remaining, requested_at,
                    submitted_at, accepted_at, delivered_at,
                    last_webhook_at, unknown_at, retry_count,
                    created_at, updated_at
                FROM zns_messages;
            """)

            conn.execute("DROP TABLE zns_messages;")
            conn.execute("ALTER TABLE zns_messages_v2 RENAME TO zns_messages;")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_tracking_id ON zns_messages(tracking_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_zalo_msg_id ON zns_messages(zalo_msg_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_idempotency_key ON zns_messages(idempotency_key);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_status ON zns_messages(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_status_accepted ON zns_messages(status, accepted_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_created_at ON zns_messages(created_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_phone_hash ON zns_messages(phone_hash);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_source ON zns_messages(source_model, source_record_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_messages_business_ref ON zns_messages(business_reference);")

            # Validate Foreign Key integrity
            fk_cur = conn.execute("PRAGMA foreign_key_check;")
            fk_violations = fk_cur.fetchall()
            if fk_violations:
                conn.execute("ROLLBACK;")
                raise RuntimeError(f"Migration v2 aborted: foreign key violations detected: {fk_violations}")

            # Assert zero data loss
            cur = conn.execute("SELECT COUNT(*) as cnt FROM zns_messages;")
            cnt_messages_after = cur.fetchone()["cnt"]

            cur = conn.execute("SELECT COUNT(*) as cnt FROM zns_message_events;")
            cnt_events_after = cur.fetchone()["cnt"]

            cur = conn.execute("SELECT COUNT(*) as cnt FROM zns_webhook_diagnostics;")
            cnt_diags_after = cur.fetchone()["cnt"]

            if (cnt_messages_after != cnt_messages_before or
                cnt_events_after != cnt_events_before or
                cnt_diags_after != cnt_diags_before):
                conn.execute("ROLLBACK;")
                raise RuntimeError(
                    f"Migration v2 aborted: Data count mismatch! "
                    f"Messages: {cnt_messages_before}->{cnt_messages_after}, "
                    f"Events: {cnt_events_before}->{cnt_events_after}, "
                    f"Diagnostics: {cnt_diags_before}->{cnt_diags_after}"
                )

            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                (2, _utc_now_iso()),
            )
            conn.execute("COMMIT;")
            logger.info("Successfully applied ZNS tracking migration v2 with verified zero-data-loss.")
        except Exception as e:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            logger.exception(f"Migration v2 failed: {e}")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.close()

    def _run_migration_3_safe(self):
        """
        Execute Migration 3:
        - Creates durable outbox table `zns_odoo_outbox` for asynchronous Odoo delivery sync with retry backoff.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            with conn:
                # Double-check if version 3 is already applied
                cur = conn.execute("SELECT version FROM schema_migrations WHERE version = 3;")
                if cur.fetchone():
                    return

                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS zns_odoo_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id TEXT NOT NULL,
                        source_model TEXT NOT NULL,
                        source_record_id INTEGER NOT NULL,
                        template_type TEXT NOT NULL,
                        zalo_msg_id TEXT,
                        delivered_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'DEAD_LETTER')),
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        max_retries INTEGER NOT NULL DEFAULT 5,
                        next_retry_at TEXT NOT NULL,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (message_id) REFERENCES zns_messages(id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_zns_odoo_outbox_poll ON zns_odoo_outbox (status, next_retry_at);
                    CREATE INDEX IF NOT EXISTS idx_zns_odoo_outbox_msg ON zns_odoo_outbox (message_id);
                """)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                    (3, _utc_now_iso()),
                )
            logger.info("Applied ZNS tracking migration v3 (Durable Odoo Outbox)")
        finally:
            conn.close()

    def _run_migration_4_safe(self):
        """
        Execute Migration 4:
        - Creates database-level trigger `trg_zns_messages_status_invariant` enforcing canonical state transitions.
        - Adds `lease_owner`, `lease_expires_at`, `event_type` to `zns_odoo_outbox` if missing.
        - Creates unique logical event index `uq_zns_odoo_outbox_msg_event` and lease index.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            with conn:
                # Double-check if version 4 is already applied
                cur = conn.execute("SELECT version FROM schema_migrations WHERE version = 4;")
                if cur.fetchone():
                    return

                # Create trigger enforcing state machine invariant on raw SQL updates
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS trg_zns_messages_status_invariant
                    BEFORE UPDATE OF status ON zns_messages
                    FOR EACH ROW
                    WHEN (
                        (OLD.status = 'DELIVERED' AND NEW.status != 'DELIVERED')
                        OR (OLD.status = 'REJECTED' AND NEW.status != 'REJECTED')
                        OR (OLD.status = 'CANCELLED' AND NEW.status != 'CANCELLED')
                        OR (OLD.status = 'QUEUED' AND NEW.status NOT IN ('SUBMITTING', 'CANCELLED', 'SUBMISSION_UNKNOWN'))
                        OR (OLD.status = 'SUBMITTING' AND NEW.status NOT IN ('ACCEPTED', 'REJECTED', 'SUBMISSION_UNKNOWN', 'DELIVERED'))
                        OR (OLD.status = 'ACCEPTED' AND NEW.status NOT IN ('DELIVERED', 'DELIVERY_UNKNOWN'))
                        OR (OLD.status = 'SUBMISSION_UNKNOWN' AND NEW.status NOT IN ('ACCEPTED', 'DELIVERED', 'REJECTED'))
                        OR (OLD.status = 'DELIVERY_UNKNOWN' AND NEW.status NOT IN ('DELIVERED'))
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'Illegal status transition in zns_messages');
                    END;
                """)

                # Add lease columns to zns_odoo_outbox if not present
                outbox_cols = {row["name"] for row in conn.execute("PRAGMA table_info(zns_odoo_outbox);").fetchall()}
                if "lease_owner" not in outbox_cols:
                    try:
                        conn.execute("ALTER TABLE zns_odoo_outbox ADD COLUMN lease_owner TEXT;")
                    except sqlite3.OperationalError as oe:
                        if "duplicate column" not in str(oe).lower():
                            raise
                if "lease_expires_at" not in outbox_cols:
                    try:
                        conn.execute("ALTER TABLE zns_odoo_outbox ADD COLUMN lease_expires_at TEXT;")
                    except sqlite3.OperationalError as oe:
                        if "duplicate column" not in str(oe).lower():
                            raise
                if "event_type" not in outbox_cols:
                    try:
                        conn.execute("ALTER TABLE zns_odoo_outbox ADD COLUMN event_type TEXT NOT NULL DEFAULT 'DELIVERY_CONFIRMED';")
                    except sqlite3.OperationalError as oe:
                        if "duplicate column" not in str(oe).lower():
                            raise

                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_zns_odoo_outbox_msg_event ON zns_odoo_outbox (message_id, event_type);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_odoo_outbox_lease ON zns_odoo_outbox (status, lease_expires_at, next_retry_at);")

                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                    (4, _utc_now_iso()),
                )
            logger.info("Applied ZNS tracking migration v4 (DB State Invariant Trigger & Outbox Leases)")
        finally:
            conn.close()

    def _run_migration_5_safe(self):
        """
        Execute Migration 5:
        - Creates durable `zns_dispatch_requests` table to act as the single source of truth for Odoo dispatch requests.
        - Adds `event_marker` and `odoo_message_id` to `zns_odoo_outbox` for idempotent chatter tracking.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            with conn:
                cur = conn.execute("SELECT version FROM schema_migrations WHERE version = 5;")
                if cur.fetchone():
                    return

                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS zns_dispatch_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_model TEXT NOT NULL,
                        source_record_id INTEGER NOT NULL,
                        template_type TEXT NOT NULL,
                        send_version INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'CLAIMED' CHECK (status IN ('CLAIMED', 'PROCESSING', 'COMPLETED', 'FAILED')),
                        claimed_by TEXT NOT NULL,
                        claimed_at TEXT NOT NULL,
                        claim_token TEXT,
                        message_id TEXT,
                        tracking_id TEXT,
                        zalo_msg_id TEXT,
                        result_status TEXT,
                        result_json TEXT,
                        last_error TEXT,
                        completed_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (source_model, source_record_id, template_type, send_version)
                    );

                    CREATE INDEX IF NOT EXISTS idx_zns_dispatch_requests_lookup
                    ON zns_dispatch_requests (source_model, source_record_id, template_type, send_version);
                    CREATE INDEX IF NOT EXISTS idx_zns_dispatch_requests_status
                    ON zns_dispatch_requests (status, claimed_at);
                """)

                # Add event_marker and odoo_message_id columns to zns_odoo_outbox if missing
                outbox_cols = {row["name"] for row in conn.execute("PRAGMA table_info(zns_odoo_outbox);").fetchall()}
                if "event_marker" not in outbox_cols:
                    try:
                        conn.execute("ALTER TABLE zns_odoo_outbox ADD COLUMN event_marker TEXT;")
                    except Exception:
                        pass
                if "odoo_message_id" not in outbox_cols:
                    try:
                        conn.execute("ALTER TABLE zns_odoo_outbox ADD COLUMN odoo_message_id INTEGER;")
                    except Exception:
                        pass

                conn.execute("CREATE INDEX IF NOT EXISTS idx_zns_odoo_outbox_marker ON zns_odoo_outbox (event_marker);")

                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                    (5, _utc_now_iso()),
                )
            logger.info("Applied ZNS tracking migration v5 (Durable Dispatch Request Queue & Chatter Idempotency)")
        finally:
            conn.close()

    def _run_migration_6_safe(self):
        """
        Execute Migration 6:
        - Adds lease owner/expiry, attempt count, next retry, and heartbeat columns
          to `zns_dispatch_requests` so FAILED/CLAIMED/PROCESSING rows can be
          reclaimed atomically instead of remaining stuck forever.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("BEGIN EXCLUSIVE TRANSACTION;")
            try:
                cur = conn.execute("SELECT version FROM schema_migrations WHERE version = 6;")
                if cur.fetchone():
                    conn.execute("COMMIT;")
                    return

                def _add_column(name: str, ddl: str) -> None:
                    cols = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(zns_dispatch_requests);").fetchall()
                    }
                    if name in cols:
                        return
                    try:
                        conn.execute(ddl)
                    except sqlite3.OperationalError as oe:
                        if "duplicate column" not in str(oe).lower():
                            raise

                _add_column("lease_owner", "ALTER TABLE zns_dispatch_requests ADD COLUMN lease_owner TEXT;")
                _add_column("lease_expires_at", "ALTER TABLE zns_dispatch_requests ADD COLUMN lease_expires_at TEXT;")
                _add_column(
                    "attempt_count",
                    "ALTER TABLE zns_dispatch_requests ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;",
                )
                _add_column(
                    "max_attempts",
                    "ALTER TABLE zns_dispatch_requests ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 8;",
                )
                _add_column("next_retry_at", "ALTER TABLE zns_dispatch_requests ADD COLUMN next_retry_at TEXT;")
                _add_column(
                    "last_heartbeat_at",
                    "ALTER TABLE zns_dispatch_requests ADD COLUMN last_heartbeat_at TEXT;",
                )

                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_zns_dispatch_requests_lease
                    ON zns_dispatch_requests (status, lease_expires_at, next_retry_at);
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                    (6, _utc_now_iso()),
                )
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            logger.info("Applied ZNS tracking migration v6 (Dispatch request lease, retry, stale reclaim)")
        finally:
            conn.close()

    # ─── Durable Dispatch Requests Queue Operations ───

    def claim_dispatch_request(
        self,
        source_model: str,
        source_record_id: int,
        template_type: str,
        send_version: int,
        worker_id: str,
        claim_token: Optional[str] = None,
        lease_duration_seconds: int = DISPATCH_LEASE_SECONDS,
    ) -> Dict[str, Any]:
        """
        Atomically insert or reclaim a dispatch request.

        A new INSERT is a fresh claim. An existing row is reclaimed only when:
        - status is FAILED and next_retry_at is due and attempts remain, or
        - status is CLAIMED/PROCESSING and the lease is missing or expired.

        COMPLETED rows and rows with a live lease are never reclaimed
        (is_new_claim=False).
        """
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expiry = (now_dt + timedelta(seconds=lease_duration_seconds)).isoformat()
        conn = self.get_connection()
        try:
            with conn:
                try:
                    cur = conn.execute(
                        """
                        INSERT INTO zns_dispatch_requests (
                            source_model, source_record_id, template_type, send_version,
                            status, claimed_by, claimed_at, claim_token,
                            lease_owner, lease_expires_at, last_heartbeat_at,
                            attempt_count, max_attempts, next_retry_at,
                            created_at, updated_at
                        ) VALUES (
                            :source_model, :source_record_id, :template_type, :send_version,
                            'CLAIMED', :worker_id, :now, :claim_token,
                            :worker_id, :lease_expiry, :now,
                            1, :max_attempts, NULL,
                            :now, :now
                        );
                        """,
                        {
                            "source_model": source_model,
                            "source_record_id": source_record_id,
                            "template_type": template_type,
                            "send_version": send_version,
                            "worker_id": worker_id,
                            "claim_token": claim_token,
                            "now": now,
                            "lease_expiry": lease_expiry,
                            "max_attempts": DISPATCH_MAX_ATTEMPTS,
                        },
                    )
                    req_id = cur.lastrowid
                    row = conn.execute("SELECT * FROM zns_dispatch_requests WHERE id = ?;", (req_id,)).fetchone()
                    return {
                        "is_new_claim": True,
                        "is_reclaim": False,
                        "id": req_id,
                        "record": dict(row) if row else {},
                    }
                except sqlite3.IntegrityError:
                    reclaim_cur = conn.execute(
                        """
                        UPDATE zns_dispatch_requests
                        SET status = 'CLAIMED',
                            claimed_by = :worker_id,
                            claimed_at = :now,
                            claim_token = :claim_token,
                            lease_owner = :worker_id,
                            lease_expires_at = :lease_expiry,
                            last_heartbeat_at = :now,
                            attempt_count = attempt_count + 1,
                            next_retry_at = NULL,
                            updated_at = :now
                        WHERE source_model = :source_model
                          AND source_record_id = :source_record_id
                          AND template_type = :template_type
                          AND send_version = :send_version
                          AND (
                              (
                                  status = 'FAILED'
                                  AND (next_retry_at IS NULL OR next_retry_at <= :now)
                                  AND attempt_count < max_attempts
                              )
                              OR (
                                  status IN ('CLAIMED', 'PROCESSING')
                                  AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
                                  AND attempt_count < max_attempts
                              )
                          );
                        """,
                        {
                            "worker_id": worker_id,
                            "claim_token": claim_token,
                            "now": now,
                            "lease_expiry": lease_expiry,
                            "source_model": source_model,
                            "source_record_id": source_record_id,
                            "template_type": template_type,
                            "send_version": send_version,
                        },
                    )
                    row = conn.execute(
                        """
                        SELECT * FROM zns_dispatch_requests
                        WHERE source_model = ? AND source_record_id = ? AND template_type = ? AND send_version = ?;
                        """,
                        (source_model, source_record_id, template_type, send_version),
                    ).fetchone()
                    reclaimed = reclaim_cur.rowcount == 1
                    return {
                        "is_new_claim": reclaimed,
                        "is_reclaim": reclaimed,
                        "id": row["id"] if row else None,
                        "record": dict(row) if row else {},
                    }
        finally:
            conn.close()

    def complete_dispatch_request(
        self,
        request_id: int,
        worker_id: str,
        claim_token: str,
        message_id: Optional[str] = None,
        tracking_id: Optional[str] = None,
        zalo_msg_id: Optional[str] = None,
        result_status: Optional[str] = None,
        result_json: Optional[str] = None,
    ) -> bool:
        """Complete a request only while the caller owns its live fenced lease."""
        now = _utc_now_iso()
        conn = self.get_connection()
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE zns_dispatch_requests
                    SET status = 'COMPLETED',
                        message_id = :message_id,
                        tracking_id = :tracking_id,
                        zalo_msg_id = :zalo_msg_id,
                        result_status = :result_status,
                        result_json = :result_json,
                        completed_at = :now,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        next_retry_at = NULL,
                        updated_at = :now
                    WHERE id = :id
                      AND status IN ('CLAIMED', 'PROCESSING')
                      AND lease_owner = :worker_id
                      AND claim_token = :claim_token
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at >= :now;
                    """,
                    {
                        "id": request_id,
                        "message_id": message_id,
                        "tracking_id": tracking_id,
                        "zalo_msg_id": zalo_msg_id,
                        "result_status": result_status,
                        "result_json": result_json,
                        "worker_id": worker_id,
                        "claim_token": claim_token,
                        "now": now,
                    },
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def fail_dispatch_request(
        self,
        request_id: int,
        worker_id: str,
        claim_token: str,
        last_error: Optional[str] = None,
        backoff_seconds: Optional[int] = None,
    ) -> bool:
        """
        Mark a durable dispatch request as FAILED only under its live fenced lease.

        next_retry_at defaults to now so the following poll cycle may reclaim
        the row (poller interval is the practical backoff). Rows that have
        exhausted max_attempts stay FAILED and are not reclaimable.
        """
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        delay = 0 if backoff_seconds is None else max(0, int(backoff_seconds))
        next_retry_at = (now_dt + timedelta(seconds=delay)).isoformat()
        conn = self.get_connection()
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE zns_dispatch_requests
                    SET status = 'FAILED',
                        last_error = :last_error,
                        next_retry_at = :next_retry_at,
                        lease_owner = NULL,
                        lease_expires_at = :now,
                        updated_at = :now
                    WHERE id = :id
                      AND status IN ('CLAIMED', 'PROCESSING')
                      AND lease_owner = :worker_id
                      AND claim_token = :claim_token
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at >= :now;
                    """,
                    {
                        "id": request_id,
                        "last_error": last_error,
                        "next_retry_at": next_retry_at,
                        "worker_id": worker_id,
                        "claim_token": claim_token,
                        "now": now,
                    },
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def mark_dispatch_processing(self, request_id: int, worker_id: str, claim_token: str) -> bool:
        """Move a claimed request into PROCESSING only under its live fenced lease."""
        now = _utc_now_iso()
        conn = self.get_connection()
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE zns_dispatch_requests
                    SET status = 'PROCESSING',
                        last_heartbeat_at = :now,
                        updated_at = :now
                    WHERE id = :id
                      AND lease_owner = :worker_id
                      AND claim_token = :claim_token
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at >= :now
                      AND status IN ('CLAIMED', 'PROCESSING');
                    """,
                    {
                        "id": request_id,
                        "worker_id": worker_id,
                        "claim_token": claim_token,
                        "now": now,
                    },
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def renew_dispatch_lease(
        self,
        request_id: int,
        worker_id: str,
        claim_token: str,
        lease_duration_seconds: int = DISPATCH_LEASE_SECONDS,
    ) -> bool:
        """Heartbeat: extend a live lease only for its fenced owner/token."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        new_expiry = (now_dt + timedelta(seconds=lease_duration_seconds)).isoformat()
        conn = self.get_connection()
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE zns_dispatch_requests
                    SET lease_expires_at = :new_expiry,
                        last_heartbeat_at = :now,
                        updated_at = :now
                    WHERE id = :id
                      AND lease_owner = :worker_id
                      AND claim_token = :claim_token
                      AND status IN ('CLAIMED', 'PROCESSING')
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at >= :now;
                    """,
                    {
                        "id": request_id,
                        "worker_id": worker_id,
                        "claim_token": claim_token,
                        "new_expiry": new_expiry,
                        "now": now,
                    },
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def get_dispatch_request(
        self,
        source_model: str,
        source_record_id: int,
        template_type: str,
        send_version: int,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a dispatch request record by unique composite key."""
        conn = self.get_connection()
        try:
            row = conn.execute(
                """
                SELECT * FROM zns_dispatch_requests
                WHERE source_model = ? AND source_record_id = ? AND template_type = ? AND send_version = ?;
                """,
                (source_model, source_record_id, template_type, send_version),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_outbox_odoo_message_id(
        self,
        task_id: int,
        odoo_message_id: int,
        event_marker: Optional[str] = None,
    ) -> bool:
        """Record the created Odoo chatter message ID on an outbox task."""
        now = _utc_now_iso()
        conn = self.get_connection()
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE zns_odoo_outbox
                    SET odoo_message_id = :odoo_message_id,
                        event_marker = COALESCE(:event_marker, event_marker),
                        updated_at = :now
                    WHERE id = :id;
                    """,
                    {
                        "id": task_id,
                        "odoo_message_id": odoo_message_id,
                        "event_marker": event_marker,
                        "now": now,
                    },
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    # ─── Message CRUD Operations ───

    def create_message(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Insert a new message record in QUEUED state.
        Raises DuplicateIdempotencyKeyError if idempotency_key already exists.
        """
        now = _utc_now_iso()
        msg_id = data.get("id") or str(uuid.uuid4())
        tracking_id = data["tracking_id"]
        idempotency_key = data.get("idempotency_key")
        status = data.get("status", "QUEUED")
        requested_at = data.get("requested_at") or now

        conn = self.get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO zns_messages (
                        id, tracking_id, idempotency_key, zalo_msg_id,
                        app_key, template_type, template_id, source,
                        source_model, source_record_id, business_reference,
                        customer_id, customer_name, phone_masked, phone_hash,
                        sent_by_user_id, sent_by_user_name, company_id,
                        status, error_code, error_message, sending_mode,
                        sent_time_ms, quota_daily, quota_remaining, requested_at,
                        submitted_at, accepted_at, delivered_at,
                        last_webhook_at, unknown_at, retry_count,
                        created_at, updated_at
                    ) VALUES (
                        :id, :tracking_id, :idempotency_key, :zalo_msg_id,
                        :app_key, :template_type, :template_id, :source,
                        :source_model, :source_record_id, :business_reference,
                        :customer_id, :customer_name, :phone_masked, :phone_hash,
                        :sent_by_user_id, :sent_by_user_name, :company_id,
                        :status, :error_code, :error_message, :sending_mode,
                        :sent_time_ms, :quota_daily, :quota_remaining, :requested_at,
                        :submitted_at, :accepted_at, :delivered_at,
                        :last_webhook_at, :unknown_at, :retry_count,
                        :created_at, :updated_at
                    );
                    """,
                    {
                        "id": msg_id,
                        "tracking_id": tracking_id,
                        "idempotency_key": idempotency_key,
                        "zalo_msg_id": data.get("zalo_msg_id"),
                        "app_key": data["app_key"],
                        "template_type": data["template_type"],
                        "template_id": str(data["template_id"]),
                        "source": data.get("source", "odoo"),
                        "source_model": data.get("source_model"),
                        "source_record_id": data.get("source_record_id"),
                        "business_reference": data.get("business_reference"),
                        "customer_id": data.get("customer_id"),
                        "customer_name": data.get("customer_name"),
                        "phone_masked": data.get("phone_masked", ""),
                        "phone_hash": data.get("phone_hash", ""),
                        "sent_by_user_id": data.get("sent_by_user_id"),
                        "sent_by_user_name": data.get("sent_by_user_name"),
                        "company_id": data.get("company_id"),
                        "status": status,
                        "error_code": data.get("error_code"),
                        "error_message": data.get("error_message"),
                        "sending_mode": data.get("sending_mode"),
                        "sent_time_ms": data.get("sent_time_ms"),
                        "quota_daily": data.get("quota_daily"),
                        "quota_remaining": data.get("quota_remaining"),
                        "requested_at": requested_at,
                        "submitted_at": data.get("submitted_at"),
                        "accepted_at": data.get("accepted_at"),
                        "delivered_at": data.get("delivered_at"),
                        "last_webhook_at": data.get("last_webhook_at"),
                        "unknown_at": data.get("unknown_at"),
                        "retry_count": data.get("retry_count", 0),
                        "created_at": data.get("created_at") or requested_at or now,
                        "updated_at": data.get("updated_at") or now,
                    },
                )
                # Auto record initial creation event
                self._record_event_locked(
                    conn,
                    message_id=msg_id,
                    event_type="MESSAGE_CREATED",
                    new_status=status,
                    previous_status=None,
                    payload={"tracking_id": tracking_id, "template_type": data.get("template_type")},
                    source=data.get("source", "internal"),
                    occurred_at=requested_at,
                )
            return self.get_message_by_id(msg_id)
        except sqlite3.IntegrityError as ie:
            ie_str = str(ie).lower()
            # Strictly map ONLY idempotency_key collisions, not other unique constraints (e.g. zalo_msg_id, tracking_id)
            if idempotency_key and ("zns_messages.idempotency_key" in ie_str or ("unique constraint failed" in ie_str and "idempotency_key" in ie_str)):
                existing = self.get_message_by_idempotency_key(idempotency_key)
                raise DuplicateIdempotencyKeyError(
                    f"Message with idempotency_key '{idempotency_key}' already exists",
                    idempotency_key=idempotency_key,
                    existing_record=existing,
                ) from ie
            raise
        finally:
            conn.close()

    def get_message_by_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single message by internal UUID."""
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT * FROM zns_messages WHERE id = ?;", (message_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_message_by_tracking_id(self, tracking_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a message by unique tracking_id."""
        if not tracking_id:
            return None
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT * FROM zns_messages WHERE tracking_id = ?;", (tracking_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_message_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Fetch a message by idempotency_key."""
        if not idempotency_key:
            return None
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT * FROM zns_messages WHERE idempotency_key = ?;", (idempotency_key,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_message_by_zalo_msg_id(self, zalo_msg_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a message by Zalo-assigned msg_id."""
        if not zalo_msg_id:
            return None
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT * FROM zns_messages WHERE zalo_msg_id = ?;", (zalo_msg_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_message_by_id_or_tracking(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Fetch a message by id first, then by tracking_id or zalo_msg_id."""
        if not identifier:
            return None
        conn = self.get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM zns_messages WHERE id = ? OR tracking_id = ? OR zalo_msg_id = ? LIMIT 1;",
                (identifier, identifier, identifier),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ─── Atomic Compare-And-Set (CAS) State Transition ───

    def transition_message(
        self,
        message_id: str,
        expected_statuses: List[str],
        new_status: str,
        event_type: str,
        event_payload: Optional[Dict[str, Any]] = None,
        event_source: str = "internal",
        occurred_at: Optional[str] = None,
        **fields,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Atomic Compare-And-Set (CAS) state transition.

        Executes:
            UPDATE zns_messages
            SET status = :new_status, updated_at = :now, ...
            WHERE id = :msg_id AND status IN (:exp_0, :exp_1, ...)

        Ensures that:
        1. Every (expected_status -> new_status) is an allowed transition in VALID_TRANSITIONS domain map.
        2. If message status was changed concurrently, rowcount will be 0 and transition safely rejected.
        3. When transitioning to DELIVERED, an entry in `zns_odoo_outbox` is atomically queued in the same transaction.

        Returns:
            (True, updated_record) if transition succeeded (rowcount == 1).
            (False, current_record) if transition was rejected (rowcount == 0).
        """
        if not expected_statuses:
            raise ValueError("expected_statuses cannot be empty for CAS transition")

        if new_status not in VALID_STATES:
            raise ValueError(f"Invalid new_status '{new_status}'")

        # Enforce domain transition validity for all candidates
        for exp_st in expected_statuses:
            if exp_st not in VALID_STATES:
                raise ValueError(f"Invalid expected_status '{exp_st}'")
            allowed_targets = VALID_TRANSITIONS.get(exp_st, set())
            if new_status not in allowed_targets:
                raise ValueError(
                    f"Illegal state transition from '{exp_st}' to '{new_status}'. "
                    f"Allowed transitions for '{exp_st}': {sorted(list(allowed_targets)) or 'None (Terminal)'}"
                )

        now = _utc_now_iso()
        fields["status"] = new_status
        fields["updated_at"] = now

        set_clauses = [f"{k} = :{k}" for k in fields.keys()]

        exp_placeholders = []
        params = {"msg_id": message_id}
        for idx, st in enumerate(expected_statuses):
            p_name = f"exp_{idx}"
            exp_placeholders.append(f":{p_name}")
            params[p_name] = st

        params.update(fields)

        sql = f"""
            UPDATE zns_messages
            SET {', '.join(set_clauses)}
            WHERE id = :msg_id AND status IN ({', '.join(exp_placeholders)});
        """

        conn = self.get_connection()
        try:
            with conn:
                cur = conn.execute(sql, params)
                if cur.rowcount == 1:
                    # Successfully transitioned -> atomically record timeline event
                    self._record_event_locked(
                        conn=conn,
                        message_id=message_id,
                        event_type=event_type,
                        new_status=new_status,
                        previous_status=expected_statuses[0] if len(expected_statuses) == 1 else "MULTIPLE_CANDIDATES",
                        payload=event_payload,
                        source=event_source,
                        occurred_at=occurred_at or now,
                    )

                    rec_cur = conn.execute("SELECT * FROM zns_messages WHERE id = ?;", (message_id,))
                    row = rec_cur.fetchone()

                    # When transitioning to DELIVERED, automatically queue durable outbox entry in same transaction
                    if new_status == "DELIVERED" and row:
                        if row["source_model"] and row["source_record_id"]:
                            delivered_at_str = fields.get("delivered_at") or occurred_at or now
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO zns_odoo_outbox (
                                    message_id, event_type, source_model, source_record_id,
                                    template_type, zalo_msg_id, delivered_at,
                                    status, retry_count, max_retries, next_retry_at,
                                    created_at, updated_at
                                ) VALUES (?, 'DELIVERY_CONFIRMED', ?, ?, ?, ?, ?, 'PENDING', 0, 5, ?, ?, ?);
                                """,
                                (
                                    message_id,
                                    row["source_model"],
                                    row["source_record_id"],
                                    row["template_type"],
                                    fields.get("zalo_msg_id") or row["zalo_msg_id"] or "",
                                    delivered_at_str,
                                    now,
                                    now,
                                    now,
                                ),
                            )

                    return True, dict(row) if row else None
                else:
                    # Transition rejected due to status mismatch
                    rec_cur = conn.execute("SELECT * FROM zns_messages WHERE id = ?;", (message_id,))
                    row = rec_cur.fetchone()
                    return False, dict(row) if row else None
        finally:
            conn.close()

    # ─── Durable Outbox Management Methods ───

    def claim_outbox_tasks(
        self,
        worker_id: str,
        batch_size: int = 10,
        lease_duration_seconds: int = 60,
    ) -> List[Dict[str, Any]]:
        """
        Atomically claim pending, failed, or expired-lease outbox tasks for execution.
        Returns list of successfully claimed tasks with active lease owned by worker_id.
        """
        now = _utc_now_iso()
        now_dt = datetime.now(timezone.utc)
        lease_expiry = (now_dt + timedelta(seconds=lease_duration_seconds)).isoformat()

        conn = self.get_connection()
        try:
            with conn:
                # Find candidate IDs eligible for claiming:
                # 1. status == 'PENDING' AND next_retry_at <= now
                # 2. status == 'PROCESSING' AND (lease_expires_at IS NULL OR lease_expires_at < now)
                # 3. status == 'FAILED' AND next_retry_at <= now AND retry_count < max_retries
                cur = conn.execute(
                    """
                    SELECT id FROM zns_odoo_outbox
                    WHERE (
                        (status = 'PENDING' AND next_retry_at <= :now)
                        OR (status = 'PROCESSING' AND (lease_expires_at IS NULL OR lease_expires_at < :now))
                        OR (status = 'FAILED' AND next_retry_at <= :now AND retry_count < max_retries)
                    )
                    ORDER BY id ASC
                    LIMIT :limit;
                    """,
                    {"now": now, "limit": batch_size},
                )
                candidate_ids = [row["id"] for row in cur.fetchall()]

                claimed_tasks = []
                for cid in candidate_ids:
                    claim_cur = conn.execute(
                        """
                        UPDATE zns_odoo_outbox
                        SET status = 'PROCESSING',
                            lease_owner = :worker_id,
                            lease_expires_at = :lease_expiry,
                            updated_at = :now
                        WHERE id = :cid
                          AND (
                              (status = 'PENDING' AND next_retry_at <= :now)
                              OR (status = 'PROCESSING' AND (lease_expires_at IS NULL OR lease_expires_at < :now))
                              OR (status = 'FAILED' AND next_retry_at <= :now AND retry_count < max_retries)
                          );
                        """,
                        {
                            "cid": cid,
                            "worker_id": worker_id,
                            "lease_expiry": lease_expiry,
                            "now": now,
                        },
                    )
                    if claim_cur.rowcount == 1:
                        task_cur = conn.execute("SELECT * FROM zns_odoo_outbox WHERE id = ?;", (cid,))
                        row = task_cur.fetchone()
                        if row:
                            claimed_tasks.append(dict(row))

                return claimed_tasks
        finally:
            conn.close()

    def complete_outbox_task(
        self,
        task_id: int,
        worker_id: str,
        success: bool,
        last_error: Optional[str] = None,
        backoff_seconds: Optional[int] = None,
    ) -> bool:
        """
        Update outbox task completion or failure ensuring only the lease owner can update.
        """
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        conn = self.get_connection()
        try:
            with conn:
                task_cur = conn.execute("SELECT * FROM zns_odoo_outbox WHERE id = ?;", (task_id,))
                task = task_cur.fetchone()
                if not task:
                    return False

                # Ensure lease owner match if lease is active
                if task["lease_owner"] and task["lease_owner"] != worker_id and task["lease_expires_at"] and task["lease_expires_at"] >= now:
                    logger.warning(f"[OUTBOX] Worker {worker_id} attempted to update task {task_id} owned by {task['lease_owner']}")
                    return False

                if success:
                    cur = conn.execute(
                        """
                        UPDATE zns_odoo_outbox
                        SET status = 'SUCCEEDED',
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            last_error = NULL,
                            updated_at = :now
                        WHERE id = :id AND (lease_owner = :worker_id OR lease_owner IS NULL);
                        """,
                        {"id": task_id, "worker_id": worker_id, "now": now},
                    )
                    return cur.rowcount == 1
                else:
                    new_retry = task["retry_count"] + 1
                    max_retries = task["max_retries"]
                    is_dead_letter = new_retry >= max_retries
                    new_status = "DEAD_LETTER" if is_dead_letter else "FAILED"

                    delay = backoff_seconds if backoff_seconds is not None else 5 * (4 ** max(0, new_retry - 1))
                    next_retry_at = (now_dt + timedelta(seconds=delay)).isoformat() if not is_dead_letter else now

                    # Sanitize error to ensure no secrets leaked
                    sanitized_err = (last_error or "")[:500]
                    for keyword in ("api_key", "password", "secret", "token", "auth"):
                        if keyword in sanitized_err.lower():
                            sanitized_err = f"[REDACTED_ERROR_CONTENT: {keyword}]"

                    cur = conn.execute(
                        """
                        UPDATE zns_odoo_outbox
                        SET status = :new_status,
                            retry_count = :new_retry,
                            next_retry_at = :next_retry_at,
                            last_error = :last_error,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            updated_at = :now
                        WHERE id = :id AND (lease_owner = :worker_id OR lease_owner IS NULL);
                        """,
                        {
                            "id": task_id,
                            "new_status": new_status,
                            "new_retry": new_retry,
                            "next_retry_at": next_retry_at,
                            "last_error": sanitized_err,
                            "worker_id": worker_id,
                            "now": now,
                        },
                    )
                    return cur.rowcount == 1
        finally:
            conn.close()

    def renew_outbox_lease(
        self,
        task_id: int,
        worker_id: str,
        lease_duration_seconds: int = 60,
    ) -> bool:
        """
        Heartbeat method to extend the lease of an outbox task currently owned by worker_id.
        Returns True if lease was renewed, False if worker lost lease.
        """
        now = _utc_now_iso()
        now_dt = datetime.now(timezone.utc)
        new_expiry = (now_dt + timedelta(seconds=lease_duration_seconds)).isoformat()

        conn = self.get_connection()
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE zns_odoo_outbox
                    SET lease_expires_at = :new_expiry,
                        updated_at = :now
                    WHERE id = :id
                      AND status = 'PROCESSING'
                      AND lease_owner = :worker_id
                      AND (lease_expires_at IS NULL OR lease_expires_at >= :now);
                    """,
                    {
                        "id": task_id,
                        "worker_id": worker_id,
                        "new_expiry": new_expiry,
                        "now": now,
                    },
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def fetch_pending_outbox_tasks(self, now_iso: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch pending and retryable outbox sync tasks eligible for execution."""
        conn = self.get_connection()
        try:
            cur = conn.execute(
                """
                SELECT * FROM zns_odoo_outbox
                WHERE (status = 'PENDING' OR (status = 'FAILED' AND retry_count < max_retries))
                  AND next_retry_at <= ?
                ORDER BY id ASC
                LIMIT ?;
                """,
                (now_iso, limit),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def update_outbox_status(
        self,
        task_id: int,
        status: str,
        retry_count: Optional[int] = None,
        next_retry_at: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> bool:
        """Update outbox task execution status, retry count, and error details."""
        now = _utc_now_iso()
        fields = ["status = ?", "updated_at = ?"]
        params = [status, now]

        if retry_count is not None:
            fields.append("retry_count = ?")
            params.append(retry_count)

        if next_retry_at is not None:
            fields.append("next_retry_at = ?")
            params.append(next_retry_at)

        if last_error is not None:
            fields.append("last_error = ?")
            params.append(last_error)

        params.append(task_id)
        sql = f"UPDATE zns_odoo_outbox SET {', '.join(fields)} WHERE id = ?;"

        conn = self.get_connection()
        try:
            with conn:
                cur = conn.execute(sql, params)
                return cur.rowcount == 1
        finally:
            conn.close()

    def get_outbox_task_by_id(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Fetch an outbox task by primary ID."""
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT * FROM zns_odoo_outbox WHERE id = ?;", (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_metadata(self, message_id: str, **fields) -> Optional[Dict[str, Any]]:
        """
        Update message metadata WITHOUT changing status.
        """
        if "status" in fields:
            raise ValueError("update_metadata cannot modify 'status'. Use transition_message instead.")

        now = _utc_now_iso()
        fields["updated_at"] = now
        set_clauses = [f"{k} = :{k}" for k in fields.keys()]
        params = {"id": message_id}
        params.update(fields)

        sql = f"UPDATE zns_messages SET {', '.join(set_clauses)} WHERE id = :id;"
        conn = self.get_connection()
        try:
            with conn:
                conn.execute(sql, params)
            return self.get_message_by_id(message_id)
        finally:
            conn.close()

    def record_event(
        self,
        message_id: str,
        event_type: str,
        new_status: str,
        previous_status: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        source: str = "internal",
        occurred_at: Optional[str] = None,
    ) -> str:
        """Standalone event logging for a message."""
        conn = self.get_connection()
        try:
            with conn:
                return self._record_event_locked(
                    conn,
                    message_id=message_id,
                    event_type=event_type,
                    new_status=new_status,
                    previous_status=previous_status,
                    payload=payload,
                    source=source,
                    occurred_at=occurred_at or _utc_now_iso(),
                )
        finally:
            conn.close()

    def _record_event_locked(
        self,
        conn: sqlite3.Connection,
        message_id: str,
        event_type: str,
        new_status: str,
        previous_status: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        source: str = "internal",
        occurred_at: Optional[str] = None,
    ) -> str:
        """Internal helper to write to zns_message_events under active transaction."""
        event_id = str(uuid.uuid4())
        now = _utc_now_iso()
        sanitized_json = None
        if payload is not None:
            clean = sanitize_payload(payload)
            sanitized_json = json.dumps(clean, ensure_ascii=False)

        conn.execute(
            """
            INSERT INTO zns_message_events (
                id, message_id, event_type, previous_status,
                new_status, payload_sanitized, source, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                event_id,
                message_id,
                event_type,
                previous_status,
                new_status,
                sanitized_json,
                source,
                occurred_at or now,
                now,
            ),
        )
        return event_id

    def record_diagnostic(
        self,
        raw_event_name: Optional[str] = None,
        app_id: Optional[str] = None,
        sender_id: Optional[str] = None,
        zalo_msg_id: Optional[str] = None,
        tracking_id: Optional[str] = None,
        signature_valid: bool = False,
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Record quarantine / diagnostic entry for unmatched or invalid webhooks."""
        diag_id = str(uuid.uuid4())
        now = _utc_now_iso()
        sanitized_json = None
        if payload is not None:
            clean = sanitize_payload(payload)
            sanitized_json = json.dumps(clean, ensure_ascii=False)

        conn = self.get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO zns_webhook_diagnostics (
                        id, raw_event_name, app_id, sender_id,
                        zalo_msg_id, tracking_id, signature_valid,
                        reason, payload_sanitized, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        diag_id,
                        raw_event_name,
                        app_id,
                        sender_id,
                        zalo_msg_id,
                        tracking_id,
                        1 if signature_valid else 0,
                        reason,
                        sanitized_json,
                        now,
                    ),
                )
            return diag_id
        finally:
            conn.close()

    def get_stale_accepted_messages(
        self,
        threshold_seconds: int = 1800,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Find messages in ACCEPTED state that have not received delivery confirmation
        longer than threshold_seconds.
        """
        conn = self.get_connection()
        try:
            cur = conn.execute(
                """
                SELECT * FROM zns_messages
                WHERE status = 'ACCEPTED'
                ORDER BY accepted_at ASC
                LIMIT ?;
                """,
                (limit,),
            )
            rows = cur.fetchall()
            stale = []
            now_ts = datetime.now(timezone.utc).timestamp()
            for r in rows:
                acc_at_str = r["accepted_at"] or r["created_at"]
                try:
                    acc_dt = datetime.fromisoformat(acc_at_str.replace("Z", "+00:00"))
                    if (now_ts - acc_dt.timestamp()) >= threshold_seconds:
                        stale.append(dict(r))
                except Exception:
                    stale.append(dict(r))
            return stale
        finally:
            conn.close()

    def cleanup_old_records(
        self,
        retention_days: int = 90,
        diagnostics_retention_days: int = 30,
    ) -> Dict[str, int]:
        """
        Data Retention Cleanup:
        - Only deletes terminal messages (DELIVERED, REJECTED, CANCELLED) older than retention_days.
        - Preserves pending and indeterminate messages (QUEUED, SUBMITTING, ACCEPTED, SUBMISSION_UNKNOWN, DELIVERY_UNKNOWN).
        - Deletes webhook diagnostics older than diagnostics_retention_days.
        """
        now = datetime.now(timezone.utc)
        msg_cutoff = (now - timedelta(days=retention_days)).isoformat()
        diag_cutoff = (now - timedelta(days=diagnostics_retention_days)).isoformat()

        conn = self.get_connection()
        try:
            with conn:
                cur_msg = conn.execute(
                    """
                    DELETE FROM zns_messages
                    WHERE status IN ('DELIVERED', 'REJECTED', 'CANCELLED')
                      AND created_at < ?
                      AND id NOT IN (
                          SELECT message_id FROM zns_odoo_outbox
                          WHERE status IN ('PENDING', 'PROCESSING', 'FAILED')
                      );
                    """,
                    (msg_cutoff,),
                )
                deleted_messages = cur_msg.rowcount

                cur_diag = conn.execute(
                    """
                    DELETE FROM zns_webhook_diagnostics
                    WHERE created_at < ?;
                    """,
                    (diag_cutoff,),
                )
                deleted_diagnostics = cur_diag.rowcount

                cur_outbox = conn.execute(
                    """
                    DELETE FROM zns_odoo_outbox
                    WHERE status = 'SUCCEEDED'
                      AND updated_at < ?;
                    """,
                    (msg_cutoff,),
                )
                deleted_outbox = cur_outbox.rowcount

            return {
                "deleted_messages": deleted_messages,
                "deleted_diagnostics": deleted_diagnostics,
                "deleted_outbox": deleted_outbox,
            }
        finally:
            conn.close()

    def query_messages(
        self,
        filters: Optional[Dict[str, Any]] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Query messages with pagination and comprehensive filtering.
        Returns (messages_list, total_count).
        """
        filters = filters or {}
        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        offset = (page - 1) * page_size

        where_clauses = ["1=1"]
        params: List[Any] = []

        if filters.get("status"):
            st = filters["status"]
            if isinstance(st, list):
                where_clauses.append(f"status IN ({','.join(['?']*len(st))})")
                params.extend(st)
            else:
                where_clauses.append("status = ?")
                params.append(st)

        if filters.get("app_key"):
            where_clauses.append("app_key = ?")
            params.append(filters["app_key"])

        if filters.get("template_type"):
            where_clauses.append("template_type = ?")
            params.append(filters["template_type"])

        if filters.get("business_reference"):
            where_clauses.append("business_reference LIKE ?")
            params.append(f"%{filters['business_reference']}%")

        if filters.get("source_model"):
            where_clauses.append("source_model = ?")
            params.append(filters["source_model"])

        if filters.get("source_record_id") is not None:
            where_clauses.append("source_record_id = ?")
            params.append(int(filters["source_record_id"]))

        if filters.get("sent_by_user_id") is not None:
            where_clauses.append("sent_by_user_id = ?")
            params.append(int(filters["sent_by_user_id"]))

        if filters.get("sent_by_user_name"):
            where_clauses.append("sent_by_user_name LIKE ?")
            params.append(f"%{filters['sent_by_user_name']}%")

        if filters.get("phone_hash"):
            where_clauses.append("phone_hash = ?")
            params.append(filters["phone_hash"])

        if filters.get("from_date"):
            where_clauses.append("created_at >= ?")
            params.append(filters["from_date"])

        if filters.get("to_date"):
            to_date_str = str(filters["to_date"])
            if len(to_date_str) == 10:  # e.g. YYYY-MM-DD
                to_date_str = f"{to_date_str}T23:59:59.999999Z"
            where_clauses.append("created_at <= ?")
            params.append(to_date_str)

        where_sql = " AND ".join(where_clauses)

        conn = self.get_connection()
        try:
            count_cur = conn.execute(f"SELECT COUNT(*) as total FROM zns_messages WHERE {where_sql};", params)
            total = count_cur.fetchone()["total"]

            query_sql = f"""
                SELECT * FROM zns_messages
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?;
            """
            item_params = list(params) + [page_size, offset]
            cur = conn.execute(query_sql, item_params)
            rows = [dict(r) for r in cur.fetchall()]

            return rows, total
        finally:
            conn.close()

    def query_events_for_message(self, message_id: str) -> List[Dict[str, Any]]:
        """Fetch all timeline events for a message in chronological order."""
        conn = self.get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM zns_message_events WHERE message_id = ? ORDER BY occurred_at ASC, created_at ASC;",
                (message_id,),
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("payload_sanitized"):
                    try:
                        d["payload"] = json.loads(d["payload_sanitized"])
                    except Exception:
                        d["payload"] = d["payload_sanitized"]
                else:
                    d["payload"] = None
                rows.append(d)
            return rows
        finally:
            conn.close()

    def query_statistics(self, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Aggregate comprehensive metrics:
        - Total requests
        - Accepted, Delivered, Rejected, Submission Unknown, Delivery Unknown counts
        - Rates (Acceptance Rate, Delivery Rate)
        - Latency percentiles (average, p50, p90, p99)
        - Breakdowns by template, app, CS user, top errors, latest quota
        """
        filters = filters or {}
        where_clauses = ["1=1"]
        params: List[Any] = []

        if filters.get("app_key"):
            where_clauses.append("app_key = ?")
            params.append(filters["app_key"])

        if filters.get("template_type"):
            where_clauses.append("template_type = ?")
            params.append(filters["template_type"])

        if filters.get("from_date"):
            where_clauses.append("created_at >= ?")
            params.append(filters["from_date"])

        if filters.get("to_date"):
            to_date_str = str(filters["to_date"])
            if len(to_date_str) == 10:
                to_date_str = f"{to_date_str}T23:59:59.999999Z"
            where_clauses.append("created_at <= ?")
            params.append(to_date_str)

        where_sql = " AND ".join(where_clauses)

        conn = self.get_connection()
        try:
            # 1. Total counts by status
            cur = conn.execute(
                f"""
                SELECT
                    COUNT(*) as total_requests,
                    SUM(CASE WHEN status = 'QUEUED' THEN 1 ELSE 0 END) as count_queued,
                    SUM(CASE WHEN status = 'SUBMITTING' THEN 1 ELSE 0 END) as count_submitting,
                    SUM(CASE WHEN status = 'ACCEPTED' THEN 1 ELSE 0 END) as count_accepted,
                    SUM(CASE WHEN status = 'DELIVERED' THEN 1 ELSE 0 END) as count_delivered,
                    SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END) as count_rejected,
                    SUM(CASE WHEN status = 'SUBMISSION_UNKNOWN' THEN 1 ELSE 0 END) as count_submission_unknown,
                    SUM(CASE WHEN status = 'DELIVERY_UNKNOWN' THEN 1 ELSE 0 END) as count_delivery_unknown,
                    SUM(CASE WHEN status = 'CANCELLED' THEN 1 ELSE 0 END) as count_cancelled
                FROM zns_messages
                WHERE {where_sql};
                """,
                params,
            )
            counts_row = cur.fetchone()
            total_requests = counts_row["total_requests"] or 0
            count_accepted = counts_row["count_accepted"] or 0
            count_delivered = counts_row["count_delivered"] or 0
            count_rejected = counts_row["count_rejected"] or 0
            count_sub_unknown = counts_row["count_submission_unknown"] or 0
            count_del_unknown = counts_row["count_delivery_unknown"] or 0
            count_queued = counts_row["count_queued"] or 0
            count_submitting = counts_row["count_submitting"] or 0
            count_cancelled = counts_row["count_cancelled"] or 0

            # Total processed/attempted (excluding QUEUED/SUBMITTING)
            total_accepted_ever = count_accepted + count_delivered + count_del_unknown
            total_attempts = total_accepted_ever + count_rejected + count_sub_unknown

            acceptance_rate = (
                round((total_accepted_ever / total_attempts) * 100, 2)
                if total_attempts > 0
                else 0.0
            )
            delivery_rate = (
                round((count_delivered / total_accepted_ever) * 100, 2)
                if total_accepted_ever > 0
                else 0.0
            )

            # 2. Delivery Latency calculation
            lat_cur = conn.execute(
                f"""
                SELECT accepted_at, delivered_at
                FROM zns_messages
                WHERE status = 'DELIVERED'
                  AND accepted_at IS NOT NULL
                  AND delivered_at IS NOT NULL
                  AND {where_sql};
                """,
                params,
            )
            latencies = []
            for r in lat_cur.fetchall():
                try:
                    t_acc = datetime.fromisoformat(r["accepted_at"].replace("Z", "+00:00")).timestamp()
                    t_del = datetime.fromisoformat(r["delivered_at"].replace("Z", "+00:00")).timestamp()
                    diff = t_del - t_acc
                    if diff >= 0:
                        latencies.append(diff)
                except Exception:
                    pass

            latencies.sort()
            avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else None
            p50_latency = round(latencies[int(len(latencies) * 0.50)], 2) if latencies else None
            p90_latency = round(latencies[int(len(latencies) * 0.90)], 2) if latencies else None
            p99_latency = round(latencies[int(len(latencies) * 0.99)], 2) if latencies else None

            # 3. By App breakdown
            app_cur = conn.execute(
                f"""
                SELECT app_key,
                       COUNT(*) as total,
                       SUM(CASE WHEN status = 'DELIVERED' THEN 1 ELSE 0 END) as delivered,
                       SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END) as rejected
                FROM zns_messages
                WHERE {where_sql}
                GROUP BY app_key;
                """,
                params,
            )
            by_app = {r["app_key"]: dict(r) for r in app_cur.fetchall()}

            # 4. By Template breakdown
            tpl_cur = conn.execute(
                f"""
                SELECT template_type,
                       COUNT(*) as total,
                       SUM(CASE WHEN status = 'DELIVERED' THEN 1 ELSE 0 END) as delivered,
                       SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END) as rejected
                FROM zns_messages
                WHERE {where_sql}
                GROUP BY template_type;
                """,
                params,
            )
            by_template = {r["template_type"]: dict(r) for r in tpl_cur.fetchall()}

            # 5. Top error codes
            err_cur = conn.execute(
                f"""
                SELECT error_code, error_message, COUNT(*) as count
                FROM zns_messages
                WHERE status = 'REJECTED' AND error_code IS NOT NULL AND {where_sql}
                GROUP BY error_code, error_message
                ORDER BY count DESC
                LIMIT 10;
                """,
                params,
            )
            top_errors = [dict(r) for r in err_cur.fetchall()]

            # 6. By CS User breakdown
            user_cur = conn.execute(
                f"""
                SELECT sent_by_user_id, sent_by_user_name,
                       COUNT(*) as total,
                       SUM(CASE WHEN status = 'DELIVERED' THEN 1 ELSE 0 END) as delivered,
                       SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END) as rejected
                FROM zns_messages
                WHERE {where_sql} AND sent_by_user_name IS NOT NULL
                GROUP BY sent_by_user_id, sent_by_user_name
                ORDER BY total DESC
                LIMIT 20;
                """,
                params,
            )
            by_cs_user = [dict(r) for r in user_cur.fetchall()]

            # 7. Latest Quota per App
            quota_by_app = {}
            for k in ("ord", "bon"):
                q_cur = conn.execute(
                    """
                    SELECT quota_daily, quota_remaining, updated_at
                    FROM zns_messages
                    WHERE app_key = ? AND quota_remaining IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 1;
                    """,
                    (k,),
                )
                q_row = q_cur.fetchone()
                if q_row:
                    quota_by_app[k] = {
                        "quota_daily": q_row["quota_daily"],
                        "quota_remaining": q_row["quota_remaining"],
                        "updated_at": q_row["updated_at"],
                    }
                else:
                    quota_by_app[k] = None

            return {
                "total_requests": total_requests,
                "counts": {
                    "queued": count_queued,
                    "submitting": count_submitting,
                    "accepted": count_accepted,
                    "delivered": count_delivered,
                    "rejected": count_rejected,
                    "submission_unknown": count_sub_unknown,
                    "delivery_unknown": count_del_unknown,
                    "cancelled": count_cancelled,
                },
                "rates": {
                    "acceptance_rate_pct": acceptance_rate,
                    "delivery_rate_pct": delivery_rate,
                    "total_attempts": total_attempts,
                    "total_accepted_ever": total_accepted_ever,
                },
                "latency_seconds": {
                    "sample_size": len(latencies),
                    "average": avg_latency,
                    "p50": p50_latency,
                    "p90": p90_latency,
                    "p99": p99_latency,
                },
                "latest_quota_by_app": quota_by_app,
                "breakdowns": {
                    "by_app": by_app,
                    "by_template": by_template,
                    "by_cs_user": by_cs_user,
                    "top_errors": top_errors,
                },
            }
        finally:
            conn.close()


# Singleton repository instance
_repo_instance: Optional[ZNSRepository] = None
_repo_lock = threading.Lock()


def get_repository() -> ZNSRepository:
    """Get or create singleton repository instance."""
    global _repo_instance
    with _repo_lock:
        target_path = Config.ZNS_TRACKING_DB_PATH
        if _repo_instance is None or _repo_instance.db_path != target_path:
            _repo_instance = ZNSRepository(db_path=target_path)
    return _repo_instance


def reset_repository():
    """Reset repository singleton (useful for test isolation)."""
    global _repo_instance
    with _repo_lock:
        _repo_instance = None
