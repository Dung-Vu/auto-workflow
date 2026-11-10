"""Hermetic OP rotation coverage: no app import, network, credentials or live writes."""
from unittest.mock import MagicMock
import importlib.util
from pathlib import Path

import pytest

from services import op_round_robin as rr


class Models:
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.calls = []

    def execute_kw(self, db, uid, key, model, method, args, kwargs):
        assert model == "sale.order"  # Never reinterpret OP IDs as res.users.
        self.calls.append((method, args, kwargs))
        if method == "write":
            for row in self.rows:
                if row["id"] in args[0]:
                    row.update(args[1])
                    row[rr.FIELD] = [args[1][rr.FIELD], "OP"]
            return True
        rows = self.rows
        for field, op, value in args[0]:
            if op == "=":
                rows = [r for r in rows if r.get(field) == value]
            else:
                rows = [r for r in rows if r.get(field) != value]
        rows = sorted(rows, key=lambda r: r["id"], reverse=kwargs.get("order") == "id desc")
        return rows[:kwargs["limit"]] if kwargs.get("limit") else rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def order(oid, company=1, op=False, state="draft"):
    return {"id": oid, "name": str(oid), "company_id": [company, "Company"] if company else False,
            rr.FIELD: [op, "OP"] if op else False, "state": state}


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(rr.Config, "OP_ROUND_ROBIN_EXCLUDE_OP_ID", 25)


@pytest.mark.parametrize("last,expected", [(None, 23), (23, 6), (6, 27), (27, 23), (25, 23), (999, 23)])
def test_global_rotation(last, expected):
    # A confirmed order in another company still defines the global last OP.
    rows = [order(3, 999)] + ([order(9, 11, last, "sale")] if last else [])
    fake = Models(rows)
    assert rr.process_round_robin(fake, rows[0]) == expected
    lookup = fake.calls[0]
    assert lookup == ("search_read", [[("id", "!=", 3), (rr.FIELD, "!=", False)]],
                      {"fields": [rr.FIELD], "order": "id desc", "limit": 1})
    assert fake.calls[-1] == ("write", [[3], {rr.FIELD: expected}], {})


def test_poll_draft_unset_only_sorted_and_current_order_excluded():
    fake = Models([order(5), order(2, 11), order(3, op=23), order(4, state="sent")])
    assert [o["id"] for o in rr.get_unassigned_quotations(fake)] == [2, 5]
    assert rr.get_last_assigned_op(fake, 3) is None
    assert fake.calls[0][1] == [[("state", "=", "draft"), (rr.FIELD, "=", False)]]


def test_global_highest_id_not_assignment_time():
    fake = Models([order(10), order(11, 11), order(100, 999, 23, "cancel")])
    for row in rr.get_unassigned_quotations(fake):
        assert rr.process_round_robin(fake, row) == 6
    # Assigning older rows does NOT advance beyond the higher-ID assigned row.
    assert [r[rr.FIELD][0] for r in fake.rows[:2]] == [6, 6]


def test_batch_advances_across_companies_when_ids_are_latest():
    fake = Models([order(1), order(2, 11), order(3, 999), order(4)])
    assert [rr.process_round_robin(fake, row) for row in rr.get_unassigned_quotations(fake)] == [23, 6, 27, 23]


@pytest.mark.parametrize("excluded,expected", [(25, [23, 6, 27]), (6, [23, 27]), (0, [23, 6, 27])])
def test_exclusions(monkeypatch, excluded, expected):
    monkeypatch.setattr(rr.Config, "OP_ROUND_ROBIN_EXCLUDE_OP_ID", excluded)
    assert rr.get_op_list(1) == rr.get_op_list(11) == rr.get_op_list(999) == expected
    assert 25 not in rr.get_op_list(None)


def test_no_company_or_no_ops_no_rpc(monkeypatch):
    fake = Models()
    assert rr.process_round_robin(fake, order(1, None)) is None
    monkeypatch.setattr(rr, "OP_IDS", ())
    assert rr.process_round_robin(fake, order(2)) is None
    assert fake.calls == []


@pytest.mark.parametrize("row", [order(1, op=6), order(1, state="sent")])
def test_recheck_does_not_overwrite_changed_order(row):
    fake = Models([row])
    assert rr.process_round_robin(fake, order(1)) is None
    assert not any(call[0] == "write" for call in fake.calls)


def test_false_write_is_error():
    fake = MagicMock()
    fake.execute_kw.side_effect = [[], [{"id": 1}], False]
    with pytest.raises(RuntimeError, match="write returned false"):
        rr.process_round_robin(fake, order(1))


def test_default_disabled_no_network(monkeypatch):
    # Ignore dotenv and real environment while checking the actual default.
    monkeypatch.delenv("OP_ROUND_ROBIN_ENABLED", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    spec = importlib.util.spec_from_file_location("isolated_rr_config", Path(rr.__file__).parents[1] / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.Config.OP_ROUND_ROBIN_ENABLED is False
    monkeypatch.setattr(rr.Config, "OP_ROUND_ROBIN_ENABLED", False)
    factory = MagicMock(side_effect=AssertionError("no network"))
    monkeypatch.setattr(rr, "get_models", factory)
    worker = rr.RoundRobinWorker()
    assert worker.start() is False
    assert worker.stop() is True
    assert worker.status()["running"] is False
    factory.assert_not_called()


def enable(monkeypatch, tmp_path):
    for name, value in {"OP_ROUND_ROBIN_ENABLED": True, "ENVIRONMENT": "development",
                        "ZNS_DISABLE_BACKGROUND_DAEMONS": False, "ODOO_URL": "https://example.invalid",
                        "ODOO_DB": "mock", "ODOO_UID": 1, "ODOO_API_KEY": "mock",
                        "OP_ROUND_ROBIN_LOCK_PATH": str(tmp_path / "op.lock")}.items():
        monkeypatch.setattr(rr.Config, name, value)


def test_lifecycle_and_duplicate_process_lock(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    # Worker remains in interruptible startup wait; no RPC happens.
    monkeypatch.setattr(rr.random, "uniform", lambda *args: 1000)
    factory = MagicMock(side_effect=AssertionError("no network"))
    monkeypatch.setattr(rr, "get_models", factory)
    a, b = rr.RoundRobinWorker(), rr.RoundRobinWorker()
    try:
        assert a.start() is True
        assert a.start() is False
        assert a.status()["running"] is True
        assert b.start() is False
        assert a.stop(timeout=2) is True
        assert a.stop(timeout=2) is True
        assert b.start() is True  # Released only after the first worker exits.
    finally:
        a.stop(timeout=2)
        b.stop(timeout=2)
    factory.assert_not_called()


@pytest.mark.parametrize("setting,value", [("ENVIRONMENT", "test"), ("ZNS_DISABLE_BACKGROUND_DAEMONS", True)])
def test_daemon_guards(monkeypatch, tmp_path, setting, value):
    enable(monkeypatch, tmp_path)
    monkeypatch.setattr(rr.Config, setting, value)
    assert rr.RoundRobinWorker().start() is False


def test_missing_fcntl_import_disabled_safe_enabled_fails_closed(monkeypatch, tmp_path):
    import builtins

    enable(monkeypatch, tmp_path)
    original_import = builtins.__import__

    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ModuleNotFoundError("No module named 'fcntl'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    spec = importlib.util.spec_from_file_location("op_round_robin_without_fcntl", rr.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.fcntl is None
    factory = MagicMock(side_effect=AssertionError("no network"))
    thread = MagicMock(side_effect=AssertionError("no thread"))
    monkeypatch.setattr(module, "get_models", factory)
    monkeypatch.setattr(module.threading, "Thread", thread)
    monkeypatch.setattr(module.Config, "OP_ROUND_ROBIN_ENABLED", False)
    worker = module.RoundRobinWorker()
    assert worker.start() is False
    assert worker.status()["running"] is False
    assert worker.stop() is True
    monkeypatch.setattr(module.Config, "OP_ROUND_ROBIN_ENABLED", True)
    with pytest.raises(RuntimeError, match="requires fcntl filesystem locking"):
        worker.start()
    assert worker._lease is None
    assert not (tmp_path / "op.lock").exists()
    factory.assert_not_called()
    thread.assert_not_called()


def test_lock_failure_closes_lease_and_never_starts(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    lease = MagicMock()
    monkeypatch.setattr("builtins.open", MagicMock(return_value=lease))
    monkeypatch.setattr(rr.fcntl, "flock", MagicMock(side_effect=OSError("unsupported lock")))
    factory = MagicMock(side_effect=AssertionError("no network"))
    monkeypatch.setattr(rr, "get_models", factory)
    worker = rr.RoundRobinWorker()
    with pytest.raises(OSError, match="unsupported lock"):
        worker.start()
    lease.close.assert_called_once()
    assert worker._lease is None
    assert worker.status()["running"] is False
    factory.assert_not_called()


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_thread_startup_failure_releases_lease(monkeypatch, tmp_path, phase):
    enable(monkeypatch, tmp_path)
    factory = MagicMock()
    if phase == "construct":
        factory.side_effect = RuntimeError("thread unavailable")
    else:
        factory.return_value.start.side_effect = RuntimeError("thread unavailable")
    monkeypatch.setattr(rr.threading, "Thread", factory)
    worker = rr.RoundRobinWorker()
    with pytest.raises(RuntimeError, match="thread unavailable"):
        worker.start()
    assert worker._lease is None
    with open(rr.Config.OP_ROUND_ROBIN_LOCK_PATH, "a+") as lease:
        rr.fcntl.flock(lease, rr.fcntl.LOCK_EX | rr.fcntl.LOCK_NB)


def test_stop_timeout_keeps_lease_until_inflight_write_exits(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    monkeypatch.setattr(rr.random, "uniform", lambda *args: 0)
    entered, release = rr.threading.Event(), rr.threading.Event()
    models = MagicMock()
    monkeypatch.setattr(rr, "get_models", lambda: models)
    monkeypatch.setattr(rr, "get_unassigned_quotations", lambda _: [order(1), order(2)])

    def blocked_write(*args):
        entered.set()
        assert release.wait(5)
        return 23

    process = MagicMock(side_effect=blocked_write)
    monkeypatch.setattr(rr, "process_round_robin", process)
    worker, contender = rr.RoundRobinWorker(), rr.RoundRobinWorker()
    try:
        assert worker.start()
        assert entered.wait(2)
        assert worker.stop(timeout=0) is False
        assert worker.status()["stopping"] is True
        assert worker._lease is not None
        assert contender.start() is False
        release.set()
        assert worker.stop(timeout=2) is True
        assert worker._lease is None
        assert process.call_count == 1
        # Check actual OS lease release without starting another RPC worker.
        with open(rr.Config.OP_ROUND_ROBIN_LOCK_PATH, "a+") as lease:
            rr.fcntl.flock(lease, rr.fcntl.LOCK_EX | rr.fcntl.LOCK_NB)
    finally:
        release.set()
        worker.stop(timeout=2)
        contender.stop(timeout=2)


def test_failed_assignment_aborts_batch_without_success_poll(monkeypatch):
    monkeypatch.setattr(rr.random, "uniform", lambda *args: 0)
    monkeypatch.setattr(rr, "get_models", MagicMock())
    monkeypatch.setattr(rr, "get_unassigned_quotations", lambda _: [order(1), order(2)])
    process = MagicMock(side_effect=RuntimeError("write failed"))
    monkeypatch.setattr(rr, "process_round_robin", process)
    worker = rr.RoundRobinWorker()
    worker._stop = Waits(2)
    worker._run()
    assert process.call_count == 1
    assert worker.errors == 1
    assert worker.last_poll is None


def test_missing_credentials_fail_closed(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    monkeypatch.setattr(rr.Config, "ODOO_API_KEY", "")
    monkeypatch.setattr(rr.Config, "OP_ROUND_ROBIN_ODOO_API_KEY", "")
    with pytest.raises(ValueError):
        rr.RoundRobinWorker().start()


class Waits:
    def __init__(self, stop_after):
        self.delays = []
        self.stop_after = stop_after

    def wait(self, delay):
        self.delays.append(delay)
        return len(self.delays) >= self.stop_after

    def is_set(self):
        return False


@pytest.mark.parametrize("message,expected", [("429 Too Many Requests", [60, 120, 240, 480, 480, 480]),
                                              ("broken", [2, 4, 8, 16, 32, 64])])
def test_polling_errors_backoff_and_proxy_recreation(monkeypatch, message, expected):
    monkeypatch.setattr(rr.random, "uniform", lambda a, b: 0)
    models = MagicMock()
    factory = MagicMock(return_value=models)
    monkeypatch.setattr(rr, "get_models", factory)
    monkeypatch.setattr(rr, "get_unassigned_quotations", MagicMock(side_effect=RuntimeError(message)))
    worker = rr.RoundRobinWorker()
    worker._stop = Waits(7)
    worker._run()
    assert worker._stop.delays == [0] + expected
    assert worker.errors == 6
    assert factory.call_count == 2


def test_success_resets_errors_and_idle_wait(monkeypatch):
    monkeypatch.setattr(rr.random, "uniform", lambda a, b: 0)
    models = MagicMock()
    monkeypatch.setattr(rr, "get_models", lambda: models)
    monkeypatch.setattr(rr, "get_unassigned_quotations", MagicMock(side_effect=[RuntimeError("x"), []]))
    worker = rr.RoundRobinWorker()
    worker._stop = Waits(3)
    worker._run()
    assert worker.errors == 0
    assert worker.last_poll
    assert worker._stop.delays == [0, 2, rr.Config.OP_ROUND_ROBIN_POLL_INTERVAL]


def test_stop_between_orders(monkeypatch):
    worker = rr.RoundRobinWorker()
    monkeypatch.setattr(rr.random, "uniform", lambda *args: 0)
    monkeypatch.setattr(rr, "get_models", MagicMock())
    monkeypatch.setattr(rr, "get_unassigned_quotations", lambda _: [order(1), order(2)])
    process = MagicMock(side_effect=lambda *args: worker._stop.set())
    monkeypatch.setattr(rr, "process_round_robin", process)
    worker._run()
    assert process.call_count == 1


@pytest.mark.parametrize("scheme,transport_class", [("https", rr._SafeTimeoutTransport),
                                                     ("http", rr._TimeoutTransport)])
def test_timeout_transport_and_no_transparent_retry(monkeypatch, scheme, transport_class):
    monkeypatch.setattr(rr.Config, "ODOO_URL", f"{scheme}://example.invalid")
    with rr.get_models() as proxy:
        transport = proxy("transport")
        assert isinstance(transport, transport_class)
        connection = transport.make_connection("example.invalid")
        assert connection.timeout == rr.Config.OP_ROUND_ROBIN_RPC_TIMEOUT
        request = MagicMock(side_effect=ConnectionResetError)
        monkeypatch.setattr(transport, "single_request", request)
        with pytest.raises(ConnectionResetError):
            transport.request("example.invalid", "/", b"", False)
        assert request.call_count == 1


def test_import_does_not_start_or_contact_odoo(monkeypatch):
    thread = MagicMock(side_effect=AssertionError("must not start thread"))
    proxy = MagicMock(side_effect=AssertionError("must not contact Odoo"))
    monkeypatch.setattr(rr.threading, "Thread", thread)
    monkeypatch.setattr(rr.xmlrpc.client, "ServerProxy", proxy)
    spec = importlib.util.spec_from_file_location("isolated_op_round_robin", rr.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.get_op_round_robin_status()["running"] is False
    thread.assert_not_called()
    proxy.assert_not_called()


def test_app_wiring_without_import_side_effects():
    # AST checks avoid importing unrelated app services / token or DB setup.
    import ast
    source = (Path(rr.__file__).parents[1] / "app.py").read_text()
    tree = ast.parse(source)
    health = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "health")
    assert "get_op_round_robin_status()" in ast.unparse(health)
    main = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    assert "start_op_round_robin()" in ast.unparse(main)
    shutdown = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == "_shutdown_daemons")
    assert "stop_op_round_robin()" in ast.unparse(shutdown)
