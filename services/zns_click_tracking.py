"""Signed ZNS CTA click links and first-click synchronization."""

import base64
import hashlib
import hmac
import html
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

from config import Config
from services.zns_repository import ZNSRepository, get_repository

logger = logging.getLogger(__name__)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_click_token(message_id: str) -> str:
    """Create a compact capability token containing a UUID plus a truncated HMAC."""
    secret = str(Config.ZNS_CLICK_SECRET or "")
    if not secret:
        raise RuntimeError("ZNS_CLICK_SECRET is not configured")
    raw_id = uuid.UUID(str(message_id)).bytes
    signature = hmac.new(secret.encode("utf-8"), raw_id, hashlib.sha256).digest()[:16]
    return f"{_b64encode(raw_id)}.{_b64encode(signature)}"


def verify_click_token(token: str) -> Optional[str]:
    """Validate token signature and return the canonical message UUID."""
    secret = str(Config.ZNS_CLICK_SECRET or "")
    if not secret or not token or "." not in token or len(token) > 100:
        return None
    try:
        encoded_id, encoded_sig = token.split(".", 1)
        raw_id = _b64decode(encoded_id)
        supplied = _b64decode(encoded_sig)
        if _b64encode(raw_id) != encoded_id or _b64encode(supplied) != encoded_sig:
            return None
        if len(raw_id) != 16 or len(supplied) != 16:
            return None
        expected = hmac.new(secret.encode("utf-8"), raw_id, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(supplied, expected):
            return None
        return str(uuid.UUID(bytes=raw_id))
    except (ValueError, TypeError):
        return None


def build_click_url(message_id: str) -> str:
    return f"{Config.ZNS_CLICK_BASE_URL.rstrip('/')}/{create_click_token(message_id)}"


def resolve_click(token: str, repo: Optional[ZNSRepository] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve a valid capability token to a message and an allowlisted destination."""
    message_id = verify_click_token(token)
    if not message_id:
        return None, None
    repository = repo or get_repository()
    message = repository.get_message_by_id(message_id)
    if not message:
        return None, None
    destination = Config.ZNS_CLICK_DESTINATIONS.get(message.get("template_type"))
    if not destination or not destination.startswith("https://"):
        return None, None
    return message, destination


def sync_first_click_to_odoo(message: Dict[str, Any]) -> None:
    """Best-effort first-click visibility in Odoo; click persistence never depends on it."""
    if not message.get("source_model") or not message.get("source_record_id"):
        return
    try:
        from services.zns_odoo_client import get_zns_odoo_client

        client = get_zns_odoo_client()
        values = {
            "x_studio_zns_clicked_at": str(message.get("clicked_at") or "").replace("T", " ")[:19],
            "x_studio_zns_click_count": int(message.get("click_count") or 1),
        }
        client.write(message["source_model"], [int(message["source_record_id"])], values)
        marker = f"zns_click_{message['id']}"
        existing = client.search_read(
            "mail.message",
            [("model", "=", message["source_model"]), ("res_id", "=", int(message["source_record_id"])), ("body", "like", marker)],
            ["id"],
            limit=1,
        )
        if not existing:
            body = (
                f'<div><!-- {marker} --><p>🔗 <b>KHÁCH HÀNG ĐÃ BẤM LIÊN KẾT ZNS</b></p>'
                f'<ul><li><b>Mẫu tin:</b> {html.escape(str(message.get("template_type") or ""))}</li>'
                f'<li><b>Đơn hàng:</b> {html.escape(str(message.get("business_reference") or ""))}</li>'
                f'<li><b>Thời gian click:</b> {html.escape(str(message.get("clicked_at") or ""))}</li></ul></div>'
            )
            posted = client.message_post(
                model=message["source_model"], record_id=int(message["source_record_id"]), body=body,
                message_type="comment", subtype_xmlid="mail.mt_note",
            )
            msg_id = posted if isinstance(posted, int) else (posted.get("id") if isinstance(posted, dict) else None)
            if msg_id:
                client.write("mail.message", [msg_id], {"body": body})
    except Exception as exc:
        logger.warning("ZNS click persisted but Odoo first-click sync failed: %s", exc)
