"""
Dashboard 247/271 "SCM - Hoạt động trễ hạn (v2)" refresh service.

Scheduled service that updates Dashboard 247/271 "SCM - Hoạt động trễ hạn (v2)"
with live data from ``mail.activity``.

The scheduler triggers monthly on the 1st at 06:00 ICT, which is 23:00 UTC the
previous day.  It queries all done SC activities (OP-, ĐPK-/DPK-, TM- types) with
``active_test=False``, computes per-user / per-month / per-type metrics, and
writes them into the dashboard's 3 sheets:

  - Sheet 0 "Theo Nhân Viên"      — per-user summary + monthly aggregate + 2026 cross-table
  - Sheet 1 "Theo Loại Activity"  — per-type stats + user×type late cross-table
  - Sheet 2 "Chi Tiết Tháng"      — per-user monthly detail with totals

All Odoo interactions go through the shared ``odoo`` singleton
(``services.odoo_client``), which provides automatic retry with exponential
back-off on transient errors.
"""

import json
import logging
import threading
import time
import uuid
from datetime import date, datetime, timezone, timedelta
from collections import defaultdict

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ─── Scheduler State ───
_scheduler_running = False
_last_run = None
_last_result = None

# Target time: 23:00 UTC == 06:00 ICT (next day) == 1st of month
TARGET_HOUR_UTC = 23
TARGET_MINUTE_UTC = 0

# ─── Dashboard 247 business constants ───
DASH_ID = getattr(Config, "DASHBOARD_247_ID", 271)

# SC user mapping: (user_id, full_name, short_name)
SC_USERS = [
    (220, 'BÙI THỊ NHÃ UYÊN', 'NHÃ UYÊN'),
    (258, 'HUỲNH THỊ LÊ VY', 'LÊ VY'),
    (262, 'NGUYỄN QUỐC BẢO', 'QUỐC BẢO'),
    (249, 'NGUYỄN THỊ MAI HỒNG', 'MAI HỒNG'),
    (122, 'NGUYỄN THỊ TRANG', 'TRANG'),
    (120, 'PHAN THỊ MAI HƯƠNG', 'MAI HƯƠNG'),
    (276, 'TRẦN THỊ MAI THU', 'MAI THU'),
]

# All SC activity types (OP-, ĐPK-/DPK-, TM-)
SC_TYPE_IDS = [
    58, 78, 59, 67, 68, 60, 61, 62, 108, 112, 63, 100, 101, 65,  # OP-
    91, 92, 114,  # ĐPK-/DPK-
    102, 103, 104,  # TM-
]

# Activity types for Sheet 1 cross-table columns
ACTIVITY_TYPE_COLS = [
    'OP - Báo giá mã chưa cần check stock',
    'OP - Báo giá đơn lớn',
    'OP - Check phí ship',
    'OP - Check stock & báo giá',
    'OP - Check sản phẩm customized',
    'OP - Lên lịch giao hàng',
    'OP - Lên lịch lắp đặt',
    'OP - Tìm mẫu thay thế',
    'OP - Sửa chữa',
    'OP - Bảo hành',
    'OP - khác',
]


# ═══════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════

def date_to_excel(dt):
    """Convert a Python date to an Excel date serial number."""
    base = date(1899, 12, 30)
    return (dt - base).days + 1


def month_end_excel(year, month):
    """Get the Excel date serial for the last day of the given month."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    return date_to_excel(last_day)


# ═══════════════════════════════════════════
#  DATA FETCH
# ═══════════════════════════════════════════

def fetch_done_activities() -> list:
    """Fetch all done SC activities for the 7 users with active_test=False."""
    ctx = {'active_test': False}
    sc_user_ids = [u[0] for u in SC_USERS]

    domain = [
        ['activity_type_id', 'in', SC_TYPE_IDS],
        ['user_id', 'in', sc_user_ids],
        ['date_done', '!=', False],
    ]
    all_ids = odoo.execute('mail.activity', 'search', [domain], {'context': ctx})

    batch = 500
    result = []
    for i in range(0, len(all_ids), batch):
        batch_ids = all_ids[i:i + batch]
        data = odoo.execute(
            'mail.activity', 'search_read',
            [[['id', 'in', batch_ids]]],
            {'fields': ['id', 'user_id', 'activity_type_id', 'date_done', 'date_deadline'],
             'context': ctx},
        )
        result.extend(data)
    return result


def fetch_overdue_open() -> dict:
    """Count currently overdue open activities per user."""
    today = date.today().isoformat()
    result = {}
    for user_id, _, _ in SC_USERS:
        domain = [
            ['activity_type_id', 'in', SC_TYPE_IDS],
            ['user_id', '=', user_id],
            ['date_done', '=', False],
            ['date_deadline', '<', today],
        ]
        cnt = odoo.execute('mail.activity', 'search_count', [domain])
        result[user_id] = cnt
    return result


# ═══════════════════════════════════════════
#  METRICS
# ═══════════════════════════════════════════

def compute_metrics(activities):
    """Compute per-user, per-month, per-type metrics from activity data.

    Returns:
        (user_monthly, user_type, monthly_late_days)
        - user_monthly[uid][(year, month)] = {'done', 'ontime', 'late'}
        - user_type[uid][type_name]       = {'done', 'ontime', 'late', 'late_days'}
        - monthly_late_days[(year, month)] = total late days across all users
    """
    user_monthly = defaultdict(lambda: defaultdict(lambda: {'done': 0, 'ontime': 0, 'late': 0}))
    user_type = defaultdict(lambda: defaultdict(lambda: {'done': 0, 'ontime': 0, 'late': 0, 'late_days': 0}))
    monthly_late_days = defaultdict(int)

    for a in activities:
        uid = a['user_id'][0]
        done = a['date_done']
        deadline = a['date_deadline']
        tname = a['activity_type_id'][1] if isinstance(a['activity_type_id'], list) else str(a['activity_type_id'])

        if not done:
            continue

        dt = date.fromisoformat(done)
        month_key = (dt.year, dt.month)
        user_monthly[uid][month_key]['done'] += 1
        user_type[uid][tname]['done'] += 1

        if deadline:
            if dt > date.fromisoformat(deadline):
                late_days = (dt - date.fromisoformat(deadline)).days
                user_monthly[uid][month_key]['late'] += 1
                user_type[uid][tname]['late'] += 1
                user_type[uid][tname]['late_days'] += late_days
                monthly_late_days[month_key] += late_days
            else:
                user_monthly[uid][month_key]['ontime'] += 1
                user_type[uid][tname]['ontime'] += 1

    return user_monthly, user_type, monthly_late_days


# ═══════════════════════════════════════════
#  CELL UPDATES
# ═══════════════════════════════════════════

def build_updates(activities, overdue_counts):
    """Build all cell updates for the 3 sheets.

    Returns:
        {sheet_index: {cell_key: value}}
    """
    user_monthly, user_type, monthly_late_days = compute_metrics(activities)
    today_str = date.today().isoformat()

    # Month range: Mar 2025 → Dec 2026
    months = []
    for year in range(2025, 2027):
        start_m = 3 if year == 2025 else 1
        for m in range(start_m, 13):
            months.append((year, m))

    updates = {0: {}, 1: {}, 2: {}}

    # ========== SHEET 1: Theo Nhân Viên ==========
    s = updates[0]
    s['A2'] = f'Live từ mail.activity (keep_done) | Cập nhật: {today_str}'

    user_rows = {220: 4, 258: 5, 262: 6, 249: 7, 122: 8, 120: 9, 276: 10}

    totals = {'done': 0, 'ontime': 0, 'late': 0, 'late_days': 0}
    for user_id, _, _ in SC_USERS:
        row = user_rows[user_id]
        total = sum(d['done'] for d in user_monthly.get(user_id, {}).values())
        ontime = sum(d['ontime'] for d in user_monthly.get(user_id, {}).values())
        late = sum(d['late'] for d in user_monthly.get(user_id, {}).values())
        late_days = sum(d['late_days'] for d in user_type.get(user_id, {}).values())
        pct = late / total if total > 0 else 0
        avg_late = round(late_days / late, 1) if late > 0 else '—'
        overdue = overdue_counts.get(user_id, 0)

        s[f'B{row}'] = str(total) if total > 0 else '—'
        s[f'C{row}'] = str(ontime) if ontime > 0 else '—'
        s[f'D{row}'] = str(late) if late > 0 else '—'
        s[f'E{row}'] = str(round(pct, 4)) if total > 0 else '0'
        s[f'F{row}'] = str(avg_late) if avg_late != '—' else '—'
        s[f'G{row}'] = str(overdue)

        totals['done'] += total
        totals['ontime'] += ontime
        totals['late'] += late
        totals['late_days'] += late_days

    # Row 11: TOTAL
    total_pct = totals['late'] / totals['done'] if totals['done'] > 0 else 0
    total_avg = round(totals['late_days'] / totals['late'], 1) if totals['late'] > 0 else '—'
    s['B11'] = str(totals['done'])
    s['C11'] = str(totals['ontime'])
    s['D11'] = str(totals['late'])
    s['E11'] = str(round(total_pct, 4))
    s['F11'] = str(total_avg) if total_avg != '—' else '—'
    s['G11'] = str(sum(overdue_counts.values()))

    # Rows 15-26: Monthly aggregate (first 12 months only)
    for idx, (year, month) in enumerate(months):
        if idx >= 12:
            break
        row = 15 + idx
        s[f'A{row}'] = str(month_end_excel(year, month))

        m_done = sum(user_monthly.get(uid, {}).get((year, month), {}).get('done', 0)
                     for uid, _, _ in SC_USERS)
        m_ontime = sum(user_monthly.get(uid, {}).get((year, month), {}).get('ontime', 0)
                       for uid, _, _ in SC_USERS)
        m_late = sum(user_monthly.get(uid, {}).get((year, month), {}).get('late', 0)
                     for uid, _, _ in SC_USERS)
        m_late_days = monthly_late_days.get((year, month), 0)
        m_pct = round(m_late / m_done, 4) if m_done > 0 else 0
        m_avg = round(m_late_days / m_late, 1) if m_late > 0 else '—'

        s[f'B{row}'] = str(m_done) if m_done > 0 else '—'
        s[f'C{row}'] = str(m_ontime) if m_ontime > 0 else '—'
        s[f'D{row}'] = str(m_late) if m_late > 0 else '—'
        s[f'E{row}'] = str(m_pct)
        s[f'F{row}'] = str(m_avg) if m_avg != '—' else '—'

    # Rows 28+: Per-employee per-month for 2026
    employee_2026 = [
        (220, 30, 'NHÃ UYÊN'),
        (258, 34, 'LÊ VY'),
        (262, 38, 'QUỐC BẢO'),
        (249, 42, 'MAI HỒNG'),
        (122, 46, 'TRANG'),
        (120, 50, 'MAI HƯƠNG'),
        (276, 54, 'MAI THU'),
    ]
    month_col_map_2026 = {
        (2026, 1): 'B', (2026, 2): 'C', (2026, 3): 'D',
        (2026, 4): 'E', (2026, 5): 'F', (2026, 6): 'G', (2026, 7): 'H',
    }

    for uid, row_start, _ in employee_2026:
        for (year, month), col in month_col_map_2026.items():
            d = user_monthly.get(uid, {}).get((year, month), {'done': 0, 'ontime': 0, 'late': 0})
            done = d['done']
            late = d['late']
            pct = late / done if done > 0 else 0
            s[f'{col}{row_start}'] = str(done) if done > 0 else '—'
            s[f'{col}{row_start + 1}'] = str(late) if late > 0 else '—'
            s[f'{col}{row_start + 2}'] = str(round(pct, 4))

    # ========== SHEET 2: Theo Loại Activity ==========
    s2 = updates[1]
    s2['A2'] = f'Cập nhật: {today_str}'

    # A4-A14: Activity type stats (all users combined)
    type_totals = defaultdict(lambda: {'done': 0, 'ontime': 0, 'late': 0, 'late_days': 0})
    for uid, _, _ in SC_USERS:
        for tname, d in user_type.get(uid, {}).items():
            type_totals[tname]['done'] += d['done']
            type_totals[tname]['ontime'] += d['ontime']
            type_totals[tname]['late'] += d['late']
            type_totals[tname]['late_days'] += d['late_days']

    for idx, tname in enumerate(ACTIVITY_TYPE_COLS):
        row = 4 + idx
        d = type_totals[tname]
        total = d['done']
        ontime = d['ontime']
        late = d['late']
        pct = round(late / total, 4) if total > 0 else 0
        avg_late = round(d['late_days'] / late, 1) if late > 0 else '—'

        s2[f'B{row}'] = str(total) if total > 0 else '—'
        s2[f'C{row}'] = str(ontime) if ontime > 0 else '—'
        s2[f'D{row}'] = str(late) if late > 0 else '—'
        s2[f'E{row}'] = str(pct) if total > 0 else '0'
        s2[f'F{row}'] = str(avg_late) if avg_late != '—' else '—'

    # Rows 19-25: Cross table (employee × activity type) — late counts
    user_row_map = {220: 19, 258: 20, 262: 21, 249: 22, 122: 23, 120: 24, 276: 25}
    type_col_map = {
        'OP - Báo giá mã chưa cần check stock': 'B',
        'OP - Báo giá đơn lớn': 'C',
        'OP - Check phí ship': 'D',
        'OP - Check stock & báo giá': 'E',
        'OP - Check sản phẩm customized': 'F',
        'OP - Lên lịch giao hàng': 'G',
        'OP - Lên lịch lắp đặt': 'H',
        'OP - Tìm mẫu thay thế': 'I',
        'OP - Sửa chữa': 'J',
        'OP - Bảo hành': 'K',
        'OP - khác': 'L',
    }

    cross_totals = defaultdict(int)
    for uid, _, _ in SC_USERS:
        row = user_row_map[uid]
        for tname, col in type_col_map.items():
            d = user_type.get(uid, {}).get(tname, {'late': 0})
            cnt = d.get('late', 0)
            cross_totals[col] += cnt
            s2[f'{col}{row}'] = str(cnt) if cnt > 0 else '—'

    for col, cnt in cross_totals.items():
        s2[f'{col}26'] = str(cnt) if cnt > 0 else '—'

    # ========== SHEET 3: Chi Tiết Tháng ==========
    s3 = updates[2]
    s3['A2'] = f'Cập nhật: {today_str}'

    user_sections = [
        (220, 3, 'BÙI THỊ NHÃ UYÊN'),
        (258, 19, 'HUỲNH THỊ LÊ VY'),
        (262, 35, 'NGUYỄN QUỐC BẢO'),
        (249, 51, 'NGUYỄN THỊ MAI HỒNG'),
        (122, 67, 'NGUYỄN THỊ TRANG'),
        (120, 83, 'PHAN THỊ MAI HƯƠNG'),
        (276, 99, 'TRẦN THỊ MAI THU'),
    ]

    for uid, start_row, _ in user_sections:
        data_start = start_row + 2
        total_row = start_row + 14

        t_done = t_ontime = t_late = t_late_days = 0

        for idx, (year, month) in enumerate(months):
            row = data_start + idx
            serial = month_end_excel(year, month)
            d = user_monthly.get(uid, {}).get((year, month), {'done': 0, 'ontime': 0, 'late': 0})
            done, ontime, late = d['done'], d['ontime'], d['late']
            pct = round(late / done, 4) if done > 0 else 0

            # Late days for this user/month
            late_days = 0
            for a in activities:
                if a['user_id'][0] == uid and a['date_done'] and a['date_deadline']:
                    dt = date.fromisoformat(a['date_done'])
                    if dt.year == year and dt.month == month and dt > date.fromisoformat(a['date_deadline']):
                        late_days += (dt - date.fromisoformat(a['date_deadline'])).days

            avg_late = round(late_days / late, 1) if late > 0 else '—'

            s3[f'A{row}'] = str(serial)
            s3[f'B{row}'] = str(done) if done > 0 else '—'
            s3[f'C{row}'] = str(ontime) if ontime > 0 else '—'
            s3[f'D{row}'] = str(late) if late > 0 else '—'
            s3[f'E{row}'] = str(pct)
            s3[f'F{row}'] = str(avg_late) if avg_late != '—' else '—'

            t_done += done
            t_ontime += ontime
            t_late += late
            t_late_days += late_days

        total_pct = round(t_late / t_done, 4) if t_done > 0 else 0
        total_avg = round(t_late_days / t_late, 1) if t_late > 0 else '—'
        s3[f'B{total_row}'] = str(t_done) if t_done > 0 else '—'
        s3[f'C{total_row}'] = str(t_ontime) if t_ontime > 0 else '—'
        s3[f'D{total_row}'] = str(t_late) if t_late > 0 else '—'
        s3[f'E{total_row}'] = str(total_pct)
        s3[f'F{total_row}'] = str(total_avg) if total_avg != '—' else '—'

    return updates


# ═══════════════════════════════════════════
#  REFRESH
# ═══════════════════════════════════════════

def run_dashboard_247_refresh(dry_run: bool = False) -> dict:
    """Run the Dashboard 247 refresh.

    Args:
        dry_run: If True, build updates and report counts without writing.

    Returns:
        dict with run summary.
    """
    global _last_run, _last_result

    ran_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("[D247] Refresh starting (dry_run=%s)", dry_run)

    # 1. Fetch data
    activities = fetch_done_activities()
    overdue_counts = fetch_overdue_open()
    logger.info("[D247] Fetched %d done activities, overdue open=%s",
                len(activities), overdue_counts)

    # 2. Build cell updates
    updates = build_updates(activities, overdue_counts)
    total_cells = sum(len(v) for v in updates.values())
    logger.info("[D247] Built %d cell updates across 3 sheets", total_cells)

    # 3. Read dashboard spreadsheet
    dash = odoo.read("spreadsheet.dashboard", [DASH_ID], ["name", "spreadsheet_data"])
    if not dash:
        raise RuntimeError(f"Dashboard {DASH_ID} not found")
    name = dash[0]["name"]
    data = json.loads(dash[0]["spreadsheet_data"])

    # 4. Apply cells per sheet
    for sheet_idx, cells in updates.items():
        sheet = data["sheets"][sheet_idx]
        for key, val in cells.items():
            sheet["cells"][key] = val
        logger.info("[D247]   Sheet %d: %d cells updated", sheet_idx, len(cells))

    # 5. Write back (no revisionId needed for this dashboard — just json.dumps)
    if not dry_run:
        odoo.write("spreadsheet.dashboard", [DASH_ID],
                   {"spreadsheet_data": json.dumps(data, ensure_ascii=False)})
        logger.info("[D247] Wrote dashboard %d (%s)", DASH_ID, name)
    else:
        logger.info("[D247] Dry-run — no write performed")

    result = {
        "ran_at": ran_at,
        "dashboard_id": DASH_ID,
        "dashboard_name": name,
        "dry_run": dry_run,
        "total_activities": len(activities),
        "total_cells": total_cells,
        "overdue_open": overdue_counts,
    }

    _last_run = ran_at
    _last_result = result
    logger.info("[D247] Refresh complete — activities=%d, cells=%d",
                len(activities), total_cells)
    return result


# ─── Scheduler ───

def _seconds_until_next_run() -> float:
    """Return seconds until the next 1st-of-month 23:00 UTC."""
    now = datetime.now(timezone.utc)

    today_target = now.replace(
        hour=TARGET_HOUR_UTC, minute=TARGET_MINUTE_UTC, second=0, microsecond=0,
    )

    if now.day == 1 and now < today_target:
        return (today_target - now).total_seconds()

    # First of next month at 23:00 UTC
    year, month = now.year, now.month + 1
    if month > 12:
        month = 1
        year += 1
    next_target = datetime(
        year, month, 1, TARGET_HOUR_UTC, TARGET_MINUTE_UTC, 0, tzinfo=timezone.utc,
    )
    return (next_target - now).total_seconds()


def _scheduler_loop():
    """Background loop that runs the Dashboard 247 refresh monthly on the 1st."""
    global _scheduler_running
    _scheduler_running = True

    # Wait 30s before calculating first run (let server finish starting)
    time.sleep(30)

    while _scheduler_running:
        wait_seconds = _seconds_until_next_run()
        hours = wait_seconds / 3600
        logger.info(f"[D247] Next run in {hours:.1f}h ({wait_seconds:.0f}s)")

        # Sleep until target time (wake every 60s to check if still running)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and _scheduler_running:
            remaining = deadline - time.monotonic()
            chunk = min(60, max(0, remaining))
            if chunk > 0:
                time.sleep(chunk)

        if not _scheduler_running:
            break

        # Run refresh
        try:
            result = run_dashboard_247_refresh(dry_run=False)
            logger.info(f"[D247] Completed: activities={result['total_activities']}, "
                        f"cells={result['total_cells']}")
        except Exception as e:
            logger.exception(f"[D247] Error: {e}")

        # Safety sleep: wait 120s before recalculating next run
        # This prevents double-execution if the job finishes within the same minute
        time.sleep(120)


def start_dashboard_247_scheduler():
    """Start the background Dashboard 247 scheduler (call once at app startup)."""
    if not getattr(Config, "DASHBOARD_247_ENABLED", False):
        logger.info("[D247] Scheduler disabled (DASHBOARD_247_ENABLED not set)")
        return

    if not Config.ODOO_UID and not Config.ODOO_USER:
        logger.warning("[D247] Odoo credentials not set — scheduler disabled")
        return

    if _scheduler_running:
        logger.info("[D247] Scheduler already running")
        return

    thread = threading.Thread(target=_scheduler_loop, daemon=True,
                              name="dashboard-247-scheduler")
    thread.start()
    logger.info("[D247] Scheduler started — target: 1st of month "
                "06:00 ICT (23:00 UTC prev day)")


def get_dashboard_247_status() -> dict:
    """Get scheduler status for health check."""
    summary = None
    if _last_result:
        summary = {
            "total_activities": _last_result.get("total_activities"),
            "total_cells": _last_result.get("total_cells"),
            "overdue_open": _last_result.get("overdue_open"),
            "dry_run": _last_result.get("dry_run"),
        }
    return {
        "scheduler_active": _scheduler_running,
        "target_time": "1st of month 06:00 ICT (23:00 UTC prev day)",
        "dashboard_id": DASH_ID,
        "last_run": _last_run,
        "last_result_summary": summary,
    }
