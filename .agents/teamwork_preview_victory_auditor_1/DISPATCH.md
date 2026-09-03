## 2026-08-27T07:17:32Z

<USER_REQUEST>
Your working directory is: /Users/dungvu/Documents/Bonario/auto-workflow/.agents/teamwork_preview_victory_auditor_1
Project root: /Users/dungvu/Documents/Bonario/auto-workflow

<original_task>
Triển khai nâng cấp toàn diện dịch vụ Zalo ZNS trong codebase auto-workflow:
1. R1: Khắc phục Race Condition & Đảm bảo An toàn Đa luồng (Concurrency Control) bằng `threading.Lock` theo từng app `ord` và `bon` khi đọc/ghi token file và gọi refresh token API.
2. R2: Token Caching Thông minh & Tái sử dụng Access Token (lưu `expires_at`, kiểm tra buffer 30 phút, chỉ refresh khi token hết hạn hoặc sắp hết hạn).
3. R3: Nâng cấp Auto-Refresh Daemon với Retry & Exponential Backoff (thử lại 3-5 lần với 1m, 2m, 5m khi gặp lỗi gián đoạn mạng hoặc API Zalo).
4. R4: Viết Bộ Kiểm thử Tự động `tests/test_zalo_zns.py` sử dụng mock kiểm tra: caching, concurrency, auto-refresh retry; đảm bảo toàn bộ test suites (`pytest` / `python -m unittest`) vượt qua 100%.

Constraints & Rules:
- Sequential Thinking: Luôn phân tích và suy nghĩ tuần tự trước khi hành động.
- Git Execution Policy: TUYỆT ĐỐI KHÔNG `git commit` hoặc `git push`. Chỉ chỉnh sửa và kiểm tra ở môi trường local.
- Maintain existing API endpoints (`/webhook/hdsd-*`, `/webhook/rating*`, `/webhook/zns-done`, `/health`).
</original_task>

Please perform the 3-phase independent victory audit (timeline verification, cheating/mocking authenticity check, independent test suite execution using `PYTHONPATH=. ./.venv/bin/pytest` and `PYTHONPATH=. ./.venv/bin/python -m unittest discover tests`).
Deliver your verdict report to `/Users/dungvu/Documents/Bonario/auto-workflow/.agents/teamwork_preview_victory_auditor_1/verdict.md` and send a message with your structured verdict.
</USER_REQUEST>
