"""Create vendor-invoice follow activities for newly completed purchase receipts.

The watcher runs outside Odoo and writes only through the regular Odoo API.  On
first start it stores a UTC cutoff and deliberately does not backfill historical
receipts.  A receipt is eligible when it is an incoming purchase receipt, is
not a return, and its ``date_done`` is later than that cutoff.

Assignee policy:
1. Resolve Sales Orders from direct/Studio fields, receipt move relations, then
   exact Purchase Order Source references.
2. Map ``OP in charge`` to exactly one active ``BON SC`` user; linked-user
   metadata is accepted only after its active state and person name are checked.
3. If no Sales Order can be resolved, use the Purchase Order Buyer.

The activity is attached to the receipt itself and is idempotent by
``(stock.picking, summary)``.  The production watcher is disabled by default;
it must be explicitly enabled after test-server acceptance.
"""

from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import Config
from services.zns_odoo_client import ZNSOdooJSONRPCClient

logger = logging.getLogger(__name__)


class _OdooAdapter:
    """Small JSON-RPC adapter matching the protocol used by this watcher."""

    def __init__(self) -> None:
        self.raw = ZNSOdooJSONRPCClient()

    def search_read(self, model, domain, fields=None, limit=0, order=None):
        return self.raw.search_read(
            model, domain, fields=fields, limit=limit, order=order
        )

    def read(self, model, ids, fields=None):
        return self.raw.read(model, ids, fields=fields)

    def search(self, model, domain, limit=0):
        return self.raw.search(model, domain, limit=limit)

    def create(self, model, values, context=None):
        kwargs = {"context": context} if context else {}
        return self.raw.execute_kw(model, "create", [values], kwargs)


odoo = _OdooAdapter()

_POLL_INTERVAL = Config.VENDOR_INVOICE_FOLLOW_POLL_INTERVAL
_ACTIVITY_TYPE_ID = Config.VENDOR_INVOICE_FOLLOW_ACTIVITY_TYPE_ID
_COMPANY_IDS = Config.VENDOR_INVOICE_FOLLOW_COMPANY_IDS
_SUMMARY = Config.VENDOR_INVOICE_FOLLOW_SUMMARY
_DEADLINE_DAYS = Config.VENDOR_INVOICE_FOLLOW_DEADLINE_DAYS

SNAPSHOT_FILE = Config.VENDOR_INVOICE_FOLLOW_STATE_PATH

_watcher_running = False
_last_poll = None
_total_polls = 0
_total_candidates = 0
_total_activities_created = 0
_total_skipped_existing = 0
_total_missing_assignee = 0
_picking_model_id = None

_SO_REFERENCE_RE = re.compile(
    r"(?<![\w-])(?:O-)?BG-[A-Z0-9]+(?:-[A-Z0-9]+)*(?![\w-])",
    re.IGNORECASE,
)


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _empty_snapshot() -> dict:
    return {"seeded": False, "cutoff_date_done": None, "processed_ids": []}


def _load_snapshot() -> dict:
    if not os.path.exists(SNAPSHOT_FILE):
        return _empty_snapshot()
    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as handle:
            raw = json.load(handle)
        return {
            "seeded": bool(raw.get("seeded")),
            "cutoff_date_done": raw.get("cutoff_date_done") or None,
            "processed_ids": [int(value) for value in raw.get("processed_ids", [])],
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("[VENDOR-INVOICE] Invalid snapshot (%s); reseeding safely", exc)
        return _empty_snapshot()


def _save_snapshot(snapshot: dict) -> None:
    """Atomically save state so a container stop cannot leave partial JSON."""
    os.makedirs(os.path.dirname(SNAPSHOT_FILE) or ".", exist_ok=True)
    normalized = dict(snapshot)
    # Keep every post-cutoff processed ID. Truncating this set can make old IDs
    # eligible again and permanently occupy the oldest-page query window.
    normalized["processed_ids"] = sorted(
        {int(value) for value in normalized.get("processed_ids", [])}
    )
    temporary = f"{SNAPSHOT_FILE}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(normalized, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, SNAPSHOT_FILE)


def _seed_snapshot(snapshot: dict, now_utc: str | None = None) -> dict:
    """Set the go-live cutoff without creating any historical activities."""
    snapshot.update(
        {
            "seeded": True,
            "cutoff_date_done": now_utc or _utc_now_str(),
            "processed_ids": [],
        }
    )
    logger.info(
        "[VENDOR-INVOICE] Seeded at %s UTC; historical receipts are excluded",
        snapshot["cutoff_date_done"],
    )
    return snapshot


def _ensure_model_id(client=odoo) -> int:
    global _picking_model_id
    if client is odoo and _picking_model_id is not None:
        return _picking_model_id
    rows = client.search_read(
        "ir.model",
        [["model", "=", "stock.picking"]],
        fields=["id"],
        limit=1,
    )
    if not rows:
        raise RuntimeError("ir.model stock.picking not found")
    model_id = int(rows[0]["id"])
    if client is odoo:
        _picking_model_id = model_id
    return model_id


def _fetch_candidates(snapshot: dict, client=odoo, limit: int = 200) -> list:
    cutoff = snapshot.get("cutoff_date_done")
    if not cutoff:
        return []
    domain = [
        ["state", "=", "done"],
        ["picking_type_code", "=", "incoming"],
        ["purchase_id", "!=", False],
        ["return_id", "=", False],
        ["date_done", ">", cutoff],
    ]
    if _COMPANY_IDS:
        domain.append(["company_id", "in", _COMPANY_IDS])
    processed = [int(value) for value in snapshot.get("processed_ids", [])]
    if processed:
        domain.append(["id", "not in", processed])
    return client.search_read(
        "stock.picking",
        domain,
        fields=[
            "id",
            "name",
            "origin",
            "partner_id",
            "company_id",
            "purchase_id",
            "sale_id",
            "x_studio_source_so",
            "move_ids",
            "date_done",
        ],
        limit=limit,
        # New receipts must not be blocked by an older unresolved retry backlog.
        order="date_done desc, id desc",
    )


def _many2one_id(value) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


def _read_sale_orders(sale_ids: list[int], client=odoo) -> list[dict]:
    unique_ids = sorted({int(sale_id) for sale_id in sale_ids if sale_id})
    if not unique_ids:
        return []
    return client.read(
        "sale.order",
        unique_ids,
        fields=["id", "name", "x_studio_op_in_charge", "x_studio_op_user_id"],
    )


def _read_sale_order(sale_id: int, client=odoo) -> dict | None:
    rows = _read_sale_orders([sale_id], client)
    return rows[0] if rows else None


def _normalized_person_name(value: str) -> str:
    """Normalize ``TEAM, PERSON`` user labels to a comparable person name."""
    person = str(value or "").rsplit(",", 1)[-1]
    return " ".join(person.casefold().split())


def _person_match_key(value: str) -> str:
    """Tolerate Odoo labels that omit the common Vietnamese middle name Thị."""
    return " ".join(
        token for token in _normalized_person_name(value).split() if token != "thị"
    )


def _resolve_op_user(sale: dict, client=odoo) -> tuple[int, str] | None:
    """Resolve the user whose person name matches the Sales Order OP.

    Some Odoo OP records have a stale/mistaken linked-user value.  Therefore a
    computed linked user is accepted only when its person name matches the OP
    label. Otherwise active users are searched and exactly one matching BON SC
    user is required; ambiguity fails closed.
    """
    op = sale.get("x_studio_op_in_charge")
    op_name = op[1] if isinstance(op, (list, tuple)) and len(op) > 1 else ""
    expected_name = _normalized_person_name(op_name)
    computed = sale.get("x_studio_op_user_id")
    computed_id = _many2one_id(computed)
    computed_name = (
        computed[1]
        if isinstance(computed, (list, tuple)) and len(computed) > 1
        else ""
    )
    if computed_id and expected_name:
        linked_users = client.search_read(
            "res.users",
            [["id", "=", computed_id], ["active", "=", True]],
            fields=["id", "name"],
            limit=1,
        )
        if linked_users:
            linked_name = str(linked_users[0].get("name") or computed_name)
            if (
                _person_match_key(linked_name) == _person_match_key(op_name)
                and linked_name.casefold().startswith("bon sc,")
            ):
                return computed_id, linked_name
    if not expected_name:
        return None

    users = client.search_read(
        "res.users",
        [["active", "=", True], ["name", "ilike", op_name]],
        fields=["id", "name"],
        limit=20,
        order="id asc",
    )
    matches = [
        user
        for user in users
        if _person_match_key(user.get("name")) == _person_match_key(op_name)
        and str(user.get("name") or "").casefold().startswith("bon sc,")
    ]
    if len(matches) == 1:
        return int(matches[0]["id"]), str(matches[0]["name"])
    logger.warning(
        "[VENDOR-INVOICE] Cannot map OP %r to one active BON SC user; matches=%s",
        op_name,
        [(row.get("id"), row.get("name")) for row in matches],
    )
    return None


def _find_sales_from_purchase_origin(purchase: dict, client=odoo) -> list[dict]:
    origin = purchase.get("origin") or ""
    references = list(dict.fromkeys(_SO_REFERENCE_RE.findall(origin)))
    if not references:
        return []
    return client.search_read(
        "sale.order",
        [["name", "in", references]],
        fields=["id", "name", "x_studio_op_in_charge", "x_studio_op_user_id"],
        order="id asc",
    )


def _find_sales_from_receipt_moves(picking: dict, client=odoo) -> list[dict]:
    """Resolve SOs through receipt move → purchase line/sale line relations."""
    move_ids = [int(value) for value in picking.get("move_ids", [])]
    if not move_ids:
        return []
    moves = client.read(
        "stock.move",
        move_ids,
        fields=["purchase_line_id", "sale_line_id"],
    )
    purchase_line_ids = sorted(
        {
            _many2one_id(move.get("purchase_line_id"))
            for move in moves
            if _many2one_id(move.get("purchase_line_id"))
        }
    )
    sale_line_ids = {
        _many2one_id(move.get("sale_line_id"))
        for move in moves
        if _many2one_id(move.get("sale_line_id"))
    }
    sale_ids: set[int] = set()
    if purchase_line_ids:
        lines = client.read(
            "purchase.order.line",
            purchase_line_ids,
            fields=["sale_order_id", "sale_line_id"],
        )
        for line in lines:
            sale_id = _many2one_id(line.get("sale_order_id"))
            if sale_id:
                sale_ids.add(sale_id)
            sale_line_id = _many2one_id(line.get("sale_line_id"))
            if sale_line_id:
                sale_line_ids.add(sale_line_id)
    if sale_line_ids:
        sale_lines = client.read(
            "sale.order.line",
            sorted(sale_line_ids),
            fields=["order_id"],
        )
        sale_ids.update(
            _many2one_id(line.get("order_id"))
            for line in sale_lines
            if _many2one_id(line.get("order_id"))
        )
    return _read_sale_orders(sorted(sale_ids), client)


def _resolve_sales_op(sales: list[dict], client=odoo) -> tuple[int, str, str] | None:
    """Resolve one common OP user across one or more source Sales Orders."""
    unique_sales = {int(sale["id"]): sale for sale in sales}.values()
    resolved = [(sale, _resolve_op_user(sale, client)) for sale in unique_sales]
    if not resolved or any(user is None for _, user in resolved):
        return None
    user_ids = {user[0] for _, user in resolved if user}
    if len(user_ids) != 1:
        logger.warning(
            "[VENDOR-INVOICE] Receipt maps to Sales Orders with different OPs: %s",
            [(sale.get("name"), user) for sale, user in resolved],
        )
        return None
    user = resolved[0][1]
    sale_names = ", ".join(sorted(str(sale.get("name") or "") for sale, _ in resolved))
    return user[0], user[1], sale_names


def _resolve_assignee(picking: dict, client=odoo) -> dict | None:
    """Resolve OP through strongest receipt→SO path, then PO Buyer fallback."""
    purchase = None
    purchase_id = _many2one_id(picking.get("purchase_id"))
    if purchase_id:
        rows = client.read(
            "purchase.order",
            [purchase_id],
            fields=["id", "name", "origin", "user_id"],
        )
        purchase = rows[0] if rows else None

    sales: list[dict] = []
    resolution_path = ""
    direct_sale_id = _many2one_id(picking.get("sale_id"))
    source_sale_id = _many2one_id(picking.get("x_studio_source_so"))
    if direct_sale_id:
        sales = _read_sale_orders([direct_sale_id], client)
        resolution_path = "picking.sale_id"
    elif source_sale_id:
        sales = _read_sale_orders([source_sale_id], client)
        resolution_path = "picking.x_studio_source_so"
    else:
        sales = _find_sales_from_receipt_moves(picking, client)
        if sales:
            resolution_path = "receipt_move_purchase_line"
        elif purchase:
            sales = _find_sales_from_purchase_origin(purchase, client)
            if sales:
                resolution_path = "purchase_order.origin"

    if sales:
        op_user = _resolve_sales_op(sales, client)
        if not op_user:
            # Known SO provenance must never silently fall back to another buyer.
            return None
        return {
            "user_id": op_user[0],
            "user_name": op_user[1],
            "source": "sale_order_op",
            "resolution_path": resolution_path,
            "sale_order": op_user[2],
            "purchase_order": (purchase or {}).get("name") or "",
        }

    buyer = purchase and purchase.get("user_id")
    buyer_id = _many2one_id(buyer)
    if buyer_id:
        return {
            "user_id": buyer_id,
            "user_name": buyer[1],
            "source": "purchase_order_buyer",
            "resolution_path": "purchase_order.user_id",
            "sale_order": "",
            "purchase_order": purchase.get("name") or "",
        }
    return None


def _has_existing_activity(picking_id: int, client=odoo, summary: str | None = None) -> bool:
    ids = client.search(
        "mail.activity",
        [
            ["res_model", "=", "stock.picking"],
            ["res_id", "=", picking_id],
            ["summary", "=", summary or _SUMMARY],
        ],
        limit=1,
    )
    return bool(ids)


def _create_activity(
    picking: dict,
    assignee: dict,
    client=odoo,
    *,
    summary: str | None = None,
    deadline_days: int | None = None,
) -> int | None:
    """Create one idempotent activity on the receipt."""
    activity_summary = summary or _SUMMARY
    if _has_existing_activity(picking["id"], client, activity_summary):
        return None

    partner = picking.get("partner_id")
    vendor_name = partner[1] if isinstance(partner, (list, tuple)) and len(partner) > 1 else "—"
    receipt_name = picking.get("name") or f"#{picking['id']}"
    purchase_name = assignee.get("purchase_order") or picking.get("origin") or "—"
    sale_name = assignee.get("sale_order") or "—"
    note = (
        "<p><strong>Follow NCC xuất hóa đơn</strong></p><ul>"
        f"<li>Phiếu nhận: <strong>{html.escape(str(receipt_name))}</strong></li>"
        f"<li>Đơn mua: {html.escape(str(purchase_name))}</li>"
        f"<li>Đơn bán: {html.escape(str(sale_name))}</li>"
        f"<li>Nhà cung cấp: {html.escape(str(vendor_name))}</li>"
        "</ul>"
    )
    days = _DEADLINE_DAYS if deadline_days is None else deadline_days
    deadline = (
        datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date() + timedelta(days=days)
    ).isoformat()
    company_id = _many2one_id(picking.get("company_id"))
    values = {
        "res_model": "stock.picking",
        "res_model_id": _ensure_model_id(client),
        "res_id": int(picking["id"]),
        "activity_type_id": _ACTIVITY_TYPE_ID,
        "summary": activity_summary,
        "note": note,
        "date_deadline": deadline,
        "user_id": int(assignee["user_id"]),
    }
    context = {"allowed_company_ids": [company_id]} if company_id else None
    return client.create("mail.activity", values, context=context)


def _poll_once(snapshot: dict, client=odoo) -> dict:
    global _last_poll, _total_candidates, _total_activities_created
    global _total_skipped_existing, _total_missing_assignee

    _last_poll = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d %H:%M:%S ICT")
    if not snapshot.get("seeded") or not snapshot.get("cutoff_date_done"):
        return _seed_snapshot(snapshot)

    processed = {int(value) for value in snapshot.get("processed_ids", [])}
    candidates = _fetch_candidates(snapshot, client)
    _total_candidates += len(candidates)
    for picking in candidates:
        picking_id = int(picking["id"])
        if _has_existing_activity(picking_id, client):
            processed.add(picking_id)
            _total_skipped_existing += 1
            continue
        assignee = _resolve_assignee(picking, client)
        if not assignee:
            _total_missing_assignee += 1
            logger.warning(
                "[VENDOR-INVOICE] %s has no SO OP or PO Buyer; will retry",
                picking.get("name") or picking_id,
            )
            continue
        activity_id = _create_activity(picking, assignee, client)
        if activity_id:
            _total_activities_created += 1
            logger.info(
                "[VENDOR-INVOICE] Activity %s on %s assigned to %s (%s)",
                activity_id,
                picking.get("name") or picking_id,
                assignee["user_name"],
                assignee["source"],
            )
        processed.add(picking_id)
    snapshot["processed_ids"] = sorted(processed)
    return snapshot


def _watcher_loop() -> None:
    global _watcher_running, _total_polls
    _watcher_running = True
    snapshot = _load_snapshot()
    time.sleep(18)
    while _watcher_running:
        _total_polls += 1
        try:
            snapshot = _poll_once(snapshot)
            _save_snapshot(snapshot)
            delay = _POLL_INTERVAL
        except Exception as exc:  # watcher must survive transient Odoo errors
            logger.exception("[VENDOR-INVOICE] Poll failed: %s", exc)
            delay = max(30, _POLL_INTERVAL)
        delay += random.uniform(0, min(5, delay * 0.1))
        deadline = time.monotonic() + delay
        while _watcher_running and time.monotonic() < deadline:
            time.sleep(min(2, max(0, deadline - time.monotonic())))


def start_vendor_invoice_follow_watcher() -> None:
    if not Config.VENDOR_INVOICE_FOLLOW_ENABLED:
        logger.info("[VENDOR-INVOICE] Watcher disabled")
        return
    if not Config.ODOO_UID and not Config.ODOO_USER:
        logger.warning("[VENDOR-INVOICE] Odoo credentials missing; watcher disabled")
        return
    threading.Thread(
        target=_watcher_loop,
        daemon=True,
        name="vendor-invoice-follow-watcher",
    ).start()


def get_vendor_invoice_follow_status() -> dict:
    snapshot = _load_snapshot()
    return {
        "active": _watcher_running,
        "enabled": Config.VENDOR_INVOICE_FOLLOW_ENABLED,
        "policy": "new completed purchase receipts only; SO OP user, then PO Buyer fallback",
        "summary": _SUMMARY,
        "company_ids": _COMPANY_IDS,
        "poll_interval_seconds": _POLL_INTERVAL,
        "seeded": snapshot.get("seeded"),
        "cutoff_date_done": snapshot.get("cutoff_date_done"),
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_candidates": _total_candidates,
        "total_activities_created": _total_activities_created,
        "total_skipped_existing": _total_skipped_existing,
        "total_missing_assignee": _total_missing_assignee,
    }
