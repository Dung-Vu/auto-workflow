"""External OP assignment; no I/O, signals or workers at import time.

IDs belong to x_studio_op_in_charge's relation, NOT res.users. Rotation is
based on the highest sale.order ID with any OP globally, not write_date.
"""
try:
    import fcntl
except ImportError:  # e.g. native Windows; disabled imports must remain usable.
    fcntl = None

import http.client
import logging
import os
import random
import threading
import xmlrpc.client
from datetime import datetime, timezone

from config import Config

log = logging.getLogger(__name__)
OP_IDS = (23, 6, 27)  # 25 is deliberately never eligible
FIELD = "x_studio_op_in_charge"


class _TimeoutTransport(xmlrpc.client.Transport):
    connection_class = http.client.HTTPConnection

    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        if self._connection and host == self._connection[0]:
            return self._connection[1]
        chost, self._extra_headers, _ = self.get_host_info(host)
        connection = self.connection_class(chost, timeout=self.timeout)
        self._connection = host, connection
        return connection

    def request(self, host, handler, request_body, verbose=False):
        # Do not transparently replay a write after an ambiguous disconnect.
        return self.single_request(host, handler, request_body, verbose)


class _SafeTimeoutTransport(_TimeoutTransport, xmlrpc.client.SafeTransport):
    connection_class = http.client.HTTPSConnection


def get_models():
    transport = (_SafeTimeoutTransport if Config.ODOO_URL.startswith("https:")
                 else _TimeoutTransport)(Config.OP_ROUND_ROBIN_RPC_TIMEOUT)
    return xmlrpc.client.ServerProxy(
        f"{Config.ODOO_URL.rstrip('/')}/xmlrpc/2/object", transport=transport
    )


def _call(models, method, args, kwargs=None):
    return models.execute_kw(
        Config.ODOO_DB, Config.OP_ROUND_ROBIN_ODOO_UID or Config.ODOO_UID,
        Config.OP_ROUND_ROBIN_ODOO_API_KEY or Config.ODOO_API_KEY,
        "sale.order", method, args, kwargs or {},
    )


def get_op_list(company_id):
    # All companies (including unknown companies) share the SAME rotation.
    return [op for op in OP_IDS if op != Config.OP_ROUND_ROBIN_EXCLUDE_OP_ID]


def get_unassigned_quotations(models):
    return _call(models, "search_read", [[
        ("state", "=", "draft"), (FIELD, "=", False),
    ]], {"fields": ["id", "name", "company_id"], "order": "id asc"})


def get_last_assigned_op(models, current_order_id, ops=None):
    # Only OPs in the rotation define the turn: a manual assignment to an excluded OP
    # (e.g. Mai Thu) must not reset the rotation back to the first OP.
    domain = [("id", "!=", current_order_id), (FIELD, "!=", False)]
    if ops:
        domain.append((FIELD, "in", list(ops)))
    rows = _call(models, "search_read", [domain],
                 {"fields": [FIELD], "order": "id desc", "limit": 1})
    return rows[0][FIELD][0] if rows and rows[0].get(FIELD) else None


def process_round_robin(models, order):
    company = order.get("company_id")
    if not company:
        log.warning("Order %s has no company; skipping", order["id"])
        return None
    ops = get_op_list(company[0])
    if not ops:
        return None
    last = get_last_assigned_op(models, order["id"], ops)
    chosen = ops[(ops.index(last) + 1) % len(ops)] if last in ops else ops[0]
    # Recheck eligibility to avoid overwriting changes since the initial poll.
    eligible = _call(models, "search_read", [[
        ("id", "=", order["id"]), ("state", "=", "draft"), (FIELD, "=", False),
    ]], {"fields": ["id"], "limit": 1})
    if not eligible:
        return None
    if not _call(models, "write", [[order["id"]], {FIELD: chosen}]):
        raise RuntimeError("OP assignment write returned false")
    log.info("Order %s company=%s last_op=%s -> OP %s", order["id"], company[0], last, chosen)
    return chosen


def _backoff(errors, rate_limited):
    base = (min(600, max(60, 30 * 2 ** min(errors, 4))) if rate_limited
            else min(600, 2 ** min(errors, 10)))
    return base + random.uniform(0, max(1, base * .25))


class RoundRobinWorker:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._lease = None
        self.errors = 0
        self.last_poll = None

    def start(self):
        with self._lock:
            if (not Config.OP_ROUND_ROBIN_ENABLED or Config.ENVIRONMENT == "test"
                    or Config.ZNS_DISABLE_BACKGROUND_DAEMONS):
                return False
            if self._thread and self._thread.is_alive():
                return False
            if fcntl is None:
                raise RuntimeError("OP round robin requires fcntl filesystem locking")
            if not (Config.ODOO_URL and Config.ODOO_DB
                    and (Config.OP_ROUND_ROBIN_ODOO_UID or Config.ODOO_UID)
                    and (Config.OP_ROUND_ROBIN_ODOO_API_KEY or Config.ODOO_API_KEY)):
                raise ValueError("OP round robin requires URL, DB, UID and API key")
            # Shared DATA_DIR protects against duplicate processes on this host/
            # mounted volume. It cannot protect against the legacy container.
            path = Config.OP_ROUND_ROBIN_LOCK_PATH
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            lease = open(path, "a+")
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lease.close()
                log.warning("OP round robin already running (lock held)")
                return False
            except BaseException:
                # Unsupported/broken locking must never fall back to unlocked
                # operation, and must not leak the descriptor on failed startup.
                lease.close()
                raise
            self._lease = lease
            self._stop.clear()
            self.errors = 0
            try:
                self._thread = threading.Thread(target=self._run, name="op-round-robin", daemon=True)
                self._thread.start()
            except Exception:
                self._lease.close()
                self._lease = None
                raise
            return True

    def stop(self, timeout=None):
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout if timeout is not None else Config.OP_ROUND_ROBIN_RPC_TIMEOUT + 5)
        return not (thread and thread.is_alive())

    def status(self):
        return {"enabled": Config.OP_ROUND_ROBIN_ENABLED,
                "running": bool(self._thread and self._thread.is_alive()),
                "stopping": bool(self._thread and self._thread.is_alive() and self._stop.is_set()),
                "op_ids": get_op_list(None), "consecutive_errors": self.errors,
                "last_poll": self.last_poll}

    def _run(self):
        models = None
        try:
            if self._stop.wait(random.uniform(10, 30)):
                return
            while not self._stop.is_set():
                try:
                    if models is None:
                        models = get_models()
                    orders = get_unassigned_quotations(models)
                    for order in orders:
                        if self._stop.is_set():
                            break
                        process_round_robin(models, order)
                    self.errors = 0
                    self.last_poll = datetime.now(timezone.utc).isoformat()
                except Exception as exc:
                    self.errors += 1
                    rate_limited = "429" in str(exc) or "Too Many Requests" in str(exc)
                    delay = _backoff(self.errors, rate_limited)
                    # Do not expose RPC faults/credentials in health or logs.
                    log.log(logging.WARNING if rate_limited else logging.ERROR,
                            "OP polling error #%s (%s); retry in %.1fs",
                            self.errors, type(exc).__name__, delay)
                    if self.errors >= 5:
                        if models is not None:
                            with models:
                                pass  # local ServerProxy context closes its socket
                        models = None
                    if self._stop.wait(delay):
                        break
                    continue
                if self._stop.wait(Config.OP_ROUND_ROBIN_POLL_INTERVAL +
                                   random.uniform(0, Config.OP_ROUND_ROBIN_POLL_JITTER)):
                    break
        finally:
            try:
                if models is not None:
                    with models:
                        pass
            finally:
                # Serialize final lease release with a concurrent start/stop.
                with self._lock:
                    if self._lease is not None:
                        self._lease.close()
                        self._lease = None


_worker = RoundRobinWorker()


def start_op_round_robin():
    return _worker.start()


def stop_op_round_robin():
    return _worker.stop()


def get_op_round_robin_status():
    return _worker.status()
