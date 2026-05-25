"""
Delivery Tracking Service.
Replaces the "Delivery Tracking" n8n workflow.

Flow: Webhook POST (phone) → query Odoo project.task + stock.move → aggregate → JSON response.
"""

import logging
from datetime import datetime
from services.odoo_client import odoo
from utils.phone import normalize_phone_vn

logger = logging.getLogger(__name__)


# ─── Helpers (ported from n8n JS Code nodes) ───

def _delivery_status_vn(status: str) -> str:
    """Map Odoo delivery status to Vietnamese label."""
    if not status:
        return "Đang cập nhật"
    s = str(status).strip().lower()
    mapping = {
        "02_changes_requested": "Đã tiếp nhận",
        "01_in_progress": "Đang xử lý giao hàng",
        "1_done": "Đã giao hàng",
    }
    return mapping.get(s, "Đang cập nhật")


def _clean_staff_name(raw) -> str:
    """Extract staff name from Odoo many2one field."""
    if not raw:
        return None
    if isinstance(raw, (list, tuple)):
        raw = raw[1] if len(raw) > 1 else str(raw[0])
    parts = str(raw).split(",")
    return parts[-1].strip()


def _format_date_vn(date_str: str) -> str:
    """Format Odoo date string to DD/MM/YYYY."""
    if not date_str:
        return None
    try:
        d = date_str.split(" ")[0]
        year, month, day = d.split("-")
        return f"{day}/{month}/{year}"
    except (ValueError, IndexError):
        return None


def track_delivery(phone_raw: str) -> dict:
    """
    Track delivery by customer phone number.
    Ported from: Delivery Tracking workflow (all nodes).

    Args:
        phone_raw: Customer phone number (any format).

    Returns:
        dict with products, deliveries, and order info.
    """
    # ─── Normalize phone ───
    phone = normalize_phone_vn(phone_raw)
    if not phone:
        return {"success": False, "error": "Invalid phone number"}

    # ─── Query 1: project.task (delivery tasks) ───
    tasks = odoo.search_read(
        "project.task",
        [
            ("display_name", "like", "GH-KH"),
            ("partner_phone", "=", phone),
        ],
        fields=[
            "partner_phone",
            "partner_id",
            "x_studio_ma_don_hang",
            "x_studio_nhan_vien",
            "planned_date_begin",
            "x_studio_status_giao_hng",
        ],
    )

    if not tasks:
        return {
            "success": True,
            "orderCode": None,
            "customerName": None,
            "phone": phone,
            "products": [],
            "deliveries": [],
            "message": "Không tìm thấy đơn hàng",
        }

    # ─── Get order code from first task ───
    order_code = tasks[0].get("x_studio_ma_don_hang", "")

    # ─── Query 2: stock.move (product lines) for this order ───
    product_lines = []
    if order_code:
        product_lines = odoo.search_read(
            "stock.move",
            [("origin", "=", order_code)],
            fields=["product_id", "partner_id", "origin", "product_uom_qty"],
        )

    # ─── Process products ───
    product_map = {}
    for line in product_lines:
        prod = line.get("product_id")
        if not prod or not isinstance(prod, (list, tuple)):
            continue
        prod_id = prod[0]
        prod_name = prod[1] if len(prod) > 1 else None
        qty = float(line.get("product_uom_qty", 0))

        if prod_id not in product_map:
            product_map[prod_id] = {
                "productId": prod_id,
                "name": prod_name,
                "quantity": qty,
            }
        else:
            product_map[prod_id]["quantity"] = max(
                product_map[prod_id]["quantity"], qty
            )

    products = list(product_map.values())

    # ─── Process deliveries ───
    deliveries = []
    for r in tasks:
        raw_date = r.get("planned_date_begin")
        date_obj = None
        if raw_date:
            try:
                date_obj = datetime.strptime(
                    raw_date.split(".")[0], "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                try:
                    date_obj = datetime.strptime(raw_date[:10], "%Y-%m-%d")
                except ValueError:
                    pass

        partner = r.get("partner_id")
        staff = r.get("x_studio_nhan_vien")

        deliveries.append({
            "id": r.get("id"),
            "orderCode": r.get("x_studio_ma_don_hang"),
            "phone": r.get("partner_phone"),
            "customerName": partner[1] if isinstance(partner, (list, tuple)) and len(partner) > 1 else None,
            "staffName": _clean_staff_name(staff),
            "status": _delivery_status_vn(r.get("x_studio_status_giao_hng")),
            "rawStatus": r.get("x_studio_status_giao_hng"),
            "deliveryDate": _format_date_vn(raw_date),
            "deliveryDateRaw": raw_date,
        })

    # Sort by date
    deliveries.sort(key=lambda d: d.get("deliveryDateRaw") or "")

    # ─── Build response ───
    customer_name = None
    if deliveries:
        customer_name = deliveries[0].get("customerName")
        order_code = deliveries[0].get("orderCode") or order_code

    return {
        "success": True,
        "orderCode": order_code,
        "customerName": customer_name,
        "phone": phone,
        "products": products,
        "deliveries": deliveries,
    }
