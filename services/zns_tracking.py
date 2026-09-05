"""
ZNS Tracking Domain Service — manages message lifecycle, state machine,
strict concurrency idempotency, multi-layer delivery webhooks, reconciliation, and metrics.
"""

import os
import json
import hmac
import hashlib
import logging
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set

from config import Config
from services.zalo_zns import send_zns, get_zns_status
from services.zns_repository import (
    get_repository,
    ZNSRepository,
    VALID_STATES,
    VALID_TRANSITIONS,
    DuplicateIdempotencyKeyError,
)
from utils.phone import normalize_phone_zalo
from utils.pii import mask_phone, hash_phone, sanitize_payload, is_valid_vn_phone

logger = logging.getLogger(__name__)

# Valid state definitions (re-exported from zns_repository for compatibility)
STATE_QUEUED = "QUEUED"
STATE_SUBMITTING = "SUBMITTING"
STATE_ACCEPTED = "ACCEPTED"
STATE_DELIVERED = "DELIVERED"
STATE_REJECTED = "REJECTED"
STATE_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
STATE_DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
STATE_CANCELLED = "CANCELLED"


# Bounded In-Memory Cache with TTL for in-flight request synchronization
class BoundedTTLCache:
    """Thread-safe bounded in-memory cache with strict capacity cap and oldest-entry eviction."""

    def __init__(self, maxsize: int = 500, ttl_seconds: float = 60.0):
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self.cache: Dict[str, Tuple[float, Any]] = {}
        self.lock = threading.Lock()

    def set(self, key: str, value: Any):
        now = time.time()
        with self.lock:
            # 1. Clean expired entries
            cutoff = now - self.ttl
            self.cache = {k: v for k, v in self.cache.items() if v[0] > cutoff}

            # 2. Hard capacity cap enforcement: if still >= maxsize, evict oldest entries
            while len(self.cache) >= self.maxsize:
                oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k][0])
                del self.cache[oldest_key]

            self.cache[key] = (now, value)

    def get(self, key: str) -> Optional[Any]:
        with self.lock:
            if key not in self.cache:
                return None
            ts, val = self.cache[key]
            if time.time() - ts > self.ttl:
                del self.cache[key]
                return None
            return val

    def pop(self, key: str, default: Any = None) -> Any:
        with self.lock:
            entry = self.cache.pop(key, None)
            return entry[1] if entry else default

    def size(self) -> int:
        with self.lock:
            return len(self.cache)

    def __len__(self) -> int:
        return self.size()

    def clear(self):
        with self.lock:
            self.cache.clear()


# In-flight request synchronization for strict single-send idempotency
_in_flight_mutex = threading.Lock()
_in_flight_events: Dict[str, threading.Event] = {}
_in_flight_results = BoundedTTLCache(maxsize=500, ttl_seconds=60.0)


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 string format."""
    return datetime.now(timezone.utc).isoformat()


def generate_tracking_id(prefix: str = "bon-zns") -> str:
    """Generate a unique tracking_id complying with Zalo <= 48 chars limit."""
    u = uuid.uuid4().hex[:24]
    return f"{prefix}-{u}"


def verify_zalo_webhook_signature(
    raw_body: bytes,
    signature_header: str,
    app_id: str,
    timestamp: str,
    secret_key: Optional[str] = None,
) -> bool:
    """
    Verify Zalo Webhook signature using official formula:
    mac = SHA256(appId + raw_body + timeStamp + OAsecretKey)
    """
    if not Config.ZNS_WEBHOOK_REQUIRE_SIGNATURE:
        return True

    if not signature_header or not app_id or not timestamp:
        return False

    # Deterministic secret key lookup
    if not secret_key:
        if str(app_id) == str(Config.ZALO_APP_ID):
            secret_key = Config.ZALO_SECRET_KEY
        elif str(app_id) == str(Config.ZALO_BON_APP_ID):
            secret_key = Config.ZALO_BON_SECRET_KEY
        else:
            return False

    if not secret_key:
        return False

    body_str = raw_body.decode("utf-8", errors="replace") if isinstance(raw_body, bytes) else str(raw_body)
    raw_data_to_hash = f"{app_id}{body_str}{timestamp}{secret_key}"
    calculated_mac = hashlib.sha256(raw_data_to_hash.encode("utf-8")).hexdigest()

    clean_sig = signature_header.strip().lower()
    if clean_sig.startswith("mac="):
        clean_sig = clean_sig[4:]

    return hmac.compare_digest(calculated_mac.lower(), clean_sig)


class ZNSTrackingService:
    """Domain service for orchestrating ZNS sends, webhooks, and state machine."""

    def __init__(self, repo: Optional[ZNSRepository] = None):
        self.repo = repo or get_repository()

    def dispatch_zns(
        self,
        template_type: str,
        phone_raw: str,
        order_code: str = "",
        order_date: str = "",
        customer_name: str = "",
        idempotency_key: Optional[str] = None,
        source: str = "odoo",
        source_model: Optional[str] = None,
        source_record_id: Optional[int] = None,
        sent_by_user_id: Optional[int] = None,
        sent_by_user_name: Optional[str] = None,
        company_id: Optional[int] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Dispatch a ZNS message with complete end-to-end tracking, idempotency,
        safe state transitions, and semantic response contracts.
        """
        # 1. Validate sending mode (Zalo OpenAPI only accepts 'development' or omitted for production)
        if mode is not None and mode not in ("development",):
            logger.warning(f"ZNS send rejected: invalid sending mode '{mode}'")
            return {
                "status": "rejected",
                "delivery_status": "failed",
                "error": {
                    "code": "INVALID_MODE",
                    "message": f"Invalid sending mode: '{mode}'. Only 'development' or omitted is permitted.",
                },
                "message": f"Chế độ gửi '{mode}' không hợp lệ. Zalo OpenAPI chỉ chấp nhận 'development' hoặc bỏ qua.",
            }

        # 2. Validate template
        template_id = Config.ZNS_TEMPLATES.get(template_type)
        if not template_id:
            logger.warning(f"ZNS send rejected: unknown template type '{template_type}'")
            return {
                "status": "rejected",
                "delivery_status": "failed",
                "error": {
                    "code": "INVALID_TEMPLATE",
                    "message": f"Unknown template type: {template_type}",
                },
                "message": f"Không tìm thấy mẫu tin ZNS '{template_type}'.",
            }

        # 3. Normalize and validate phone
        phone = normalize_phone_zalo(phone_raw)
        if not phone or not is_valid_vn_phone(phone):
            logger.warning(f"ZNS send rejected: invalid VN phone number '{mask_phone(phone_raw)}'")
            return {
                "status": "rejected",
                "delivery_status": "failed",
                "error": {
                    "code": "INVALID_PHONE",
                    "message": f"Phone number is invalid or empty: '{mask_phone(phone_raw)}'",
                },
                "message": "Số điện thoại không hợp lệ hoặc không có định dạng di động Việt Nam.",
            }

        app_key = Config.ZNS_TEMPLATE_APP.get(template_type, "ord")
        phone_masked_val = mask_phone(phone)
        phone_hash_val = hash_phone(phone)
        requested_at = _utc_now_iso()

        # 4. Strict Concurrency & Idempotency Check
        if idempotency_key:
            # 4.1 Check in-memory bounded TTL cache
            cached_res = _in_flight_results.get(idempotency_key)
            if cached_res:
                res = dict(cached_res)
                res["is_duplicate"] = True
                return res

            # 4.2 Check existing completed or processed in DB
            existing = self.repo.get_message_by_idempotency_key(idempotency_key)
            if existing:
                if existing["status"] not in (STATE_QUEUED, STATE_SUBMITTING):
                    res = self._build_response_from_record(existing, is_duplicate=True)
                    _in_flight_results.set(idempotency_key, res)
                    return res

            # 4.3 Check if another thread is currently executing with this idempotency_key
            wait_event = None
            with _in_flight_mutex:
                if idempotency_key in _in_flight_events:
                    wait_event = _in_flight_events[idempotency_key]
                else:
                    _in_flight_events[idempotency_key] = threading.Event()

            if wait_event:
                logger.info(f"Concurrent request with same idempotency_key='{idempotency_key}' — waiting for primary thread")
                wait_event.wait(timeout=25.0)
                cached_res = _in_flight_results.get(idempotency_key)
                if cached_res:
                    res = dict(cached_res)
                    res["is_duplicate"] = True
                    return res
                existing_after_wait = self.repo.get_message_by_idempotency_key(idempotency_key)
                if existing_after_wait:
                    if existing_after_wait["status"] in (STATE_QUEUED, STATE_SUBMITTING):
                        for _ in range(50):
                            time.sleep(0.1)
                            poll_rec = self.repo.get_message_by_id(existing_after_wait["id"])
                            if poll_rec and poll_rec["status"] not in (STATE_QUEUED, STATE_SUBMITTING):
                                existing_after_wait = poll_rec
                                break
                    res = self._build_response_from_record(existing_after_wait, is_duplicate=True)
                    _in_flight_results.set(idempotency_key, res)
                    return res

        try:
            res = self._execute_send_flow(
                template_type=template_type,
                template_id=template_id,
                app_key=app_key,
                phone=phone,
                phone_masked_val=phone_masked_val,
                phone_hash_val=phone_hash_val,
                order_code=order_code,
                order_date=order_date,
                customer_name=customer_name,
                idempotency_key=idempotency_key,
                source=source,
                source_model=source_model,
                source_record_id=source_record_id,
                sent_by_user_id=sent_by_user_id,
                sent_by_user_name=sent_by_user_name,
                company_id=company_id,
                mode=mode,
                requested_at=requested_at,
            )
            if idempotency_key:
                _in_flight_results.set(idempotency_key, res)
            return res
        except DuplicateIdempotencyKeyError as dup_err:
            existing = dup_err.existing_record or self.repo.get_message_by_idempotency_key(idempotency_key)
            if existing:
                if existing["status"] in (STATE_QUEUED, STATE_SUBMITTING):
                    for _ in range(50):
                        time.sleep(0.1)
                        poll_rec = self.repo.get_message_by_id(existing["id"])
                        if poll_rec and poll_rec["status"] not in (STATE_QUEUED, STATE_SUBMITTING):
                            existing = poll_rec
                            break
                res = self._build_response_from_record(existing, is_duplicate=True)
            else:
                res = {
                    "status": "accepted",
                    "delivery_status": "pending",
                    "tracking_id": "duplicate_in_flight",
                    "is_duplicate": True,
                    "message": "Yêu cầu trùng lặp đã được tiếp nhận.",
                }
            if idempotency_key:
                _in_flight_results.set(idempotency_key, res)
            return res
        finally:
            if idempotency_key:
                with _in_flight_mutex:
                    ev = _in_flight_events.pop(idempotency_key, None)
                    if ev:
                        ev.set()

    def _execute_send_flow(
        self,
        template_type: str,
        template_id: str,
        app_key: str,
        phone: str,
        phone_masked_val: str,
        phone_hash_val: str,
        order_code: str,
        order_date: str,
        customer_name: str,
        idempotency_key: Optional[str],
        source: str,
        source_model: Optional[str],
        source_record_id: Optional[int],
        sent_by_user_id: Optional[int],
        sent_by_user_name: Optional[str],
        company_id: Optional[int],
        mode: Optional[str],
        requested_at: str,
    ) -> Dict[str, Any]:
        """Execute the core ZNS dispatch workflow with CAS state transitions."""
        tracking_id = generate_tracking_id(prefix=f"bon-{app_key}")

        # 1. Insert message in QUEUED state
        message_data = {
            "tracking_id": tracking_id,
            "idempotency_key": idempotency_key,
            "app_key": app_key,
            "template_type": template_type,
            "template_id": template_id,
            "source": source,
            "source_model": source_model,
            "source_record_id": source_record_id,
            "business_reference": order_code,
            "customer_name": customer_name,
            "phone_masked": phone_masked_val,
            "phone_hash": phone_hash_val,
            "sent_by_user_id": sent_by_user_id,
            "sent_by_user_name": sent_by_user_name,
            "company_id": company_id,
            "status": STATE_QUEUED,
            "sending_mode": mode or "production",
            "requested_at": requested_at,
        }

        created_record = self.repo.create_message(message_data)
        message_id = created_record["id"]

        # 2. Atomic CAS: Transition QUEUED -> SUBMITTING
        submitted_at = _utc_now_iso()
        self.repo.transition_message(
            message_id=message_id,
            expected_statuses=[STATE_QUEUED],
            new_status=STATE_SUBMITTING,
            event_type="SUBMITTING_TO_ZALO",
            event_payload={"mode": mode, "tracking_id": tracking_id},
            submitted_at=submitted_at,
        )

        # 3. Call Zalo OpenAPI with tracking_id (max 48 chars). Click data is
        # sent only for templates explicitly enabled after Zalo approval.
        extra_template_data = None
        click_param = Config.ZNS_CLICK_PARAM_BY_TEMPLATE.get(template_type)
        if click_param:
            from services.zns_click_tracking import build_click_url
            extra_template_data = {click_param: build_click_url(message_id)}
        try:
            raw_result = send_zns(
                template_type=template_type,
                phone=phone,
                order_code=order_code,
                order_date=order_date,
                customer_name=customer_name,
                tracking_id=tracking_id,
                mode=mode,
                extra_template_data=extra_template_data,
            )
        except Exception as send_exc:
            # Network Timeout / Connection Error / 5xx Non-JSON
            unknown_at = _utc_now_iso()
            logger.warning(
                f"ZNS send network exception for tracking_id='{tracking_id}' "
                f"to {phone_masked_val}: {send_exc} -> Marking {STATE_SUBMISSION_UNKNOWN}"
            )
            success_sub, updated_sub = self.repo.transition_message(
                message_id=message_id,
                expected_statuses=[STATE_SUBMITTING],
                new_status=STATE_SUBMISSION_UNKNOWN,
                event_type="SUBMISSION_TIMEOUT_OR_NETWORK_ERROR",
                event_payload={"error": str(send_exc)},
                unknown_at=unknown_at,
                error_code=-999,
                error_message=str(send_exc),
            )
            if not success_sub and updated_sub:
                res = self._build_response_from_record(updated_sub)
            else:
                res = {
                    "status": "submission_unknown",
                    "delivery_status": "unknown",
                    "tracking_id": tracking_id,
                    "message_id": message_id,
                    "error": {
                        "code": "NETWORK_TIMEOUT_OR_ERROR",
                        "message": "Không nhận được phản hồi từ Zalo Gateway do lỗi kết nối hoặc timeout.",
                    },
                    "message": (
                        "Chưa xác định kết quả gửi do mất kết nối hoặc timeout từ máy chủ Zalo. "
                        "Hệ thống đang theo dõi xác nhận; không bấm gửi lại để tránh gửi trùng."
                    ),
                }
            if idempotency_key:
                _in_flight_results.set(idempotency_key, res)
            return res

        # 4. Semantic response parsing
        if not isinstance(raw_result, dict):
            logger.error(f"ZNS returned invalid response type: {type(raw_result).__name__}")
            success_mal, updated_mal = self.repo.transition_message(
                message_id=message_id,
                expected_statuses=[STATE_SUBMITTING],
                new_status=STATE_SUBMISSION_UNKNOWN,
                event_type="MALFORMED_RESPONSE",
                event_payload={"raw": str(raw_result)},
                error_message="Malformed response from Zalo API",
            )
            if not success_mal and updated_mal:
                res = self._build_response_from_record(updated_mal)
            else:
                res = {
                    "status": "submission_unknown",
                    "delivery_status": "unknown",
                    "tracking_id": tracking_id,
                    "message_id": message_id,
                    "error": {"code": "MALFORMED_RESPONSE", "message": "Phản hồi từ Zalo không đúng định dạng."},
                    "message": "Phản hồi từ Zalo không đúng định dạng JSON.",
                }
            if idempotency_key:
                _in_flight_results.set(idempotency_key, res)
            return res

        error_code = raw_result.get("error", -1)
        error_msg = raw_result.get("message", "")
        data_block = raw_result.get("data") or {}

        if error_code == 0:
            # ACCEPTED by Zalo Gateway
            accepted_at = _utc_now_iso()
            zalo_msg_id = data_block.get("msg_id") or ""
            sending_mode = str(data_block.get("sending_mode", ""))
            quota_info = data_block.get("quota") or {}
            sent_time_ms = None
            if data_block.get("sent_time") is not None:
                try:
                    sent_time_ms = int(data_block["sent_time"])
                except (ValueError, TypeError):
                    pass

            quota_daily = None
            quota_remaining = None
            try:
                if quota_info.get("dailyQuota") is not None:
                    quota_daily = int(quota_info["dailyQuota"])
                if quota_info.get("remainingQuota") is not None:
                    quota_remaining = int(quota_info["remainingQuota"])
            except (ValueError, TypeError):
                pass

            # Atomic CAS: Transition SUBMITTING -> ACCEPTED
            success, updated = self.repo.transition_message(
                message_id=message_id,
                expected_statuses=[STATE_SUBMITTING],
                new_status=STATE_ACCEPTED,
                event_type="ZALO_ACCEPTED",
                event_payload=data_block,
                event_source="zalo_api",
                zalo_msg_id=zalo_msg_id if zalo_msg_id else None,
                accepted_at=accepted_at,
                sending_mode=sending_mode,
                sent_time_ms=sent_time_ms,
                quota_daily=quota_daily,
                quota_remaining=quota_remaining,
            )

            if not success and updated:
                logger.info(f"Message {message_id} was already transitioned concurrently to {updated.get('status')}")
                res = self._build_response_from_record(updated)
            else:
                logger.info(
                    f"ZNS [{template_type}→{app_key.upper()}] ACCEPTED by Zalo Gateway: "
                    f"tracking_id='{tracking_id}', msg_id='{zalo_msg_id}', recipient={phone_masked_val}"
                )
                res = {
                    "status": "accepted",
                    "delivery_status": "pending",
                    "tracking_id": tracking_id,
                    "message_id": message_id,
                    "msg_id": zalo_msg_id,
                    "quota": {
                        "daily": quota_daily,
                        "remaining": quota_remaining,
                    },
                    "message": "Zalo đã tiếp nhận yêu cầu gửi. Hệ thống đang chờ xác nhận phát tới thiết bị khách.",
                }

            if idempotency_key:
                _in_flight_results.set(idempotency_key, res)
            return res

        else:
            # REJECTED by Zalo Gateway (Business / Validation / Quota error)
            success_rej, updated_rej = self.repo.transition_message(
                message_id=message_id,
                expected_statuses=[STATE_SUBMITTING],
                new_status=STATE_REJECTED,
                event_type="ZALO_REJECTED",
                event_payload=raw_result,
                event_source="zalo_api",
                error_code=error_code,
                error_message=error_msg,
            )

            if not success_rej and updated_rej:
                res = self._build_response_from_record(updated_rej)
            else:
                logger.warning(
                    f"ZNS [{template_type}→{app_key.upper()}] REJECTED by Zalo: "
                    f"code={error_code}, msg='{error_msg}', tracking_id='{tracking_id}', recipient={phone_masked_val}"
                )
                res = {
                    "status": "rejected",
                    "delivery_status": "failed",
                    "tracking_id": tracking_id,
                    "message_id": message_id,
                    "error": {
                        "code": error_code,
                        "message": error_msg,
                    },
                    "message": f"Zalo từ chối yêu cầu gửi: {error_msg} (mã {error_code}).",
                }

            if idempotency_key:
                _in_flight_results.set(idempotency_key, res)
            return res

    def process_delivery_webhook(
        self,
        raw_body: bytes,
        headers: Dict[str, str],
        payload: Dict[str, Any],
    ) -> Tuple[bool, str, int]:
        """
        Process delivery confirmation webhook (`user_received_message`) with strictly ordered 7-Layer security pipeline.
        Guarantees ZERO DB I/O before cryptographic signature verification.
        Returns (success: bool, message: str, http_status_code: int).
        """
        # ═════════════════════════════════════════════════════════════════════
        # LAYER 1: Protocol & Schema Validation
        # ═════════════════════════════════════════════════════════════════════
        if not isinstance(payload, dict):
            logger.warning("Delivery webhook received non-dict JSON body")
            return False, "Invalid payload format: body must be a JSON object", 400

        event_name = str(payload.get("event_name", "")).strip()
        app_id = str(payload.get("app_id", "")).strip()
        timestamp_raw = str(payload.get("timestamp", "")).strip()

        if not event_name or not app_id or not timestamp_raw:
            return False, "Missing required top-level webhook fields (event_name, app_id, timestamp)", 400

        sender_info = payload.get("sender")
        sender_id = str(sender_info.get("id", "")).strip() if isinstance(sender_info, dict) else ""
        msg_block = payload.get("message")
        zalo_msg_id = str(msg_block.get("msg_id", "")).strip() if isinstance(msg_block, dict) else ""
        tracking_id = str(msg_block.get("tracking_id", "")).strip() if isinstance(msg_block, dict) else ""
        delivery_time_raw = str(msg_block.get("delivery_time", "")).strip() if isinstance(msg_block, dict) else ""

        # ═════════════════════════════════════════════════════════════════════
        # LAYER 2: Deterministic App Resolution & Fail-Closed Secret Lookup
        # ═════════════════════════════════════════════════════════════════════
        resolved_app_key = None
        secret_key = None
        allowed_oa_id = None

        ord_app_id = str(Config.ZALO_APP_ID).strip() if Config.ZALO_APP_ID else None
        bon_app_id = str(Config.ZALO_BON_APP_ID).strip() if Config.ZALO_BON_APP_ID else None

        if ord_app_id and app_id == ord_app_id:
            resolved_app_key = "ord"
            secret_key = str(Config.ZALO_SECRET_KEY).strip() if Config.ZALO_SECRET_KEY else None
            allowed_oa_id = str(Config.ZALO_OA_ID).strip() if Config.ZALO_OA_ID else None
        elif bon_app_id and app_id == bon_app_id:
            resolved_app_key = "bon"
            secret_key = str(Config.ZALO_BON_SECRET_KEY).strip() if Config.ZALO_BON_SECRET_KEY else None
            allowed_oa_id = str(Config.ZALO_BON_OA_ID).strip() if Config.ZALO_BON_OA_ID else None
        else:
            logger.warning(f"Delivery webhook rejected: unknown or unconfigured app_id='{app_id}'")
            return False, "Unauthorized app_id", 401

        # Fail-closed 503 if secret or OA ID is unconfigured
        if not secret_key or not allowed_oa_id:
            logger.error(f"Delivery webhook rejected: secret key or OA ID not configured for app '{resolved_app_key}'")
            return False, f"Server configuration error: missing credentials/OA ID for app '{resolved_app_key}'", 503

        # ═════════════════════════════════════════════════════════════════════
        # LAYER 3: Cryptographic HMAC SHA256 Signature Verification (Zero DB I/O)
        # ═════════════════════════════════════════════════════════════════════
        signature_header = (
            headers.get("X-ZEvent-Signature")
            or headers.get("x-zevent-signature")
            or headers.get("X-Zevent-Signature")
            or ""
        ).strip()

        if Config.ZNS_WEBHOOK_REQUIRE_SIGNATURE:
            if not signature_header:
                return False, "Missing X-ZEvent-Signature header", 401

            sig_valid = verify_zalo_webhook_signature(
                raw_body=raw_body,
                signature_header=signature_header,
                app_id=app_id,
                timestamp=timestamp_raw,
                secret_key=secret_key,
            )

            if not sig_valid:
                logger.warning(f"Delivery webhook signature invalid for app_id={app_id}, msg_id={zalo_msg_id}")
                return False, "Invalid X-ZEvent-Signature", 401

        # ═════════════════════════════════════════════════════════════════════
        # LAYER 4: Timestamp Freshness & Strict Numeric Replay Protection
        # ═════════════════════════════════════════════════════════════════════
        if not timestamp_raw.isdigit():
            logger.warning(f"Delivery webhook rejected: non-numeric timestamp '{timestamp_raw}'")
            return False, "Invalid non-numeric timestamp", 400

        try:
            ts_numeric = int(timestamp_raw)
            ts_sec = ts_numeric / 1000.0 if len(timestamp_raw) > 10 else float(ts_numeric)
        except (ValueError, OverflowError):
            return False, "Malformed timestamp value", 400

        now_ts = datetime.now(timezone.utc).timestamp()
        if abs(now_ts - ts_sec) > Config.ZNS_WEBHOOK_TIMESTAMP_TOLERANCE:
            age = abs(now_ts - ts_sec)
            logger.warning(f"Delivery webhook timestamp stale (age: {age:.1f}s, tolerance: {Config.ZNS_WEBHOOK_TIMESTAMP_TOLERANCE}s)")
            self.repo.record_diagnostic(
                raw_event_name=event_name,
                app_id=app_id,
                sender_id=sender_id,
                zalo_msg_id=zalo_msg_id,
                tracking_id=tracking_id,
                signature_valid=True,
                reason=f"Stale timestamp (age: {age:.1f}s)",
                payload=payload,
            )
            return False, "Stale timestamp", 400

        # ═════════════════════════════════════════════════════════════════════
        # LAYER 5: OA ID Allowlist & Event Filtering
        # ═════════════════════════════════════════════════════════════════════
        if not sender_id or sender_id != allowed_oa_id:
            logger.warning(f"Delivery webhook sender OA '{sender_id}' does not match expected '{allowed_oa_id}'")
            self.repo.record_diagnostic(
                raw_event_name=event_name,
                app_id=app_id,
                sender_id=sender_id,
                zalo_msg_id=zalo_msg_id,
                tracking_id=tracking_id,
                signature_valid=True,
                reason=f"Unauthorized sender OA ID '{sender_id}' (expected '{allowed_oa_id}')",
                payload=payload,
            )
            return False, "Unauthorized OA sender", 403

        if event_name != "user_received_message":
            logger.info(f"Delivery webhook acknowledged non-delivery event: '{event_name}'")
            self.repo.record_diagnostic(
                raw_event_name=event_name,
                app_id=app_id,
                sender_id=sender_id,
                zalo_msg_id=zalo_msg_id,
                tracking_id=tracking_id,
                signature_valid=True,
                reason=f"Acknowledged non-delivery event: {event_name}",
                payload=payload,
            )
            return True, "Non-delivery event acknowledged", 200

        # Delivery time must be numeric
        if not delivery_time_raw or not delivery_time_raw.isdigit():
            logger.warning(f"Delivery webhook rejected: invalid or missing numeric delivery_time '{delivery_time_raw}'")
            return False, "Invalid or missing numeric delivery_time in delivery event", 400

        # ═════════════════════════════════════════════════════════════════════
        # LAYER 6: Entity Matching & Cross-Tenant Protection Guard
        # ═════════════════════════════════════════════════════════════════════
        record = None
        if tracking_id:
            record = self.repo.get_message_by_tracking_id(tracking_id)
        if not record and zalo_msg_id:
            record = self.repo.get_message_by_zalo_msg_id(zalo_msg_id)

        if not record:
            logger.warning(f"Delivery webhook received for unknown message (tracking_id={tracking_id}, zalo_msg_id={zalo_msg_id})")
            self.repo.record_diagnostic(
                raw_event_name=event_name,
                app_id=app_id,
                sender_id=sender_id,
                zalo_msg_id=zalo_msg_id,
                tracking_id=tracking_id,
                signature_valid=True,
                reason="Message not found in database",
                payload=payload,
            )
            return True, "Diagnostic logged for untracked message", 200

        # Cross-Tenant Isolation Guard
        if record["app_key"] != resolved_app_key:
            logger.critical(
                f"CROSS-TENANT VIOLATION: Webhook app '{resolved_app_key}' "
                f"attempted to modify record belonging to app '{record['app_key']}'!"
            )
            self.repo.record_diagnostic(
                raw_event_name=event_name,
                app_id=app_id,
                sender_id=sender_id,
                zalo_msg_id=zalo_msg_id,
                tracking_id=tracking_id,
                signature_valid=True,
                reason=f"Cross-tenant violation: record app '{record['app_key']}' != callback app '{resolved_app_key}'",
                payload=payload,
            )
            return False, "Cross-tenant mismatch", 403

        # Message ID Mismatch Guard
        if record.get("zalo_msg_id") and zalo_msg_id and record["zalo_msg_id"] != zalo_msg_id:
            logger.warning(f"Delivery webhook msg_id mismatch: record='{record['zalo_msg_id']}', callback='{zalo_msg_id}'")
            self.repo.record_diagnostic(
                raw_event_name=event_name,
                app_id=app_id,
                sender_id=sender_id,
                zalo_msg_id=zalo_msg_id,
                tracking_id=tracking_id,
                signature_valid=True,
                reason=f"Msg ID mismatch: record='{record['zalo_msg_id']}', callback='{zalo_msg_id}'",
                payload=payload,
            )
            return False, "Message ID mismatch", 400

        # ═════════════════════════════════════════════════════════════════════
        # LAYER 7: Idempotent State Machine & Atomic CAS Transition
        # ═════════════════════════════════════════════════════════════════════
        if record["status"] == STATE_DELIVERED:
            logger.info(f"Delivery webhook duplicate for message id='{record['id']}' — already DELIVERED")
            return True, "Already delivered", 200

        # Parse delivery_time
        delivered_at_iso = _utc_now_iso()
        if delivery_time_raw and delivery_time_raw.isdigit():
            try:
                ms = int(delivery_time_raw)
                delivered_at_iso = datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat()
            except (ValueError, OverflowError):
                pass

        last_webhook_at = _utc_now_iso()

        success, updated = self.repo.transition_message(
            message_id=record["id"],
            expected_statuses=[STATE_SUBMITTING, STATE_ACCEPTED, STATE_SUBMISSION_UNKNOWN, STATE_DELIVERY_UNKNOWN],
            new_status=STATE_DELIVERED,
            event_type="DELIVERY_CONFIRMED",
            event_payload=payload,
            event_source="zalo_webhook",
            occurred_at=delivered_at_iso,
            delivered_at=delivered_at_iso,
            last_webhook_at=last_webhook_at,
            zalo_msg_id=zalo_msg_id if zalo_msg_id else record.get("zalo_msg_id"),
        )

        if not success:
            logger.info(f"Message {record['id']} was not transitioned (current status: {updated.get('status') if updated else 'unknown'})")
            return True, "Already processed or terminal", 200

        logger.info(f"ZNS message id='{record['id']}' tracking_id='{record['tracking_id']}' transitioned to DELIVERED")
        return True, "Delivery confirmed successfully", 200

    def reconcile_stale_messages(self, threshold_seconds: Optional[int] = None) -> int:
        """
        Background reconciliation job:
        Finds messages in ACCEPTED state older than SLA and uses Atomic CAS to move them to DELIVERY_UNKNOWN.
        """
        sla = threshold_seconds if threshold_seconds is not None else Config.ZNS_DELIVERY_SLA_SECONDS
        poll_after = min(sla, max(0, Config.ZNS_STATUS_POLL_AFTER_SECONDS))
        stale_messages = self.repo.get_stale_accepted_messages(threshold_seconds=poll_after)
        if not stale_messages:
            return 0

        reconciled_count = 0
        now = _utc_now_iso()

        for msg in stale_messages:
            msg_id = msg["id"]
            try:
                status_result = get_zns_status(msg.get("zalo_msg_id"), msg.get("app_key"))
                status_data = status_result.get("data") or {} if isinstance(status_result, dict) else {}
                if status_result.get("error") == 0 and int(status_data.get("status", 0)) == 1:
                    delivery_raw = str(status_data.get("delivery_time") or "")
                    delivered_at = now
                    if delivery_raw.isdigit():
                        delivered_at = datetime.fromtimestamp(int(delivery_raw) / 1000.0, timezone.utc).isoformat()
                    success, _ = self.repo.transition_message(
                        message_id=msg_id,
                        expected_statuses=[STATE_ACCEPTED],
                        new_status=STATE_DELIVERED,
                        event_type="DELIVERY_CONFIRMED_BY_STATUS_API",
                        event_payload=status_data,
                        event_source="zalo_status_api",
                        occurred_at=delivered_at,
                        delivered_at=delivered_at,
                        last_webhook_at=now,
                        zalo_msg_id=msg.get("zalo_msg_id"),
                    )
                    if success:
                        reconciled_count += 1
                    continue
            except Exception as exc:
                logger.warning("[ZNS-RECONCILIATION] Status API lookup failed for %s: %s", msg_id, exc)

            accepted_at = msg.get("accepted_at") or msg.get("updated_at") or now
            try:
                age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(accepted_at.replace("Z", "+00:00")).timestamp()
            except Exception:
                age = sla
            if age < sla:
                continue

            # Atomic CAS: ACCEPTED -> DELIVERY_UNKNOWN after the full SLA.
            success, updated = self.repo.transition_message(
                message_id=msg_id,
                expected_statuses=[STATE_ACCEPTED],
                new_status=STATE_DELIVERY_UNKNOWN,
                event_type="DELIVERY_SLA_EXCEEDED",
                event_payload={
                    "sla_seconds": sla,
                    "accepted_at": msg.get("accepted_at"),
                    "reconciliation_time": now,
                },
                event_source="reconciliation",
                unknown_at=now,
            )
            if success:
                reconciled_count += 1
                logger.info(
                    f"[ZNS-RECONCILIATION] Transitioned message id='{msg_id}' "
                    f"tracking_id='{msg['tracking_id']}' to DELIVERY_UNKNOWN (SLA: {sla}s exceeded)"
                )
            else:
                logger.info(f"[ZNS-RECONCILIATION] Skipped message id='{msg_id}' (already transitioned to {updated.get('status') if updated else 'unknown'})")

        return reconciled_count

    def reap_stale_in_flight_messages(
        self,
        queued_timeout_seconds: int = 300,
        submitting_timeout_seconds: int = 300,
    ) -> Dict[str, int]:
        """
        Crash recovery reaper:
        1. Finds messages stuck in QUEUED > queued_timeout_seconds -> Marks as SUBMISSION_UNKNOWN
        2. Finds messages stuck in SUBMITTING > submitting_timeout_seconds -> Marks as SUBMISSION_UNKNOWN
        """
        now = _utc_now_iso()
        now_ts = datetime.now(timezone.utc).timestamp()
        conn = self.repo.get_connection()
        stale_queued = []
        stale_submitting = []

        try:
            cur = conn.execute("""
                SELECT id, tracking_id, status, requested_at, submitted_at, created_at
                FROM zns_messages
                WHERE status IN ('QUEUED', 'SUBMITTING');
            """)
            for row in cur.fetchall():
                r = dict(row)
                t_str = r.get("submitted_at") or r.get("requested_at") or r.get("created_at")
                try:
                    dt = datetime.fromisoformat(t_str.replace("Z", "+00:00")).timestamp()
                    age = now_ts - dt
                    if r["status"] == STATE_QUEUED and age >= queued_timeout_seconds:
                        stale_queued.append(r)
                    elif r["status"] == STATE_SUBMITTING and age >= submitting_timeout_seconds:
                        stale_submitting.append(r)
                except Exception:
                    pass
        finally:
            conn.close()

        reaped_queued = 0
        for m in stale_queued:
            ok, _ = self.repo.transition_message(
                message_id=m["id"],
                expected_statuses=[STATE_QUEUED],
                new_status=STATE_SUBMISSION_UNKNOWN,
                event_type="REAPED_ORPHAN_QUEUED",
                event_payload={"reason": f"Worker crashed before submission (timeout: {queued_timeout_seconds}s)"},
                event_source="crash_reaper",
                unknown_at=now,
                error_code=-998,
                error_message="Worker process crashed or lost while in QUEUED state",
            )
            if ok:
                reaped_queued += 1

        reaped_submitting = 0
        for m in stale_submitting:
            ok, _ = self.repo.transition_message(
                message_id=m["id"],
                expected_statuses=[STATE_SUBMITTING],
                new_status=STATE_SUBMISSION_UNKNOWN,
                event_type="REAPED_DEAD_SUBMITTING",
                event_payload={"reason": f"Worker died during HTTP dispatch (timeout: {submitting_timeout_seconds}s)"},
                event_source="crash_reaper",
                unknown_at=now,
                error_code=-997,
                error_message="Worker process died while awaiting upstream Zalo response",
            )
            if ok:
                reaped_submitting += 1

        return {
            "reaped_queued": reaped_queued,
            "reaped_submitting": reaped_submitting,
        }

    def _build_response_from_record(self, record: Dict[str, Any], is_duplicate: bool = False) -> Dict[str, Any]:
        """Convert a database record into a standardized API response."""
        st = record["status"]
        if st == STATE_ACCEPTED:
            return {
                "status": "accepted",
                "delivery_status": "pending",
                "tracking_id": record["tracking_id"],
                "message_id": record["id"],
                "msg_id": record.get("zalo_msg_id"),
                "quota": {
                    "daily": record.get("quota_daily"),
                    "remaining": record.get("quota_remaining"),
                },
                "is_duplicate": is_duplicate,
                "message": "Zalo đã tiếp nhận yêu cầu gửi. Hệ thống đang chờ xác nhận phát tới thiết bị khách.",
            }
        elif st == STATE_DELIVERED:
            return {
                "status": "delivered",
                "delivery_status": "delivered",
                "tracking_id": record["tracking_id"],
                "message_id": record["id"],
                "msg_id": record.get("zalo_msg_id"),
                "delivered_at": record.get("delivered_at"),
                "is_duplicate": is_duplicate,
                "message": "Tin nhắn đã phát thành công tới thiết bị của khách hàng.",
            }
        elif st == STATE_REJECTED:
            return {
                "status": "rejected",
                "delivery_status": "failed",
                "tracking_id": record["tracking_id"],
                "message_id": record["id"],
                "error": {
                    "code": record.get("error_code"),
                    "message": record.get("error_message"),
                },
                "is_duplicate": is_duplicate,
                "message": f"Zalo từ chối yêu cầu gửi: {record.get('error_message')} (mã {record.get('error_code')}).",
            }
        else:
            return {
                "status": st.lower(),
                "delivery_status": "unknown" if "UNKNOWN" in st else "pending",
                "tracking_id": record["tracking_id"],
                "message_id": record["id"],
                "is_duplicate": is_duplicate,
                "message": f"Trạng thái tin nhắn hiện tại: {st}.",
            }


# ══════════════════════════════════════════════════════════════════════════════
# DURABLE ODOO OUTBOX WORKER & BACKGROUND RECONCILIATION DAEMON
# ══════════════════════════════════════════════════════════════════════════════

class ZNSOdooOutboxWorker:
    """
    Worker processing transactional outbox tasks to sync delivery confirmations to Odoo via XML-RPC.
    Features exponential backoff retry, dead-letter queue, and idempotent chatter posting.
    """

    def __init__(self, repo: Optional[ZNSRepository] = None):
        self.repo = repo or get_repository()

    def process_pending_tasks(self, worker_id: Optional[str] = None, limit: int = 50) -> int:
        """Process pending outbox sync tasks with atomic lease claiming and heartbeat lease renewal."""
        worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        claimed_tasks = self.repo.claim_outbox_tasks(
            worker_id=worker_id,
            batch_size=limit,
            lease_duration_seconds=60,
        )
        if not claimed_tasks:
            return 0

        processed = 0
        for task in claimed_tasks:
            task_id = task["id"]
            # Heartbeat lease renewal before side-effect
            renewed = self.repo.renew_outbox_lease(task_id, worker_id, lease_duration_seconds=60)
            if not renewed:
                logger.warning(f"[ZNS-OUTBOX] Lost lease on task {task_id} before Odoo sync — skipping execution")
                continue

            success, err_msg = self._sync_task_to_odoo(task, worker_id=worker_id)
            completed_ok = self.repo.complete_outbox_task(
                task_id=task_id,
                worker_id=worker_id,
                success=success,
                last_error=err_msg,
            )
            # Only count as processed when both Odoo sync and DB state update succeeded under valid lease
            if success and completed_ok:
                processed += 1

        return processed

    def _heartbeat_outbox(self, task_id: int, worker_id: Optional[str], lease_duration_seconds: int = 60) -> bool:
        """Renew the outbox lease. Missing worker_id is a no-op (direct unit-test calls)."""
        if not worker_id:
            return True
        ok = self.repo.renew_outbox_lease(task_id, worker_id, lease_duration_seconds=lease_duration_seconds)
        if not ok:
            logger.warning(f"[ZNS-OUTBOX] Lost lease on task {task_id} during Odoo sync")
        return ok

    def _call_odoo_with_heartbeat(self, fn, *args, heartbeat=None, **kwargs):
        """Invoke an Odoo client method, passing heartbeat when the callee supports it."""
        if heartbeat is None:
            return fn(*args, **kwargs)
        try:
            return fn(*args, heartbeat=heartbeat, **kwargs)
        except TypeError as e:
            if "heartbeat" not in str(e):
                raise
            return fn(*args, **kwargs)

    def _sync_task_to_odoo(self, task: Dict[str, Any], worker_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """Sync a single delivery task to Odoo via JSON-RPC 2.0."""
        source_model = task.get("source_model")
        source_record_id = task.get("source_record_id")
        zalo_msg_id = task.get("zalo_msg_id") or ""
        delivered_at_iso = task.get("delivered_at") or _utc_now_iso()
        task_id = task.get("id")

        if not source_model or not source_record_id:
            return True, None

        if not Config.ODOO_URL or not Config.ODOO_DB or not Config.ODOO_API_KEY:
            logger.warning("[ZNS-OUTBOX] Odoo credentials unconfigured on server — cannot complete outbox sync")
            return False, "Odoo server credentials unconfigured"

        def heartbeat() -> bool:
            return self._heartbeat_outbox(task_id, worker_id)

        try:
            from services.zns_odoo_client import get_zns_odoo_client
            odoo_client = get_zns_odoo_client()

            odoo_utc_formatted = delivered_at_iso
            try:
                dt = datetime.fromisoformat(delivered_at_iso.replace("Z", "+00:00"))
                odoo_utc_formatted = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

            if not heartbeat():
                return False, "lost outbox lease before Odoo write"

            # 1. Update fields on Odoo record via JSON-RPC
            write_res = self._call_odoo_with_heartbeat(
                odoo_client.write,
                source_model,
                [source_record_id],
                {
                    "x_studio_zns_status": "delivered",
                    "x_studio_zns_delivered_at": odoo_utc_formatted,
                },
                heartbeat=heartbeat,
            )
            if write_res is False:
                return False, f"Odoo write returned False for {source_model}({source_record_id})"

            # Keep the original queue note synchronized as well as the fields.
            from services.zns_odoo_poller import ZNSOdooPoller
            message_row = self.repo.get_message_by_id(task.get("message_id")) or {}
            idempotency_key = str(message_row.get("idempotency_key") or "")
            send_version = 1
            if ":v" in idempotency_key:
                try:
                    send_version = int(idempotency_key.rsplit(":v", 1)[1])
                except ValueError:
                    pass
            chatter_updated = ZNSOdooPoller(odoo_client=odoo_client, repo=self.repo)._update_request_chatter_status(
                source_model, int(source_record_id), send_version, "DELIVERED", zalo_msg_id=zalo_msg_id
            )
            if not chatter_updated:
                return False, f"original queue chatter update failed for {source_model}({source_record_id}) v{send_version}"

            # Delivery never creates a second Chatter note. The original queue note
            # above is the single source of truth and is updated in place.
            if not heartbeat():
                return False, "lost outbox lease after Odoo sync"
            return True, None
        except Exception as e:
            logger.warning(f"[ZNS-OUTBOX] Odoo writeback error for {source_model}({source_record_id}): {e}")
            return False, str(e)


# Background Reconciliation & Outbox Daemon
_reconciliation_thread: Optional[threading.Thread] = None
_reconciliation_stop = threading.Event()
_reconciliation_lock = threading.Lock()
_last_retention_cleanup = 0.0


def _reconciliation_loop():
    """Background loop executing SLA reconciliation, outbox processing, crash recovery, and retention cleanup."""
    global _last_retention_cleanup
    interval = Config.ZNS_RECONCILIATION_INTERVAL
    logger.info(f"[ZNS-RECONCILIATION] Started background loop (interval: {interval}s)")
    service = ZNSTrackingService()
    outbox_worker = ZNSOdooOutboxWorker()

    while not _reconciliation_stop.is_set():
        try:
            reconciled = service.reconcile_stale_messages()
            reaped = service.reap_stale_in_flight_messages()
            outbox_synced = outbox_worker.process_pending_tasks(limit=50)

            # Periodic retention cleanup (every 24 hours = 86400s)
            now_ts = time.time()
            if now_ts - _last_retention_cleanup > 86400:
                try:
                    cleaned = service.repo.cleanup_old_records(
                        retention_days=Config.ZNS_RETENTION_DAYS,
                        diagnostics_retention_days=Config.ZNS_DIAGNOSTICS_RETENTION_DAYS,
                    )
                    logger.info(f"[ZNS-RETENTION] Periodic cleanup completed: {cleaned}")
                    _last_retention_cleanup = now_ts
                except Exception as clean_err:
                    logger.error(f"[ZNS-RETENTION] Error during periodic cleanup: {clean_err}")

            if reconciled > 0 or reaped.get("reaped_queued", 0) > 0 or reaped.get("reaped_submitting", 0) > 0 or outbox_synced > 0:
                logger.info(
                    f"[ZNS-RECONCILIATION] Stale reconciliation: {reconciled} message(s), "
                    f"Outbox synced: {outbox_synced} task(s), "
                    f"Reaper: {reaped['reaped_queued']} queued, {reaped['reaped_submitting']} submitting"
                )
        except Exception as e:
            logger.error(f"[ZNS-RECONCILIATION] Error during cycle: {e}")

        # Sleep with stop event interruptibility
        _reconciliation_stop.wait(timeout=interval)


def start_reconciliation():
    """Start the reconciliation background daemon thread."""
    global _reconciliation_thread
    with _reconciliation_lock:
        if not Config.ZNS_RECONCILIATION_ENABLED:
            logger.info("[ZNS-RECONCILIATION] Disabled via configuration — not starting")
            return
        if _reconciliation_thread is None or not _reconciliation_thread.is_alive():
            _reconciliation_stop.clear()
            _reconciliation_thread = threading.Thread(
                target=_reconciliation_loop,
                name="zns-reconciliation-daemon",
                daemon=True,
            )
            _reconciliation_thread.start()
            logger.info("[ZNS-RECONCILIATION] Background daemon thread launched")


def stop_reconciliation():
    """Stop the reconciliation daemon thread gracefully."""
    global _reconciliation_thread
    with _reconciliation_lock:
        _reconciliation_stop.set()
        if _reconciliation_thread and _reconciliation_thread.is_alive():
            _reconciliation_thread.join(timeout=2.0)
        _reconciliation_thread = None


def get_reconciliation_status() -> Dict[str, Any]:
    """Return runtime status of the ZNS reconciliation daemon."""
    global _reconciliation_thread
    with _reconciliation_lock:
        is_running = _reconciliation_thread is not None and _reconciliation_thread.is_alive()
        return {
            "enabled": Config.ZNS_RECONCILIATION_ENABLED,
            "running": is_running,
            "interval_seconds": Config.ZNS_RECONCILIATION_INTERVAL,
            "sla_seconds": Config.ZNS_DELIVERY_SLA_SECONDS,
        }


# Aliases for lifecycle compatibility
start_reconciliation_daemon = start_reconciliation
stop_reconciliation_daemon = stop_reconciliation
