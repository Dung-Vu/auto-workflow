"""
End-to-end test for Deadline Watcher on the testing server.

Steps:
  1. Find an activity on the testing server
  2. Seed a snapshot with a FAKE earlier deadline for that activity
  3. Run one poll cycle
  4. Verify the note was appended with the warning
  5. Clean up: restore original note
"""
import json
import os
import sys
import xmlrpc.client
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

# Setup path so we can import services
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from config import Config
from services.deadline_watcher import _load_snapshot, _save_snapshot, _poll_and_check, _odoo_call, SNAPSHOT_FILE

print("=" * 60)
print("  Deadline Watcher — E2E Test")
print("=" * 60)
print()

# 1) Find a suitable activity
print("[1] Finding a test activity...")
activities = _odoo_call(
    "mail.activity",
    "search_read",
    [[]],
    {
        "fields": ["id", "date_deadline", "note", "summary", "res_name", "write_uid"],
        "limit": 1,
        "order": "id desc",
    },
)

if not activities:
    print("No activities found — cannot test!")
    sys.exit(1)

act = activities[0]
act_id = act["id"]
real_deadline = act["date_deadline"]
original_note = act.get("note") or ""

print(f"  Activity #{act_id}: {act.get('summary', '')} | {act.get('res_name', '')}")
print(f"  Real deadline: {real_deadline}")
print(f"  Current note length: {len(original_note)} chars")
print()

# 2) Seed snapshot with a FAKE earlier deadline (1 day before real)
fake_earlier = (datetime.strptime(real_deadline, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
print(f"[2] Seeding snapshot with fake earlier deadline: {fake_earlier}")

# Load full snapshot or create one
snapshot = {}
all_acts = _odoo_call(
    "mail.activity",
    "search_read",
    [[]],
    {"fields": ["id", "date_deadline"]},
) or []

for a in all_acts:
    snapshot[str(a["id"])] = a["date_deadline"]

# Override the test activity with the fake earlier date
snapshot[str(act_id)] = fake_earlier
_save_snapshot(snapshot)
print(f"  Snapshot saved with {len(snapshot)} activities")
print()

# 3) Run one poll cycle
print("[3] Running poll cycle...")
snapshot = _load_snapshot()
snapshot = _poll_and_check(snapshot)
_save_snapshot(snapshot)
print()

# 4) Verify note was appended
print("[4] Verifying note was appended...")
updated_act = _odoo_call(
    "mail.activity",
    "search_read",
    [[["id", "=", act_id]]],
    {"fields": ["id", "note"], "limit": 1},
)

if updated_act:
    new_note = updated_act[0].get("note") or ""
    if "ĐÃ ĐỔI DEADLINE" in new_note:
        print(f"  [PASS] Warning found in note!")
        # Show the appended part
        if original_note:
            added = new_note[len(original_note):]
            print(f"  Added: {added[:200]}")
        else:
            print(f"  Full note: {new_note[:200]}")
    else:
        print(f"  [FAIL] Warning NOT found in note!")
        print(f"  Note content: {new_note[:300]}")
else:
    print(f"  [FAIL] Could not re-read activity #{act_id}")

print()

# 5) Clean up — restore original note
print("[5] Cleaning up — restoring original note...")
try:
    _odoo_call("mail.activity", "write", [[act_id], {"note": original_note}])
    print("  Original note restored.")
except Exception as e:
    print(f"  WARNING: Failed to restore note: {e}")

# Also clean up snapshot file
if os.path.exists(SNAPSHOT_FILE):
    os.remove(SNAPSHOT_FILE)
    print("  Snapshot file removed.")

print()
print("Test complete!")
