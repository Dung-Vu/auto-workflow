# Auto-Conducted Setup

## Tích hợp sẵn trong Auto-Workflow

Script auto-tick Conducted đã được tích hợp vào Flask app. **Không cần setup riêng** — chỉ cần rebuild Docker.

## Cách hoạt động

Mỗi ngày **08:00 ICT** (01:00 UTC), scheduler tự động:

1. Tìm Management Meetings đã kết thúc trong T+1
2. Có Meeting Minutes → **tick Conducted ✅**
3. Không có Minutes → **skip (Non-compliance ⚠️)**

## Env vars cần thiết (trong `.env`)

```
CONDUCTED_ODOO_UID=145
CONDUCTED_ODOO_API_KEY=your_api_key_here
```

## Deploy

```bash
# Rebuild Docker
docker build -t auto-workflow .
docker run --env-file .env -p 5050:5050 -v $(pwd)/data:/app/data auto-workflow
```

## Manual trigger

```bash
# Dry-run (chỉ xem, không tick)
curl -X POST "http://localhost:5050/webhook/conducted?dry_run=true"

# Chạy thật
curl -X POST "http://localhost:5050/webhook/conducted"
```

## Health check

```bash
curl http://localhost:5050/health
# → conducted.scheduler_active, conducted.last_run, etc.
```
