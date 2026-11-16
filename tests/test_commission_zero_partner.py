"""Customers listed in COMMISSION_REVENUE_ZERO_PARTNER_IDS always get commission 0."""
from unittest.mock import patch

import services.commission_revenue as cr


def _so(so_id, partner_id, amount=1000.0, ratio=0.5):
    return {
        "id": so_id, "name": f"SO{so_id}", "amount_untaxed": amount,
        "x_studio_ratio": ratio,
        "partner_id": [partner_id, "x"] if partner_id else False,
        "x_studio_commission_revenue": 500.0,
        "x_studio_commission_revenue_sp2": 500.0,
    }


def test_company_and_contact_get_zero_others_unchanged():
    sos = [_so(1, 51426), _so(2, 60001), _so(3, 70000), _so(4, False)]
    partners = [
        {"id": 51426, "commercial_partner_id": [51426, "DERUCCI"]},
        {"id": 60001, "commercial_partner_id": [51426, "DERUCCI"]},  # contact
        {"id": 70000, "commercial_partner_id": [70000, "Other"]},
    ]
    with patch.object(cr, "_ZERO_PARTNER_IDS", {51426}), \
            patch.object(cr.odoo, "read", return_value=partners):
        cr._mark_zero_commission(sos)
    res = [cr._calculate_commission(s, []) for s in sos]
    assert res[0] == (0.0, 0.0)
    assert res[1] == (0.0, 0.0)
    assert res[2] == (500.0, 500.0)
    assert res[3] == (500.0, 500.0)


def test_zero_even_without_ratio():
    so = _so(1, 51426, ratio=False)
    with patch.object(cr, "_ZERO_PARTNER_IDS", {51426}), \
            patch.object(cr.odoo, "read",
                         return_value=[{"id": 51426, "commercial_partner_id": [51426, "D"]}]):
        cr._mark_zero_commission([so])
    assert cr._calculate_commission(so, []) == (0.0, 0.0)


def test_empty_config_makes_no_rpc():
    with patch.object(cr, "_ZERO_PARTNER_IDS", set()), \
            patch.object(cr.odoo, "read") as rd:
        cr._mark_zero_commission([_so(1, 51426)])
    rd.assert_not_called()
