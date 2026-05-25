"""Phone number normalization utilities — ported from n8n Code nodes."""

import re


def normalize_phone_vn(phone_raw: str) -> str:
    """
    Normalize Vietnamese phone number to +84 XXX XXX XXX format.
    Ported from: Delivery Tracking / Code in JavaScript node.

    Examples:
        "0772990730"   -> "+84 772 990 730"
        "84772990730"  -> "+84 772 990 730"
        "+84772990730" -> "+84 772 990 730"
    """
    if not phone_raw:
        return ""

    # Keep digits only
    digits = re.sub(r"\D", "", phone_raw)
    if not digits:
        return ""

    # Strip leading 0 → add 84
    if digits.startswith("0"):
        digits = "84" + digits[1:]

    # Strip "84" prefix to get the 9-digit core
    if digits.startswith("84"):
        digits = digits[2:]

    # Pad to 9 digits
    digits = digits.zfill(9)

    # Format as +84 XXX XXX XXX
    formatted = f"+84 {digits[:3]} {digits[3:6]} {digits[6:9]}"
    return formatted


def normalize_phone_zalo(phone_raw: str) -> str:
    """
    Normalize phone for Zalo ZNS API (needs +84xxxxxxxxx format, no spaces).
    Ported from: ZNS ORD / Normalize Phone node.
    """
    if not phone_raw:
        return ""

    digits = re.sub(r"\D", "", phone_raw)
    if not digits:
        return ""

    if digits.startswith("0"):
        digits = "+84" + digits[1:]
    elif digits.startswith("84"):
        digits = "+84" + digits[2:]
    else:
        # Assume bare 9-digit number, prepend country code
        digits = "+84" + digits

    return digits


def clean_phone_for_shopify(phone_raw: str) -> str:
    """
    Clean phone number for Shopify contact sync.
    Ported from: Create Contact Odoo / Code node.
    Removes (), -, ., and spaces.
    """
    if not phone_raw:
        return ""
    return re.sub(r"[()\\.\-\s]", "", phone_raw)
