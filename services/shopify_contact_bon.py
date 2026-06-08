"""
Shopify Bonario → Odoo Contact Sync Service.
Store: 56562d-2.myshopify.com
Metafield key: custom.bn_1 (Bạn là) — Vietnamese values
"""

import json
import logging
import re
import requests
from config import Config
from services.odoo_client import odoo
from utils.phone import clean_phone_for_shopify

logger = logging.getLogger(__name__)


# ─── Partner Type Mapping (Vietnamese values from custom.bn_1) ───
PARTNER_TYPE_MAP_BON = {
    "": "CN",
    "kiến trúc sư": "DB - Design and build",
    "kien truc su": "DB - Design and build",
    "nhà thầu dân dụng": "CONS - Contractor",
    "nha thau dan dung": "CONS - Contractor",
    "nhà thiết kế nội thất": "DB - Design and build",
    "nha thiet ke noi that": "DB - Design and build",
    "chủ nhà": "CN",
    "chu nha": "CN",
}


def _fetch_metafields(customer_id: int) -> list:
    """Fetch Shopify customer metafields via REST API (Bonario store)."""
    url = (
        f"https://{Config.BONARIO_SHOPIFY_STORE}/admin/api/2024-07"
        f"/customers/{customer_id}/metafields.json"
    )
    resp = requests.get(
        url,
        headers={"X-Shopify-Access-Token": Config.BONARIO_SHOPIFY_ACCESS_TOKEN},
        timeout=10,
    )
    if resp.status_code == 200:
        metafields = resp.json().get("metafields", [])
        logger.info(f"Bonario metafields for customer {customer_id}: {len(metafields)} found")
        for m in metafields:
            logger.info(f"  meta: ns={m.get('namespace')} key={m.get('key')} val={str(m.get('value'))[:120]}")
        return metafields
    logger.warning(f"Failed to fetch Bonario metafields for customer {customer_id}: {resp.status_code}")
    return []


def _get_meta(metafields: list, key: str) -> str:
    """Get metafield value by key (supports both 'custom.bn_1' and 'bn_1')."""
    key_lower = key.lower().strip()
    for m in metafields:
        meta_key = (m.get("key") or "").lower().strip()
        if meta_key == key_lower or meta_key.endswith("." + key_lower):
            return (m.get("value") or "").strip()
    return ""


def _get_meta_fuzzy(metafields: list, *patterns: str) -> str:
    """Get metafield value whose key contains any of the given patterns (case-insensitive)."""
    for m in metafields:
        meta_key = (m.get("key") or "").lower().strip()
        for p in patterns:
            if p.lower() in meta_key or meta_key in p.lower():
                return (m.get("value") or "").strip()
    return ""


def _normalize_list_value(raw: str) -> str:
    """If value is a JSON array (multi-select metafield), join with commas."""
    if not raw:
        return ""
    if raw.startswith("[") and raw.endswith("]"):
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                return ", ".join(items)
        except (json.JSONDecodeError, TypeError):
            pass
    return raw


def _resolve_phone(shop_data: dict) -> str:
    """Extract and clean phone from Shopify customer data."""
    candidates = [
        shop_data.get("phone"),
        shop_data.get("phone_number"),
    ]

    for addr in shop_data.get("addresses", []):
        candidates.append(addr.get("phone"))
        candidates.append(addr.get("phone_number"))

    default_addr = shop_data.get("default_address") or {}
    candidates.append(default_addr.get("phone"))
    candidates.append(default_addr.get("phone_number"))

    for c in candidates:
        cleaned = clean_phone_for_shopify(c or "")
        if cleaned:
            return cleaned
    return ""


def sync_bonario_customer(shop_data: dict) -> dict:
    """
    Process a Shopify Bonario customer/create webhook payload and create an Odoo contact.

    Args:
        shop_data: The Shopify webhook JSON payload for customer/create event.

    Returns:
        dict with result info (created/skipped, odoo_id, etc.)
    """
    customer_id = shop_data.get("id")
    raw_email = (shop_data.get("email") or "").strip().lower()

    # ── Fetch metafields ───
    metafields = _fetch_metafields(customer_id)

    # ── Check existing contact in Odoo ───
    existing = odoo.search_read(
        "res.partner",
        [("email", "=", raw_email)],
        fields=["id", "name", "email"],
        limit=1,
    )

    if existing:
        logger.info(f"Bonario contact already exists in Odoo: {existing[0]['id']} ({raw_email})")
        return {
            "status": "skipped",
            "reason": "contact_exists",
            "odoo_id": existing[0]["id"],
            "email": raw_email,
        }

    # ─── Company detection ───
    company_name = (
        _get_meta(metafields, "company_name")
        or _get_meta(metafields, "company name")
        or _get_meta(metafields, "companyname")
        or (shop_data.get("default_address") or {}).get("company", "").strip()
    )
    is_company = bool(company_name)

    # ─── Name resolution ───
    first = (shop_data.get("first_name") or "").strip()
    last = (shop_data.get("last_name") or "").strip()
    full_name = f"{first} {last}".strip()

    if is_company:
        name = company_name
        representative = full_name
    else:
        name = full_name or raw_email or "Shopify Customer"
        representative = ""

    # ─── Partner type mapping (Vietnamese values from custom.bn_1) ───
    you_are = _get_meta(metafields, "custom.bn_1").lower().strip()
    partner_type = PARTNER_TYPE_MAP_BON.get(you_are, "Others")

    # ── Phone ───
    phone_clean = _resolve_phone(shop_data)

    # ─── Product Preferences ───
    product_prefs_raw = _get_meta_fuzzy(
        metafields,
        "select_the_products",
        "products_to_explore",
        "product_preferences",
        "product_interests",
        "select_the_products_youd_like_to_explore",
    )
    product_prefs = _normalize_list_value(product_prefs_raw)
    logger.info(f"Bonario product preferences: raw={product_prefs_raw!r} → normalized={product_prefs!r}")

    # ─── Create Odoo contact ──
    values = {
        "name": name,
        "email": raw_email,
        "phone": phone_clean,
        "is_company": is_company,
        "x_studio_partner_type": partner_type,
        "user_id": Config.BONARIO_ODOO_SALESPERSON_ID,
        "x_studio_sales_team": Config.BONARIO_ODOO_SALES_TEAM_ID,
        "x_studio_source": Config.BONARIO_ODOO_SOURCE_ID,
        "company_id": Config.BONARIO_ODOO_COMPANY_ID,
        "x_studio_membership": True,
    }

    if representative:
        values["x_studio_i_din_cng_ty"] = representative

    if product_prefs:
        values["x_studio_personal_preferences_2"] = product_prefs

    odoo_id = odoo.create(
        "res.partner",
        values,
        context={"allowed_company_ids": [Config.BONARIO_ODOO_COMPANY_ID]},
    )

    logger.info(f"Created Bonario Odoo contact: {odoo_id} — {name} ({raw_email})")
    return {
        "status": "created",
        "odoo_id": odoo_id,
        "name": name,
        "email": raw_email,
        "is_company": is_company,
        "partner_type": partner_type,
        "product_preferences": product_prefs,
    }
