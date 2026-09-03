# Original User Request

## 2026-08-27T06:59:33Z

This is a single self-contained fix; keep it small and focused.

Triển khai nâng cấp toàn diện dịch vụ Zalo ZNS trong codebase auto-workflow: khắc phục triệt để rủi ro Race Condition khi làm mới token đồng thời, bổ sung cơ chế Token Caching thông minh (chỉ refresh khi token sắp hết hạn), tăng cường khả năng chịu lỗi cho Auto-Refresh daemon với cơ chế Retry Exponential Backoff, và viết bộ unit test tự động xác thực toàn bộ tính năng.

Working directory: /Users/dungvu/Documents/Bonario/auto-workflow
Integrity mode: development

## Requirements

### R1. Khắc phục Race Condition & Đảm bảo An toàn Đa luồng (Concurrency Control)
Triển khai cơ chế khóa đồng thời (`threading.Lock` theo từng app `ord` và `bon`) cho quá trình đọc/ghi token file và gọi API cấp mới token. Đảm bảo khi nhiều luồng Flask/Waitress cùng gửi tin ZNS hoặc khi daemon auto-refresh chạy đồng thời, chỉ có một luồng duy nhất được thực hiện refresh token tại một thời điểm, ngăn chặn hoàn toàn việc vô hiệu hóa Refresh Token của Zalo ngoài ý muốn.

### R2. Token Caching Thông minh & Tái sử dụng Access Token
Lưu trữ thời gian hết hạn (`expires_at`) cùng với token. Khi hàm gửi tin ZNS (`send_zns`) cần Access Token, kiểm tra nếu Access Token hiện tại vẫn còn hạn (với khoảng đệm an toàn buffer 30 phút), tái sử dụng ngay Access Token đó mà không gọi API refresh, giúp giảm 50% độ trễ (latency) và tránh chạm rate limit của Zalo. Chỉ gọi refresh khi token thực sự hết hạn hoặc sắp hết hạn.

### R3. Nâng cấp Auto-Refresh Daemon với Retry & Exponential Backoff
Cải tiến hàm `_auto_refresh_loop()`: khi gặp lỗi gián đoạn mạng hoặc API Zalo tạm thời không phản hồi, tự động thử lại (retry) sau khoảng thời gian tăng dần (exponential backoff: ví dụ 1m, 2m, 5m, tối đa 3-5 lần) trước khi chuyển sang chu kỳ 24h tiếp theo, ghi log cảnh báo chi tiết.

### R4. Viết Bộ Kiểm thử Tự động (Unit Tests) & Xác thực Toàn diện
Tạo file kiểm thử `tests/test_zalo_zns.py` sử dụng mock để kiểm tra:
1. Cơ chế Token Caching (tái sử dụng token còn hạn, tự động refresh khi hết hạn).
2. Xử lý Concurrency (mô phỏng nhiều thread gọi đồng thời mà không xung đột token).
3. Auto-Refresh Retry logic khi gặp sự cố mạng.
4. Đảm bảo toàn bộ test suites hiện có của dự án (`pytest` / `python -m unittest`) vượt qua 100%.

## Acceptance Criteria

### Tính Đúng đắn & An toàn Đa luồng
- [ ] Không còn tình trạng gọi refresh token ở mọi request `send_zns()` khi Access Token vẫn còn hạn.
- [ ] Mọi thao tác đọc/ghi file token và gọi refresh token đều được bảo vệ bởi Thread Lock riêng biệt cho từng app (`ord`, `bon`).
- [ ] Token JSON lưu trữ đầy đủ `access_token`, `refresh_token`, `expires_at`, `updated_at`.

### Khả năng Chịu lỗi & Retry
- [ ] Khi Zalo OAuth API trả về lỗi hoặc timeout, daemon auto-refresh thử lại tối thiểu 3 lần với backoff thay vì ngủ ngay 24h.
- [ ] Có ghi log rõ ràng ở mức INFO/WARNING/ERROR tương ứng với từng trạng thái.

### Kiểm thử & Tương thích
- [ ] File test `tests/test_zalo_zns.py` chạy thành công 100% với pytest/unittest.
- [ ] Không làm phá vỡ các API endpoint hiện tại (`/webhook/hdsd-*`, `/webhook/rating*`, `/webhook/zns-done`, `/health`).
- [ ] Tuân thủ chính sách Git: không tự ý `git commit` hoặc `git push`.
