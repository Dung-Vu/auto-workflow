"""
Centralized configuration loader.
All credentials and settings are loaded from .env file.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Application configuration from environment variables."""

    # Environment mode: 'development', 'staging', 'production', 'test'
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()

    # Flask
    FLASK_PORT = int(os.getenv("FLASK_PORT") or "5050")
    FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    # Odoo
    ODOO_URL = os.getenv("ODOO_URL", "")
    ODOO_DB = os.getenv("ODOO_DB", "")
    ODOO_UID = int(os.getenv("ODOO_UID") or "0") or None  # Direct UID (preferred)
    ODOO_API_KEY = os.getenv("ODOO_API_KEY", "")
    ODOO_USER = os.getenv("ODOO_USER", "")  # Optional: only needed if ODOO_UID not set

    # Odoo — Deadline Watcher
    DEADLINE_POLL_INTERVAL = int(os.getenv("DEADLINE_POLL_INTERVAL", "60"))  # seconds

    # Odoo — CRM Lost Watcher
    CRM_POLL_INTERVAL = int(os.getenv("CRM_POLL_INTERVAL", "60"))  # seconds

    # Odoo — Completion Days Dashboard (weekly refresh, Monday 08:00 ICT)
    COMPLETION_DAYS_DASHBOARD_ID = int(os.getenv("COMPLETION_DAYS_DASHBOARD_ID", "278"))
    COMPLETION_DAYS_ENABLED = os.getenv("COMPLETION_DAYS_ENABLED", "true").lower() == "true"

    # Odoo — Dashboard 259 Approval (monthly refresh, 1st of month 06:00 ICT)
    DASHBOARD_259_ID = int(os.getenv("DASHBOARD_259_ID", "259"))
    DASHBOARD_259_APPROVAL_TYPE_ID = int(os.getenv("DASHBOARD_259_APPROVAL_TYPE_ID", "13"))
    DASHBOARD_259_ENABLED = os.getenv("DASHBOARD_259_ENABLED", "true").lower() == "true"
    # Separate UID for approval queries (original script used UID 145)
    DASHBOARD_259_ODOO_UID = int(os.getenv("DASHBOARD_259_ODOO_UID") or "0") or None
    DASHBOARD_259_ODOO_API_KEY = os.getenv("DASHBOARD_259_ODOO_API_KEY", "")

    # Odoo — KHO MAU CTL Overdue (daily 11:00 ICT / 04:00 UTC)
    CTL_ENABLED = os.getenv("CTL_ENABLED", "true").lower() == "true"
    CTL_PICKING_TYPE_ID = int(os.getenv("CTL_PICKING_TYPE_ID", "230"))
    CTL_LOCATION_ID = int(os.getenv("CTL_LOCATION_ID", "63"))
    CTL_USER_ID = int(os.getenv("CTL_USER_ID", "218"))
    CTL_ACTIVITY_TYPE_NAME = os.getenv("CTL_ACTIVITY_TYPE_NAME", "CTL - Phiếu mượn quá hạn")
    CTL_LENDING_MIN_DATE = os.getenv("CTL_LENDING_MIN_DATE", "2026-01-01")

    # Odoo — Checklist Overdue (monthly 1st, 00:00 ICT / prev day 17:00 UTC)
    CHECKLIST_OVERDUE_ENABLED = os.getenv("CHECKLIST_OVERDUE_ENABLED", "true").lower() == "true"
    CHECKLIST_ACTIVITY_TYPE_ID = int(os.getenv("CHECKLIST_ACTIVITY_TYPE_ID", "108"))

    # Odoo — Dashboard 247 SC activities (monthly 1st, 06:00 ICT / 23:00 UTC prev day)
    DASHBOARD_247_ID = int(os.getenv("DASHBOARD_247_ID", "271"))
    DASHBOARD_247_ENABLED = os.getenv("DASHBOARD_247_ENABLED", "true").lower() == "true"

    # Odoo — Section 3 Timeline (monthly 1st, 06:00 ICT / 23:00 UTC prev day)
    SECTION3_TIMELINE_ENABLED = os.getenv("SECTION3_TIMELINE_ENABLED", "true").lower() == "true"

    # Odoo — Section 5.2 SO-to-FSM violations (monthly 1st, 06:00 ICT / 23:00 UTC prev day)
    SECTION5_2_ENABLED = os.getenv("SECTION5_2_ENABLED", "true").lower() == "true"

    # Odoo — Commission Revenue (polling, replaces base.automation ID 64)
    COMMISSION_REVENUE_ENABLED = os.getenv("COMMISSION_REVENUE_ENABLED", "true").lower() == "true"
    COMMISSION_REVENUE_POLL_INTERVAL = int(os.getenv("COMMISSION_REVENUE_POLL_INTERVAL", "60"))  # seconds
    # Product category IDs excluded from commission base (discount/voucher categories)
    COMMISSION_REVENUE_EXCLUDED_CATEGORIES = [
        int(x) for x in os.getenv(
            "COMMISSION_REVENUE_EXCLUDED_CATEGORIES", "19,998,999,1097"
        ).split(",") if x.strip()
    ]
    # Product name prefixes (pipe-separated) excluded from commission base.
    # Matches if display_name STARTS WITH any of these strings.
    # Use this for products inside a shared category that should be excluded
    # individually (e.g. MOTOR construction-fee variants that share category
    # with revenue-counting MOTOR products).
    COMMISSION_REVENUE_EXCLUDED_NAME_PREFIXES = [
        x.strip() for x in os.getenv(
            "COMMISSION_REVENUE_EXCLUDED_NAME_PREFIXES", ""
        ).split("|") if x.strip()
    ]

    # Odoo — OP Delivery Date Sync (polling, replaces base.automation ID 80)
    OP_DELIVERY_DATE_ENABLED = os.getenv("OP_DELIVERY_DATE_ENABLED", "true").lower() == "true"
    OP_DELIVERY_DATE_POLL_INTERVAL = int(os.getenv("OP_DELIVERY_DATE_POLL_INTERVAL", "60"))  # seconds

    # Odoo — Approval Document Number (webhook, replaces base.automation ID 36)
    APPROVAL_DOC_NUMBER_ENABLED = os.getenv("APPROVAL_DOC_NUMBER_ENABLED", "true").lower() == "true"
    APPROVAL_DOC_SEQUENCE_CODE = os.getenv("APPROVAL_DOC_SEQUENCE_CODE", "approval.banhanh")
    APPROVAL_DOC_FALLBACK_NUM = os.getenv("APPROVAL_DOC_FALLBACK_NUM", "00000")

    # Odoo — Follow Activity on Order State Change (polling, new automation)
    FOLLOW_ACTIVITY_ENABLED = os.getenv("FOLLOW_ACTIVITY_ENABLED", "true").lower() == "true"
    FOLLOW_ACTIVITY_POLL_INTERVAL = int(os.getenv("FOLLOW_ACTIVITY_POLL_INTERVAL", "30"))  # seconds
    FOLLOW_ACTIVITY_TRIGGER_VALUES = [
        v.strip() for v in os.getenv(
            "FOLLOW_ACTIVITY_TRIGGER_VALUES", "Hàng về/Chờ thi công"
        ).split(",") if v.strip()
    ]
    FOLLOW_ACTIVITY_TYPE_ID = int(os.getenv("FOLLOW_ACTIVITY_TYPE_ID", "72"))
    FOLLOW_ACTIVITY_FIXED_USER_IDS = [
        int(x) for x in os.getenv(
            "FOLLOW_ACTIVITY_FIXED_USER_IDS", "237,253"
        ).split(",") if x.strip()
    ]
    FOLLOW_ACTIVITY_COMPANY_USER_IDS = {
        int(company_id.strip()): int(user_id.strip())
        for entry in os.getenv(
            "FOLLOW_ACTIVITY_COMPANY_USER_IDS", "1:218,11:9"
        ).split(",") if entry.strip()
        for company_id, user_id in [entry.split(":", 1)]
    }
    FOLLOW_ACTIVITY_DEADLINE_DAYS = int(os.getenv("FOLLOW_ACTIVITY_DEADLINE_DAYS", "2"))

    # Odoo — Return Activity (poll stock.picking returns → activity on return slip)
    RETURN_ACTIVITY_ENABLED = os.getenv("RETURN_ACTIVITY_ENABLED", "true").lower() == "true"
    RETURN_ACTIVITY_POLL_INTERVAL = int(os.getenv("RETURN_ACTIVITY_POLL_INTERVAL", "5"))  # seconds
    # BON SC: TRẦN THIỆN NHÂN (295), NGUYỄN THỊ HẰNG (293)
    RETURN_ACTIVITY_USER_IDS = [
        int(x.strip()) for x in os.getenv("RETURN_ACTIVITY_USER_IDS", "295,293").split(",")
        if x.strip().isdigit()
    ]
    RETURN_ACTIVITY_SUMMARY = os.getenv("RETURN_ACTIVITY_SUMMARY", "Return mới cần xem")
    RETURN_ACTIVITY_TYPE_ID = int(os.getenv("RETURN_ACTIVITY_TYPE_ID", "4"))  # To-Do
    RETURN_ACTIVITY_DEADLINE_DAYS = int(os.getenv("RETURN_ACTIVITY_DEADLINE_DAYS", "0"))
    # incoming = return of delivery; internal = return of internal/CTL transfers
    RETURN_ACTIVITY_PICKING_CODES = [
        c.strip()
        for c in os.getenv("RETURN_ACTIVITY_PICKING_CODES", "incoming,internal").split(",")
        if c.strip()
    ]

    # Odoo — Test Server (testing0808.odoo.com)
    # Used by Lot/Serial automation and any future test-only modules.
    ODOO_TEST_URL = os.getenv("ODOO_TEST_URL", "")
    ODOO_TEST_DB = os.getenv("ODOO_TEST_DB", "")
    ODOO_TEST_UID = int(os.getenv("ODOO_TEST_UID") or "0") or None
    ODOO_TEST_API_KEY = os.getenv("ODOO_TEST_API_KEY", "")

    # Lot/Serial — warehouse whitelist for serial rename operations
    LOT_SERIAL_WHITELIST_WAREHOUSES = [
        w.strip() for w in os.getenv(
            "LOT_SERIAL_WHITELIST_WAREHOUSES", "ORDST,ORDAP"
        ).split(",") if w.strip()
    ]
    LOT_SERIAL_SOURCE_WAREHOUSES = [
        w.strip() for w in os.getenv(
            "LOT_SERIAL_SOURCE_WAREHOUSES", "MID"
        ).split(",") if w.strip()
    ]
    LOT_SERIAL_ENABLED = os.getenv("LOT_SERIAL_ENABLED", "false").lower() == "true"
    LOT_SERIAL_API_TOKEN = os.getenv("LOT_SERIAL_API_TOKEN", "")
    LOT_SERIAL_ALLOW_SCHEMA_SETUP = os.getenv(
        "LOT_SERIAL_ALLOW_SCHEMA_SETUP", "false"
    ).lower() == "true"

    # Odoo — Auto-Conducted (separate UID/key for meeting tick automation)
    CONDUCTED_ODOO_UID = int(os.getenv("CONDUCTED_ODOO_UID") or "0") or None
    CONDUCTED_ODOO_API_KEY = os.getenv("CONDUCTED_ODOO_API_KEY", "")

    # Shopify — Ordinaire
    SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "")
    SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    SHOPIFY_ODOO_COMPANY_ID = int(os.getenv("SHOPIFY_ODOO_COMPANY_ID", "9"))
    SHOPIFY_ODOO_SALESPERSON_ID = int(os.getenv("SHOPIFY_ODOO_SALESPERSON_ID", "6"))
    SHOPIFY_ODOO_SALES_TEAM_ID = int(os.getenv("SHOPIFY_ODOO_SALES_TEAM_ID", "15"))
    SHOPIFY_ODOO_SOURCE_ID = int(os.getenv("SHOPIFY_ODOO_SOURCE_ID", "46"))

    # Shopify — Bonario
    BONARIO_SHOPIFY_STORE = os.getenv("BONARIO_SHOPIFY_STORE", "")
    BONARIO_SHOPIFY_ACCESS_TOKEN = os.getenv("BONARIO_SHOPIFY_ACCESS_TOKEN", "")
    BONARIO_ODOO_COMPANY_ID = int(os.getenv("BONARIO_ODOO_COMPANY_ID") or "1")
    BONARIO_ODOO_SALESPERSON_ID = int(os.getenv("BONARIO_ODOO_SALESPERSON_ID") or "6")
    BONARIO_ODOO_SALES_TEAM_ID = int(os.getenv("BONARIO_ODOO_SALES_TEAM_ID") or "15")
    BONARIO_ODOO_SOURCE_ID = int(os.getenv("BONARIO_ODOO_SOURCE_ID") or "46")

    # Zalo ZNS — ORD (Ordinaire) App
    ZALO_APP_ID = os.getenv("ZALO_APP_ID", "")
    ZALO_SECRET_KEY = os.getenv("ZALO_SECRET_KEY", "")
    ZALO_REDIRECT_URI = os.getenv("ZALO_REDIRECT_URI", "")

    # Zalo ZNS — BON (Bonario) App
    ZALO_BON_APP_ID = os.getenv("ZALO_BON_APP_ID", "")
    ZALO_BON_SECRET_KEY = os.getenv("ZALO_BON_SECRET_KEY", "")

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # ZNS Template IDs
    ZNS_TEMPLATES = {
        "hdsd-eng": "497200",
        "hdsd-vie": "497198",
        "rating-ord-eng": "512158",
        "rating-ord-vie": "510308",
        "rating": "463253",
    }

    # Which Zalo app each template uses: "ord" (default) or "bon"
    ZNS_TEMPLATE_APP = {
        "hdsd-eng": "ord",
        "hdsd-vie": "ord",
        "rating-ord-eng": "ord",
        "rating-ord-vie": "ord",
        "rating": "bon",
    }

    # ZNS template_data field name mapping
    # BON Token Refresh uses "customer_name" instead of "name"
    ZNS_TEMPLATE_DATA_KEYS = {
        "hdsd-eng": {"name_key": "name", "include_date": True},
        "hdsd-vie": {"name_key": "name", "include_date": True},
        "rating-ord-eng": {"name_key": "name", "include_date": False},
        "rating-ord-vie": {"name_key": "name", "include_date": True},
        "rating": {"name_key": "customer_name", "include_date": True},
    }

    # Zalo App & OA Configuration (ORD / Bonario)
    ZALO_APP_ID = os.getenv("ZALO_APP_ID", "")
    ZALO_SECRET_KEY = os.getenv("ZALO_SECRET_KEY", "")
    ZALO_OA_ID = os.getenv("ZALO_OA_ID", "")

    ZALO_BON_APP_ID = os.getenv("ZALO_BON_APP_ID", "")
    ZALO_BON_SECRET_KEY = os.getenv("ZALO_BON_SECRET_KEY", "")
    ZALO_BON_OA_ID = os.getenv("ZALO_BON_OA_ID", "")

    # ZNS Tracking, Security & Persistence
    ZNS_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
    ZNS_TRACKING_DB_PATH = os.getenv(
        "ZNS_TRACKING_DB_PATH",
        os.path.join(ZNS_DATA_DIR, "zns_tracking.sqlite3"),
    )
    ZNS_ADMIN_API_KEY = os.getenv("ZNS_ADMIN_API_KEY", "")
    ZNS_INBOUND_API_KEY = os.getenv("ZNS_INBOUND_API_KEY", "")
    ZNS_INBOUND_AUTH_REQUIRED = os.getenv("ZNS_INBOUND_AUTH_REQUIRED", "true").lower() == "true"
    ZNS_PHONE_HASH_KEY = os.getenv("ZNS_PHONE_HASH_KEY", "")
    ZNS_ALLOW_INSECURE_DEV = os.getenv("ZNS_ALLOW_INSECURE_DEV", "false").lower() == "true"

    # ZNS SLA, Webhook & Reconciliation
    ZNS_DELIVERY_SLA_SECONDS = int(os.getenv("ZNS_DELIVERY_SLA_SECONDS", "1800"))  # 30 mins
    ZNS_RECONCILIATION_ENABLED = os.getenv("ZNS_RECONCILIATION_ENABLED", "true").lower() == "true"
    ZNS_DISABLE_BACKGROUND_DAEMONS = os.getenv("ZNS_DISABLE_BACKGROUND_DAEMONS", "false").lower() in ("1", "true", "yes")
    ZNS_RECONCILIATION_INTERVAL = int(os.getenv("ZNS_RECONCILIATION_INTERVAL", "300"))  # 5 mins
    ZNS_WEBHOOK_REQUIRE_SIGNATURE = os.getenv("ZNS_WEBHOOK_REQUIRE_SIGNATURE", "true").lower() == "true"
    ZNS_WEBHOOK_TIMESTAMP_TOLERANCE = int(os.getenv("ZNS_WEBHOOK_TIMESTAMP_TOLERANCE", "300"))  # 5 mins
    ZNS_MAX_PAYLOAD_BYTES = int(os.getenv("ZNS_MAX_PAYLOAD_BYTES", "102400"))  # 100 KB
    ZNS_RETENTION_DAYS = int(os.getenv("ZNS_RETENTION_DAYS", "90"))
    ZNS_DIAGNOSTICS_RETENTION_DAYS = int(os.getenv("ZNS_DIAGNOSTICS_RETENTION_DAYS", "30"))
