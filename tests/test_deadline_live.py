"""
Live test for Deadline Watcher v3 (anti-deletion + edit tracking).

Test flow:
  1. Change a deadline (push forward) → "ĐÃ ĐỔI DEADLINE" appears
  2. Edit the note content → "EDITED BY" appears  
  3. Delete a warning → it comes back
"""
import sys, os, time, logging

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from dotenv import load_dotenv
load_dotenv()

from services.deadline_watcher import (
    _load_snapshot, _save_snapshot, _poll_and_check, _odoo_call,
    _note_hash, SNAPSHOT_FILE
)

print("=" * 60)
print("  LIVE Deadline Watcher v3 (Edit Tracking)")
print(f"  Snapshot: {os.path.abspath(SNAPSHOT_FILE)}")
print("=" * 60)

# Seed
print("\n[1] Seeding snapshot...")
activities = _odoo_call(
    "mail.activity", "search_read", [[]],
    {"fields": ["id", "date_deadline", "note"]},
) or []

snapshot = {}
for a in activities:
    note = a.get("note") or ""
    snapshot[str(a["id"])] = {
        "deadline": a["date_deadline"],
        "warnings": [],
        "edit_logs": [],
        "note_hash": _note_hash(note),
    }

_save_snapshot(snapshot)
print(f"    {len(snapshot)} activities saved")

# Poll loop
print("\n[2] Polling every 10s...")
print("    Test A: Change Due Date (push forward) --> deadline warning")
print("    Test B: Edit the note text            --> edit log")
print("    Test C: Delete a warning line          --> it comes back")
print("    Ctrl+C to stop\n")

try:
    n = 0
    while True:
        n += 1
        snapshot = _load_snapshot()
        snapshot = _poll_and_check(snapshot)
        _save_snapshot(snapshot)
        print(f"    Poll #{n} done")
        time.sleep(10)
except KeyboardInterrupt:
    print("\nStopped.")
