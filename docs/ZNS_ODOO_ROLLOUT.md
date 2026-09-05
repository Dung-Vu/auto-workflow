# ZNS Odoo rollout — external automation only

## Nguyên tắc bắt buộc

**Không tạo hoặc chạy Python Code trong Odoo Server Action / Automation Rule.**
Toàn bộ logic ZNS chạy trong service `auto-workflow`; Odoo chỉ là nguồn dữ liệu và nơi nhận kết quả qua JSON-RPC.

## Kiến trúc

1. Odoo Automation Rule chỉ dùng built-in **Send Webhook Notification** (không có Python Code) để báo thay đổi gần như tức thời tới service ngoài.
2. Endpoint ngoài xác thực capability token, đọc lại `sale.order` qua JSON-RPC và kiểm tra điều kiện trước khi tạo queue.
3. `services/zns_odoo_poller.py` vẫn đọc thay đổi mỗi 10 giây làm fallback nếu webhook tạm thời lỗi. Lần khởi động đầu chỉ snapshot trạng thái hiện tại, **không gửi hồi tố**.
4. Khi một đơn mới thỏa `state = sale` và `Order State = Done`, service ngoài ánh xạ các trường Studio thành template:
   - ORD `Đã gửi (Vie)` → `hdsd-vie`
   - ORD `Đã gửi (Eng)` → `hdsd-eng`
   - ORD đánh giá Việt/Anh → `rating-ord-vie` / `rating-ord-eng`
   - BON đánh giá → `rating`
5. Service ngoài tạo queue request và một Chatter note qua JSON-RPC. Không có code nào được thực thi trong Odoo.
6. Durable SQLite queue, lease và idempotency chống gửi trùng.
7. Zalo Status API/webhook cập nhật `ACCEPTED`, `DELIVERED`, `REJECTED` lên đúng Chatter note ban đầu.

## Odoo configuration

Chỉ giữ Studio fields và Automation Rules `55–59`. Mỗi rule gọi một action loại `webhook`; trường `code` phải rỗng.

Các Python Server Action `2370–2374` đã được xóa ngày 04/09/2026. Chúng được thay bằng built-in webhook actions `2375–2379`, không chứa Python và không thực thi code trên Odoo.

## External state

- State file: `/app/data/zns_external_automation_state.json`
- Config:
  - `ZNS_EXTERNAL_AUTOMATION_ENABLED=true`
  - `ZNS_EXTERNAL_AUTOMATION_STATE_PATH=/app/data/zns_external_automation_state.json`
  - `ZNS_ODOO_WEBHOOK_TOKEN=<high-entropy capability>`
- Nếu state file mất hoặc hỏng, service fail-safe/bootstrap lại và không backfill đơn cũ.

## Custom tracking fields

- `x_studio_zns_request_state`
- `x_studio_zns_status`
- `x_studio_zns_msg_id`
- `x_studio_zns_last_template`
- `x_studio_zns_last_sent_at`
- `x_studio_zns_delivered_at`
- `x_studio_zns_send_count`
- `x_studio_zns_claim_token`
- `x_studio_zns_claim_owner`
- `x_studio_zns_processing_started_at`
- `x_studio_zns_sent_by_user_id`
- `x_studio_zns_sent_by_user_name`

## UAT tối thiểu

1. Dùng một đơn test chưa từng gửi ZNS.
2. Chọn đúng thương hiệu và mẫu tin khi đơn chưa `Done`: không được gửi.
3. Chuyển đơn thành Sales Order và `Order State = Done`.
4. Trong tối đa hai chu kỳ poll, kiểm tra:
   - Có đúng một Chatter note yêu cầu.
   - Trạng thái lần lượt `QUEUED → ACCEPTED → DELIVERED/REJECTED`.
   - Dashboard có đúng một bản ghi.
5. Không dùng đơn production cũ để thử vì cơ chế chống gửi trùng sẽ bỏ qua template đã gửi.
