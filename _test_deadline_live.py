"""
Live test for Deadline Watcher v2 (with anti-deletion).
Seeds snapshot, then polls every 10s.

Test flow:
  1. Change a deadline in Odoo (push forward) → note appears
  2. Delete the warning from the note → warning comes back on next poll
"""
import sys, os, time, logging

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from dotenv import load_dotenv
load_dotenv()

from services.deadline_watcher import (
    _load_snapshot, _save_snapshot, _poll_and_check, _odoo_call, SNAPSHOT_FILE
)

print("=" * 60)
print("  LIVE Deadline Watcher v2 (Anti-Deletion)")
print(f"  Snapshot: {os.path.abspath(SNAPSHOT_FILE)}")
print("=" * 60)

# Seed
print("\n[1] Seeding snapshot...")
activities = _odoo_call(
    "mail.activity", "search_read", [[]],
    {"fields": ["id", "date_deadline"]},
) or []

snapshot = {}
for a in activities:
    snapshot[str(a["id"])] = {"deadline": a["date_deadline"], "warnings": []}

_save_snapshot(snapshot)
print(f"    {len(snapshot)} activities saved")

# Poll loop
print("\n[2] Polling every 10s...")
print("    Test 1: Change a Due Date (push forward) → warning appears")
print("    Test 2: Delete the warning text → it comes back!")
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
