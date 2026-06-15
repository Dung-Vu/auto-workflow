"""
Unit tests for Deadline Watcher logic using mocked Odoo JSON-RPC calls.
Covers anti-deletion, edit tracking, restoration, and deferred edit detection.
"""
import sys
import os
import logging

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Configure logging to show info messages
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Import functions under test
import services.deadline_watcher as watcher
from services.deadline_watcher import (
    _poll_and_check, _note_hash, _load_snapshot, _save_snapshot
)

# ═══════════════════════════════════════════
#  MOCK DATABASE & ODOO CALL
# ═══════════════════════════════════════════

activities_db = {
    100: {
        "id": 100,
        "date_deadline": "2026-06-23",
        "note": "Original content",
        "write_uid": [208, "Nguyễn Quang Hà"]
    }
}

def mock_odoo_call(model, method, args=None, kwargs=None):
    global activities_db
    if model == "mail.activity":
        if method == "search_read":
            # Check if search_read is filtering for a specific ID (fresh read)
            if args and len(args) > 0 and isinstance(args[0], list) and len(args[0]) > 0:
                domain = args[0][0]
                if isinstance(domain, list) and len(domain) == 3 and domain[0] == "id" and domain[1] == "=":
                    act_id = int(domain[2])
                    act = activities_db.get(act_id)
                    if act:
                        return [{"id": act["id"], "note": act["note"]}]
                    return []
            # Return all activities
            return list(activities_db.values())
        
        elif method == "write":
            act_ids = args[0]
            vals = args[1]
            for aid in act_ids:
                if aid in activities_db:
                    activities_db[aid].update(vals)
            return True
            
    raise Exception(f"Unmocked call: {model}.{method} with {args} {kwargs}")

# Inject mock
watcher._odoo_call = mock_odoo_call


# ═══════════════════════════════════════════
#  RUN TEST SCENARIOS
# ═══════════════════════════════════════════

def run_tests():
    global activities_db
    print("=" * 60)
    print("  RUNNING MOCKED DEADLINE WATCHER LOGIC TESTS")
    print("=" * 60)

    # Clean temporary snapshot file from previous runs
    watcher.SNAPSHOT_FILE = "deadline_snapshot_test.json"
    if os.path.exists(watcher.SNAPSHOT_FILE):
        os.remove(watcher.SNAPSHOT_FILE)

    # ----------------------------------------------------
    # Scenario 1: Initial Seed (Startup)
    # ----------------------------------------------------
    print("\n--- [Scenario 1] Initial Seed ---")
    snapshot = {}
    
    # Run poll to seed
    snapshot = _poll_and_check(snapshot)
    
    entry = snapshot["100"]
    assert entry["deadline"] == "2026-06-23"
    assert entry["note_hash"] == _note_hash("Original content")
    assert len(entry["warnings"]) == 0
    assert len(entry["edit_logs"]) == 0
    print("PASS: Initial seed configured correctly without appending note.")

    # ----------------------------------------------------
    # Scenario 2: Due date pushed forward (23 -> 24)
    # ----------------------------------------------------
    print("\n--- [Scenario 2] Deadline pushed forward (23 -> 24) ---")
    activities_db[100]["date_deadline"] = "2026-06-24"
    
    snapshot = _poll_and_check(snapshot)
    
    entry = snapshot["100"]
    assert entry["deadline"] == "2026-06-24"
    assert len(entry["warnings"]) == 1
    warning_key = entry["warnings"][0]["key"]
    assert warning_key == "23/06/2026 → 24/06/2026"
    assert warning_key in activities_db[100]["note"]
    print(f"PASS: Appended deadline warning: {warning_key}")

    # Save current state for next scenarios
    state_after_warning = activities_db[100]["note"]
    hash_after_warning = entry["note_hash"]

    # ----------------------------------------------------
    # Scenario 3: User edits note content (adds warning/edit log)
    # ----------------------------------------------------
    print("\n--- [Scenario 3] User modifies note content ---")
    # Simulate user changing text but keeping the warning
    user_edited_note = "Original content modified\n" + entry["warnings"][0]["html"]
    activities_db[100]["note"] = user_edited_note
    
    snapshot = _poll_and_check(snapshot)
    
    entry = snapshot["100"]
    assert len(entry["edit_logs"]) == 1
    edit_key = entry["edit_logs"][0]["key"]
    assert "Nguyễn" in edit_key
    assert edit_key in activities_db[100]["note"]
    hash_after_edit = entry["note_hash"]
    print(f"PASS: Logged note edit: {edit_key}", flush=True)

    # ----------------------------------------------------
    # Scenario 4: User deletes both Warning A and Edit C (No other changes)
    # ----------------------------------------------------
    print("\n--- [Scenario 4] User deletes Warning A + Edit C ---")
    # Simulate user deleting both warning and edit log
    activities_db[100]["note"] = "Original content modified"
    
    print("  [Cycle 1] Restoring Warning A + Edit C, skip edit detection:")
    snapshot = _poll_and_check(snapshot)
    
    # Assertions for Cycle 1
    entry = snapshot["100"]
    # 1. Warning A and Edit C must be restored to Odoo
    assert warning_key in activities_db[100]["note"]
    assert edit_key in activities_db[100]["note"]
    # 2. No new edit log should be created
    assert len(entry["edit_logs"]) == 1
    # 3. Note hash in snapshot must NOT be updated (must be equal to hash_after_edit)
    assert entry["note_hash"] == hash_after_edit
    print("  PASS: Cycle 1 restored both items, skipped edit detection, kept old hash.")

    print("  [Cycle 2] Stable state, no changes:")
    snapshot = _poll_and_check(snapshot)
    
    # Assertions for Cycle 2
    entry = snapshot["100"]
    assert len(entry["edit_logs"]) == 1
    assert entry["note_hash"] == _note_hash(activities_db[100]["note"])
    hash_after_cycle2 = entry["note_hash"]
    print("  PASS: Cycle 2 did not trigger edit detection since content matches restored note.")

    # ----------------------------------------------------
    # Scenario 5: User deletes Warning A + Edit C AND adds new text
    # ----------------------------------------------------
    print("\n--- [Scenario 5] User deletes Warning A + Edit C + adds new text ---")
    # Simulate user deleting warnings/edits AND adding new text
    activities_db[100]["note"] = "Original content modified + NEW TEXT CONTENT"
    
    print("  [Cycle 1] Restoring Warning A + Edit C, skip edit detection, keep old hash:")
    snapshot = _poll_and_check(snapshot)
    
    entry = snapshot["100"]
    # 1. Warning A and Edit C must be restored
    assert warning_key in activities_db[100]["note"]
    assert edit_key in activities_db[100]["note"]
    # 2. The new text is still present in the note
    assert "NEW TEXT CONTENT" in activities_db[100]["note"]
    # 3. No new edit log should be created in Cycle 1
    assert len(entry["edit_logs"]) == 1
    # 4. Hash is NOT updated (remains equal to hash_after_cycle2)
    assert entry["note_hash"] == hash_after_cycle2
    assert entry["note_hash"] != _note_hash(activities_db[100]["note"])
    print("  PASS: Cycle 1 restored both items, skipped edit detection, preserved new text.")

    print("  [Cycle 2] Detecting the new text → create Edit E:")
    snapshot = _poll_and_check(snapshot)
    
    entry = snapshot["100"]
    # A new edit log must be created
    assert len(entry["edit_logs"]) == 2
    new_edit_key = entry["edit_logs"][1]["key"]
    assert new_edit_key in activities_db[100]["note"]
    # Hash must be updated now
    assert entry["note_hash"] == _note_hash(activities_db[100]["note"])
    print(f"  PASS: Cycle 2 created new edit log E: {new_edit_key}")

    # Clean up test snapshot
    if os.path.exists(watcher.SNAPSHOT_FILE):
        os.remove(watcher.SNAPSHOT_FILE)

    print("\n" + "=" * 60)
    print("  ALL TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
