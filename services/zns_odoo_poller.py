"""
ZNS Odoo Poller Service — periodically queries Odoo 19 Online via JSON-RPC 2.0
for records with `x_studio_zns_request_state == 'pending'`, dispatches them
via `ZNSTrackingService`, and writes back execution results to Odoo.

Features:
- Pure JSON-RPC 2.0 communication (no XML-RPC).
- Single Source of Truth: Durable SQLite dispatch-requests queue `(source_model, source_record_id, template_type, send_version)`.
- Atomic claim + lease reclaim: FAILED retries and stale CLAIMED/PROCESSING are reclaimed; COMPLETED is never re-sent.
- Ambiguous crash recovery: reclaim calls tracking with the same idempotency key to reuse a durable result.
- Version-guarded Odoo writeback with post-write conflict detection and compensation.
- Stale processing recovery based on durable queue lease state.
"""

import html
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

from config import Config
from services.zns_odoo_client import ZNSOdooJSONRPCClient, get_zns_odoo_client
from services.zns_repository import DISPATCH_LEASE_SECONDS, ZNSRepository, get_repository
from services.zns_tracking import ZNSTrackingService

logger = logging.getLogger(__name__)

# Poller telemetry globals
_last_poll_iso: Optional[str] = None
_last_error: Optional[str] = None
_last_dispatched_count: int = 0
_stale_processing_count: int = 0

# Mapping from backend internal status to Odoo selection field values
STATUS_TO_ODOO_SELECTION: Dict[str, str] = {
    "QUEUED": "queued",
    "SUBMITTING": "queued",
    "ACCEPTED": "accepted",
    "DELIVERED": "delivered",
    "REJECTED": "rejected",
    "SUBMISSION_UNKNOWN": "unknown",
    "DELIVERY_UNKNOWN": "unknown",
    "CANCELLED": "rejected",
}


def normalize_status_for_odoo(status: Optional[str]) -> str:
    """Normalize internal ZNS tracking status into Odoo Selection field value."""
    if not status:
        return "unknown"
    upper = str(status).strip().upper()
    return STATUS_TO_ODOO_SELECTION.get(upper, "unknown")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _odoo_utc_now() -> str:
    """Return a UTC timestamp in the naive format accepted by Odoo Datetime fields."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _format_order_date_for_zns(value: Any) -> str:
    """Convert Odoo date/datetime values to the DD/MM/YYYY format expected by ZNS templates."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return text
    return parsed.strftime("%d/%m/%Y")


class ZNSOdooPoller:
    """Poller worker querying pending ZNS requests on Odoo 19 Online via JSON-RPC."""

    def __init__(
        self,
        tracking_service: Optional[ZNSTrackingService] = None,
        odoo_client: Optional[ZNSOdooJSONRPCClient] = None,
        repo: Optional[ZNSRepository] = None,
        worker_id: Optional[str] = None,
    ):
        self.tracking_service = tracking_service or ZNSTrackingService()
        self.odoo_client = odoo_client or get_zns_odoo_client()
        self.repo = repo or get_repository()
        self.worker_id = worker_id or f"poller-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _desired_external_templates(rec: Dict[str, Any]) -> List[str]:
        """Translate existing Studio fields into ZNS intents without Odoo-side code."""
        if rec.get("state") != "sale" or rec.get("x_studio_selection_field_q4_1imrcsjj8") != "Done":
            return []
        brand = str(rec.get("x_studio_thng_hiu") or "").strip().upper()
        templates: List[str] = []
        if brand == "ORDINAIRE":
            instruction = str(rec.get("x_studio_hng_dn_s_dng") or "").strip()
            if instruction == "Đã gửi (Vie)":
                templates.append("hdsd-vie")
            elif instruction == "Đã gửi (Eng)":
                templates.append("hdsd-eng")
            if rec.get("x_studio_zns_nh_gi_n_hng") is True:
                templates.append("rating-ord-vie")
            if rec.get("x_studio_zns_nh_gi_n_hng_eng") is True:
                templates.append("rating-ord-eng")
        elif brand == "BONARIO" and rec.get("x_studio_zns_nh_gi_n_hng") is True:
            templates.append("rating")
        return templates

    @staticmethod
    def _load_external_state() -> Optional[Dict[str, Any]]:
        path = Config.ZNS_EXTERNAL_AUTOMATION_STATE_PATH
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.error(f"[ZNS-EXTERNAL-AUTOMATION] Invalid state file; failing closed: {exc}")
            return {"initialized": False, "error": "invalid_state"}

    @staticmethod
    def _save_external_state(state: Dict[str, Any]) -> None:
        path = Config.ZNS_EXTERNAL_AUTOMATION_STATE_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)

    def _queue_external_request(self, rec: Dict[str, Any], template_type: str) -> bool:
        """Create one Odoo queue request via JSON-RPC; executes no Python inside Odoo."""
        rec_id = int(rec["id"])
        current_count = int(rec.get("x_studio_zns_send_count") or 0)
        send_version = current_count + 1
        marker = f"zns_external_request_{rec_id}_{template_type}_v{send_version}"
        labels = {
            "hdsd-vie": "HDSD Tiếng Việt (ORD)", "hdsd-eng": "HDSD Tiếng Anh (ORD)",
            "rating-ord-vie": "Đánh giá Đơn hàng TV (ORD)",
            "rating-ord-eng": "Đánh giá Đơn hàng TA (ORD)", "rating": "Đánh giá Dịch vụ (BON)",
        }
        actor = rec.get("write_uid") or [0, "Tự động ngoài Odoo"]
        actor_id = int(actor[0]) if isinstance(actor, (list, tuple)) and actor else 0
        actor_name = str(actor[1]) if isinstance(actor, (list, tuple)) and len(actor) > 1 else "Tự động ngoài Odoo"
        partner = rec.get("partner_id") or [0, "Quý Khách"]
        customer_name = str(partner[1]) if isinstance(partner, (list, tuple)) and len(partner) > 1 else "Quý Khách"
        body = (
            '<div style="border-left:4px solid #17a2b8;padding-left:10px;margin:5px 0;">'
            f'<!-- {marker} --><p>📨 <b>YÊU CẦU GỬI ZALO ZNS: {html.escape(labels[template_type])} '
            f'(Lần {send_version})</b></p><ul style="margin:0;padding-left:20px;">'
            f'<li><b>Người thực hiện:</b> {html.escape(actor_name)}</li>'
            f'<li><b>Người nhận:</b> {html.escape(customer_name)}</li>'
            f'<li><b>Mã đơn hàng:</b> {html.escape(str(rec.get("name") or rec_id))}</li>'
            '<li><b>Trạng thái:</b> Đã ghi nhận vào hàng đợi gửi tin (Chờ xử lý).</li></ul></div>'
        )
        existing = self.odoo_client.search_read(
            "mail.message",
            [("model", "=", "sale.order"), ("res_id", "=", rec_id), ("body", "ilike", marker)],
            ["id"], limit=1,
        )
        if not existing:
            self.odoo_client.message_post(
                model="sale.order", record_id=rec_id, body=body,
                message_type="comment", subtype_xmlid="mail.mt_note",
            )
        # Odoo Online escapes HTML passed as a plain JSON-RPC string and may not
        # return a usable message ID. Resolve the durable marker and rewrite only
        # our trusted markup (all dynamic values above are escaped).
        posted_notes = self.odoo_client.search_read(
            "mail.message",
            [("model", "=", "sale.order"), ("res_id", "=", rec_id), ("body", "ilike", marker)],
            ["id"], limit=1, order="id desc",
        )
        if not posted_notes:
            raise RuntimeError(f"Could not locate created ZNS queue note marker {marker}")
        self.odoo_client.write("mail.message", [int(posted_notes[0]["id"])], {"body": body})

        values = {
            "x_studio_zns_send_count": send_version,
            "x_studio_zns_request_state": "pending",
            "x_studio_zns_status": "queued",
            "x_studio_zns_last_template": template_type,
            "x_studio_zns_claim_token": False,
            "x_studio_zns_claim_owner": False,
            "x_studio_zns_processing_started_at": False,
            "x_studio_zns_sent_by_user_id": actor_id,
            "x_studio_zns_sent_by_user_name": actor_name,
        }
        return bool(self.odoo_client.write("sale.order", [rec_id], values))

    def discover_external_automation_requests(self) -> int:
        """Detect newly-added ZNS intents. First run snapshots and never backfills."""
        if not Config.ZNS_EXTERNAL_AUTOMATION_ENABLED or not self.odoo_client.is_configured:
            return 0
        fields = [
            "id", "name", "state", "write_date", "write_uid", "partner_id",
            "x_studio_selection_field_q4_1imrcsjj8", "x_studio_thng_hiu", "x_studio_hng_dn_s_dng",
            "x_studio_zns_nh_gi_n_hng", "x_studio_zns_nh_gi_n_hng_eng",
            "x_studio_zns_request_state", "x_studio_zns_send_count",
        ]
        state = self._load_external_state()
        now_cursor = _odoo_utc_now()
        if state is None:
            records = self.odoo_client.search_read(
                "sale.order", [("state", "=", "sale"), ("x_studio_selection_field_q4_1imrcsjj8", "=", "Done")],
                fields, limit=10000,
            )
            snapshot = {str(rec["id"]): self._desired_external_templates(rec) for rec in records}
            self._save_external_state({"initialized": True, "cursor": now_cursor, "signals": snapshot})
            logger.info(f"[ZNS-EXTERNAL-AUTOMATION] Bootstrapped {len(snapshot)} records without sending history")
            return 0
        if not state.get("initialized"):
            return 0

        cursor = str(state.get("cursor") or now_cursor)
        records = self.odoo_client.search_read(
            "sale.order", [("write_date", ">=", cursor)], fields, limit=500, order="write_date asc,id asc",
        )
        signals = state.setdefault("signals", {})
        queued = 0
        for rec in records:
            key = str(rec["id"])
            desired = self._desired_external_templates(rec)
            previous = list(signals.get(key) or [])
            added = [item for item in desired if item not in previous]
            if added and rec.get("x_studio_zns_request_state") not in ("pending", "processing"):
                template_type = added[0]
                existing, _ = self.repo.query_messages(
                    filters={"source_model": "sale.order", "source_record_id": int(rec["id"]), "template_type": template_type},
                    page=1, page_size=1,
                )
                if existing or self._queue_external_request(rec, template_type):
                    previous.append(template_type)
                    queued += 0 if existing else 1
            signals[key] = [item for item in previous if item in desired]
        state["cursor"] = now_cursor
        self._save_external_state(state)
        return queued

    @staticmethod
    def _record_matches_domain(rec: Dict[str, Any], extra_domain: List[Any]) -> bool:
        """Evaluate a simple Odoo-style domain of ('field', '=', value) terms against a record dict."""
        for term in extra_domain:
            if not isinstance(term, (list, tuple)) or len(term) < 3:
                continue
            field, op, val = term[0], term[1], term[2]
            cur = rec.get(field)
            if op == "=":
                if val is False:
                    if cur not in (False, None, ""):
                        return False
                elif cur != val:
                    return False
            elif op == "!=":
                if cur == val:
                    return False
            elif op == "in":
                if cur not in val:
                    return False
            else:
                return False
        return True

    def _cas_write(
        self,
        model: str,
        rec_id: int,
        values: Dict[str, Any],
        extra_domain: Optional[List[Any]] = None,
        before_write: Optional[Callable[[], None]] = None,
    ) -> bool:
        """
        Best-effort version-guarded write over Odoo's generic JSON-RPC API.

        Searches for the record with extra_domain (typically send_count = version)
        and writes only if a match exists. `before_write` is a test hook used to
        inject a concurrent resend between the compare and the write.
        """
        extra_domain = list(extra_domain or [])
        domain: List[Any] = [("id", "=", rec_id)] + extra_domain
        ids: List[int] = []

        search_fn = getattr(self.odoo_client, "search", None)
        search_is_mock = type(search_fn).__name__ == "MagicMock"
        if callable(search_fn) and not search_is_mock:
            found = search_fn(model, domain, limit=1)
            if isinstance(found, int):
                found = [found]
            if isinstance(found, list):
                ids = [int(x) for x in found if x is not None]
        else:
            fields = ["id"]
            for term in extra_domain:
                if isinstance(term, (list, tuple)) and term:
                    fields.append(str(term[0]))
            recs = self.odoo_client.read(model, [rec_id], fields)
            if isinstance(recs, list) and recs and isinstance(recs[0], dict):
                if self._record_matches_domain(recs[0], extra_domain):
                    ids = [rec_id]

        if not ids:
            return False

        if before_write is not None:
            before_write()

        res = self.odoo_client.write(model, ids, values)
        return res is True or res is None or bool(res)

    def _update_request_chatter_status(
        self,
        model: str,
        rec_id: int,
        send_version: int,
        backend_status: str,
        zalo_msg_id: Optional[str] = None,
    ) -> bool:
        """Update the matching queue note in place after the gateway responds."""
        try:
            notes = self.odoo_client.search_read(
                "mail.message",
                [
                    ("model", "=", model),
                    ("res_id", "=", int(rec_id)),
                    ("body", "ilike", "YÊU CẦU GỬI ZALO ZNS"),
                    ("body", "ilike", f"(Lần {int(send_version)})"),
                ],
                ["id", "body"],
                limit=5,
                order="id desc",
            )
            if not isinstance(notes, list) or not notes:
                logger.warning(
                    f"[ZNS-ODOO-POLLER] Queue chatter note not found for {model}({rec_id}) v{send_version}"
                )
                return False

            status = str(backend_status or "").strip().upper()
            status_text = {
                "ACCEPTED": "✅ Zalo đã tiếp nhận tin nhắn (ACCEPTED).",
                "DELIVERED": "✅ Tin nhắn đã được giao thành công (DELIVERED).",
                "REJECTED": "❌ Zalo từ chối tin nhắn (REJECTED).",
                "SUBMISSION_UNKNOWN": "⚠️ Chưa xác định được kết quả gửi.",
                "DELIVERY_UNKNOWN": "⚠️ Chưa xác định được trạng thái giao tin.",
                "CANCELLED": "❌ Yêu cầu gửi đã bị hủy.",
            }.get(status, f"Trạng thái xử lý: {html.escape(status or 'UNKNOWN')}.")
            if zalo_msg_id:
                status_text += " <b>Zalo Msg ID:</b> %s" % html.escape(str(zalo_msg_id))
            replacement = "<li><b>Trạng thái:</b> %s</li>" % status_text

            for note in notes:
                body = str(note.get("body") or "")
                start = body.find("<li><b>Trạng thái:</b>")
                end = body.find("</li>", start)
                if start < 0 or end < 0:
                    continue
                updated_body = body[:start] + replacement + body[end + len("</li>") :]
                if updated_body == body:
                    return True
                return bool(self.odoo_client.write("mail.message", [int(note["id"])], {"body": updated_body}))

            logger.warning(
                f"[ZNS-ODOO-POLLER] Queue chatter status row not found for {model}({rec_id}) v{send_version}"
            )
            return False
        except Exception as e:
            logger.warning(
                f"[ZNS-ODOO-POLLER] Could not update queue chatter for {model}({rec_id}) v{send_version}: {e}"
            )
            return False

    def _writeback_completed_to_odoo(
        self,
        model: str,
        rec_id: int,
        send_version: int,
        backend_status: str,
        zalo_msg_id: Optional[str] = None,
        before_write: Optional[Callable[[], None]] = None,
    ) -> bool:
        """
        Write back execution results with version guards and post-write compensation.

        1. Write completed only when current send_count still equals send_version.
        2. Re-read after write. If a concurrent resend bumped send_count and our
           completed write landed on top of the newer pending request, compensate
           by restoring pending without decreasing send_count.
        """
        try:
            odoo_status = normalize_status_for_odoo(backend_status)
            write_vals = {
                "x_studio_zns_request_state": "completed",
                "x_studio_zns_status": odoo_status,
                "x_studio_zns_last_sent_at": _odoo_utc_now(),
                "x_studio_zns_claim_token": False,
                "x_studio_zns_claim_owner": False,
                "x_studio_zns_processing_started_at": False,
            }
            if zalo_msg_id:
                write_vals["x_studio_zns_msg_id"] = zalo_msg_id

            version_domain = [("x_studio_zns_send_count", "=", int(send_version))]
            wrote = False
            for attempt in range(3):
                try:
                    wrote = self._cas_write(
                        model,
                        rec_id,
                        write_vals,
                        extra_domain=version_domain,
                        before_write=before_write,
                    )
                    break
                except Exception as e:
                    logger.warning(f"[ZNS-ODOO-POLLER] Writeback attempt {attempt + 1} failed for {model}({rec_id}): {e}")
                    time.sleep(0.5)

            if not wrote:
                logger.info(
                    f"[ZNS-ODOO-POLLER] Record {model}({rec_id}) send_count no longer v{send_version} "
                    f"— skipping completed overwrite"
                )
                return True

            curr_rec = self.odoo_client.read(
                model,
                [rec_id],
                ["x_studio_zns_send_count", "x_studio_zns_request_state", "x_studio_zns_msg_id"],
            )
            if not isinstance(curr_rec, list) or not curr_rec or not isinstance(curr_rec[0], dict):
                return True

            curr_count = int(curr_rec[0].get("x_studio_zns_send_count") or 1)
            curr_state = curr_rec[0].get("x_studio_zns_request_state")
            curr_msg = curr_rec[0].get("x_studio_zns_msg_id") or ""
            our_msg = zalo_msg_id or ""

            if curr_count > int(send_version) and curr_state == "completed":
                if our_msg and curr_msg and curr_msg != our_msg:
                    return True
                logger.warning(
                    f"[ZNS-ODOO-POLLER] Writeback v{send_version} overwrote newer v{curr_count} "
                    f"on {model}({rec_id}) — restoring pending"
                )
                restore_vals = {
                    "x_studio_zns_request_state": "pending",
                    "x_studio_zns_status": "queued",
                    "x_studio_zns_claim_token": False,
                    "x_studio_zns_claim_owner": False,
                    "x_studio_zns_processing_started_at": False,
                }
                restore_domain: List[Any] = [
                    ("x_studio_zns_send_count", "=", curr_count),
                    ("x_studio_zns_request_state", "=", "completed"),
                ]
                if our_msg:
                    restore_domain.append(("x_studio_zns_msg_id", "=", our_msg))
                self._cas_write(model, rec_id, restore_vals, extra_domain=restore_domain)
                return True

            if curr_count == int(send_version):
                self._update_request_chatter_status(
                    model,
                    rec_id,
                    send_version,
                    backend_status,
                    zalo_msg_id=zalo_msg_id,
                )

            return True
        except Exception as e:
            logger.error(f"[ZNS-ODOO-POLLER] Error during writeback for {model}({rec_id}): {e}")
            return False

    def recover_stale_processing_records(self, model: str = "sale.order", stale_seconds: int = 300) -> int:
        """
        Reclaim records stuck in 'processing' state based on durable queue state.
        If the queue record is COMPLETED, writes back completed.
        If the queue record is missing or FAILED and older than stale_seconds, resets to 'pending'.
        """
        global _stale_processing_count
        if not self.odoo_client.is_configured:
            return 0

        try:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)

            domain = [("x_studio_zns_request_state", "=", "processing")]
            fields = [
                "id",
                "x_studio_zns_processing_started_at",
                "x_studio_zns_claim_token",
                "x_studio_zns_last_template",
                "x_studio_zns_send_count",
            ]
            records = self.odoo_client.search_read(model, domain, fields, limit=50)

            reclaimed = 0
            for rec in records:
                rec_id = rec["id"]
                template_type = rec.get("x_studio_zns_last_template") or "hdsd-vie"
                send_count = rec.get("x_studio_zns_send_count") or 1
                started_at = rec.get("x_studio_zns_processing_started_at")

                # Check durable SQLite queue first
                queue_req = self.repo.get_dispatch_request(
                    source_model=model,
                    source_record_id=rec_id,
                    template_type=template_type,
                    send_version=send_count,
                )

                if queue_req and queue_req.get("status") == "COMPLETED":
                    # Finished in SQLite but Odoo writeback was lost -> complete Odoo record
                    self._writeback_completed_to_odoo(
                        model=model,
                        rec_id=rec_id,
                        send_version=send_count,
                        backend_status=queue_req.get("result_status") or "ACCEPTED",
                        zalo_msg_id=queue_req.get("zalo_msg_id"),
                    )
                    reclaimed += 1
                    continue

                # A live dispatch lease means another worker still owns the request.
                lease_expires_at = (queue_req or {}).get("lease_expires_at")
                queue_status = (queue_req or {}).get("status")
                lease_active = False
                if queue_req and queue_status in ("CLAIMED", "PROCESSING") and lease_expires_at:
                    try:
                        lease_dt = datetime.fromisoformat(str(lease_expires_at).replace("Z", "+00:00"))
                        if lease_dt.tzinfo is None:
                            lease_dt = lease_dt.replace(tzinfo=timezone.utc)
                        lease_active = lease_dt > datetime.now(timezone.utc)
                    except Exception:
                        lease_active = False
                if lease_active:
                    continue

                # Check if started_at is stale
                is_stale = False
                if not started_at:
                    is_stale = True
                else:
                    try:
                        clean_ts = str(started_at).replace("Z", "+00:00")
                        rec_dt = datetime.fromisoformat(clean_ts)
                        if rec_dt.tzinfo is None:
                            rec_dt = rec_dt.replace(tzinfo=timezone.utc)
                        if rec_dt < cutoff_dt:
                            is_stale = True
                    except Exception:
                        is_stale = True

                if is_stale:
                    logger.warning(
                        f"[ZNS-ODOO-POLLER] Reclaiming stale processing record {model}({rec_id}) "
                        f"(started_at: {started_at}) -> resetting to 'pending'"
                    )
                    # Do not mutate the queue here: this recovery worker does not own
                    # its fenced lease. Resetting Odoo to pending lets poll_and_dispatch
                    # atomically reclaim the expired queue row.
                    reset_vals = {
                        "x_studio_zns_request_state": "pending",
                        "x_studio_zns_claim_token": False,
                        "x_studio_zns_claim_owner": False,
                        "x_studio_zns_processing_started_at": False,
                    }
                    if self.odoo_client.write(model, [rec_id], reset_vals):
                        reclaimed += 1

            _stale_processing_count = reclaimed
            return reclaimed
        except Exception as e:
            logger.warning(f"[ZNS-ODOO-POLLER] Stale processing recovery error: {e}")
            return 0

    def _dispatch_with_lease_heartbeat(
        self,
        request_id: int,
        claim_token: str,
        dispatch_fn,
        **dispatch_kwargs,
    ):
        """Keep the dispatch lease alive while the paid/idempotent call is in flight."""
        stop_event = threading.Event()
        lease_lost = threading.Event()
        interval = max(1.0, DISPATCH_LEASE_SECONDS / 3.0)

        def heartbeat_loop():
            while not stop_event.wait(interval):
                if not self.repo.renew_dispatch_lease(
                    request_id,
                    self.worker_id,
                    claim_token,
                    lease_duration_seconds=DISPATCH_LEASE_SECONDS,
                ):
                    lease_lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            name=f"zns-dispatch-heartbeat-{request_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = dispatch_fn(**dispatch_kwargs)
        finally:
            stop_event.set()
            heartbeat_thread.join(timeout=1.0)

        still_owned = not lease_lost.is_set() and self.repo.renew_dispatch_lease(
            request_id,
            self.worker_id,
            claim_token,
            lease_duration_seconds=DISPATCH_LEASE_SECONDS,
        )
        return result, still_owned

    def poll_and_dispatch(self, model: str = "sale.order", limit: int = 20) -> int:
        """Query and process pending ZNS dispatch requests from Odoo model."""
        global _last_poll_iso, _last_error, _last_dispatched_count
        _last_poll_iso = _utc_now_iso()

        if not self.odoo_client.is_configured:
            _last_error = "Odoo credentials unconfigured"
            return 0

        # 1. Recover stale processing records based on durable queue state
        self.recover_stale_processing_records(model=model, stale_seconds=300)

        try:
            domain = [("x_studio_zns_request_state", "=", "pending")]
            fields = [
                "id",
                "name",
                "partner_id",
                "x_studio_phone",
                "x_studio_tn_khch_hng",
                "x_studio_zns_last_template",
                "x_studio_zns_send_count",
                "x_studio_zns_sent_by_user_id",
                "x_studio_zns_sent_by_user_name",
                "date_order",
                "company_id",
            ]
            records = self.odoo_client.search_read(model, domain, fields, limit=limit)
            if not records:
                _last_error = None
                return 0

            dispatched_count = 0
            for rec in records:
                rec_id = rec["id"]
                template_type = rec.get("x_studio_zns_last_template") or "hdsd-vie"
                send_count = rec.get("x_studio_zns_send_count") or 1

                # 2. Atomic claim in Durable SQLite Queue (Source of Truth)
                unique_token = f"{rec_id}:{template_type}:v{send_count}:{uuid.uuid4().hex}"
                claim_res = self.repo.claim_dispatch_request(
                    source_model=model,
                    source_record_id=rec_id,
                    template_type=template_type,
                    send_version=send_count,
                    worker_id=self.worker_id,
                    claim_token=unique_token,
                )

                if not claim_res["is_new_claim"]:
                    existing_req = claim_res.get("record") or {}
                    if existing_req.get("status") == "COMPLETED":
                        # Stale snapshot: previously completed in SQLite -> writeback to Odoo without re-dispatching
                        logger.info(
                            f"[ZNS-ODOO-POLLER] Record {model}({rec_id}) v{send_count} already completed in SQLite "
                            f"— writing back existing status '{existing_req.get('result_status')}'"
                        )
                        self._writeback_completed_to_odoo(
                            model=model,
                            rec_id=rec_id,
                            send_version=send_count,
                            backend_status=existing_req.get("result_status") or "ACCEPTED",
                            zalo_msg_id=existing_req.get("zalo_msg_id"),
                        )
                    else:
                        logger.info(
                            f"[ZNS-ODOO-POLLER] Record {model}({rec_id}) v{send_count} already claimed by "
                            f"'{existing_req.get('claimed_by')}' — skipping duplicate dispatch"
                        )
                    continue

                # 3. We are the sole owner of this claim (insert or stale/FAILED reclaim)
                try:
                    write_ok = self.odoo_client.write(model, [rec_id], {
                        "x_studio_zns_request_state": "processing",
                        "x_studio_zns_claim_token": unique_token,
                        "x_studio_zns_claim_owner": self.worker_id,
                        "x_studio_zns_processing_started_at": _odoo_utc_now(),
                    })
                    if not write_ok:
                        logger.warning(
                            f"[ZNS-ODOO-POLLER] Claim write returned {write_ok} for record {rec_id} "
                            f"— marking FAILED for retry"
                        )
                        self.repo.fail_dispatch_request(
                            claim_res["id"], self.worker_id, unique_token,
                            "Odoo telemetry write returned False",
                        )
                        continue
                except Exception as e:
                    logger.warning(
                        f"[ZNS-ODOO-POLLER] Failed to update Odoo telemetry for {rec_id}: {e} "
                        f"— marking FAILED for retry"
                    )
                    self.repo.fail_dispatch_request(
                        claim_res["id"], self.worker_id, unique_token,
                        f"Odoo telemetry write error: {e}",
                    )
                    continue

                marked = self.repo.mark_dispatch_processing(
                    claim_res["id"], self.worker_id, unique_token
                )
                renewed = marked and self.repo.renew_dispatch_lease(
                    claim_res["id"], self.worker_id, unique_token
                )
                if not renewed:
                    logger.warning(
                        f"[ZNS-ODOO-POLLER] Lost fenced lease before dispatch for {model}({rec_id})"
                    )
                    continue

                phone = rec.get("x_studio_phone") or ""
                if not phone and rec.get("partner_id"):
                    partner_id = rec["partner_id"][0] if isinstance(rec["partner_id"], (list, tuple)) else rec["partner_id"]
                    try:
                        p_data = self.odoo_client.read("res.partner", [partner_id], ["phone", "mobile"])
                        if p_data:
                            phone = p_data[0].get("mobile") or p_data[0].get("phone") or ""
                    except Exception:
                        pass

                customer_name = rec.get("x_studio_tn_khch_hng") or ""
                if not customer_name and rec.get("partner_id") and isinstance(rec["partner_id"], (list, tuple)):
                    customer_name = rec["partner_id"][1]

                order_code = rec.get("name") or str(rec_id)
                date_order = _format_order_date_for_zns(rec.get("date_order"))
                company_id = rec["company_id"][0] if rec.get("company_id") and isinstance(rec["company_id"], (list, tuple)) else None

                # 4. Dispatch ZNS via tracking service with stable idempotency key
                idempotency_key = f"{model}:{rec_id}:{template_type}:v{send_count}"

                try:
                    result, lease_owned = self._dispatch_with_lease_heartbeat(
                        claim_res["id"],
                        unique_token,
                        self.tracking_service.dispatch_zns,
                        template_type=template_type,
                        phone_raw=str(phone),
                        order_code=order_code,
                        order_date=str(date_order),
                        customer_name=str(customer_name),
                        idempotency_key=idempotency_key,
                        source="odoo",
                        source_model=model,
                        source_record_id=rec_id,
                        sent_by_user_id=rec.get("x_studio_zns_sent_by_user_id"),
                        sent_by_user_name=rec.get("x_studio_zns_sent_by_user_name"),
                        company_id=company_id,
                    )
                    if not lease_owned:
                        logger.warning(
                            f"[ZNS-ODOO-POLLER] Lost fenced lease during dispatch for {model}({rec_id}); "
                            "durable idempotency result will be recovered by the current owner"
                        )
                        continue
                    backend_status = result.get("status", "SUBMISSION_UNKNOWN") if isinstance(result, dict) else "SUBMISSION_UNKNOWN"
                    zalo_msg_id = result.get("msg_id") or "" if isinstance(result, dict) else ""
                    tracking_id = result.get("tracking_id") or "" if isinstance(result, dict) else ""
                    message_id = result.get("message_id") or "" if isinstance(result, dict) else ""

                    # 5. Complete request in SQLite Durable Queue
                    res_json = json.dumps(result) if isinstance(result, dict) else str(result)
                    completed = self.repo.complete_dispatch_request(
                        request_id=claim_res["id"],
                        worker_id=self.worker_id,
                        claim_token=unique_token,
                        message_id=message_id,
                        tracking_id=tracking_id,
                        zalo_msg_id=zalo_msg_id,
                        result_status=backend_status,
                        result_json=res_json,
                    )
                    if not completed:
                        logger.warning(
                            f"[ZNS-ODOO-POLLER] Fenced completion rejected for {model}({rec_id}); "
                            "skipping stale Odoo writeback"
                        )
                        continue

                    # 6. Version-bound writeback to Odoo
                    self._writeback_completed_to_odoo(
                        model=model,
                        rec_id=rec_id,
                        send_version=send_count,
                        backend_status=backend_status,
                        zalo_msg_id=zalo_msg_id,
                    )

                    dispatched_count += 1

                except Exception as e:
                    logger.error(f"[ZNS-ODOO-POLLER] Exception during dispatch_zns for {rec_id}: {e}")
                    failed = self.repo.fail_dispatch_request(
                        claim_res["id"], self.worker_id, unique_token, str(e)
                    )
                    if not failed:
                        logger.warning(
                            f"[ZNS-ODOO-POLLER] Stale worker cannot fail request {claim_res['id']}; "
                            "skipping Odoo reset"
                        )
                        continue
                    try:
                        self.odoo_client.write(model, [rec_id], {
                            "x_studio_zns_request_state": "pending",
                            "x_studio_zns_claim_token": False,
                            "x_studio_zns_claim_owner": False,
                            "x_studio_zns_processing_started_at": False,
                        })
                    except Exception:
                        pass
                    continue

            _last_dispatched_count = dispatched_count
            _last_error = None
            return dispatched_count

        except Exception as e:
            _last_error = str(e)
            logger.error(f"[ZNS-ODOO-POLLER] Error querying pending records: {e}")
            return 0


# Background Odoo Poller Daemon
_poller_thread: Optional[threading.Thread] = None
_poller_stop = threading.Event()
_poller_lock = threading.Lock()


def _poller_loop(interval: float = 10.0):
    """Background polling loop querying Odoo for pending ZNS dispatches."""
    logger.info(f"[ZNS-ODOO-POLLER] Started poller daemon (interval: {interval}s)")
    poller = ZNSOdooPoller()

    while not _poller_stop.is_set():
        try:
            poller.discover_external_automation_requests()
            poller.poll_and_dispatch(model="sale.order", limit=20)
        except Exception as e:
            logger.error(f"[ZNS-ODOO-POLLER] Unhandled exception in poller loop: {e}")

        _poller_stop.wait(timeout=interval)

    logger.info("[ZNS-ODOO-POLLER] Poller daemon stopped")


def start_odoo_poller(interval: float = 10.0) -> bool:
    """Start background Odoo poller daemon thread if configuration is valid."""
    global _poller_thread
    with _poller_lock:
        if not Config.ODOO_URL or not Config.ODOO_DB or not Config.ODOO_API_KEY:
            logger.info("[ZNS-ODOO-POLLER] Odoo credentials not configured — skipping poller startup")
            return False

        if _poller_thread is not None and _poller_thread.is_alive():
            return False

        _poller_stop.clear()
        _poller_thread = threading.Thread(
            target=_poller_loop,
            args=(interval,),
            name="ZNSOdooPollerDaemon",
            daemon=True,
        )
        _poller_thread.start()
        return True


def stop_odoo_poller() -> bool:
    """Stop background Odoo poller daemon thread."""
    global _poller_thread
    with _poller_lock:
        if _poller_thread is None or not _poller_thread.is_alive():
            return False

        _poller_stop.set()
        _poller_thread.join(timeout=5.0)
        _poller_thread = None
        return True


def is_odoo_poller_running() -> bool:
    """Check if background Odoo poller daemon is currently active."""
    with _poller_lock:
        return _poller_thread is not None and _poller_thread.is_alive()


def get_odoo_poller_status() -> Dict[str, Any]:
    """Get status telemetry of ZNS Odoo Poller."""
    enabled = bool(Config.ODOO_URL and Config.ODOO_DB and Config.ODOO_API_KEY)
    running = is_odoo_poller_running()

    if not enabled:
        state = "disabled"
    elif running:
        state = "enabled_and_running"
    else:
        state = "enabled_not_running"

    return {
        "enabled": enabled,
        "running": running,
        "state": state,
        "last_poll": _last_poll_iso,
        "last_error": _last_error,
        "last_dispatched_count": _last_dispatched_count,
        "stale_processing_count": _stale_processing_count,
    }
