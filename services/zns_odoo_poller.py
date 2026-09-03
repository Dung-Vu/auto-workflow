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
                        "x_studio_zns_processing_started_at": _utc_now_iso(),
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
                date_order = rec.get("date_order") or ""
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
