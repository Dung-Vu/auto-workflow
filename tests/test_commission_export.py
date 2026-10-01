from __future__ import annotations

import io
from datetime import date, datetime, timezone

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from services import commission_export as ce


class FakeOdoo:
    def __init__(self):
        self.orders = {}
        self.invoices = {}
        self.payments = []
        self.moves = {}
        self.partials = []
        self.aml = {}
        self.partners = {}
        self.tags = {}
        self.inv_lines = []
        self.writes = []
        self.created = []
        self.executed = []

    def search(self, model, domain, limit=0):
        assert model == "sale.order"
        d = {f: (op, v) for f, op, v in domain}
        if "invoice_ids" in d:
            inv = set(d["invoice_ids"][1])
            return [o["id"] for o in self.orders.values()
                    if o["date_order"] < d["date_order"][1] and inv & set(o["invoice_ids"])]
        start = [v for f, op, v in domain if f == "date_order" and op == ">="][0]
        end = [v for f, op, v in domain if f == "date_order" and op == "<"][0]
        return [o["id"] for o in self.orders.values() if start <= o["date_order"] < end]

    def search_read(self, model, domain, fields=None, limit=0, order=None):
        if model == "account.payment":
            return self.payments
        if model == "account.partial.reconcile":
            return self.partials
        if model == "account.move.line":
            ids = set([v for f, op, v in domain if f == "move_id"][0])
            return [ln for ln in self.inv_lines if ln["move_id"][0] in ids]
        if model == "ir.attachment":
            return []
        if model == "mail.message":
            return [{"id": 999}]
        raise AssertionError(model)

    def read(self, model, ids, fields=None):
        moves = {i: {"id": i, "move_type": "entry"} for i in ids if i > 1000}
        moves.update(self.moves)
        moves.update(self.invoices)
        src = {"sale.order": self.orders, "account.move": moves, "account.move.line": self.aml,
               "res.partner": self.partners, "x_sale_order_tag": self.tags,
               "project.task": {7: {"id": 7, "description": "<p>Mô tả</p>"}}}[model]
        return [src[i] for i in ids if i in src]

    def execute(self, model, method, args=None, kwargs=None):
        self.executed.append((model, method, args, kwargs))
        if method == "fields_get":
            return {ce.ORDER_STATE_FIELD: {"selection": [["Done", "Hoàn tất"], ["In progress", "Đang xử lý"]]}}
        if method == "generate_access_token":
            return ["tok"]
        return True

    def create(self, model, values, context=None):
        self.created.append((model, values))
        return 55

    def write(self, model, ids, values):
        self.writes.append((model, ids, values))
        return True


def _order(oid, name, date_order, invoices, total=1100.0, untaxed=1000.0, sp1=900.0, sp2=0.0, sp2_name=None):
    return {
        "id": oid, "name": name, "date_order": date_order,
        "partner_id": [10, "Khách A"], "x_studio_tn_khch_hng": "Khách A", "x_studio_phone": "",
        "x_sale_order_tag_ids": [1], "amount_undiscounted": 1200.0, "x_studio_total_discount": 200.0,
        "amount_untaxed": untaxed, "amount_tax": total - untaxed, "amount_total": total,
        "x_studio_commission_revenue": sp1, "x_studio_commission_revenue_sp2": sp2,
        ce.ORDER_STATE_FIELD: "In progress", "user_id": [1, "Sale 1"],
        "x_studio_salesperson_2": [2, sp2_name] if sp2_name else False,
        "invoice_ids": invoices,
    }


def _inv(iid, lines, move_type="out_invoice"):
    return {"id": iid, "move_type": move_type, "state": "posted",
            "invoice_payments_widget": {"content": lines}}


def _pay(pid, d, amount, **kw):
    return {"account_payment_id": pid, "move_id": pid + 1000, "date": d, "amount": amount, **kw}


def _fake():
    f = FakeOdoo()
    f.partners = {10: {"id": 10, "name": "Khách A", "street": "1 Lê Lợi", "street2": "", "city": "Q1",
                       "state_id": [5, "TP Hồ Chí Minh"], "phone": "0901"}}
    f.tags = {1: {"id": 1, "x_name": "Rèm"}}
    # 1 Sep 00:30 ICT == 31 Aug 17:30 UTC -> belongs to September
    f.orders[1] = _order(1, "SO-SEP", "2026-08-31 17:30:00", [101], sp2=100.0, sp1=400.0, sp2_name="Sale 2")
    f.orders[2] = _order(2, "SO-OLD", "2026-05-02 03:00:00", [102])
    f.orders[3] = _order(3, "SO-AUG-LATE", "2026-08-31 16:59:59", [103])  # 31 Aug 23:59 ICT
    f.invoices = {
        101: _inv(101, [_pay(1, "2026-09-02", 500.0), _pay(2, "2026-09-20", 300.0),
                        # credit-note offset (counterpart is a document, not cash)
                        {"account_payment_id": False, "move_id": 201, "date": "2026-09-21", "amount": 50.0}]),
        102: _inv(102, [_pay(3, "2026-05-02", 600.0), _pay(4, "2026-09-15", 200.0)]),
        103: _inv(103, [_pay(5, "2026-08-31", 1100.0)]),
    }
    # credit note 201 belongs to the same order as invoice 101 -> internal offset
    f.orders[1]["invoice_ids"] = [101, 201]
    f.invoices[201] = _inv(201, [{"account_payment_id": False, "move_id": 101, "date": "2026-09-21", "amount": 50.0}],
                           move_type="out_refund")
    _link(f, 102, 1004)   # cash on old order -> sheet 2
    _link(f, 101, 1002)
    _link(f, 103, 201)    # invoice <-> credit note offset only: not cash
    f.payments = [{"id": 8, "name": "PBNK/8", "date": "2026-09-09", "amount": 42.0,
                   "partner_id": [10, "Khách A"]}]  # unreconciled customer payment
    return f


def _link(f, doc, other):
    a, b = len(f.aml) + 1, len(f.aml) + 2
    f.aml[a] = {"id": a, "move_id": [doc, "x"]}
    f.aml[b] = {"id": b, "move_id": [other, "x"]}
    f.partials.append({"debit_move_id": [a, "x"], "credit_move_id": [b, "x"]})


def test_month_bounds_use_ict():
    assert ce.month_bounds_utc(2026, 9) == ("2026-08-31 17:00:00", "2026-09-30 17:00:00")
    assert ce.month_bounds_utc(2026, 12) == ("2026-11-30 17:00:00", "2026-12-31 17:00:00")


def test_build_report_sheets_and_money():
    r = ce.build_report(2026, 9, client=_fake())
    assert [x["name"] for x in r["current"]] == ["SO-SEP"]
    assert [x["name"] for x in r["debt"]] == ["SO-OLD"]
    assert r["unmatched_payment_ids"] == [8]

    cur = r["current"][0]
    assert cur["order_date"] == date(2026, 9, 1)
    assert cur["non_revenue_fee"] == 1000.0 - (400.0 + 100.0)
    assert [p["amount"] for p in cur["payments"]] == [500.0, 300.0]  # refund offset ignored
    assert cur["remaining"] == 1100.0 - (500.0 + 300.0)
    assert cur["salers"] == "Sale 1 / Sale 2"
    assert cur["tags"] == "Rèm"
    assert cur["address"] == "1 Lê Lợi, Q1, TP Hồ Chí Minh"
    assert cur["phone"] == "0901"
    assert cur["state"] == "Đang xử lý"

    old = r["debt"][0]
    assert old["remaining"] == 1100.0 - 800.0


def test_split_payment_across_invoices_counts_once():
    f = _fake()
    f.orders[1]["invoice_ids"] = [101, 201, 104]
    f.invoices[104] = _inv(104, [_pay(1, "2026-09-02", 100.0)])
    r = ce.build_report(2026, 9, client=f)
    assert [p["amount"] for p in r["current"][0]["payments"]] == [600.0, 300.0]


def test_cash_refund_to_customer_reduces_received():
    f = _fake()
    f.orders[1]["invoice_ids"] = [101, 201, 105]
    f.invoices[105] = _inv(105, [_pay(7, "2026-09-03", 100.0)], move_type="out_refund")
    r = ce.build_report(2026, 9, client=f)
    row = r["current"][0]
    assert len(row["payments"]) == 2 and row["refunded"] == 100.0
    assert row["remaining"] == 1100.0 - (800.0 - 100.0)
    header = [c.value for c in load_workbook(io.BytesIO(ce.render_xlsx(r)))[ce.SHEET_CURRENT][1]]
    assert "Số tiền hoàn lại khách" in header


def test_bank_statement_cash_without_payment_record_found_for_old_order():
    f = _fake()
    f.orders[4] = _order(4, "SO-BANK", "2026-07-01 03:00:00", [106])
    f.invoices[106] = _inv(106, [_pay(3, "2026-07-01", 1000.0),
                                 {"account_payment_id": False, "move_id": 5555, "date": "2026-09-03", "amount": 100.0}])
    _link(f, 106, 5555)
    r = ce.build_report(2026, 9, client=f)
    old = {x["name"]: x for x in r["debt"]}
    assert set(old) == {"SO-OLD", "SO-BANK"}
    assert old["SO-BANK"]["remaining"] == 0.0


def test_render_dynamic_columns():
    r = ce.build_report(2026, 9, client=_fake())
    wb = load_workbook(io.BytesIO(ce.render_xlsx(r)))
    assert wb.sheetnames == [ce.SHEET_CURRENT, ce.SHEET_DEBT, ce.SHEET_UNMATCHED]
    assert [c.value for c in wb[ce.SHEET_UNMATCHED][2]][:2] == ["PBNK/8", datetime(2026, 9, 9)]
    cur = [c.value for c in wb[ce.SHEET_CURRENT][1]]
    assert "Số tiền Cọc L2" in cur and "Số tiền Cọc L3" not in cur
    assert "TỔNG SAU KHI CHIA (HH) - SALER 2" in cur
    debt = [c.value for c in wb[ce.SHEET_DEBT][1]]
    assert "TỔNG SAU KHI CHIA (HH) - SALER 2" not in debt
    assert wb[ce.SHEET_CURRENT].cell(row=3, column=1).value == "TỔNG"
    assert cur[-3:] == ["Số tiền Nợ còn lại", "Trạng thái", "Saler"]
    assert not {"Số tiền đã CK", "Ngày TT nợ còn lại", "Chênh lệch"} & set(cur)


def test_render_uses_excel_formulas_for_derived_columns():
    r = ce.build_report(2026, 9, client=_fake())
    wb = load_workbook(io.BytesIO(ce.render_xlsx(r)))
    for title in (ce.SHEET_CURRENT, ce.SHEET_DEBT):
        ws = wb[title]
        head = [c.value for c in ws[1]]
        col = {h: get_column_letter(i) for i, h in enumerate(head, 1)}
        cell = lambda label: ws[f"{col[label]}2"].value
        assert cell("Total Discount") == f"={col['Số tiền trước chiết khấu']}2-{col['TỔNG TIỀN SAU CHIẾT KHẤU (CHƯA BAO GỒM VAT)']}2"
        assert cell("TỔNG SAU VAT") == f"={col['TỔNG TIỀN SAU CHIẾT KHẤU (CHƯA BAO GỒM VAT)']}2+{col['VAT']}2"
        assert cell("PHÍ KHÔNG TÍNH DOANH SỐ").startswith(f"={col['TỔNG TIỀN SAU CHIẾT KHẤU (CHƯA BAO GỒM VAT)']}2-(")
        paid = ",".join(f"{col[h]}2" for h in head if h.startswith("Số tiền Cọc L"))
        expected = f"={col['TỔNG SAU VAT']}2-SUM({paid})"
        if "Số tiền hoàn lại khách" in col:
            expected += f"+{col['Số tiền hoàn lại khách']}2"
        assert cell("Số tiền Nợ còn lại") == expected
        assert isinstance(cell("VAT"), (int, float))


def test_seconds_until_next_run_last_day_2300_ict():
    now = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
    assert ce._seconds_until_next_run(now) == 3600
    now = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)
    assert ce._seconds_until_next_run(now) == (datetime(2026, 10, 31, 16, tzinfo=timezone.utc) - now).total_seconds()
    now = datetime(2026, 2, 3, tzinfo=timezone.utc)
    assert ce._seconds_until_next_run(now) == (datetime(2026, 2, 28, 16, tzinfo=timezone.utc) - now).total_seconds()
    now = datetime(2026, 12, 31, 17, tzinfo=timezone.utc)
    assert ce._seconds_until_next_run(now) == (datetime(2027, 1, 31, 16, tzinfo=timezone.utc) - now).total_seconds()


def test_deliver_attaches_links_and_notifies():
    f = _fake()
    r = ce.build_report(2026, 9, client=f)
    res = ce.deliver(r, b"xlsx", 7, [10], client=f)
    assert res["attachment_id"] == 55 and res["message_id"] == 999
    att = f.created[0][1]
    assert att["name"] == "Du_lieu_tinh_hoa_hong_2026-09.xlsx" and att["res_id"] == 7
    desc = [w for w in f.writes if w[0] == "project.task"][0][2]["description"]
    assert desc.startswith("<p>Mô tả</p>") and "access_token=tok" in desc
    body = [w for w in f.writes if w[0] == "mail.message"][0][2]["body"]
    assert 'data-oe-id="10"' in body and "1 khoản thu" in body and ce.SHEET_UNMATCHED in body
    post = [e for e in f.executed if e[1] == "message_post"][0]
    assert post[3]["partner_ids"] == [10] and post[3]["subtype_xmlid"] == "mail.mt_note"


def test_deliver_is_idempotent():
    f = _fake()
    f.search_read = lambda model, domain, fields=None, limit=0, order=None: [{"id": 3}] if model == "ir.attachment" else []
    res = ce.deliver({"year": 2026, "month": 9, "current": [], "debt": []}, b"x", 7, [10], client=f)
    assert res == {"skipped": True, "attachment_id": 3}
    assert not f.created and not f.writes


def test_credit_carried_between_orders_counts_as_cash():
    f = _fake()
    # credit note 301 of SO-OLD offsets invoice 107 of SO-SEP
    f.orders[2]["invoice_ids"] = [102, 301]
    f.orders[1]["invoice_ids"] = [101, 201, 107]
    f.invoices[301] = _inv(301, [{"account_payment_id": False, "move_id": 107, "date": "2026-09-05", "amount": 70.0}],
                           move_type="out_refund")
    f.invoices[107] = _inv(107, [{"account_payment_id": False, "move_id": 301, "date": "2026-09-05", "amount": 70.0}])
    r = ce.build_report(2026, 9, client=f)
    cur, old = r["current"][0], r["debt"][0]
    assert [p["amount"] for p in cur["payments"]] == [500.0, 70.0, 300.0]
    assert old["refunded"] == 70.0
    assert cur["remaining"] == 1100.0 - 870.0
    assert old["remaining"] == 1100.0 - (800.0 - 70.0)


def test_invoice_only_lines_adjust_remaining_debt():
    f = _fake()
    # "Phí MPOS" added on invoice 102 only (not on the order)
    f.inv_lines = [{"move_id": [102, "INV/102"], "price_total": -300.0}]
    r = ce.build_report(2026, 9, client=f)
    old = [x for x in r["debt"] if x["name"] == "SO-OLD"][0]
    assert old["invoice_adjustment"] == -300.0
    assert old["remaining"] == 1100 - 300 - 800
    wb = load_workbook(io.BytesIO(ce.render_xlsx(r)))
    ws = wb[ce.SHEET_DEBT]
    head = [c.value for c in ws[1]]
    col = {h: get_column_letter(i) for i, h in enumerate(head, 1)}
    assert "Điều chỉnh trên hóa đơn (phí MPOS…)" in col
    assert "Điều chỉnh trên hóa đơn (phí MPOS…)" not in [c.value for c in wb[ce.SHEET_CURRENT][1]]
    assert ws[f"{col['Số tiền Nợ còn lại']}2"].value.startswith(
        f"={col['TỔNG SAU VAT']}2+{col['Điều chỉnh trên hóa đơn (phí MPOS…)']}2-SUM(")
