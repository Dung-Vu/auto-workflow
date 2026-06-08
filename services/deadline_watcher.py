"""
Deadline Watcher — Background Polling Service.

Monitors mail.activity records for deadline extensions (date pushed forward).
When a user edits an activity's Due Date to a LATER date, this service
automatically appends a warning line to the activity's Note field.

If a user deletes the warning from the note, the watcher will detect it
and re-append on the next poll cycle.

Strategy: Polling + Snapshot
  - Every N seconds, fetch all mail.activity records
  - Compare date_deadline against a persisted snapshot
  - If deadline moved forward → append "⚠️ ĐÃ ĐỔI DEADLINE" to note
  - If deadline moved backward or unchanged → skip
  - New activities (not in snapshot) are recorded WITHOUT appending
  - Each poll also checks that previous warnings are still present in the note

Snapshot format:
  {
    "activity_id": {
      "deadline": "YYYY-MM-DD",
      "warnings": [
        {"key": "21/05/2026 → 23/05/2026", "html": "<p>⚠️ ...</p>"}
      ]
    }
  }

Snapshot is persisted to {DATA_DIR}/deadline_snapshot.json so state
survives container restarts.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests

from config import Config

logger = logging.getLogger(__name__)

# ─── State ───
_watcher_running = False
_last_poll = None
_changes_detected = 0
_reappend_count = 0
_total_polls = 0

# ICT timezone offset (UTC+7)
ICT = timezone(timedelta(hours=7))

SNAPSHOT_FILE = os.path.join(
    os.getenv("DATA_DIR", "."),
    "deadline_snapshot.json",
)


# ═══════════════════════════════════════════
#  SNAPSHOT PERSISTENCE
# ═══════════════════════════════════════════

def _load_snapshot() -> dict:
    """Load the deadline snapshot from disk.

    Returns:
        dict mapping activity_id (str) → {"deadline": str, "warnings": list}
    """
    if not os.path.exists(SNAPSHOT_FILE):
        logger.info(f"[DEADLINE] No snapshot file found at {SNAPSHOT_FILE} — starting fresh")
        return {}

    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Migrate old format: {id: "date_str"} → {id: {"deadline": "date_str", "warnings": []}}
        migrated = False
        for k, v in data.items():
            if isinstance(v, str):
                data[k] = {"deadline": v, "warnings": []}
                migrated = True

        if migrated:
            logger.info(f"[DEADLINE] Migrated snapshot from old format")

        logger.info(f"[DEADLINE] Loaded snapshot: {len(data)} activities tracked")
        return data
    except Exception as e:
        logger.warning(f"[DEADLINE] Failed to load snapshot: {e} — starting fresh")
        return {}


def _save_snapshot(snapshot: dict):
    """Persist the snapshot to disk."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_FILE) or ".", exist_ok=True)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[DEADLINE] Failed to save snapshot: {e}")


# ═══════════════════════════════════════════
#  ODOO JSON-RPC HELPER
# ═══════════════════════════════════════════

def _odoo_call(model: str, method: str, args: list = None, kwargs: dict = None):
    """Execute Odoo JSON-RPC call using main credentials."""
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [
                Config.ODOO_DB,
                Config.ODOO_UID,
                Config.ODOO_API_KEY,
                model,
                method,
                args or [],
                kwargs or {},
            ],
        },
        "id": 1,
    }
    r = requests.post(f"{Config.ODOO_URL}/jsonrpc", json=payload, timeout=30)
    res = r.json()
    if "error" in res:
        raise Exception(
            res["error"].get("data", {}).get("message", str(res["error"]))
        )
    return res.get("result")


# ═══════════════════════════════════════════
#  CORE POLLING LOGIC
# ═══════════════════════════════════════════

def _poll_and_check(snapshot: dict) -> dict:
    """Poll Odoo for all activities, detect deadline extensions,
    and re-append warnings that were deleted by users.

    Args:
        snapshot: Current snapshot {id_str: {"deadline": str, "warnings": list}}

    Returns:
        Updated snapshot dict.
    """
    global _changes_detected, _reappend_count, _total_polls, _last_poll

    _total_polls += 1
    now_ict = datetime.now(ICT)
    _last_poll = now_ict.strftime("%Y-%m-%d %H:%M:%S ICT")

    # Fetch ALL activities
    activities = _odoo_call(
        "mail.activity",
        "search_read",
        [[]],
        {
            "fields": ["id", "date_deadline", "note", "write_uid"],
        },
    ) or []

    current_ids = set()
    new_warnings = []

    for act in activities:
        act_id = str(act["id"])
        new_date = act["date_deadline"]  # "YYYY-MM-DD"
        current_note = act.get("note") or ""
        current_ids.add(act_id)

        if act_id not in snapshot:
            # New activity — record it, do NOT append note
            snapshot[act_id] = {"deadline": new_date, "warnings": []}
            continue

        entry = snapshot[act_id]
        old_date = entry["deadline"]

        # ─── Check for deadline extension ───
        if new_date != old_date:
            if new_date > old_date:
                # Deadline pushed forward → create new warning
                write_user = act.get("write_uid")
                user_name = write_user[1] if isinstance(write_user, (list, tuple)) and len(write_user) > 1 else "Unknown"

                warning = _build_warning(old_date, new_date, user_name)
                new_warnings.append({
                    "id": act["id"],
                    "warning": warning,
                })
                entry["warnings"].append(warning)
                logger.info(
                    f"[DEADLINE] Detected extension: activity #{act['id']} "
                    f"{old_date} → {new_date} (by {user_name})"
                )
            else:
                logger.debug(
                    f"[DEADLINE] Deadline moved earlier: activity #{act['id']} "
                    f"{old_date} → {new_date} — skipping note"
                )

            # Update deadline regardless of direction
            entry["deadline"] = new_date

        # ─── Check for deleted warnings ───
        if entry["warnings"]:
            missing = [w for w in entry["warnings"] if w["key"] not in current_note]
            if missing:
                for w in missing:
                    logger.info(
                        f"[DEADLINE] Warning deleted by user on activity #{act['id']}: "
                        f"{w['key']} — re-appending"
                    )
                _reappend_warnings(act["id"], missing)
                _reappend_count += len(missing)

    # Apply NEW warnings (first-time appends)
    for item in new_warnings:
        _write_warning_to_note(item["id"], item["warning"])
        _changes_detected += 1

    # Clean up: remove activities from snapshot that no longer exist
    stale_ids = set(snapshot.keys()) - current_ids
    if stale_ids:
        for sid in stale_ids:
            del snapshot[sid]
        logger.debug(f"[DEADLINE] Cleaned {len(stale_ids)} stale entries from snapshot")

    if new_warnings:
        logger.info(f"[DEADLINE] Poll #{_total_polls}: {len(new_warnings)} new deadline extension(s)")

    return snapshot


def _build_warning(old_date: str, new_date: str, user: str) -> dict:
    """Build a warning dict with a stable key for detection and HTML for display."""
    try:
        old_fmt = datetime.strptime(old_date, "%Y-%m-%d").strftime("%d/%m/%Y")
        new_fmt = datetime.strptime(new_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        old_fmt = old_date
        new_fmt = new_date

    now_ict = datetime.now(ICT).strftime("%d/%m/%Y %H:%M")

    # The key is used for deletion detection — it's the unique part of the warning
    key = f"{old_fmt} → {new_fmt}"

    html = (
        f'<p>⚠️ <strong>ĐÃ ĐỔI DEADLINE</strong>: '
        f'{key} '
        f'(bởi {user} lúc {now_ict})</p>'
    )

    return {"key": key, "html": html}


def _write_warning_to_note(act_id: int, warning: dict):
    """Append a single warning to an activity's note (fresh read + write)."""
    try:
        fresh = _odoo_call(
            "mail.activity",
            "search_read",
            [[["id", "=", act_id]]],
            {"fields": ["note"], "limit": 1},
        )
        current_note = (fresh[0].get("note") if fresh else None) or ""
    except Exception as e:
        logger.error(f"[DEADLINE] Failed to re-read note for activity #{act_id}: {e}")
        current_note = ""

    # Don't double-append if warning is already there
    if warning["key"] in current_note:
        return

    if current_note and current_note.strip():
        new_note = current_note.rstrip() + "\n" + warning["html"]
    else:
        new_note = warning["html"]

    try:
        _odoo_call(
            "mail.activity",
            "write",
            [[act_id], {"note": new_note}],
        )
        logger.info(f"[DEADLINE] Appended note to activity #{act_id}: {warning['key']}")
    except Exception as e:
        logger.error(f"[DEADLINE] Failed to write note on activity #{act_id}: {e}")


def _reappend_warnings(act_id: int, warnings: list):
    """Re-append multiple deleted warnings to an activity's note."""
    try:
        fresh = _odoo_call(
            "mail.activity",
            "search_read",
            [[["id", "=", act_id]]],
            {"fields": ["note"], "limit": 1},
        )
        current_note = (fresh[0].get("note") if fresh else None) or ""
    except Exception as e:
        logger.error(f"[DEADLINE] Failed to re-read note for activity #{act_id}: {e}")
        current_note = ""

    # Collect only truly missing warnings
    to_append = [w for w in warnings if w["key"] not in current_note]
    if not to_append:
        return

    new_note = current_note.rstrip() if current_note and current_note.strip() else ""
    for w in to_append:
        new_note = (new_note + "\n" + w["html"]) if new_note else w["html"]

    try:
        _odoo_call(
            "mail.activity",
            "write",
            [[act_id], {"note": new_note}],
        )
        logger.info(
            f"[DEADLINE] Re-appended {len(to_append)} deleted warning(s) "
            f"to activity #{act_id}"
        )
    except Exception as e:
        logger.error(f"[DEADLINE] Failed to re-append on activity #{act_id}: {e}")


# ═══════════════════════════════════════════
#  BACKGROUND THREAD
# ═══════════════════════════════════════════

def _watcher_loop():
    """Background loop: poll every DEADLINE_POLL_INTERVAL seconds."""
    global _watcher_running
    _watcher_running = True

    interval = Config.DEADLINE_POLL_INTERVAL
    logger.info(f"[DEADLINE] Watcher loop starting — interval: {interval}s")

    # Initial delay to let the server finish starting
    time.sleep(10)

    # Load snapshot
    snapshot = _load_snapshot()

    # First run: if snapshot is empty, seed it without triggering notes
    if not snapshot:
        logger.info("[DEADLINE] First run — seeding snapshot (no notes will be appended)")
        try:
            activities = _odoo_call(
                "mail.activity",
                "search_read",
                [[]],
                {"fields": ["id", "date_deadline"]},
            ) or []
            for act in activities:
                snapshot[str(act["id"])] = {"deadline": act["date_deadline"], "warnings": []}
            _save_snapshot(snapshot)
            logger.info(f"[DEADLINE] Seeded snapshot with {len(snapshot)} activities")
        except Exception as e:
            logger.error(f"[DEADLINE] Failed to seed snapshot: {e}")

    while _watcher_running:
        try:
            snapshot = _poll_and_check(snapshot)
            _save_snapshot(snapshot)
        except Exception as e:
            logger.error(f"[DEADLINE] Poll error: {e}")

        # Sleep in small chunks so we can stop cleanly
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and _watcher_running:
            remaining = deadline - time.monotonic()
            chunk = min(5, max(0, remaining))
            if chunk > 0:
                time.sleep(chunk)


def start_deadline_watcher():
    """Start the deadline watcher background thread (call once at app startup)."""
    if not Config.ODOO_UID or not Config.ODOO_API_KEY:
        logger.warning("[DEADLINE] ODOO_UID or ODOO_API_KEY not set — watcher disabled")
        return

    thread = threading.Thread(
        target=_watcher_loop,
        daemon=True,
        name="deadline-watcher",
    )
    thread.start()
    logger.info(
        f"[DEADLINE] Watcher started — polling every {Config.DEADLINE_POLL_INTERVAL}s"
    )


def get_deadline_watcher_status() -> dict:
    """Get watcher status for health check endpoint."""
    return {
        "active": _watcher_running,
        "poll_interval_seconds": Config.DEADLINE_POLL_INTERVAL,
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_changes_detected": _changes_detected,
        "total_reappends": _reappend_count,
    }
