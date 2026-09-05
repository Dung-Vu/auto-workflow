from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from services import vendor_invoice_follow as service


class FakeOdoo:
    def __init__(self):
        self.search_read_results = {}
        self.read_results = {}
        self.search_results = []
        self.created = []
        self.calls = []

    def search_read(self, model, domain, fields=None, limit=0, order=None):
        self.calls.append(("search_read", model, deepcopy(domain), fields, limit, order))
        rows = deepcopy(self.search_read_results.get(model, []))
        for field, operator, value in domain:
            if field == "id" and operator == "=":
                rows = [row for row in rows if row.get("id") == value]
            if field == "active" and operator == "=":
                rows = [row for row in rows if row.get("active", True) == value]
        return rows[:limit] if limit else rows

    def read(self, model, ids, fields=None):
        self.calls.append(("read", model, list(ids), fields))
        return deepcopy(self.read_results.get((model, tuple(ids)), []))

    def search(self, model, domain, limit=0):
        self.calls.append(("search", model, deepcopy(domain), limit))
        return list(self.search_results)

    def create(self, model, values, context=None):
        self.calls.append(("create", model, deepcopy(values), deepcopy(context)))
        self.created.append((model, deepcopy(values), deepcopy(context)))
        return 99001


def picking(**overrides):
    result = {
        "id": 15689,
        "name": "MID/NHAN/06929",
        "origin": "MH08545",
        "partner_id": [23954, "Xưởng may ACE"],
        "company_id": [1, "Bonario Vietnam"],
        "purchase_id": [8570, "MH08545"],
        "sale_id": [3474, "BG-202512-3272"],
        "date_done": "2026-09-03 03:53:06",
    }
    result.update(overrides)
    return result


def configure_direct_sale(fake: FakeOdoo):
    fake.read_results[("sale.order", (3474,))] = [
        {
            "id": 3474,
            "name": "BG-202512-3272",
            "x_studio_op_in_charge": [6, "NGUYỄN THỊ TRANG"],
            "x_studio_op_user_id": [122, "BON SC, NGUYỄN THỊ TRANG"],
        }
    ]
    fake.read_results[("purchase.order", (8570,))] = [
        {
            "id": 8570,
            "name": "MH08545",
            "origin": "BG-202512-3272",
            "user_id": [122, "BON SC, NGUYỄN THỊ TRANG"],
        }
    ]
    fake.search_read_results["res.users"] = [
        {"id": 122, "name": "BON SC, NGUYỄN THỊ TRANG", "active": True}
    ]


def test_first_poll_seeds_without_querying_or_creating(monkeypatch):
    fake = FakeOdoo()
    monkeypatch.setattr(service, "_utc_now_str", lambda: "2026-09-05 06:00:00")

    result = service._poll_once(service._empty_snapshot(), fake)

    assert result == {
        "seeded": True,
        "cutoff_date_done": "2026-09-05 06:00:00",
        "processed_ids": [],
    }
    assert fake.calls == []


def test_snapshot_keeps_more_than_5000_processed_ids(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "vendor-invoice.json"
    monkeypatch.setattr(service, "SNAPSHOT_FILE", str(snapshot_path))
    snapshot = {
        "seeded": True,
        "cutoff_date_done": "2026-09-05 06:00:00",
        "processed_ids": list(range(1, 6002)),
    }

    service._save_snapshot(snapshot)

    assert service._load_snapshot()["processed_ids"] == list(range(1, 6002))


def test_candidate_domain_is_strictly_post_cutoff_purchase_receipts():
    fake = FakeOdoo()
    snapshot = {
        "seeded": True,
        "cutoff_date_done": "2026-09-05 06:00:00",
        "processed_ids": [10, 11],
    }

    service._fetch_candidates(snapshot, fake)

    call = fake.calls[0]
    assert call[0:2] == ("search_read", "stock.picking")
    domain = call[2]
    assert ["state", "=", "done"] in domain
    assert ["picking_type_code", "=", "incoming"] in domain
    assert ["purchase_id", "!=", False] in domain
    assert ["return_id", "=", False] in domain
    assert ["date_done", ">", "2026-09-05 06:00:00"] in domain
    assert ["company_id", "in", [1, 11]] in domain
    assert ["id", "not in", [10, 11]] in domain


def test_resolves_direct_sales_order_op_before_po_buyer():
    fake = FakeOdoo()
    configure_direct_sale(fake)
    fake.read_results[("purchase.order", (8570,))][0]["user_id"] = [
        220,
        "BON SC, BÙI THỊ NHÃ UYÊN",
    ]

    result = service._resolve_assignee(picking(), fake)

    assert result == {
        "user_id": 122,
        "user_name": "BON SC, NGUYỄN THỊ TRANG",
        "source": "sale_order_op",
        "resolution_path": "picking.sale_id",
        "sale_order": "BG-202512-3272",
        "purchase_order": "MH08545",
    }


def test_resolves_sales_order_from_purchase_origin_when_picking_has_no_sale():
    fake = FakeOdoo()
    fake.read_results[("purchase.order", (8570,))] = [
        {
            "id": 8570,
            "name": "MH08545",
            "origin": "BG-202512-3272",
            "user_id": [220, "BON SC, BÙI THỊ NHÃ UYÊN"],
        }
    ]
    fake.search_read_results["sale.order"] = [
        {
            "id": 3474,
            "name": "BG-202512-3272",
            "x_studio_op_in_charge": [6, "NGUYỄN THỊ TRANG"],
            "x_studio_op_user_id": [122, "BON SC, NGUYỄN THỊ TRANG"],
        }
    ]
    fake.search_read_results["res.users"] = [
        {"id": 122, "name": "BON SC, NGUYỄN THỊ TRANG", "active": True}
    ]

    result = service._resolve_assignee(picking(sale_id=False), fake)

    assert result["user_id"] == 122
    assert result["source"] == "sale_order_op"


def test_computed_user_allows_op_label_that_only_omits_middle_name_thi():
    fake = FakeOdoo()
    fake.read_results[("sale.order", (4675,))] = [
        {
            "id": 4675,
            "name": "BG-2606-0639",
            "x_studio_op_in_charge": [27, "NGUYỄN MAI NGÂN"],
            "x_studio_op_user_id": [298, "BON SC, NGUYỄN THỊ MAI NGÂN"],
        }
    ]
    fake.read_results[("purchase.order", (9534,))] = [
        {"id": 9534, "name": "MH09341", "origin": "", "user_id": [220, "Buyer"]}
    ]
    fake.search_read_results["res.users"] = [
        {"id": 298, "name": "BON SC, NGUYỄN THỊ MAI NGÂN", "active": True}
    ]

    result = service._resolve_assignee(
        picking(
            purchase_id=[9534, "MH09341"],
            sale_id=[4675, "BG-2606-0639"],
        ),
        fake,
    )

    assert result["user_id"] == 298
    assert result["user_name"] == "BON SC, NGUYỄN THỊ MAI NGÂN"


def test_inactive_computed_op_user_is_rejected():
    fake = FakeOdoo()
    fake.read_results[("sale.order", (4675,))] = [
        {
            "id": 4675,
            "name": "BG-2606-0639",
            "x_studio_op_in_charge": [27, "NGUYỄN MAI NGÂN"],
            "x_studio_op_user_id": [298, "BON SC, NGUYỄN THỊ MAI NGÂN"],
        }
    ]
    fake.read_results[("purchase.order", (9534,))] = [
        {"id": 9534, "name": "MH09341", "origin": "", "user_id": [220, "Buyer"]}
    ]
    fake.search_read_results["res.users"] = [
        {"id": 298, "name": "BON SC, NGUYỄN THỊ MAI NGÂN", "active": False}
    ]

    assert service._resolve_assignee(
        picking(purchase_id=[9534, "MH09341"], sale_id=[4675, "BG-2606-0639"]),
        fake,
    ) is None


def test_stale_computed_op_user_is_rejected_and_exact_bon_sc_user_is_used():
    fake = FakeOdoo()
    fake.read_results[("sale.order", (4675,))] = [
        {
            "id": 4675,
            "name": "BG-2606-0639",
            "x_studio_op_in_charge": [25, "TRẦN THỊ MAI THU"],
            "x_studio_op_user_id": [298, "BON SC, NGUYỄN THỊ MAI NGÂN"],
        }
    ]
    fake.read_results[("purchase.order", (9534,))] = [
        {
            "id": 9534,
            "name": "MH09341",
            "origin": "BG-2606-0639",
            "user_id": [220, "BON SC, BÙI THỊ NHÃ UYÊN"],
        }
    ]
    fake.search_read_results["res.users"] = [
        {"id": 276, "name": "BON SC, TRẦN THỊ MAI THU"}
    ]

    result = service._resolve_assignee(
        picking(
            purchase_id=[9534, "MH09341"],
            sale_id=[4675, "BG-2606-0639"],
        ),
        fake,
    )

    assert result["user_id"] == 276
    assert result["user_name"] == "BON SC, TRẦN THỊ MAI THU"
    assert result["source"] == "sale_order_op"


def test_known_sale_with_unmappable_op_fails_closed_instead_of_using_buyer():
    fake = FakeOdoo()
    fake.read_results[("sale.order", (3474,))] = [
        {
            "id": 3474,
            "name": "BG-202512-3272",
            "x_studio_op_in_charge": [99, "UNKNOWN OP"],
            "x_studio_op_user_id": False,
        }
    ]
    fake.read_results[("purchase.order", (8570,))] = [
        {
            "id": 8570,
            "name": "MH08545",
            "origin": "BG-202512-3272",
            "user_id": [220, "BON SC, BÙI THỊ NHÃ UYÊN"],
        }
    ]

    assert service._resolve_assignee(picking(), fake) is None


def test_falls_back_to_purchase_buyer_when_no_sales_order_can_be_resolved():
    fake = FakeOdoo()
    fake.read_results[("purchase.order", (8570,))] = [
        {
            "id": 8570,
            "name": "MH08545",
            "origin": "Mua bổ sung kho",
            "user_id": [220, "BON SC, BÙI THỊ NHÃ UYÊN"],
        }
    ]

    result = service._resolve_assignee(picking(sale_id=False), fake)

    assert result["user_id"] == 220
    assert result["source"] == "purchase_order_buyer"
    assert result["sale_order"] == ""


def test_multiple_origin_sales_with_different_ops_fails_closed():
    fake = FakeOdoo()
    fake.read_results[("purchase.order", (8570,))] = [
        {
            "id": 8570,
            "name": "MH08545",
            "origin": "BG-2605-0001, BG-2605-0002",
            "user_id": [220, "BON SC, BÙI THỊ NHÃ UYÊN"],
        }
    ]
    fake.search_read_results["sale.order"] = [
        {"id": 1, "name": "BG-2605-0001", "x_studio_op_user_id": [122, "Trang"]},
        {"id": 2, "name": "BG-2605-0002", "x_studio_op_user_id": [298, "Ngân"]},
    ]

    result = service._resolve_assignee(picking(sale_id=False), fake)

    assert result is None


def test_resolves_source_so_before_purchase_origin():
    fake = FakeOdoo()
    configure_direct_sale(fake)

    result = service._resolve_assignee(
        picking(sale_id=False, x_studio_source_so=[3474, "BG-202512-3272"]),
        fake,
    )

    assert result["user_id"] == 122
    assert result["resolution_path"] == "picking.x_studio_source_so"


def test_resolves_so_through_receipt_move_and_purchase_line():
    fake = FakeOdoo()
    fake.read_results[("purchase.order", (8570,))] = [
        {"id": 8570, "name": "MH08545", "origin": "", "user_id": [220, "Buyer"]}
    ]
    fake.read_results[("stock.move", (54351,))] = [
        {"id": 54351, "purchase_line_id": [17824, "Line"], "sale_line_id": False}
    ]
    fake.read_results[("purchase.order.line", (17824,))] = [
        {
            "id": 17824,
            "sale_order_id": [3474, "BG-202512-3272"],
            "sale_line_id": False,
        }
    ]
    configure_direct_sale(fake)

    result = service._resolve_assignee(
        picking(sale_id=False, x_studio_source_so=False, move_ids=[54351]),
        fake,
    )

    assert result["user_id"] == 122
    assert result["resolution_path"] == "receipt_move_purchase_line"


def test_create_activity_assigns_op_and_attaches_only_to_receipt(monkeypatch):
    fake = FakeOdoo()
    fake.search_read_results["ir.model"] = [{"id": 134}]
    assignee = {
        "user_id": 122,
        "user_name": "BON SC, NGUYỄN THỊ TRANG",
        "source": "sale_order_op",
        "sale_order": "BG-202512-3272",
        "purchase_order": "MH08545",
    }
    monkeypatch.setattr(service, "_has_existing_activity", lambda *args, **kwargs: False)

    activity_id = service._create_activity(picking(), assignee, fake)

    assert activity_id == 99001
    model, values, context = fake.created[0]
    assert model == "mail.activity"
    assert values["res_model"] == "stock.picking"
    assert values["res_id"] == 15689
    assert values["user_id"] == 122
    assert values["summary"] == "Follow NCC xuất hóa đơn"
    assert values["activity_type_id"] == 4
    assert datetime.strptime(values["date_deadline"], "%Y-%m-%d").date() >= date_today()
    assert "MID/NHAN/06929" in values["note"]
    assert "BG-202512-3272" in values["note"]
    assert context == {"allowed_company_ids": [1]}


def date_today():
    return datetime.now().date()


def test_existing_summary_on_receipt_is_idempotent(monkeypatch):
    fake = FakeOdoo()
    monkeypatch.setattr(service, "_has_existing_activity", lambda *args, **kwargs: True)

    result = service._create_activity(
        picking(),
        {"user_id": 122, "user_name": "Trang", "source": "sale_order_op"},
        fake,
    )

    assert result is None
    assert fake.created == []


def test_poll_marks_success_and_existing_but_retries_missing_assignee(monkeypatch):
    fake = FakeOdoo()
    candidates = [picking(id=1), picking(id=2), picking(id=3)]
    monkeypatch.setattr(service, "_fetch_candidates", lambda snapshot, client: candidates)
    monkeypatch.setattr(
        service,
        "_has_existing_activity",
        lambda picking_id, client, summary=None: picking_id == 1,
    )
    monkeypatch.setattr(
        service,
        "_resolve_assignee",
        lambda item, client: (
            None
            if item["id"] == 2
            else {"user_id": 122, "user_name": "Trang", "source": "sale_order_op"}
        ),
    )
    monkeypatch.setattr(service, "_create_activity", lambda *args, **kwargs: 90003)
    snapshot = {"seeded": True, "cutoff_date_done": "2026-09-05 06:00:00", "processed_ids": []}

    result = service._poll_once(snapshot, fake)

    assert result["processed_ids"] == [1, 3]


def test_html_is_escaped_in_activity_note(monkeypatch):
    fake = FakeOdoo()
    fake.search_read_results["ir.model"] = [{"id": 134}]
    monkeypatch.setattr(service, "_has_existing_activity", lambda *args, **kwargs: False)
    malicious = picking(name="<script>alert(1)</script>", partner_id=[1, "A&B"])

    service._create_activity(
        malicious,
        {
            "user_id": 122,
            "user_name": "Trang",
            "source": "sale_order_op",
            "sale_order": "<SO>",
            "purchase_order": "PO&1",
        },
        fake,
    )

    note = fake.created[0][1]["note"]
    assert "<script>" not in note
    assert "&lt;script&gt;" in note
    assert "A&amp;B" in note
    assert "&lt;SO&gt;" in note
