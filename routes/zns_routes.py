"""
ZNS Routes Blueprint — unified routes for ZNS dispatches, delivery webhooks,
authenticated history / metrics APIs, and dashboard view.
"""

import hmac
import logging
from datetime import datetime
from functools import wraps
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request, render_template_string, Response

from config import Config
from services.zns_repository import get_repository
from services.zns_tracking import ZNSTrackingService

logger = logging.getLogger(__name__)

zns_bp = Blueprint("zns", __name__)


@zns_bp.before_request
def enforce_zns_payload_limit():
    """Enforce 100KB payload limit specifically for ZNS endpoints without buffering oversized streams."""
    if request.method in ("POST", "PUT", "PATCH"):
        max_bytes = Config.ZNS_MAX_PAYLOAD_BYTES
        # 1. Fast path: check Content-Length header
        if request.content_length is not None:
            if request.content_length > max_bytes:
                return jsonify({
                    "status": "error",
                    "error": "PAYLOAD_TOO_LARGE",
                    "message": f"Dung lượng request vượt quá giới hạn cho phép ({max_bytes} bytes).",
                }), 413
        else:
            # 2. Bounded stream reader for chunked transfer encoding (Content-Length is None)
            stream = request.environ.get("wsgi.input")
            if stream:
                try:
                    # Read at most max_bytes + 1 to prevent buffering oversized stream into memory
                    chunk = stream.read(max_bytes + 1)
                    if len(chunk) > max_bytes:
                        return jsonify({
                            "status": "error",
                            "error": "PAYLOAD_TOO_LARGE",
                            "message": f"Dung lượng request vượt quá giới hạn cho phép ({max_bytes} bytes).",
                        }), 413
                    import io
                    request.environ["wsgi.input"] = io.BytesIO(chunk)
                    request._cached_data = chunk
                except Exception as e:
                    logger.warning(f"Error reading bounded stream: {e}")


def _get_tracking_service() -> ZNSTrackingService:
    return ZNSTrackingService()


# ═══════════════════════════════════════════
#  AUTHENTICATION DECORATORS (FAIL-CLOSED)
# ═══════════════════════════════════════════

def zns_admin_required(f):
    """
    Decorator enforcing Header-Only API Key authentication for ZNS management and stats APIs.
    Fails closed: if ZNS_ADMIN_API_KEY is not configured and ZNS_ALLOW_INSECURE_DEV is False (or in production),
    returns HTTP 503 Service Unavailable.
    Accepts X-API-Key header or Authorization: Bearer <key>.
    Query parameters (?api_key=) are strictly disallowed to prevent secret leakage in logs.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        configured_key = Config.ZNS_ADMIN_API_KEY
        if not configured_key:
            if Config.ZNS_ALLOW_INSECURE_DEV and Config.ENVIRONMENT != "production":
                logger.warning("ZNS Admin API accessed without authentication (ZNS_ALLOW_INSECURE_DEV=true in non-prod)")
                return f(*args, **kwargs)
            return jsonify({
                "status": "error",
                "error": "SERVICE_UNAVAILABLE",
                "message": "Dịch vụ quản trị ZNS chưa được cấu hình khóa bảo mật (ZNS_ADMIN_API_KEY).",
            }), 503

        provided_key = (
            request.headers.get("X-API-Key")
            or request.headers.get("x-api-key")
            or ""
        )
        if not provided_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                provided_key = auth_header[7:].strip()

        if not provided_key or not hmac.compare_digest(provided_key, configured_key):
            return jsonify({
                "status": "error",
                "error": "UNAUTHORIZED",
                "message": "Truy cập bị từ chối: Khóa API không hợp lệ hoặc thiếu.",
            }), 401

        return f(*args, **kwargs)

    return decorated_function


def zns_dashboard_auth_required(f):
    """
    Decorator enforcing HTTP Basic Auth or Bearer / X-API-Key for browser dashboard access.
    Prompts browser login modal via WWW-Authenticate: Basic.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        configured_key = Config.ZNS_ADMIN_API_KEY
        if not configured_key:
            if Config.ZNS_ALLOW_INSECURE_DEV and Config.ENVIRONMENT != "production":
                return f(*args, **kwargs)
            return Response("ZNS_ADMIN_API_KEY not configured", 503)

        # 1. Check Header X-API-Key
        provided_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key") or ""

        # 2. Check Bearer
        if not provided_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                provided_key = auth_header[7:].strip()

        # 3. Check HTTP Basic Auth
        if not provided_key and request.authorization:
            auth = request.authorization
            provided_key = auth.password or auth.username or ""

        if not provided_key or not hmac.compare_digest(provided_key, configured_key):
            return Response(
                "Truy cập Dashboard yêu cầu xác thực API Key.",
                401,
                {"WWW-Authenticate": 'Basic realm="ZNS Dashboard"'},
            )

        return f(*args, **kwargs)

    return decorated_function


def zns_inbound_required(f):
    """
    Decorator enforcing trusted caller authentication for paid ZNS send routes.
    Fails closed: if ZNS_INBOUND_AUTH_REQUIRED is True, verifies X-ZNS-API-Key / Bearer token.
    Fallback to ZNS_ADMIN_API_KEY is prohibited.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not Config.ZNS_INBOUND_AUTH_REQUIRED:
            if Config.ZNS_ALLOW_INSECURE_DEV and Config.ENVIRONMENT != "production":
                return f(*args, **kwargs)
            if Config.ENVIRONMENT == "production":
                return jsonify({
                    "status": "error",
                    "error": "SERVICE_UNAVAILABLE",
                    "message": "Inbound authentication is mandatory in production environment.",
                }), 503
            return f(*args, **kwargs)

        configured_inbound_key = Config.ZNS_INBOUND_API_KEY
        if not configured_inbound_key:
            if Config.ZNS_ALLOW_INSECURE_DEV and Config.ENVIRONMENT != "production":
                return f(*args, **kwargs)
            return jsonify({
                "status": "error",
                "error": "SERVICE_UNAVAILABLE",
                "message": "Cổng gửi ZNS chưa được cấu hình khóa xác thực (ZNS_INBOUND_API_KEY).",
            }), 503

        provided_key = (
            request.headers.get("X-ZNS-API-Key")
            or request.headers.get("x-zns-api-key")
            or ""
        )
        if not provided_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                provided_key = auth_header[7:].strip()

        if not provided_key or not hmac.compare_digest(provided_key, configured_inbound_key):
            return jsonify({
                "status": "error",
                "delivery_status": "failed",
                "error": {"code": "UNAUTHORIZED", "message": "Missing or invalid ZNS Inbound API key"},
                "message": "Không có quyền gửi ZNS: Thiếu hoặc sai khóa xác thực (X-ZNS-API-Key).",
            }), 401

        return f(*args, **kwargs)

    return decorated_function


# ═══════════════════════════════════════════
#  5 INBOUND ZNS SEND ROUTES
# ═══════════════════════════════════════════

ZNS_ROUTES = ["hdsd-eng", "hdsd-vie", "rating-ord-eng", "rating-ord-vie", "rating"]


def _dispatch_zns_handler(template_type: str):
    """Unified handler for all 5 inbound ZNS webhook routes."""
    # 1. Payload size check on raw stream and content-length
    raw_data = request.get_data(cache=True)
    if len(raw_data) > Config.ZNS_MAX_PAYLOAD_BYTES or (request.content_length and request.content_length > Config.ZNS_MAX_PAYLOAD_BYTES):
        return jsonify({
            "status": "error",
            "delivery_status": "failed",
            "error": {"code": "PAYLOAD_TOO_LARGE", "message": f"Payload exceeds {Config.ZNS_MAX_PAYLOAD_BYTES} bytes"},
            "message": "Dữ liệu yêu cầu quá lớn.",
        }), 413

    try:
        data = request.get_json(force=True, silent=True) or {}
        body = data.get("body", data) if isinstance(data, dict) else {}

        # Extract fields (supporting Odoo studio fields and standard names)
        phone_raw = (
            body.get("x_studio_phone")
            or body.get("phone")
            or body.get("mobile")
            or ""
        )
        order_code = (
            body.get("name")
            or body.get("order_code")
            or body.get("order_id")
            or ""
        )
        customer_name = (
            body.get("x_studio_tn_khch_hng")
            or body.get("customer_name")
            or body.get("name_customer")
            or ""
        )
        date_order = (
            body.get("date_order")
            or body.get("order_date")
            or ""
        )

        # Context fields
        idempotency_key = (
            request.headers.get("Idempotency-Key")
            or request.headers.get("idempotency-key")
            or body.get("idempotency_key")
            or None
        )
        source = body.get("source", "odoo")
        source_model = body.get("model") or body.get("source_model")
        source_record_id = body.get("record_id") or body.get("source_record_id")
        sent_by_user_id = body.get("sent_by_user_id") or body.get("user_id")
        sent_by_user_name = body.get("sent_by_user_name") or body.get("user_name")
        company_id = body.get("company_id")
        mode = body.get("mode")

        # Field length caps
        if phone_raw:
            phone_raw = str(phone_raw)[:32]
        if order_code:
            order_code = str(order_code)[:64]
        if customer_name:
            customer_name = str(customer_name)[:128]
        if date_order:
            date_order = str(date_order)[:64]
        if idempotency_key:
            idempotency_key = str(idempotency_key)[:128]

        # Dispatch via domain tracking service
        result = _get_tracking_service().dispatch_zns(
            template_type=template_type,
            phone_raw=phone_raw,
            order_code=order_code,
            order_date=date_order,
            customer_name=customer_name,
            idempotency_key=idempotency_key,
            source=str(source)[:32] if source else "odoo",
            source_model=str(source_model)[:64] if source_model else None,
            source_record_id=int(source_record_id) if source_record_id is not None else None,
            sent_by_user_id=int(sent_by_user_id) if sent_by_user_id is not None else None,
            sent_by_user_name=str(sent_by_user_name)[:64] if sent_by_user_name else None,
            company_id=int(company_id) if company_id is not None else None,
            mode=str(mode)[:32] if mode else None,
        )

        status_code = 200
        if result.get("status") == "rejected" and result.get("error", {}).get("code") in ("INVALID_PHONE", "INVALID_TEMPLATE"):
            status_code = 400

        return jsonify(result), status_code

    except Exception as e:
        logger.exception(f"Unexpected error in ZNS route [{template_type}]: {e}")
        return jsonify({
            "status": "error",
            "delivery_status": "failed",
            "error": {"code": "INTERNAL_SERVER_ERROR", "message": "An unexpected internal error occurred."},
            "message": "Lỗi máy chủ nội bộ trong quá trình xử lý gửi ZNS.",
        }), 500


# Register all 5 ZNS send routes with inbound auth
for route_name in ZNS_ROUTES:
    zns_bp.add_url_rule(
        f"/webhook/{route_name}",
        endpoint=f"zns_{route_name.replace('-', '_')}",
        view_func=zns_inbound_required(lambda rt=route_name: _dispatch_zns_handler(rt)),
        methods=["POST"],
    )


# ═══════════════════════════════════════════
#  DELIVERY REPORT WEBHOOK (DLR)
# ═══════════════════════════════════════════

@zns_bp.route("/webhook/zalo/zns-delivery", methods=["POST"])
def zalo_delivery_webhook():
    """
    Endpoint receiving Zalo OpenAPI delivery events (`user_received_message`).
    Verifies X-ZEvent-Signature and updates message status to DELIVERED.
    """
    raw_body = request.get_data(cache=True)
    if len(raw_body) > Config.ZNS_MAX_PAYLOAD_BYTES or (request.content_length and request.content_length > Config.ZNS_MAX_PAYLOAD_BYTES):
        return jsonify({"success": False, "message": f"Payload exceeds {Config.ZNS_MAX_PAYLOAD_BYTES} bytes"}), 413

    headers = dict(request.headers)
    payload = request.get_json(force=True, silent=True) or {}

    success, message, status_code = _get_tracking_service().process_delivery_webhook(
        raw_body=raw_body,
        headers=headers,
        payload=payload,
    )

    return jsonify({"success": success, "message": message}), status_code


# ═══════════════════════════════════════════
#  AUTHENTICATED HISTORY & STATS APIS
# ═══════════════════════════════════════════

@zns_bp.route("/api/zns/messages", methods=["GET"])
@zns_admin_required
def get_zns_messages():
    """
    Query historical ZNS messages with filtering and pagination.
    """
    repo = get_repository()
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)

    filters = {}
    if request.args.get("status"):
        filters["status"] = request.args.get("status")
    if request.args.get("app_key"):
        filters["app_key"] = request.args.get("app_key")
    if request.args.get("template_type"):
        filters["template_type"] = request.args.get("template_type")
    if request.args.get("business_reference"):
        filters["business_reference"] = request.args.get("business_reference")
    if request.args.get("source_model"):
        filters["source_model"] = request.args.get("source_model")
    if request.args.get("source_record_id"):
        filters["source_record_id"] = request.args.get("source_record_id", type=int)
    if request.args.get("sent_by_user_id"):
        filters["sent_by_user_id"] = request.args.get("sent_by_user_id", type=int)
    if request.args.get("sent_by_user_name"):
        filters["sent_by_user_name"] = request.args.get("sent_by_user_name")
    if request.args.get("from_date"):
        filters["from_date"] = request.args.get("from_date")
    if request.args.get("to_date"):
        filters["to_date"] = request.args.get("to_date")

    messages, total = repo.query_messages(filters=filters, page=page, page_size=page_size)

    return jsonify({
        "status": "success",
        "data": messages,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 1,
        },
    }), 200


@zns_bp.route("/api/zns/messages/<identifier>", methods=["GET"])
@zns_admin_required
def get_zns_message_detail(identifier: str):
    """
    Get full details and timeline event history for a message by internal ID, tracking_id, or zalo_msg_id.
    """
    repo = get_repository()
    message = repo.get_message_by_id_or_tracking(identifier)
    if not message:
        return jsonify({"error": f"Message '{identifier}' not found"}), 404

    events = repo.query_events_for_message(message["id"])
    return jsonify({
        "status": "success",
        "message": message,
        "timeline_events": events,
    }), 200


@zns_bp.route("/api/zns/stats", methods=["GET"])
@zns_admin_required
def get_zns_statistics():
    """
    Get aggregated ZNS metrics, rates, latency percentiles, and breakdowns.
    """
    repo = get_repository()
    filters = {}
    if request.args.get("app_key"):
        filters["app_key"] = request.args.get("app_key")
    if request.args.get("template_type"):
        filters["template_type"] = request.args.get("template_type")
    if request.args.get("from_date"):
        filters["from_date"] = request.args.get("from_date")
    if request.args.get("to_date"):
        filters["to_date"] = request.args.get("to_date")

    stats = repo.query_statistics(filters=filters)
    return jsonify({
        "status": "success",
        "stats": stats,
    }), 200


# ═══════════════════════════════════════════
#  FLASK HTML DASHBOARD VIEW
# ═══════════════════════════════════════════

_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <title>Bonario ZNS Tracking Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --primary: #0068ff;
            --success: #28a745;
            --danger: #dc3545;
            --warning: #ffc107;
            --info: #17a2b8;
            --dark: #212529;
            --light: #f8f9fa;
            --border: #e9ecef;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background: #f4f6f9; color: var(--dark); }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 25px; }
        .card { background: #fff; padding: 18px; border-radius: 8px; border: 1px solid var(--border); box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .card-title { font-size: 13px; font-weight: 600; text-transform: uppercase; color: #6c757d; margin-bottom: 8px; }
        .card-value { font-size: 24px; font-weight: 700; }
        .table-wrap { background: #fff; border-radius: 8px; border: 1px solid var(--border); overflow-x: auto; margin-bottom: 25px; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }
        th, td { padding: 12px 16px; border-bottom: 1px solid var(--border); }
        th { background: #f8f9fa; font-weight: 600; color: #495057; }
        .badge { padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
        .badge-ACCEPTED { background: #e8f4fd; color: var(--primary); }
        .badge-DELIVERED { background: #e8f8ec; color: var(--success); }
        .badge-REJECTED { background: #fde8e8; color: var(--danger); }
        .badge-SUBMISSION_UNKNOWN, .badge-DELIVERY_UNKNOWN { background: #fff8e1; color: #b78103; }
        .badge-QUEUED, .badge-SUBMITTING { background: #e2e3e5; color: #383d41; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1 style="margin: 0; font-size: 24px;">📊 Zalo ZNS Delivery & Tracking Dashboard</h1>
                <p style="margin: 4px 0 0 0; color: #6c757d; font-size: 14px;">Hệ thống giám sát và đối soát tin nhắn Zalo Notification Service</p>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <div class="card-title">Tổng Yêu Cầu</div>
                <div class="card-value">{{ stats.total_requests }}</div>
            </div>
            <div class="card">
                <div class="card-title">Tỷ Lệ Tiếp Nhận</div>
                <div class="card-value" style="color: var(--primary);">{{ stats.rates.acceptance_rate_pct }}%</div>
            </div>
            <div class="card">
                <div class="card-title">Tỷ Lệ Giao Thành Công</div>
                <div class="card-value" style="color: var(--success);">{{ stats.rates.delivery_rate_pct }}%</div>
            </div>
            <div class="card">
                <div class="card-title">Đã Phát Tới Khách</div>
                <div class="card-value" style="color: var(--success);">{{ stats.counts.delivered }}</div>
            </div>
            <div class="card">
                <div class="card-title">Từ Chối / Thất Bại</div>
                <div class="card-value" style="color: var(--danger);">{{ stats.counts.rejected }}</div>
            </div>
        </div>

        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Thời gian (UTC)</th>
                        <th>App</th>
                        <th>Mẫu tin</th>
                        <th>Người nhận</th>
                        <th>Đơn hàng</th>
                        <th>Trạng thái</th>
                        <th>Zalo Msg ID</th>
                    </tr>
                </thead>
                <tbody>
                    {% for m in messages %}
                    <tr>
                        <td>{{ m.created_at[:19].replace('T', ' ') }}</td>
                        <td><strong>{{ m.app_key.upper() }}</strong></td>
                        <td>{{ m.template_type }}</td>
                        <td><code>{{ m.phone_masked }}</code></td>
                        <td>{{ m.business_reference or '-' }}</td>
                        <td><span class="badge badge-{{ m.status }}">{{ m.status }}</span></td>
                        <td><code>{{ m.zalo_msg_id or '-' }}</code></td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="7" style="text-align: center; color: #6c757d; padding: 20px;">Chưa có dữ liệu tin nhắn</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""


@zns_bp.route("/zns/dashboard", methods=["GET"])
@zns_dashboard_auth_required
def zns_dashboard_view():
    """
    Render server-side HTML dashboard with metrics and recent messages table.
    """
    repo = get_repository()
    stats = repo.query_statistics()
    messages, _ = repo.query_messages(page=1, page_size=25)
    return render_template_string(_DASHBOARD_HTML, stats=stats, messages=messages)
