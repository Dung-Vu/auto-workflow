# HƯỚNG DẪN TRIỂN KHAI ODOO ZALO ZNS TRACKING & UX TOAST
**Dự án:** Bonario / Ordinaire Automation Workflow
**Tài liệu:** Quy trình triển khai Odoo 19 Online Studio, Server Action Safe-Eval, Base Automation & Đồng bộ Durable Outbox
**Phiên bản Odoo hỗ trợ:** Odoo 19 Online (SaaS), Odoo 16.0/17.0/18.0 (Odoo.sh & On-Premise)

---

## 1. TỔNG QUAN KIẾN TRÚC & NGUYÊN LÝ HOẠT ĐỘNG

Hệ thống cung cấp trải nghiệm toàn diện (End-to-End UX) cho nhân viên CS (Customer Service) và tự động hóa quy trình gửi tin Zalo ZNS:
- **Tương thích hoàn toàn Odoo 19 Online (SaaS Safe-Eval):** Server Action sử dụng 100% cú pháp Python nội bộ hợp lệ trong safe_eval, không dùng `import requests` hay `import json` bị cấm trên môi trường SaaS.
- **Phản hồi Tức thì (Instant Toast Notification):** Khi CS bấm nút gửi, Odoo hiển thị Toast Notification ghi nhận yêu cầu ("Đã ghi nhận yêu cầu gửi ZNS (Chờ xử lý)") trong vòng `< 0.2 giây` mà không làm đơ giao diện.
- **Tự động tăng Version khi Gửi lại (Resend Versioning):** Mỗi lần bấm nút gửi lại, `x_studio_zns_send_count` tự động tăng (`v1`, `v2`, `v3`...), sinh `idempotency_key = sale.order:{record_id}:{template_type}:v{send_version}` để cho phép gửi lại hợp lệ khi sửa SĐT hoặc sau khi bị `REJECTED`, đồng thời triệt tiêu double-click trong cùng 1 lần bấm.
- **Giao tiếp Chuẩn JSON-RPC 2.0:** Thay thế toàn bộ XML-RPC bằng HTTP JSON-RPC 2.0 an toàn và hiệu năng cao qua endpoint `{ODOO_URL}/jsonrpc`.
- **Durable Dispatch Queue & Atomic Claim:** Single source of truth đặt tại bảng SQLite `zns_dispatch_requests` với composite unique key `(source_model, source_record_id, template_type, send_version)`. Worker giành claim (INSERT) hoặc reclaim atomic khi row `FAILED` (còn lượt retry) / `CLAIMED|PROCESSING` hết lease, tối đa 8 attempts. Mọi heartbeat/complete/fail được fence bằng `lease_owner + claim_token + lease_expires_at`; stale worker không thể ghi kết quả hoặc writeback Odoo. `COMPLETED` không gửi lại. Crash mơ hồ gọi `dispatch_zns` với cùng `idempotency_key` để lấy kết quả durable. Writeback completed dùng version guard và post-write compensation nếu resend xen giữa; generic Odoo JSON-RPC không được mô tả là atomic CAS.
- **Đồng bộ Bền vững (Durable Transactional Outbox):** Khi nhận DLR Webhook xác nhận khách đã nhận tin, hệ thống tự động ghi task vào bảng SQLite `zns_odoo_outbox` và worker chạy ngầm gọi JSON-RPC cập nhật Odoo với cơ chế Exponential Backoff Retry và Idempotent Chatter (tránh duplicate note khi mất response).

---

## BƯỚC 1: KHỞI TẠO CUSTOM FIELDS TRÊN ODOO STUDIO

Truy cập **Odoo Studio** trên model `sale.order` (và tùy chọn trên `res.partner`), thêm các trường kỹ thuật sau:

| Tên hiển thị (Field Label) | Mã kỹ thuật (Field Name) | Loại dữ liệu (Field Type) | Chi tiết cấu hình / Giá trị Selection |
| :--- | :--- | :--- | :--- |
| **ZNS Request State** | `x_studio_zns_request_state` | `Selection` | `pending`: Chờ xử lý<br/>`processing`: Đang gửi<br/>`completed`: Đã hoàn tất |
| **ZNS Status** | `x_studio_zns_status` | `Selection` | `draft`: Bản nháp<br/>`queued`: Đang xếp hàng<br/>`accepted`: Đã tiếp nhận (Chờ phát)<br/>`delivered`: Đã phát tới khách<br/>`rejected`: Từ chối gửi<br/>`unknown`: Đang đối soát |
| **ZNS Msg ID** | `x_studio_zns_msg_id` | `Char` | Lưu mã `msg_id` trả về từ Zalo OpenAPI |
| **ZNS Last Template** | `x_studio_zns_last_template` | `Char` | Tên mẫu tin gửi gần nhất (ví dụ: *hdsd-vie*) |
| **ZNS Last Sent At** | `x_studio_zns_last_sent_at` | `Datetime` | Thời điểm thực hiện gửi tin |
| **ZNS Delivered At** | `x_studio_zns_delivered_at` | `Datetime` | Thời điểm tin nhắn phát thành công tới máy khách |
| **ZNS Send Count** | `x_studio_zns_send_count` | `Integer` | Default: `0`. Tự động tăng khi CS bấm gửi/gửi lại |
| **ZNS Claim Token** | `x_studio_zns_claim_token` | `Char` | Unique Token phục vụ telemetry kiểm soát poller |
| **ZNS Claim Owner** | `x_studio_zns_claim_owner` | `Char` | Worker ID đang giữ lock xử lý |
| **ZNS Processing Started At** | `x_studio_zns_processing_started_at` | `Datetime` | Mốc thời gian bắt đầu xử lý để phục hồi stale |

---

## BƯỚC 2: CÀI ĐẶT SERVER ACTIONS & NÚT BẤM THỦ CÔNG CHO CS

### 1. Tạo 5 Server Actions tương ứng 5 Mẫu tin
Vào **Settings ➔ Technical ➔ Actions ➔ Server Actions** (`ir.actions.server`), tạo mới 5 bản ghi với cấu hình:

1. **Model:** `Sales Order` (`sale.order`)
2. **Action To Do:** `Execute Python Code`
3. **Python Code:** Dán toàn bộ nội dung từ file `odoo/zns_server_action.py`.
4. **Context Parameters:** Đặt biến `zns_template` trong Context tương ứng:

| Tên Server Action | Template Code | Context cấu hình trong Action | Ứng dụng & Đối tượng |
| :--- | :--- | :--- | :--- |
| **ZNS: Gửi HDSD Tiếng Việt** | `hdsd-vie` | `{'zns_template': 'hdsd-vie'}` | Ordinaire App (`ord`) |
| **ZNS: Gửi HDSD Tiếng Anh** | `hdsd-eng` | `{'zns_template': 'hdsd-eng'}` | Ordinaire App (`ord`) |
| **ZNS: Gửi Đánh Giá Đơn TV** | `rating-ord-vie` | `{'zns_template': 'rating-ord-vie'}` | Ordinaire App (`ord`) |
| **ZNS: Gửi Đánh Giá Đơn TA** | `rating-ord-eng` | `{'zns_template': 'rating-ord-eng'}` | Ordinaire App (`ord`) |
| **ZNS: Gửi Đánh Giá Bonario** | `rating` | `{'zns_template': 'rating'}` | Bonario App (`bon`) |

### 2. Gắn Nút bấm lên Form View Đơn Hàng (`sale.order`)
Trong Odoo Studio trên Form View của `sale.order`:
- Tạo nút bấm trên thanh Header (hoặc menu Action):
  - **Button Label:** `Gửi HDSD (Tiếng Việt)` ➔ Chọn Server Action: *ZNS: Gửi HDSD Tiếng Việt*.
  - **Button Label:** `Gửi Đánh Giá (Tiếng Việt)` ➔ Chọn Server Action: *ZNS: Gửi Đánh Giá Đơn TV*.
  - **Button Label:** `Gửi Đánh Giá (Bonario)` ➔ Chọn Server Action: *ZNS: Gửi Đánh Giá Bonario*.

---

## BƯỚC 3: KIỂM TRA KẾT NỐI READ-ONLY (PREFLIGHT CHECK)

Trước khi kích hoạt background workers, chạy lệnh preflight read-only để xác minh kết nối JSON-RPC tới Odoo:

```bash
./.venv/bin/python -c "from services.zns_odoo_client import get_zns_odoo_client; res = get_zns_odoo_client().test_connection(); print(res)"
```

**Output mẫu (Credentials/Secrets đã được tự động sanitize):**
```json
{
  "success": true,
  "server_version": "19.0+e",
  "uid": 2,
  "company_name": "Bonario JSC",
  "write_performed": false
}
```

---

## BƯỚC 4: CẤU HÌNH DURABLE DELIVERY WRITEBACK

Khi Flask nhận webhook báo phát thành công từ Zalo OpenAPI (`user_received_message`), hệ thống tự động:
1. Ghi nhận `status = DELIVERED` trên bảng SQLite `zns_messages`.
2. Tạo tác vụ đồng bộ trong bảng `zns_odoo_outbox` trong **cùng 1 transaction**.
3. Worker `ZNSOdooOutboxWorker` thực hiện gọi JSON-RPC 2.0 cập nhật Odoo:
   - `sale.order.write({'x_studio_zns_status': 'delivered', 'x_studio_zns_delivered_at': '...'})`
   - Ghi Chatter Note có marker định danh chống trùng lặp, kèm mốc thời gian UTC chính xác.
   - Tự động retry theo lũy thừa nếu Odoo tạm thời bận hoặc mất kết nối.

---

## KỊCH BẢN KIỂM THỬ & NGHIỆM THU (UAT TEST CASES)

| ID | Kịch bản kiểm thử | Thao tác thực hiện | Kết quả mong đợi |
| :--- | :--- | :--- | :--- |
| **TC-01** | Bấm gửi thành công (Happy Path) | CS mở SO hợp lệ, bấm nút "Gửi HDSD Tiếng Việt". | - Toast `Zalo ZNS: Đã Ghi Nhận Yêu Cầu`.<br/>- Chatter ghi nhận Internal Note kèm mã đơn hàng và SĐT đã mask.<br/>- `x_studio_zns_request_state` thành `pending`, `send_count` tăng lên 1. |
| **TC-02** | Bấm đúp liên tục (Double Click) | CS bấm liên tục 3 lần nút gửi trong 2 giây. | - Không phát sinh gửi trùng lặp trên Zalo.<br/>- Idempotency key bảo vệ an toàn trên backend. |
| **TC-03** | Đơn hàng không có SĐT | Xóa SĐT trên đơn/khách hàng, bấm gửi ZNS. | - Toast đỏ `Zalo ZNS: Thiếu Số Điện Thoại` (Sticky).<br/>- Không gọi HTTP ra ngoài.<br/>- Ghi Chatter cảnh báo thiếu SĐT. |
| **TC-04** | Gửi lại khi bị từ chối (Resend) | Đơn hàng bị Rejected hoặc CS sửa SĐT và bấm gửi lại. | - `x_studio_zns_send_count` tăng lên 2.<br/>- Idempotency key sinh ra `v2`, cho phép gửi lại thành công. |
| **TC-05** | Khách nhận tin (Delivery Callback) | Zalo gửi webhook `user_received_message`. | - Odoo tự động cập nhật `x_studio_zns_status` sang `delivered`.<br/>- Chatter ghi nhận log phát tin thành công qua Durable Outbox. |
