"""
Static AST validation test for Odoo 19 Online Safe-Eval compliance.
Verifies that `odoo/zns_server_action.py` adheres strictly to Odoo's safe_eval sandbox restrictions:
- No import statements (ast.Import, ast.ImportFrom)
- No disallowed builtins (locals, globals, hasattr, getattr, setattr, eval, exec, open, __import__)
- Valid Python AST compilation
"""

import ast
import os
import unittest


class TestOdooSafeEvalStatic(unittest.TestCase):
    """Static AST analysis verifying Odoo 19 safe_eval compliance."""

    @classmethod
    def setUpClass(cls):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.server_action_path = os.path.join(base_dir, "odoo", "zns_server_action.py")
        with open(cls.server_action_path, "r", encoding="utf-8") as f:
            cls.source_code = f.read()
        cls.tree = ast.parse(cls.source_code, filename=cls.server_action_path)

    def test_syntax_is_valid_python(self):
        """Verify the file compiles to a valid Python AST."""
        self.assertIsNotNone(self.tree)
        self.assertIsInstance(self.tree, ast.Module)

    def test_no_import_statements(self):
        """Ensure no 'import' or 'from ... import ...' statements exist."""
        imports = [node for node in ast.walk(self.tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        import_names = []
        for imp in imports:
            if isinstance(imp, ast.Import):
                import_names.extend(alias.name for alias in imp.names)
            elif isinstance(imp, ast.ImportFrom):
                import_names.append(imp.module or "")
        self.assertEqual(
            len(imports),
            0,
            f"Found forbidden import statements in Odoo Safe-Eval script: {import_names}",
        )

    def test_no_disallowed_builtin_calls(self):
        """Ensure no calls to restricted builtins like hasattr, getattr, locals, globals, exec, eval."""
        disallowed_calls = {
            "hasattr",
            "getattr",
            "setattr",
            "locals",
            "globals",
            "eval",
            "exec",
            "open",
            "__import__",
            "compile",
        }

        found_disallowed = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in disallowed_calls:
                    found_disallowed.append((node.func.id, node.lineno))

        self.assertEqual(
            len(found_disallowed),
            0,
            f"Found forbidden builtin calls in Odoo Safe-Eval script: {found_disallowed}",
        )

    def test_safe_eval_mock_execution(self):
        """Simulate execution inside a restricted Odoo 19 safe_eval dictionary."""
        class MockRecord:
            def __init__(self):
                self._fields = {
                    "id": None,
                    "name": None,
                    "x_studio_phone": None,
                    "x_studio_tn_khch_hng": None,
                    "x_studio_zns_send_count": None,
                    "x_studio_zns_request_state": None,
                    "x_studio_zns_status": None,
                    "x_studio_zns_last_template": None,
                    "partner_id": None,
                }
                self.id = 101
                self.name = "SO101"
                self.x_studio_phone = "0912345678"
                self.x_studio_tn_khch_hng = "Nguyen Van A"
                self.x_studio_zns_send_count = 0
                self.x_studio_zns_request_state = "ready"
                self.x_studio_zns_status = "none"
                self.x_studio_zns_last_template = False
                self.partner_id = None
                self.chatter_messages = []

            def sudo(self):
                return self

            def write(self, vals):
                for k, v in vals.items():
                    setattr(self, k, v)
                return True

            def message_post(self, body, message_type="comment", subtype_xmlid=None):
                self.chatter_messages.append(body)

        class MockUser:
            name = "Test Admin"

        class MockEnv:
            context = {"zns_template": "hdsd-vie"}
            user = MockUser()

        mock_record = MockRecord()
        safe_globals = {
            "__builtins__": {
                "str": str,
                "int": int,
                "len": len,
                "bool": bool,
                "dict": dict,
                "list": list,
                "tuple": tuple,
                "set": set,
                "isinstance": isinstance,
            },
            "env": MockEnv(),
            "record": mock_record,
            "records": [mock_record],
            "time": None,
            "datetime": None,
        }

        # Execute safe_eval code
        exec(self.source_code, safe_globals)

        action = safe_globals.get("action")
        self.assertIsNotNone(action)
        self.assertEqual(action.get("type"), "ir.actions.client")
        self.assertEqual(action.get("tag"), "display_notification")
        self.assertEqual(mock_record.x_studio_zns_request_state, "pending")
        self.assertEqual(mock_record.x_studio_zns_send_count, 1)
        self.assertEqual(len(mock_record.chatter_messages), 1)

    def test_double_click_pending_warns_and_does_not_increment_version(self):
        """When record state is already 'pending', second click without resend flag must NOT increment version."""
        class MockRecordPending:
            def __init__(self):
                self._fields = {
                    "id": None, "name": None, "x_studio_phone": None, "x_studio_tn_khch_hng": None,
                    "x_studio_zns_send_count": None, "x_studio_zns_request_state": None,
                    "x_studio_zns_status": None, "x_studio_zns_last_template": None, "partner_id": None,
                }
                self.id = 102
                self.name = "SO102"
                self.x_studio_phone = "0912345678"
                self.x_studio_tn_khch_hng = "Nguyen Van B"
                self.x_studio_zns_send_count = 1
                self.x_studio_zns_request_state = "pending"  # Already pending
                self.x_studio_zns_status = "queued"
                self.x_studio_zns_last_template = "hdsd-vie"
                self.partner_id = None
                self.chatter_messages = []

            def sudo(self):
                return self

            def write(self, vals):
                for k, v in vals.items():
                    setattr(self, k, v)
                return True

            def message_post(self, body, message_type="comment", subtype_xmlid=None):
                self.chatter_messages.append(body)

        class MockEnv:
            context = {"zns_template": "hdsd-vie", "resend": False}
            user = type("MockUser", (), {"name": "Admin"})()

        mock_record = MockRecordPending()
        safe_globals = {
            "__builtins__": {
                "str": str, "int": int, "len": len, "bool": bool,
                "dict": dict, "list": list, "tuple": tuple, "set": set, "isinstance": isinstance,
            },
            "env": MockEnv(),
            "record": mock_record,
            "records": [mock_record],
            "time": None, "datetime": None,
        }

        exec(self.source_code, safe_globals)

        action = safe_globals.get("action")
        self.assertIsNotNone(action)
        self.assertEqual(action.get("params", {}).get("type"), "warning")
        self.assertIn("Đang Chờ Xử Lý", action.get("params", {}).get("title", ""))
        # Version must stay 1 (not incremented)
        self.assertEqual(mock_record.x_studio_zns_send_count, 1)

    def test_explicit_resend_increments_version(self):
        """When resend=True in context, state is reset to pending and version count increments."""
        class MockRecordResend:
            def __init__(self):
                self._fields = {
                    "id": None, "name": None, "x_studio_phone": None, "x_studio_tn_khch_hng": None,
                    "x_studio_zns_send_count": None, "x_studio_zns_request_state": None,
                    "x_studio_zns_status": None, "x_studio_zns_last_template": None, "partner_id": None,
                }
                self.id = 103
                self.name = "SO103"
                self.x_studio_phone = "0912345678"
                self.x_studio_tn_khch_hng = "Nguyen Van C"
                self.x_studio_zns_send_count = 1
                self.x_studio_zns_request_state = "pending"
                self.x_studio_zns_status = "queued"
                self.x_studio_zns_last_template = "hdsd-vie"
                self.partner_id = None
                self.chatter_messages = []

            def sudo(self):
                return self

            def write(self, vals):
                for k, v in vals.items():
                    setattr(self, k, v)
                return True

            def message_post(self, body, message_type="comment", subtype_xmlid=None):
                self.chatter_messages.append(body)

        class MockEnv:
            context = {"zns_template": "hdsd-vie", "resend": True}  # Explicit Resend
            user = type("MockUser", (), {"name": "Admin"})()

        mock_record = MockRecordResend()
        safe_globals = {
            "__builtins__": {
                "str": str, "int": int, "len": len, "bool": bool,
                "dict": dict, "list": list, "tuple": tuple, "set": set, "isinstance": isinstance,
            },
            "env": MockEnv(),
            "record": mock_record,
            "records": [mock_record],
            "time": None, "datetime": None,
        }

        exec(self.source_code, safe_globals)

        action = safe_globals.get("action")
        self.assertIsNotNone(action)
        # Version must increment from 1 to 2
        self.assertEqual(mock_record.x_studio_zns_send_count, 2)
        self.assertEqual(mock_record.x_studio_zns_request_state, "pending")

    def test_double_click_during_processing_warns_and_blocks(self):
        """When record state is 'processing', second click without resend must warn and block."""
        class MockRecordProcessing:
            def __init__(self):
                self._fields = {
                    "id": None, "name": None, "x_studio_phone": None, "x_studio_tn_khch_hng": None,
                    "x_studio_zns_send_count": None, "x_studio_zns_request_state": None,
                    "x_studio_zns_status": None, "x_studio_zns_last_template": None, "partner_id": None,
                }
                self.id = 104
                self.name = "SO104"
                self.x_studio_phone = "0912345678"
                self.x_studio_tn_khch_hng = "Nguyen Van D"
                self.x_studio_zns_send_count = 1
                self.x_studio_zns_request_state = "processing"  # Poller is actively processing
                self.x_studio_zns_status = "queued"
                self.x_studio_zns_last_template = "hdsd-vie"
                self.partner_id = None
                self.chatter_messages = []

            def sudo(self):
                return self

            def write(self, vals):
                for k, v in vals.items():
                    setattr(self, k, v)
                return True

            def message_post(self, body, message_type="comment", subtype_xmlid=None):
                self.chatter_messages.append(body)

        class MockEnv:
            context = {"zns_template": "hdsd-vie", "resend": False}
            user = type("MockUser", (), {"name": "Admin"})()

        mock_record = MockRecordProcessing()
        safe_globals = {
            "__builtins__": {
                "str": str, "int": int, "len": len, "bool": bool,
                "dict": dict, "list": list, "tuple": tuple, "set": set, "isinstance": isinstance,
            },
            "env": MockEnv(),
            "record": mock_record,
            "records": [mock_record],
            "time": None, "datetime": None,
        }

        exec(self.source_code, safe_globals)

        action = safe_globals.get("action")
        self.assertIsNotNone(action)
        self.assertEqual(action.get("params", {}).get("type"), "warning")
        self.assertEqual(mock_record.x_studio_zns_send_count, 1)

    def test_resend_during_active_processing_warns_and_blocks(self):
        """When record state is actively 'processing', explicit resend must be blocked until finished."""
        class MockRecordActiveProcessing:
            def __init__(self):
                self._fields = {
                    "id": None, "name": None, "x_studio_phone": None, "x_studio_tn_khch_hng": None,
                    "x_studio_zns_send_count": None, "x_studio_zns_request_state": None,
                    "x_studio_zns_status": None, "x_studio_zns_last_template": None, "partner_id": None,
                }
                self.id = 105
                self.name = "SO105"
                self.x_studio_phone = "0912345678"
                self.x_studio_tn_khch_hng = "Nguyen Van E"
                self.x_studio_zns_send_count = 1
                self.x_studio_zns_request_state = "processing"
                self.x_studio_zns_status = "submitting"
                self.x_studio_zns_last_template = "hdsd-vie"
                self.partner_id = None
                self.chatter_messages = []

            def sudo(self):
                return self

            def write(self, vals):
                for k, v in vals.items():
                    setattr(self, k, v)
                return True

            def message_post(self, body, message_type="comment", subtype_xmlid=None):
                self.chatter_messages.append(body)

        class MockEnv:
            context = {"zns_template": "hdsd-vie", "resend": True}  # Resend attempted while sending
            user = type("MockUser", (), {"name": "Admin"})()

        mock_record = MockRecordActiveProcessing()
        safe_globals = {
            "__builtins__": {
                "str": str, "int": int, "len": len, "bool": bool,
                "dict": dict, "list": list, "tuple": tuple, "set": set, "isinstance": isinstance,
            },
            "env": MockEnv(),
            "record": mock_record,
            "records": [mock_record],
            "time": None, "datetime": None,
        }

        exec(self.source_code, safe_globals)

        action = safe_globals.get("action")
        self.assertIsNotNone(action)
        self.assertEqual(action.get("params", {}).get("type"), "warning")
        self.assertIn("Đang Xử Lý Gửi Tin", action.get("params", {}).get("title", ""))
        # Version must stay 1 (blocked)
        self.assertEqual(mock_record.x_studio_zns_send_count, 1)


if __name__ == "__main__":
    unittest.main()
