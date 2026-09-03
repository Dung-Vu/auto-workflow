# SỔ TAY VẬN HÀNH SAO LƯU & BẢO TRÌ DỮ LIỆU SQLITE ZNS TRACKING
**Tài liệu:** SQLite Backup, Restore & Data Retention Runbook
**Database Path:** `/app/data/zns_tracking.sqlite3`
**Chế độ hoạt động:** WAL Mode (Write-Ahead Logging), Foreign Keys ON, Busy Timeout 5000ms

---

## 1. NGUYÊN TẮC SAO LƯU TRỰC TUYẾN (ONLINE BACKUP) TRONG WAL MODE

Khi SQLite chạy ở chế độ **WAL (Write-Ahead Logging)**:
- Không được dùng lệnh copy thông thường (`cp`) trên file `.sqlite3` khi database đang có transaction ghi, vì có thể tạo bản sao lưu bị phân mảnh hoặc thiếu dữ liệu đang nằm trong file `-wal`.
- **Phương pháp chuẩn:** Sử dụng SQLite Online Backup API (`sqlite3.Connection.backup` trong Python hoặc lệnh `.backup` trong CLI) để tạo snapshot nhất quán 100% trong lúc ứng dụng đang chạy.

---

## 2. LỆNH SAO LƯU TRỰC TUYẾN ĐỊNH KỲ (ONLINE BACKUP SCRIPT)

### A. Lệnh sao lưu bằng Python CLI (Khuyến nghị):
Chạy script định kỳ (Cronjob lúc 02:00 sáng mỗi ngày):

```bash
docker exec -t auto-workflow python -c "
import sqlite3, os, datetime
src_db = '/app/data/zns_tracking.sqlite3'
backup_dir = '/app/data/backups'
os.makedirs(backup_dir, exist_ok=True)
timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
dst_db = os.path.join(backup_dir, f'zns_tracking_{timestamp}.sqlite3')

source = sqlite3.connect(src_db)
dest = sqlite3.connect(dst_db)
with dest:
    source.backup(dest, pages=100, sleep=0.01)
source.close()
dest.close()
print(f'Backup completed successfully: {dst_db}')
"
```

### B. Lệnh sao lưu trực tiếp bằng SQLite CLI:
```bash
sqlite3 /app/data/zns_tracking.sqlite3 ".backup '/app/data/backups/zns_tracking_backup.sqlite3'"
```

---

## 3. QUY TRÌNH PHỤC HỒI DỮ LIỆU (RESTORE PROCEDURE)

Trong trường hợp dữ liệu bị lỗi hoặc cần chuyển đổi máy chủ:

1. **Dừng container ứng dụng:**
   ```bash
   docker compose stop workflow
   ```
2. **Sao lưu lại file hỏng hiện tại (nếu cần điều tra):**
   ```bash
   mv /path/to/data/zns_tracking.sqlite3 /path/to/data/zns_tracking.sqlite3.corrupted
   rm -f /path/to/data/zns_tracking.sqlite3-wal /path/to/data/zns_tracking.sqlite3-shm
   ```
3. **Phục hồi từ bản sao lưu:**
   ```bash
   cp /path/to/data/backups/zns_tracking_20260828_020000.sqlite3 /path/to/data/zns_tracking.sqlite3
   chmod 644 /path/to/data/zns_tracking.sqlite3
   ```
4. **Khởi động lại container và kiểm tra integrity:**
   ```bash
   docker compose start workflow
   docker exec -it auto-workflow python -c "
   from services.zns_repository import get_repository
   repo = get_repository()
   conn = repo.get_connection()
   cur = conn.execute('PRAGMA integrity_check;')
   print('Integrity Check:', cur.fetchone()[0])
   conn.close()
   "
   ```

---

## 4. CHÍNH SÁCH DỌN DẸP & LƯU TRỮ DỮ LIỆU (DATA RETENTION POLICY)

- **Thời hạn lưu trữ tin nhắn (`ZNS_RETENTION_DAYS` = 90 ngày):**
  - Tự động dọn dẹp các tin nhắn ở trạng thái cuối (`DELIVERED`, `REJECTED`, `CANCELLED`) cũ hơn 90 ngày.
  - **Tuyệt đối không xóa** các tin nhắn đang ở trạng thái chưa hoàn tất (`QUEUED`, `SUBMITTING`, `ACCEPTED`, `SUBMISSION_UNKNOWN`, `DELIVERY_UNKNOWN`).
- **Thời hạn lưu trữ nhật ký Webhook Diagnostic (`ZNS_DIAGNOSTICS_RETENTION_DAYS` = 30 ngày):**
  - Tự động dọn dẹp các bản ghi webhook không khớp/không hợp lệ cũ hơn 30 ngày để tiết kiệm dung lượng đĩa.

---

## 5. CẤU HÌNH GIỚI HẠN DUNG LƯỢNG PAYLOAD REVERSE-PROXY (100 KB BODY LIMIT)

Để bảo vệ hệ thống trước các cuộc tấn công DoS và chèn payload dung lượng lớn, bắt buộc cấu hình giới hạn kích thước body ở tầng Reverse-Proxy (Nginx / Caddy / Cloudflare / Traefik) đồng bộ với `Config.ZNS_MAX_PAYLOAD_BYTES = 102400` (100 KB):

### A. Nginx Configuration
```nginx
server {
    server_name workflow.bonario.vn;
    client_max_body_size 100k;

    location /webhook/zalo/zns-delivery {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 100k;
    }

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### B. Caddyfile Configuration
```caddy
workflow.bonario.vn {
    request_body {
        max_size 100KB
    }
    reverse_proxy 127.0.0.1:5000
}
```

### C. Traefik (Docker Labels)
```yaml
labels:
  - "traefik.http.middlewares.limit-body.buffering.maxRequestBodyBytes=102400"
  - "traefik.http.routers.workflow.middlewares=limit-body"
```
