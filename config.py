"""
Centralized configuration loader.
All credentials and settings are loaded from .env file.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Application configuration from environment variables."""

    # Flask
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5050"))
    FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    # Odoo
    ODOO_URL = os.getenv("ODOO_URL", "")
    ODOO_DB = os.getenv("ODOO_DB", "")
    ODOO_UID = int(os.getenv("ODOO_UID", "0")) or None  # Direct UID (preferred)
    ODOO_API_KEY = os.getenv("ODOO_API_KEY", "")
    ODOO_USER = os.getenv("ODOO_USER", "")  # Optional: only needed if ODOO_UID not set

    # Odoo — Deadline Watcher
    DEADLINE_POLL_INTERVAL = int(os.getenv("DEADLINE_POLL_INTERVAL", "30"))  # seconds

    # Odoo — Auto-Conducted (separate UID/key for meeting tick automation)
    CONDUCTED_ODOO_UID = int(os.getenv("CONDUCTED_ODOO_UID", "0")) or None
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
