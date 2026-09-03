# ==============================================================================
# ARTIFACT: odoo/zns_server_action.py
# Production-ready Python Code for Odoo Server Actions & Automated Actions
# Compatibility: Odoo 19 Online (SaaS Safe-Eval Sandbox), Odoo.sh, Odoo On-Premise
# Target Models: sale.order, res.partner
# ==============================================================================
# HƯỚNG DẪN SỬ DỤNG CHO ODOO 19 ONLINE (SAAS):
# Trong Odoo 19 Online Studio, Server Action chạy trong môi trường `safe_eval`
# bị cấm các lệnh: `import`, `locals()`, `globals()`, `hasattr()`, `getattr()`.
#
# Đoạn mã dưới đây được thiết kế 100% tuân thủ Odoo 19 Safe-Eval:
# 1. Thuần tuý sử dụng `env`, `record`, `records`, `time`, `datetime`.
# 2. Kiểm tra trường bằng `'_fields'` (ví dụ: `'x_studio_phone' in target_record._fields`).
# 3. Chuẩn hoá chuỗi và escape HTML thuần tuý mà không dùng thư viện ngoài.
# 4. Ngăn chặn double-click khi trạng thái đang là 'pending'.
# 5. Hỗ trợ gửi lại (resend) với versioning tự tăng (`v1`, `v2`...).
# 6. Trả về Toast Notification `display_notification` tức thời cho CS.
# ==============================================================================

TEMPLATE_LABELS = {
    "hdsd-vie": "HDSD Tiếng Việt (ORD)",
    "hdsd-eng": "HDSD Tiếng Anh (ORD)",
    "rating-ord-vie": "Đánh giá Đơn hàng TV (ORD)",
    "rating-ord-eng": "Đánh giá Đơn hàng TA (ORD)",
    "rating": "Đánh giá Dịch vụ (BON)",
}

def _escape_html(text):
    s = str(text or "")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")
    return s

# 1. Xác định bản ghi mục tiêu (Target Record)
target_record = record if record else (records[0] if records else False)

if not target_record:
    action = {
        "type": "ir.actions.client",
        "tag": "display_notification",
        "params": {
            "title": "Lỗi Thực Thi",
            "message": "Không tìm thấy bản ghi để thực hiện gửi ZNS.",
            "type": "danger",
            "sticky": True,
        },
    }
else:
    # 2. Kiểm tra tránh gửi lặp khi yêu cầu trước đang chờ hoặc đang xử lý
    is_resend = env.context.get("resend", False)
    current_request_state = target_record.x_studio_zns_request_state if "x_studio_zns_request_state" in target_record._fields else ""

    if current_request_state in ("pending", "processing") and not is_resend:
        action = {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Zalo ZNS: Đang Chờ Xử Lý",
                "message": "Yêu cầu gửi trước đó đang ở hàng đợi hoặc đang xử lý. Vui lòng chờ hệ thống gửi tin hoặc chọn 'Gửi lại'.",
                "type": "warning",
                "sticky": False,
            },
        }
    elif current_request_state == "processing" and is_resend:
        action = {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Zalo ZNS: Đang Xử Lý Gửi Tin",
                "message": "Yêu cầu gửi trước đó đang được tiến hành gửi đi. Vui lòng chờ hệ thống hoàn tất.",
                "type": "warning",
                "sticky": False,
            },
        }
    else:
        # 3. Xác định template được chọn từ Context
        selected_template = env.context.get("zns_template") or "hdsd-vie"
        template_label = TEMPLATE_LABELS.get(selected_template, selected_template)

        # 4. Trích xuất và chuẩn hoá số điện thoại
        raw_phone = ""
        if "x_studio_phone" in target_record._fields and target_record.x_studio_phone:
            raw_phone = str(target_record.x_studio_phone).strip()
        elif "partner_id" in target_record._fields and target_record.partner_id:
            raw_phone = str(target_record.partner_id.mobile or target_record.partner_id.phone or "").strip()
        elif "mobile" in target_record._fields and target_record.mobile:
            raw_phone = str(target_record.mobile).strip()
        elif "phone" in target_record._fields and target_record.phone:
            raw_phone = str(target_record.phone).strip()

        # Lọc chỉ lấy chữ số
        phone_digits = "".join([c for c in raw_phone if c in "0123456789"])
        if phone_digits.startswith("84") and len(phone_digits) >= 11:
            phone_digits = "0" + phone_digits[2:]
        elif len(phone_digits) == 9 and not phone_digits.startswith("0"):
            phone_digits = "0" + phone_digits

        # 5. Trích xuất tên khách hàng & mã đơn hàng
        customer_name = ""
        if "x_studio_tn_khch_hng" in target_record._fields and target_record.x_studio_tn_khch_hng:
            customer_name = str(target_record.x_studio_tn_khch_hng).strip()
        elif "partner_id" in target_record._fields and target_record.partner_id:
            customer_name = str(target_record.partner_id.name).strip()
        elif "name" in target_record._fields and target_record._name == "res.partner":
            customer_name = str(target_record.name).strip()

        customer_display = customer_name or "Quý Khách"
        order_code = target_record.name if "name" in target_record._fields else str(target_record.id)

        # 6. Kiểm tra hợp lệ số điện thoại
        if not phone_digits or len(phone_digits) < 10:
            err_msg = "Đơn hàng/Khách hàng không có SĐT di động Việt Nam hợp lệ (hiện tại: '%s')." % (raw_phone or "Trống")

            target_record.message_post(
                body="<p>⚠️ <b>Lỗi gửi ZNS [%s]:</b> %s</p>" % (_escape_html(template_label), _escape_html(err_msg)),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

            action = {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Zalo ZNS: Thiếu Số Điện Thoại",
                    "message": err_msg,
                    "type": "danger",
                    "sticky": True,
                },
            }
        else:
            # 7. Tính toán Send Version (Tự tăng khi gửi lại)
            current_count = 0
            if "x_studio_zns_send_count" in target_record._fields and target_record.x_studio_zns_send_count:
                current_count = int(target_record.x_studio_zns_send_count)
            next_count = current_count + 1

            # 8. Cập nhật trạng thái trên bản ghi Odoo
            write_vals = {}
            if "x_studio_zns_send_count" in target_record._fields:
                write_vals["x_studio_zns_send_count"] = next_count
            if "x_studio_zns_request_state" in target_record._fields:
                write_vals["x_studio_zns_request_state"] = "pending"
            if "x_studio_zns_claim_token" in target_record._fields:
                write_vals["x_studio_zns_claim_token"] = False
            if "x_studio_zns_claim_owner" in target_record._fields:
                write_vals["x_studio_zns_claim_owner"] = False
            if "x_studio_zns_processing_started_at" in target_record._fields:
                write_vals["x_studio_zns_processing_started_at"] = False
            if "x_studio_zns_status" in target_record._fields:
                write_vals["x_studio_zns_status"] = "queued"
            if "x_studio_zns_last_template" in target_record._fields:
                write_vals["x_studio_zns_last_template"] = selected_template

            if write_vals:
                target_record.sudo().write(write_vals)

            # 9. Mask số điện thoại hiển thị trên Chatter
            masked_phone = phone_digits
            if len(phone_digits) >= 7:
                masked_phone = phone_digits[:2] + "***" + phone_digits[-4:]

            # 10. Ghi nhận Chatter Note
            chatter_html = """
            <div style="border-left: 4px solid #17a2b8; padding-left: 10px; margin: 5px 0;">
                <p>📨 <b>YÊU CẦU GỬI ZALO ZNS: %s (Lần %d)</b></p>
                <ul style="margin: 0; padding-left: 20px;">
                    <li><b>Người thực hiện:</b> %s</li>
                    <li><b>Người nhận:</b> %s (%s)</li>
                    <li><b>Mã đơn hàng:</b> %s</li>
                    <li><b>Trạng thái:</b> Đã ghi nhận vào hàng đợi gửi tin (Chờ xử lý).</li>
                </ul>
            </div>
            """ % (
                _escape_html(template_label),
                next_count,
                _escape_html(env.user.name),
                _escape_html(customer_display),
                _escape_html(masked_phone),
                _escape_html(order_code),
            )
            target_record.message_post(body=chatter_html, message_type="comment", subtype_xmlid="mail.mt_note")

            # 11. Trả về Toast Notification tức thời cho CS
            action = {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Zalo ZNS: Đã Ghi Nhận Yêu Cầu",
                    "message": "Yêu cầu gửi [%s] cho %s đã được chuyển vào hàng đợi xử lý." % (template_label, customer_display),
                    "type": "info",
                    "sticky": False,
                },
            }
