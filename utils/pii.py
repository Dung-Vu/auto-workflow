"""
PII (Personally Identifiable Information) masking and sanitization utilities.
Ensures phone numbers, secrets, and sensitive tokens are not leaked in logs, DB events, or API responses.
"""

import os
import re
import hmac
import hashlib
from typing import Any, Dict, Optional


def mask_phone(phone: str) -> str:
    """
    Mask a phone number for safe display and logging.
    Keeps leading country code / prefix and trailing 3-4 digits, masking middle digits with asterisks.

    Examples:
        "+84987654321" -> "+849****321"
        "0987654321"   -> "098****321"
        "0901234567"   -> "090****567"
        ""             -> ""
    """
    if not phone or not isinstance(phone, str):
        return ""

    phone_clean = phone.strip()
    if len(phone_clean) <= 5:
        return "***"

    if phone_clean.startswith("+"):
        prefix_len = 4  # e.g. +849
        suffix_len = 3  # e.g. 321
    else:
        prefix_len = 3  # e.g. 098
        suffix_len = 3  # e.g. 321

    if len(phone_clean) <= prefix_len + suffix_len:
        return phone_clean[:2] + "****" + phone_clean[-2:]

    prefix = phone_clean[:prefix_len]
    suffix = phone_clean[-suffix_len:]
    return f"{prefix}****{suffix}"


def hash_phone(phone: str, key: Optional[str] = None) -> str:
    """
    Generate deterministic HMAC-SHA256 hash for phone reconciliation without storing plaintext.
    Uses ZNS_PHONE_HASH_KEY from Config if key is not passed.
    Fails closed in production if key is missing and ZNS_ALLOW_INSECURE_DEV is False.
    """
    if not phone or not isinstance(phone, str):
        return ""

    if key is None:
        from config import Config
        key = Config.ZNS_PHONE_HASH_KEY
        if not key:
            if Config.ZNS_ALLOW_INSECURE_DEV:
                key = "dev_fallback_phone_hash_key_do_not_use_in_prod"
            else:
                raise RuntimeError(
                    "ZNS_PHONE_HASH_KEY is not configured in production mode. "
                    "Refusing to hash PII with insecure fallback."
                )

    normalized = re.sub(r"\D", "", phone)
    return hmac.new(key.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def is_valid_vn_phone(phone: str) -> bool:
    """
    Strict validation of Vietnamese mobile phone numbers:
    - Must start with valid VN telecom prefix (03, 05, 07, 08, 09 or +843, +845, +847, +848, +849)
    - Exactly 10 digits (domestic) or 11/12 chars (+84xxxxxxxxx).
    """
    if not phone or not isinstance(phone, str):
        return False

    cleaned = phone.strip().replace(" ", "").replace(".", "").replace("-", "")
    # Check +84 format
    if cleaned.startswith("+84"):
        digits = cleaned[3:]
        if len(digits) == 9 and digits[0] in "35789":
            return True
        return False
    elif cleaned.startswith("84") and len(cleaned) == 11:
        digits = cleaned[2:]
        if len(digits) == 9 and digits[0] in "35789":
            return True
        return False
    elif cleaned.startswith("0"):
        digits = cleaned[1:]
        if len(digits) == 9 and digits[0] in "35789":
            return True
        return False

    return False


_SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "secret_key",
    "api_key",
    "password",
    "code_verifier",
    "token",
    "authorization",
    "x-api-key",
    "x-zns-api-key",
}


def sanitize_payload(obj: Any) -> Any:
    """
    Recursively sanitize a dictionary or list, redacting tokens, secrets, and masking phone numbers.
    """
    if isinstance(obj, dict):
        sanitized = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            if any(sens in k_lower for sens in _SENSITIVE_KEYS):
                sanitized[k] = "[REDACTED]"
            elif "phone" in k_lower and isinstance(v, str):
                sanitized[k] = mask_phone(v)
            else:
                sanitized[k] = sanitize_payload(v)
        return sanitized
    elif isinstance(obj, list):
        return [sanitize_payload(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(sanitize_payload(item) for item in obj)
    return obj
