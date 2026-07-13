"""
Completion Days Dashboard Refresh — Scheduled Service.
Converted from standalone completion_days_monthly.py cron script.

Runs weekly on Monday at 08:00 ICT (01:00 UTC) to refresh
dashboard "ORD - Số ngày chốt đơn hàng" (ID configurable via .env).

Logic:
  - Fetch active users (res.users)
  - Fetch done SOs (state=sale, completion=Done, has dates)
  - Fetch SO lines + product categories → determine dominant brand (BON/ORD)
  - Aggregate completion days by month/brand/user
  - Build cell updates (4×3 grid layout, BON + ORD tables per month)
  - Write cells to spreadsheet.dashboard
"""

import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ─── Scheduler State ───
_scheduler_running = False
_last_run = None
_last_result = None

# Target: Monday 08:00 ICT = 01:00 UTC
TARGET_HOUR_UTC = 1
TARGET_MINUTE_UTC = 0
TARGET_WEEKDAY = 0  # Monday (Python weekday(): 0=Mon)

# ─── Brand category IDs ───
BON_IDS = {19, 20, 21, 998, 999, 1000, 1002, 1003, 1004, 1005, 1006, 1007, 1008,
           1010, 1011, 1012, 1013, 1020, 1051, 1088, 1090, 1094, 1118, 1173,
           1174, 1175}
ORD_IDS = {1001, 1009, 1040, 1041, 1045, 1046, 1050, 1054, 1055, 1057, 1058, 1059,
           1060, 1061, 1062, 1064, 1065, 1066, 1067, 1068, 1069, 1070, 1077,
           1080, 1084, 1085, 1086, 1091, 1095, 1096, 1097, 1098, 1100, 1101,
           1102, 1103, 1104, 1106, 1107, 1108, 1109, 1110, 1111, 1112, 1113,
           1114, 1115, 1116, 1117, 1122, 1124, 1125, 1129, 1130, 1131, 1132,
           1133, 1134, 1135, 1136, 1137, 1138, 1139, 1140, 1153, 1154, 1156,
           1157, 1158, 1159, 1160, 1161, 1162, 1163, 1164, 1165, 1166, 1167,
           1168, 1169, 1170, 1171}
START_YEAR = 2026

# ─── Grid layout (must match build_completion_dashboard_offline.py) ───
SUMMARY_DATA_START = 5
SUMMARY_MONTHS_MAX = 12
SUMMARY_TOTAL_ROW = 17
GRID_DATA_START = 20
GRID_ROWS = 4
GRID_COLS = 3
SLOT_ROW_HEIGHT = 70
SLOT_COL_WIDTH = 4
SLOT_COL_GAP = 1
SLOTS_MAX = GRID_ROWS * GRID_COLS  # 12
B_DATA_OFF = 2
B_USERS_MAX = 30
B_TONG_OFF = 32
O_DATA_OFF = 37
O_TONG_OFF = 67

# ─── Output / log dirs (persist under DATA_DIR) ───
_DATA_DIR = os.getenv("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
OUTPUT_DIR = Path(_DATA_DIR) / "completion_days"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
JSONL = OUTPUT_DIR / "completion_days.jsonl"


# ═══════════════════════════════════════════
#  DATA FETCH
# ═══════════════════════════════════════════

def fetch_active_users() -> dict:
    """Fetch active users → {id: name}."""
    rows = odoo.search_read("res.users", [["active", "=", True]],
                            fields=["id", "name"], order="id")
    return {r["id"]: r["name"] for r in rows}


def fetch_done_so_ids() -> list:
    """Fetch IDs of done sale orders with completion dates."""
    return odoo.search("sale.order", [
        ["state", "=", "sale"],
        ["x_studio_selection_field_q4_1imrcsjj8", "=", "Done"],
        ["x_studio_date_of_completion", "!=", False],
        ["date_order", "!=", False],
    ])


def fetch_so_data(so_ids: list) -> list:
    """Fetch SO records + their lines (with product category)."""
    if not so_ids:
        return []
    sos = odoo.read("sale.order", so_ids,
                    fields=["id", "name", "user_id", "date_order",
                            "x_studio_date_of_completion"])
    lines = odoo.search_read("sale.order.line", [["order_id", "in", so_ids]],
                             fields=["order_id", "product_id", "price_subtotal"])
    product_ids = list({l["product_id"][0] for l in lines if l.get("product_id")})
    product_categ = {}
    if product_ids:
        prods = odoo.read("product.product", product_ids, fields=["id", "categ_id"])
        product_categ = {
            p["id"]: p["categ_id"][0] if p.get("categ_id") else None
            for p in prods
        }
    so_lines = defaultdict(list)
    for l in lines:
        if not l.get("product_id") or not l.get("order_id"):
            continue
        so_lines[l["order_id"][0]].append({
            "categ_id": product_categ.get(l["product_id"][0]),
            "subtotal": l["price_subtotal"],
        })
    for so in sos:
        so["_lines"] = so_lines.get(so["id"], [])
    return sos


# ═══════════════════════════════════════════
#  AGGREGATION
# ═══════════════════════════════════════════

def dominant_brand(lines):
    """Determine dominant brand (BON/ORD/OTHER) by subtotal."""
    bb = {"BON": 0.0, "ORD": 0.0, "OTHER": 0.0}
    for ln in lines:
        cid = ln.get("categ_id")
        sub = ln.get("subtotal", 0.0) or 0.0
        if cid in BON_IDS:
            bb["BON"] += sub
        elif cid in ORD_IDS:
            bb["ORD"] += sub
        else:
            bb["OTHER"] += sub
    top = max(bb.values())
    if bb["BON"] == top and top > 0:
        return "BON"
    if bb["ORD"] == top and top > 0:
        return "ORD"
    return "OTHER"


def month_of(so):
    """Extract (year, month) from completion date, or None."""
    cd = so.get("x_studio_date_of_completion")
    if not cd:
        return None
    if isinstance(cd, str):
        cd = date.fromisoformat(cd)
    if cd.year < START_YEAR:
        return None
    return (cd.year, cd.month)


def all_months():
    """All months from START_YEAR to current."""
    today = date.today()
    months = []
    y, m = START_YEAR, 1
    while (y, m) <= (today.year, today.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1; y += 1
    return months


def aggregate(sos, active_users):
    """Aggregate completion days by (month, brand) and (month, user, brand)."""
    by_mb = defaultdict(list)
    by_mub = defaultdict(list)
    for so in sos:
        user = so.get("user_id")
        if not user or user[0] not in active_users:
            continue
        ym = month_of(so)
        if not ym:
            continue
        cd = so["x_studio_date_of_completion"]
        od = so.get("date_order")
        if isinstance(cd, str):
            cd = date.fromisoformat(cd)
        if isinstance(od, str):
            od = datetime.fromisoformat(od.replace("Z", "+00:00"))
        if isinstance(od, datetime):
            od = od.date()
        days = (cd - od).days
        brand = dominant_brand(so["_lines"])
        if brand == "OTHER":
            continue
        by_mb[(ym, brand)].append(days)
        by_mub[(ym, user[0], brand)].append(days)
    return by_mb, by_mub


def stats(days_list):
    """Return (average, count) for a list of days."""
    if not days_list:
        return (0, 0)
    return (round(sum(days_list) / len(days_list), 1), len(days_list))


# ═══════════════════════════════════════════
#  CELL BUILDING
# ═══════════════════════════════════════════

def col_letter(n):
    return chr(ord('A') + n - 1)


def slot_col_start(month_idx):
    c = month_idx % GRID_COLS
    return 1 + c * (SLOT_COL_WIDTH + SLOT_COL_GAP)


def slot_row_start(month_idx):
    r = month_idx // GRID_COLS
    return GRID_DATA_START + r * SLOT_ROW_HEIGHT


def month_idx_for(ym, all_months_list):
    """Return 0-based index of ym in the year-month sequence, or None."""
    for i, m in enumerate(all_months_list):
        if m == ym:
            return i
    return None


def build_cell_updates(by_mb, by_mub, months_with_data, all_months_list, active_users):
    """Build the dict of {cell_coord: value} for the dashboard."""
    cells = {}

    # Brand Summary (12 months × 2 brands)
    for i, ym in enumerate(all_months_list[:SUMMARY_MONTHS_MAX]):
        r = SUMMARY_DATA_START + i
        cells[f"A{r}"] = f"{ym[0]}-{ym[1]:02d}"
        for brand, cols in [("BON", ("B", "C")), ("ORD", ("D", "E"))]:
            avg, count = stats(by_mb.get((ym, brand), []))
            cells[f"{cols[0]}{r}"] = count
            cells[f"{cols[1]}{r}"] = avg
    r = SUMMARY_TOTAL_ROW
    bon_all = [d for (m, b), dl in by_mb.items() if b == "BON" for d in dl]
    ord_all = [d for (m, b), dl in by_mb.items() if b == "ORD" for d in dl]
    avg, count = stats(bon_all)
    cells[f"B{r}"] = count; cells[f"C{r}"] = avg
    avg, count = stats(ord_all)
    cells[f"D{r}"] = count; cells[f"E{r}"] = avg

    # Per-month grid slots (4 rows × 3 cols)
    for ym in months_with_data:
        m_idx = month_idx_for(ym, all_months_list)
        if m_idx is None or m_idx >= SLOTS_MAX:
            logger.warning(f"[COMPLETION] Month {ym} out of grid range; skipping")
            continue
        cs = slot_col_start(m_idx)
        rs = slot_row_start(m_idx)
        slot_cols = [col_letter(cs + i) for i in range(SLOT_COL_WIDTH)]
        c1, c2, c3, c4 = slot_cols  # STT, NHANVIEN, count, avg
        month_label = f"{ym[0]}-{ym[1]:02d}"

        # BON table
        cells[f"{c4}{rs + 0}"] = f"BON — {month_label}"
        cells[f"{c1}{rs + 1}"] = "STT"
        cells[f"{c2}{rs + 1}"] = "NHÂN VIÊN"
        cells[f"{c3}{rs + 1}"] = "Số đơn"
        cells[f"{c4}{rs + 1}"] = "TB ngày"
        bon_uids = sorted({uid for (mm, uid, b) in by_mub
                           if mm == ym and b == "BON"}, key=int)[:B_USERS_MAX]
        for i, uid in enumerate(bon_uids):
            r = rs + B_DATA_OFF + i
            cells[f"{c1}{r}"] = str(i + 1)
            cells[f"{c2}{r}"] = active_users.get(uid, f"User {uid}")
            avg, count = stats(by_mub.get((ym, uid, "BON"), []))
            cells[f"{c3}{r}"] = count
            cells[f"{c4}{r}"] = avg
        r = rs + B_TONG_OFF
        bon_data = [d for (mm, u, b), dl in by_mub.items()
                    if mm == ym and b == "BON" for d in dl]
        avg, count = stats(bon_data)
        cells[f"{c3}{r}"] = count
        cells[f"{c4}{r}"] = avg

        # ORD table
        cells[f"{c4}{rs + 35}"] = f"ORD — {month_label}"
        cells[f"{c1}{rs + 36}"] = "STT"
        cells[f"{c2}{rs + 36}"] = "NHÂN VIÊN"
        cells[f"{c3}{rs + 36}"] = "Số đơn"
        cells[f"{c4}{rs + 36}"] = "TB ngày"
        ord_uids = sorted({uid for (mm, uid, b) in by_mub
                           if mm == ym and b == "ORD"}, key=int)[:B_USERS_MAX]
        for i, uid in enumerate(ord_uids):
            r = rs + O_DATA_OFF + i
            cells[f"{c1}{r}"] = str(i + 1)
            cells[f"{c2}{r}"] = active_users.get(uid, f"User {uid}")
            avg, count = stats(by_mub.get((ym, uid, "ORD"), []))
            cells[f"{c3}{r}"] = count
            cells[f"{c4}{r}"] = avg
        r = rs + O_TONG_OFF
        ord_data = [d for (mm, u, b), dl in by_mub.items()
                    if mm == ym and b == "ORD" for d in dl]
        avg, count = stats(ord_data)
        cells[f"{c3}{r}"] = count
        cells[f"{c4}{r}"] = avg

    return cells


# ═══════════════════════════════════════════
#  DASHBOARD UPDATE
# ═══════════════════════════════════════════

def update_dashboard(dashboard_id, cell_updates, dry_run=False):
    """Read dashboard spreadsheet, apply cell updates, write back."""
    res = odoo.read("spreadsheet.dashboard", [dashboard_id],
                    fields=["spreadsheet_data"])
    if not res:
        raise RuntimeError(f"Dashboard {dashboard_id} not found")
    data = json.loads(res[0]["spreadsheet_data"])
    cells = data["sheets"][0]["cells"]
    for coord, val in cell_updates.items():
        cells[coord] = str(val)
    logger.info(f"[COMPLETION] Cell updates prepared: {len(cell_updates)}")
    if dry_run:
        logger.info(f"[COMPLETION] [DRY-RUN] Would write {len(cell_updates)} cells")
        return
    data["revisionId"] = str(uuid.uuid4())
    ss_json = json.dumps(data, ensure_ascii=False)
    odoo.write("spreadsheet.dashboard", [dashboard_id],
               {"spreadsheet_data": ss_json})
    logger.info(f"[COMPLETION] Written {len(cell_updates)} cells, "
                f"revisionId={data['revisionId']}")


# ═══════════════════════════════════════════
#  MAIN RUN
# ═══════════════════════════════════════════

def run_completion_days_refresh(dashboard_id=None, dry_run=False) -> dict:
    """
    Run the completion-days dashboard refresh.

    Args:
        dashboard_id: Override Config.COMPLETION_DAYS_DASHBOARD_ID if set.
        dry_run: If True, only report what would be written without writing.

    Returns:
        dict with run summary.
    """
    global _last_run, _last_result

    dashboard_id = dashboard_id or Config.COMPLETION_DAYS_DASHBOARD_ID
    now = datetime.now(timezone.utc)
    logger.info(f"[COMPLETION] === refresh dashboard={dashboard_id} "
                f"dry_run={dry_run} ===")

    active_users = fetch_active_users()
    logger.info(f"[COMPLETION] Active users: {len(active_users)}")

    so_ids = fetch_done_so_ids()
    logger.info(f"[COMPLETION] SOs matching filter: {len(so_ids)}")
    sos = fetch_so_data(so_ids)

    by_mb, by_mub = aggregate(sos, active_users)
    months_with_data = sorted({m for (m, b) in by_mb}, key=lambda x: (x[0], x[1]))
    logger.info(f"[COMPLETION] Months with data: "
                f"{[f'{y}-{m:02d}' for y, m in months_with_data]}")

    months_full = all_months()
    cell_updates = build_cell_updates(by_mb, by_mub, months_with_data,
                                      months_full, active_users)
    logger.info(f"[COMPLETION] Cell updates: {len(cell_updates)}")
    update_dashboard(dashboard_id, cell_updates, dry_run=dry_run)

    result = {
        "ran_at": now.isoformat(),
        "dashboard_id": dashboard_id,
        "dry_run": dry_run,
        "so_count": len(sos),
        "active_user_count": len(active_users),
        "months_with_data": [f"{y}-{m:02d}" for y, m in months_with_data],
        "n_cell_updates": len(cell_updates),
    }

    _last_run = now.strftime("%Y-%m-%d %H:%M:%S")
    _last_result = result

    # Persist summary
    out_file = OUTPUT_DIR / f"completion_days_{date.today().strftime('%Y-%m')}.json"
    with out_file.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    logger.info(f"[COMPLETION] Summary -> {out_file}")
    logger.info(f"[COMPLETION] === done ===")
    return result


# ─── Scheduler ───

def _seconds_until_next_run() -> float:
    """Calculate seconds until next Monday 08:00 ICT (01:00 UTC)."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=TARGET_HOUR_UTC, minute=TARGET_MINUTE_UTC,
                         second=0, microsecond=0)
    # Move to the target weekday (Monday=0)
    days_ahead = TARGET_WEEKDAY - target.weekday()
    if days_ahead < 0:
        days_ahead += 7
    target += timedelta(days=days_ahead)
    # If today is the target weekday but time already passed, schedule next week
    if now >= target:
        target += timedelta(days=7)
    delta = (target - now).total_seconds()
    return delta


def _scheduler_loop():
    """Background loop that runs completion-days refresh weekly on Monday."""
    global _scheduler_running
    _scheduler_running = True

    # Wait 30s before calculating first run (let server finish starting)
    time.sleep(30)

    while _scheduler_running:
        wait_seconds = _seconds_until_next_run()
        hours = wait_seconds / 3600
        logger.info(f"[COMPLETION] Next run in {hours:.1f}h ({wait_seconds:.0f}s)")

        # Sleep until target time (wake every 60s to check if still running)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and _scheduler_running:
            remaining = deadline - time.monotonic()
            chunk = min(60, max(0, remaining))
            if chunk > 0:
                time.sleep(chunk)

        if not _scheduler_running:
            break

        # Run completion-days refresh
        try:
            result = run_completion_days_refresh(dry_run=False)
            logger.info(f"[COMPLETION] Completed: "
                        f"cells={result['n_cell_updates']}, "
                        f"so_count={result['so_count']}")
        except Exception as e:
            logger.exception(f"[COMPLETION] Error: {e}")

        # Safety sleep: wait 120s before recalculating next run
        # This prevents double-execution if the job finishes within the same minute
        time.sleep(120)


def start_completion_days_scheduler():
    """Start the background completion-days scheduler (call once at app startup)."""
    if not Config.COMPLETION_DAYS_ENABLED:
        logger.warning("[COMPLETION] COMPLETION_DAYS_ENABLED not true — "
                       "scheduler disabled")
        return
    if not Config.ODOO_UID and not Config.ODOO_USER:
        logger.warning("[COMPLETION] Odoo credentials not set — scheduler disabled")
        return

    thread = threading.Thread(target=_scheduler_loop, daemon=True,
                              name="completion-days-scheduler")
    thread.start()
    logger.info("[COMPLETION] Scheduler started — target: "
                "Monday 08:00 ICT (01:00 UTC) weekly")


def get_completion_days_status() -> dict:
    """Get scheduler status for health check."""
    return {
        "scheduler_active": _scheduler_running,
        "target_time": "Monday 08:00 ICT (01:00 UTC) weekly",
        "dashboard_id": Config.COMPLETION_DAYS_DASHBOARD_ID,
        "last_run": _last_run,
        "last_result_summary": {
            "so_count": _last_result["so_count"],
            "n_cell_updates": _last_result["n_cell_updates"],
            "months_with_data": _last_result["months_with_data"],
        } if _last_result else None,
    }
