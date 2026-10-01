"""
Commission data export — monthly Excel for Accounting (Helpdesk ticket 1547).

Builds a workbook from confirmed sale orders:
  - "Đơn trong tháng": orders whose Order Date (date_order, ICT) falls in the month.
  - "Thu nợ đơn cũ":   orders from earlier months that received a customer
                        payment dated inside the month.
  - "Khoản thu chưa gắn HĐ": customer payments of the month not yet matched
                        to any invoice (only when there are some).

Payments are read from each order's posted customer invoices
(``invoice_payments_widget``) and grouped per payment into deposit rounds
L1, L2, ... — extra L-columns appear only when some order needs them.

Money columns:
  - Phí không tính doanh số = amount_untaxed − (commission SP1 + commission SP2)
  - Tổng sau khi chia (HH)   = x_studio_commission_revenue (SP1 share)
  - Số tiền hoàn lại khách   = cash refunded on credit notes (column shown only if any)
  - Số tiền nợ còn lại       = amount_total − (L1 + L2 + ... − refunded) (current receivable)

The scheduler runs on the last day of each month at 23:00 ICT (16:00 UTC),
attaches the file to a fixed project.task, adds a download link to the task
description and posts an internal note tagging the configured partners.
"""

import base64
import calendar
import io
import logging
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

ICT = timezone(timedelta(hours=7))

# 23:00 ICT on the last day of the month == 16:00 UTC the same day
TARGET_HOUR_UTC = 16
TARGET_MINUTE_UTC = 0

_scheduler_running = False
_last_run = None
_last_result = None

SHEET_CURRENT = "Đơn trong tháng"
SHEET_DEBT = "Thu nợ đơn cũ"
SHEET_UNMATCHED = "Khoản thu chưa gắn HĐ"

ORDER_FIELDS = [
    "name", "date_order", "partner_id", "x_studio_tn_khch_hng", "x_studio_phone",
    "x_sale_order_tag_ids", "amount_undiscounted", "x_studio_total_discount",
    "amount_untaxed", "amount_tax", "amount_total",
    "x_studio_commission_revenue", "x_studio_commission_revenue_sp2",
    "x_studio_selection_field_q4_1imrcsjj8", "user_id", "x_studio_salesperson_2",
    "invoice_ids",
]
ORDER_STATE_FIELD = "x_studio_selection_field_q4_1imrcsjj8"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def month_bounds_utc(year, month):
    """Return (start, end) of the ICT calendar month as naive UTC strings for Odoo domains."""
    start = datetime(year, month, 1, tzinfo=ICT)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(ny, nm, 1, tzinfo=ICT)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.astimezone(timezone.utc).strftime(fmt), end.astimezone(timezone.utc).strftime(fmt)


def _utc_str_to_ict_date(value):
    if not value:
        return None
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(ICT).date()


def _to_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


# ---------------------------------------------------------------------------
# Odoo reads
# ---------------------------------------------------------------------------

def _chunks(ids, size=200):
    ids = list(ids)
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def _read_orders(client, ids):
    rows = []
    for chunk in _chunks(ids):
        rows.extend(client.read("sale.order", chunk, ORDER_FIELDS))
    return rows


CUSTOMER_DOC_TYPES = ("out_invoice", "out_refund")


def _move_types(client, move_ids):
    types = {}
    for chunk in _chunks(set(move_ids)):
        for m in client.read("account.move", chunk, ["move_type"]):
            types[m["id"]] = m["move_type"]
    return types


def _order_payments(client, orders):
    """Return {order_id: {"in": [...], "out": [...]}}; each entry {"date", "amount", "key"}.

    Only cash counts: a reconciliation whose counterpart is a payment or bank
    statement entry (move_type ``entry``), or a customer document belonging to
    another order (credit carried from one order to another). Offsets between
    documents of the same order and exchange differences are skipped. Cash on invoices is money received
    ("in"); cash on credit notes is money refunded to the customer ("out").
    A payment split across several documents of the same order is merged.
    Payment state is not checked: Odoo 19 keeps some reconciled bank
    payments in ``draft`` even though the invoice is paid.
    """
    doc_to_orders = defaultdict(set)
    order_docs = {}
    for o in orders:
        order_docs[o["id"]] = set(o.get("invoice_ids") or [])
        for inv_id in o.get("invoice_ids") or []:
            doc_to_orders[inv_id].add(o["id"])

    docs = []
    for chunk in _chunks(doc_to_orders):
        docs.extend(client.read(
            "account.move", chunk, ["move_type", "state", "invoice_payments_widget"],
        ))
    docs = [d for d in docs if d.get("move_type") in CUSTOMER_DOC_TYPES and d.get("state") == "posted"]
    lines_by_doc = {d["id"]: (d.get("invoice_payments_widget") or {}).get("content") or [] for d in docs}
    counterpart_types = _move_types(
        client, [ln["move_id"] for lines in lines_by_doc.values() for ln in lines if ln.get("move_id")],
    )

    grouped = defaultdict(lambda: {"in": {}, "out": {}})
    for d in docs:
        direction = "in" if d["move_type"] == "out_invoice" else "out"
        for line in lines_by_doc[d["id"]]:
            other = line.get("move_id")
            other_type = counterpart_types.get(other)
            if line.get("is_exchange") or other_type not in ("entry",) + CUSTOMER_DOC_TYPES:
                continue
            key = line.get("account_payment_id") or ("move", other)
            for order_id in doc_to_orders[d["id"]]:
                if other_type != "entry" and other in order_docs[order_id]:
                    continue
                entry = grouped[order_id][direction].setdefault(
                    key, {"date": _to_date(line.get("date")), "amount": 0.0, "key": str(key)},
                )
                entry["amount"] += float(line.get("amount") or 0.0)

    sort_key = lambda e: (e["date"] or date.min, e["key"])
    return {
        order_id: {k: sorted(v.values(), key=sort_key) for k, v in dirs.items()}
        for order_id, dirs in grouped.items()
    }


def _invoice_adjustments(client, orders):
    """Return {order_id: amount} for posted invoice lines not linked to any order line.

    Accounting sometimes adds lines on the invoice only (e.g. "Phí MPOS"
    -387,425), so the invoiced total differs from the order total. Credit
    note lines count with the opposite sign.
    """
    doc_to_orders = defaultdict(set)
    for o in orders:
        for inv_id in o.get("invoice_ids") or []:
            doc_to_orders[inv_id].add(o["id"])
    sign = {}
    for chunk in _chunks(doc_to_orders):
        for d in client.read("account.move", chunk, ["move_type", "state"]):
            if d.get("state") == "posted" and d.get("move_type") in CUSTOMER_DOC_TYPES:
                sign[d["id"]] = 1 if d["move_type"] == "out_invoice" else -1
    result = defaultdict(float)
    for chunk in _chunks(sign):
        lines = client.search_read("account.move.line", [
            ("move_id", "in", chunk), ("display_type", "=", "product"), ("sale_line_ids", "=", False),
        ], ["move_id", "price_total"])
        for line in lines:
            move_id = line["move_id"][0] if isinstance(line["move_id"], (list, tuple)) else line["move_id"]
            if move_id not in sign:
                continue
            for order_id in doc_to_orders[move_id]:
                result[order_id] += sign[move_id] * float(line.get("price_total") or 0.0)
    return dict(result)


def _docs_with_cash_in_month(client, month_start, month_end):
    """Customer invoices/credit notes reconciled with a payment/bank entry dated in the month."""
    parts = client.search_read("account.partial.reconcile", [
        ("max_date", ">=", month_start.isoformat()), ("max_date", "<=", month_end.isoformat()),
    ], ["debit_move_id", "credit_move_id"])
    line_ids = {p[side][0] for p in parts for side in ("debit_move_id", "credit_move_id")}
    line_move = {}
    for chunk in _chunks(line_ids):
        for ln in client.read("account.move.line", chunk, ["move_id"]):
            line_move[ln["id"]] = ln["move_id"][0]
    types = _move_types(client, line_move.values())
    docs = set()
    for p in parts:
        a, b = line_move.get(p["debit_move_id"][0]), line_move.get(p["credit_move_id"][0])
        for doc, other in ((a, b), (b, a)):
            if types.get(doc) in CUSTOMER_DOC_TYPES and types.get(other) == "entry":
                docs.add(doc)
    return docs


def _partner_info(client, partner_ids):
    info = {}
    for chunk in _chunks(set(partner_ids)):
        for p in client.read("res.partner", chunk,
                             ["street", "street2", "city", "state_id", "phone"]):
            parts = [p.get("street"), p.get("street2"), p.get("city"),
                     p["state_id"][1] if p.get("state_id") else None]
            info[p["id"]] = {
                "address": ", ".join(x.strip() for x in parts if x and x.strip()),
                "phone": p.get("phone") or "",
            }
    return info


def _name_map(client, model, ids, field):
    out = {}
    for chunk in _chunks(set(ids)):
        for r in client.read(model, chunk, [field]):
            out[r["id"]] = r[field]
    return out


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------

def build_report(year, month, client=None):
    """Collect rows for both sheets. Read-only."""
    client = client or odoo
    start_utc, end_utc = month_bounds_utc(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])

    current_ids = client.search("sale.order", [
        ("state", "=", "sale"), ("date_order", ">=", start_utc), ("date_order", "<", end_utc),
    ])

    # Older orders: customer documents with cash reconciled this month, then their orders.
    paid_inv_ids = _docs_with_cash_in_month(client, month_start, month_end)
    pays = client.search_read("account.payment", [
        ("date", ">=", month_start.isoformat()), ("date", "<=", month_end.isoformat()),
        ("payment_type", "=", "inbound"), ("partner_type", "=", "customer"),
        ("state", "not in", ["canceled", "cancel"]), ("is_reconciled", "=", False),
    ], ["name", "date", "amount", "partner_id"], order="date, id")
    unmatched = [p["id"] for p in pays]
    unmatched_rows = [{
        "name": p.get("name") or "", "date": _to_date(p.get("date")),
        "amount": float(p.get("amount") or 0.0),
        "partner": p["partner_id"][1] if p.get("partner_id") else "",
    } for p in pays]
    old_ids = []
    if paid_inv_ids:
        old_ids = client.search("sale.order", [
            ("state", "=", "sale"), ("date_order", "<", start_utc),
            ("invoice_ids", "in", list(paid_inv_ids)),
        ])

    orders = _read_orders(client, list(current_ids) + list(old_ids))
    payments = _order_payments(client, orders)
    adjustments = _invoice_adjustments(client, orders)
    partners = _partner_info(client, [o["partner_id"][0] for o in orders if o.get("partner_id")])
    tag_names = _name_map(client, "x_sale_order_tag",
                          [t for o in orders for t in o.get("x_sale_order_tag_ids") or []], "x_name")
    state_labels = dict(client.execute("sale.order", "fields_get", [[ORDER_STATE_FIELD]],
                                       {"attributes": ["selection"]})[ORDER_STATE_FIELD]["selection"])

    def to_row(o):
        cash = payments.get(o["id"], {})
        pays_o = cash.get("in", [])
        refunds_o = cash.get("out", [])
        total = float(o.get("amount_total") or 0.0)
        paid = sum(p["amount"] for p in pays_o)
        refunded = sum(p["amount"] for p in refunds_o)
        adjustment = adjustments.get(o["id"], 0.0)
        sp1 = float(o.get("x_studio_commission_revenue") or 0.0)
        sp2 = float(o.get("x_studio_commission_revenue_sp2") or 0.0)
        partner = partners.get(o["partner_id"][0], {}) if o.get("partner_id") else {}
        salers = [o["user_id"][1]] if o.get("user_id") else []
        if o.get("x_studio_salesperson_2"):
            salers.append(o["x_studio_salesperson_2"][1])
        return {
            "id": o["id"],
            "order_date": _utc_str_to_ict_date(o.get("date_order")),
            "name": o["name"],
            "tags": ", ".join(tag_names.get(t, "") for t in o.get("x_sale_order_tag_ids") or []),
            "customer": o.get("x_studio_tn_khch_hng") or (o["partner_id"][1] if o.get("partner_id") else ""),
            "address": partner.get("address", ""),
            "phone": o.get("x_studio_phone") or partner.get("phone", ""),
            "undiscounted": float(o.get("amount_undiscounted") or 0.0),
            "discount": float(o.get("x_studio_total_discount") or 0.0),
            "non_revenue_fee": float(o.get("amount_untaxed") or 0.0) - (sp1 + sp2),
            "untaxed": float(o.get("amount_untaxed") or 0.0),
            "tax": float(o.get("amount_tax") or 0.0),
            "total": total,
            "commission_sp1": sp1,
            "commission_sp2": sp2,
            "payments": pays_o,
            "refunds": refunds_o,
            "refunded": refunded,
            "invoice_adjustment": adjustment,
            "remaining": total + adjustment - (paid - refunded),
            "state": state_labels.get(o.get(ORDER_STATE_FIELD), o.get(ORDER_STATE_FIELD) or ""),
            "salers": " / ".join(salers),
        }

    current_set = set(current_ids)
    by_id = {o["id"]: o for o in orders}
    current_rows = [to_row(by_id[i]) for i in current_ids if i in by_id]
    debt_rows = []
    for i in old_ids:
        if i in current_set or i not in by_id:
            continue
        row = to_row(by_id[i])
        # keep only orders with a payment actually dated in the month
        if any(p["date"] and month_start <= p["date"] <= month_end
               for p in row["payments"] + row["refunds"]):
            debt_rows.append(row)

    key = lambda r: (r["order_date"] or date.min, r["name"])
    current_rows.sort(key=key)
    debt_rows.sort(key=key)
    return {
        "year": year, "month": month,
        "current": current_rows, "debt": debt_rows,
        "unmatched_payment_ids": unmatched,
        "unmatched_payments": unmatched_rows,
    }


# ---------------------------------------------------------------------------
# Excel rendering
# ---------------------------------------------------------------------------

MONEY_FMT = "#,##0"
DATE_FMT = "dd/mm/yyyy"


def _columns(rows):
    max_l = max([len(r["payments"]) for r in rows] + [1])
    has_sp2 = any(r["commission_sp2"] for r in rows)
    has_refund = any(r["refunded"] for r in rows)
    has_adjustment = any(round(r["invoice_adjustment"]) for r in rows)
    cols = [
        ("Ngày báo giá", "order_date", "date"),
        ("Số đơn hàng", "name", None),
        ("SO Tags", "tags", None),
        ("Tên khách hàng", "customer", None),
        ("Địa chỉ", "address", None),
        ("SĐT khách (Nếu có)", "phone", None),
        ("Số tiền trước chiết khấu", "undiscounted", "money"),
        ("Total Discount", "discount", "money"),
        ("PHÍ KHÔNG TÍNH DOANH SỐ", "non_revenue_fee", "money"),
        ("TỔNG TIỀN SAU CHIẾT KHẤU (CHƯA BAO GỒM VAT)", "untaxed", "money"),
        ("VAT", "tax", "money"),
        ("TỔNG SAU VAT", "total", "money"),
        ("TỔNG SAU KHI CHIA (HH)", "commission_sp1", "money"),
    ]
    if has_sp2:
        cols.append(("TỔNG SAU KHI CHIA (HH) - SALER 2", "commission_sp2", "money"))
    for n in range(1, max_l + 1):
        cols.append((f"Số tiền Cọc L{n}", ("pay_amount", n - 1), "money"))
        cols.append((f"Ngày chuyển cọc L{n}", ("pay_date", n - 1), "date"))
    if has_refund:
        cols.append(("Số tiền hoàn lại khách", "refunded", "money"))
    if has_adjustment:
        cols.append(("Điều chỉnh trên hóa đơn (phí MPOS…)", "invoice_adjustment", "money"))
    cols += [
        ("Số tiền Nợ còn lại", "remaining", "money"),
        ("Trạng thái", "state", None),
        ("Saler", "salers", None),
    ]
    return cols


def _cell_value(row, key):
    if isinstance(key, tuple):
        kind, idx = key
        if idx >= len(row["payments"]):
            return None
        return row["payments"][idx]["amount" if kind == "pay_amount" else "date"]
    return row[key]


def _row_formula(key, r, letters):
    """Excel formula for derived columns; None means write the Odoo value."""
    L = lambda k: f"{letters[k]}{r}"
    if key == "discount":
        return f"={L('undiscounted')}-{L('untaxed')}"
    if key == "non_revenue_fee":
        commission = L("commission_sp1")
        if "commission_sp2" in letters:
            commission += f"+{L('commission_sp2')}"
        return f"={L('untaxed')}-({commission})"
    if key == "total":
        return f"={L('untaxed')}+{L('tax')}"
    if key == "remaining":
        paid = [f"{col}{r}" for k, col in letters.items() if isinstance(k, tuple) and k[0] == "pay_amount"]
        formula = f"={L('total')}"
        if "invoice_adjustment" in letters:
            formula += f"+{L('invoice_adjustment')}"
        formula += f"-SUM({','.join(paid)})"
        if "refunded" in letters:
            formula += f"+{L('refunded')}"
        return formula
    return None


def render_xlsx(report):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    for title, rows in ((SHEET_CURRENT, report["current"]), (SHEET_DEBT, report["debt"])):
        ws = wb.create_sheet(title)
        cols = _columns(rows)
        letters = {key: get_column_letter(c) for c, (_, key, _) in enumerate(cols, 1)}
        for c, (label, _, _) in enumerate(cols, 1):
            cell = ws.cell(row=1, column=c, value=label)
            cell.font = Font(bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        for r, row in enumerate(rows, 2):
            for c, (_, key, kind) in enumerate(cols, 1):
                value = _row_formula(key, r, letters) or _cell_value(row, key)
                cell = ws.cell(row=r, column=c, value=value)
                if kind == "money":
                    cell.number_format = MONEY_FMT
                elif kind == "date":
                    cell.number_format = DATE_FMT
        if rows:
            total_row = len(rows) + 2
            ws.cell(row=total_row, column=1, value="TỔNG").font = Font(bold=True)
            for c, (_, key, kind) in enumerate(cols, 1):
                if kind == "money":
                    col = get_column_letter(c)
                    cell = ws.cell(row=total_row, column=c, value=f"=SUM({col}2:{col}{total_row - 1})")
                    cell.number_format = MONEY_FMT
                    cell.font = Font(bold=True)
        for c, (label, _, kind) in enumerate(cols, 1):
            width = 14 if kind else 22
            if label in ("Địa chỉ", "Tên khách hàng", "Saler"):
                width = 34
            ws.column_dimensions[get_column_letter(c)].width = width
        ws.row_dimensions[1].height = 48
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = ws.dimensions

    unmatched = report.get("unmatched_payments") or []
    if unmatched:
        ws = wb.create_sheet(SHEET_UNMATCHED)
        cols = [("Số phiếu thu", "name", None, 22), ("Ngày", "date", DATE_FMT, 14),
                ("Khách hàng", "partner", None, 40), ("Số tiền", "amount", MONEY_FMT, 16)]
        for c, (label, _, _, width) in enumerate(cols, 1):
            cell = ws.cell(row=1, column=c, value=label)
            cell.font = Font(bold=True)
            cell.fill = header_fill
            ws.column_dimensions[get_column_letter(c)].width = width
        for r, row in enumerate(unmatched, 2):
            for c, (_, key, fmt, _) in enumerate(cols, 1):
                cell = ws.cell(row=r, column=c, value=row[key])
                if fmt:
                    cell.number_format = fmt
        ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def file_name(year, month):
    return f"Du_lieu_tinh_hoa_hong_{year}-{month:02d}.xlsx"


# ---------------------------------------------------------------------------
# Delivery to Odoo task
# ---------------------------------------------------------------------------

def _post_note(client, task_id, partner_ids, html_body, marker):
    """Post an internal note; JSON/XML-RPC escapes HTML, so post a marker then rewrite."""
    client.execute("project.task", "message_post", [[task_id]], {
        "body": marker, "message_type": "comment",
        "subtype_xmlid": "mail.mt_note", "partner_ids": partner_ids,
    })
    msg = client.search_read("mail.message", [
        ("model", "=", "project.task"), ("res_id", "=", task_id), ("body", "ilike", marker),
    ], ["id"], limit=1, order="id desc")
    if msg:
        client.write("mail.message", [msg[0]["id"]], {"body": html_body})
        return msg[0]["id"]
    return None


def deliver(report, content, task_id, partner_ids, client=None):
    """Attach the workbook to the task, link it in the description and notify. Idempotent per month."""
    client = client or odoo
    year, month = report["year"], report["month"]
    name = file_name(year, month)
    existing = client.search_read("ir.attachment", [
        ("res_model", "=", "project.task"), ("res_id", "=", task_id), ("name", "=", name),
    ], ["id"], limit=1)
    if existing:
        logger.info("[COMM-EXPORT] %s already attached to task %s — skip", name, task_id)
        return {"skipped": True, "attachment_id": existing[0]["id"]}

    att_id = client.create("ir.attachment", {
        "name": name, "res_model": "project.task", "res_id": task_id, "type": "binary",
        "datas": base64.b64encode(content).decode(),
        "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    })
    token = client.execute("ir.attachment", "generate_access_token", [[att_id]])
    token = token[0] if isinstance(token, list) else token
    link = f"{Config.ODOO_URL}/web/content/{att_id}?download=true&access_token={token}"

    task = client.read("project.task", [task_id], ["description"])[0]
    entry = (f'<li>Tháng {month:02d}/{year}: <a href="{escape(link)}" target="_blank">{escape(name)}</a>'
             f' ({len(report["current"])} đơn trong tháng, {len(report["debt"])} đơn thu nợ)</li>')
    client.write("project.task", [task_id], {
        "description": (task.get("description") or "") + f"<ul>{entry}</ul>",
    })

    mentions = ""
    if partner_ids:
        partners = client.read("res.partner", partner_ids, ["name"])
        mentions = " ".join(
            f'<a href="/odoo/res.partner/{p["id"]}" class="o_mail_redirect" data-oe-id="{p["id"]}" '
            f'data-oe-model="res.partner" target="_blank">@{escape(p["name"])}</a>'
            for p in partners
        )
    unmatched = report.get("unmatched_payment_ids") or []
    extra = (f"<li>Sheet \"{SHEET_UNMATCHED}\": {len(unmatched)} khoản thu trong tháng chưa gắn hóa đơn "
             f"nên chưa xác định được đơn hàng, nhờ chị đối chiếu thêm.</li>") if unmatched else ""
    body = (f"<p>{mentions} Dữ liệu tính hoa hồng tháng {month:02d}/{year} đã xuất xong:</p><ul>"
            f'<li>File: <a href="{escape(link)}" target="_blank">{escape(name)}</a></li>'
            f"<li>Sheet \"{SHEET_CURRENT}\": {len(report['current'])} đơn.</li>"
            f"<li>Sheet \"{SHEET_DEBT}\": {len(report['debt'])} đơn tháng trước có thanh toán trong tháng.</li>"
            f"{extra}</ul>")
    msg_id = _post_note(client, task_id, partner_ids, body, f"[COMM-EXPORT-{year}-{month:02d}]")
    return {"skipped": False, "attachment_id": att_id, "message_id": msg_id}


def run_commission_export(year=None, month=None, dry_run=False):
    """Build, render and (unless dry_run) deliver the export for a month (default: current ICT month)."""
    global _last_run, _last_result
    now = datetime.now(ICT)
    year, month = year or now.year, month or now.month
    report = build_report(year, month)
    content = render_xlsx(report)
    result = {
        "month": f"{year}-{month:02d}", "current": len(report["current"]),
        "debt": len(report["debt"]), "unmatched": len(report["unmatched_payment_ids"]),
        "dry_run": dry_run,
    }
    if not dry_run:
        task_id = Config.COMMISSION_EXPORT_TASK_ID
        if not task_id:
            raise RuntimeError("COMMISSION_EXPORT_TASK_ID not configured")
        result.update(deliver(report, content, task_id, Config.COMMISSION_EXPORT_NOTIFY_PARTNER_IDS))
    _last_run = datetime.now(timezone.utc).isoformat()
    _last_result = result
    logger.info("[COMM-EXPORT] %s", result)
    return result, content


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def _seconds_until_next_run(now=None):
    """Seconds until the next last-day-of-month 16:00 UTC (23:00 ICT)."""
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    for _ in range(2):
        last_day = calendar.monthrange(year, month)[1]
        target = datetime(year, month, last_day, TARGET_HOUR_UTC, TARGET_MINUTE_UTC, tzinfo=timezone.utc)
        if now < target:
            return (target - now).total_seconds()
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise AssertionError("unreachable")


def _scheduler_loop():
    global _scheduler_running
    time.sleep(30)
    while _scheduler_running:
        wait = _seconds_until_next_run()
        logger.info("[COMM-EXPORT] Next run in %.1f hours", wait / 3600)
        end_time = time.time() + wait
        while _scheduler_running and time.time() < end_time:
            time.sleep(min(30, max(0, end_time - time.time())))
        if not _scheduler_running:
            break
        try:
            run_commission_export(dry_run=False)
        except Exception:
            logger.exception("[COMM-EXPORT] Export failed")
        time.sleep(120)


def start_commission_export_scheduler():
    global _scheduler_running
    if not getattr(Config, "COMMISSION_EXPORT_ENABLED", False):
        logger.info("[COMM-EXPORT] Scheduler disabled (COMMISSION_EXPORT_ENABLED not set)")
        return
    if not getattr(Config, "COMMISSION_EXPORT_TASK_ID", None):
        logger.warning("[COMM-EXPORT] COMMISSION_EXPORT_TASK_ID missing — scheduler not started")
        return
    if _scheduler_running:
        return
    _scheduler_running = True
    threading.Thread(target=_scheduler_loop, name="commission-export-scheduler", daemon=True).start()
    logger.info("[COMM-EXPORT] Scheduler started — target: last day of month 23:00 ICT")


def get_commission_export_status():
    return {
        "scheduler_active": _scheduler_running,
        "target_time": "last day of month 23:00 ICT (16:00 UTC)",
        "task_id": getattr(Config, "COMMISSION_EXPORT_TASK_ID", None),
        "last_run": _last_run,
        "last_result": _last_result,
    }
