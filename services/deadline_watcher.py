"""
Deadline Watcher — Background Polling Service.

Monitors mail.activity records for deadline extensions (date pushed forward).
When a user edits an activity's Due Date to a LATER date, this service
automatically appends a warning line to the activity's Note field.

Features:
  - Deadline extension detection: append "⚠️ ĐÃ ĐỔI DEADLINE"
  - Anti-deletion: re-append warnings and edit logs if user deletes them
  - Edit tracking: append "✏️ EDITED BY" when user modifies note content

Poll cycle per activity (order matters):
  1. Restore deleted warnings and edit logs (write to Odoo, update local note)
  2. After restorations settle, compare note_hash for genuine new edits
     - Skip if any restoration happened this cycle (wait for next cycle)
  3. Check deadline changes

Scenario coverage:
  - Delete Warning A + Edit C → restore A, restore C, skip edit (restored flag)
  - Delete Warning A + Edit C + add new text → Cycle 1: restore A+C, skip edit.
    Cycle 2: hash mismatch from new text → create Edit E

Snapshot format:
  {
    "activity_id": {
      "deadline": "YYYY-MM-DD",
      "warnings": [{"key": "...", "html": "..."}],
      "edit_logs": [{"key": "...", "html": "..."}],
      "note_hash": "md5hex"
    }
  }

Snapshot is persisted to {DATA_DIR}/deadline_snapshot.json so state
survives container restarts.
"""

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests

from config import Config

logger = logging.getLogger(__name__)

# ─── Odoo credentials (loaded from Config / .env) ───
_WATCHER_ODOO_URL = Config.ODOO_URL
_WATCHER_ODOO_DB = Config.ODOO_DB
_WATCHER_ODOO_UID = Config.ODOO_UID
_WATCHER_ODOO_API_KEY = Config.ODOO_API_KEY

# ─── Runtime state ───
_watcher_running = False
_last_poll = None
_changes_detected = 0
_reappend_count = 0
_edit_count = 0
_total_polls = 0

# ICT timezone offset (UTC+7)
ICT = timezone(timedelta(hours=7))

SNAPSHOT_FILE = os.path.join(
    os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")),
    "deadline_snapshot.json",
)


# ═══════════════════════════════════════════
#  SNAPSHOT PERSISTENCE
# ═══════════════════════════════════════════

def _load_snapshot() -> dict:
    """Load snapshot from disk. Returns dict {id_str: entry_dict}."""
    if not os.path.exists(SNAPSHOT_FILE):
        logger.info(f"[DEADLINE] No snapshot file — starting fresh")
        return {}
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migrate old flat format
        for k, v in data.items():
            if isinstance(v, str):
                data[k] = {"deadline": v, "warnings": [], "edit_logs": [], "note_hash": None}
        logger.info(f"[DEADLINE] Loaded snapshot: {len(data)} activities")
        return data
    except Exception as e:
        logger.warning(f"[DEADLINE] Failed to load snapshot: {e} — starting fresh")
        return {}


def _save_snapshot(snapshot: dict):
    """Persist snapshot to disk."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_FILE) or ".", exist_ok=True)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[DEADLINE] Failed to save snapshot: {e}")


def _note_hash(note: str) -> str:
    """MD5 hash of note content for change detection."""
    return hashlib.md5(note.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════
#  ODOO JSON-RPC
# ═══════════════════════════════════════════

def _odoo_call(model: str, method: str, args: list = None, kwargs: dict = None):
    """Execute Odoo JSON-RPC call."""
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [
                _WATCHER_ODOO_DB,
                _WATCHER_ODOO_UID,
                _WATCHER_ODOO_API_KEY,
                model, method,
                args or [], kwargs or {},
            ],
        },
        "id": 1,
    }
    r = requests.post(f"{_WATCHER_ODOO_URL}/jsonrpc", json=payload, timeout=30)
    res = r.json()
    if "error" in res:
        raise Exception(res["error"].get("data", {}).get("message", str(res["error"])))
    return res.get("result")


def _fresh_read_note(act_id: int) -> str:
    """Read note field fresh from Odoo (avoids stale data)."""
    try:
        rows = _odoo_call(
            "mail.activity", "search_read",
            [[["id", "=", act_id]]],
            {"fields": ["note"], "limit": 1},
        )
        return (rows[0].get("note") if rows else None) or ""
    except Exception as e:
        logger.error(f"[DEADLINE] Failed to read note for #{act_id}: {e}")
        return ""


def _write_note(act_id: int, note: str) -> bool:
    """Write note field to Odoo. Returns True on success."""
    try:
        _odoo_call("mail.activity", "write", [[act_id], {"note": note}])
        return True
    except Exception as e:
        logger.error(f"[DEADLINE] Failed to write note for #{act_id}: {e}")
        return False


# ═══════════════════════════════════════════
#  HTML BUILDERS
# ═══════════════════════════════════════════

def _build_warning(old_date: str, new_date: str, user: str) -> dict:
    """Build deadline warning with stable key for detection."""
    try:
        old_fmt = datetime.strptime(old_date, "%Y-%m-%d").strftime("%d/%m/%Y")
        new_fmt = datetime.strptime(new_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        old_fmt, new_fmt = old_date, new_date

    now_ict = datetime.now(ICT).strftime("%d/%m/%Y %H:%M")
    key = f"{old_fmt} → {new_fmt}"
    html = f'<p>⚠️ <strong>ĐÃ ĐỔI DEADLINE</strong>: {key} (bởi {user} lúc {now_ict})</p>'
    return {"key": key, "html": html}


def _build_edit_log(user_name: str) -> dict:
    """Build edit log entry with stable key for detection."""
    now_ict = datetime.now(ICT).strftime("%d/%m/%Y %H:%M")
    key = f"{user_name} \u2014 {now_ict}"
    html = f'<p>✏️ <strong>EDITED BY</strong>: {key}</p>'
    return {"key": key, "html": html}


# ═══════════════════════════════════════════
#  NOTE APPEND HELPERS
# ═══════════════════════════════════════════

def _append_to_note(act_id: int, items: list[dict]) -> str | None:
    """Fresh-read note, append items whose key is missing, write back.

    Args:
        act_id: Odoo activity ID
        items: list of {"key": str, "html": str} dicts

    Returns:
        The new note content after write, or None if nothing changed / error.
    """
    note = _fresh_read_note(act_id)
    to_add = [it for it in items if it["key"] not in note]
    if not to_add:
        return None

    new_note = note.rstrip() if note and note.strip() else ""
    for it in to_add:
        new_note = (new_note + "\n" + it["html"]) if new_note else it["html"]

    if _write_note(act_id, new_note):
        labels = [it["key"] for it in to_add]
        logger.info(f"[DEADLINE] Appended to #{act_id}: {labels}")
        return new_note
    return None


# ═══════════════════════════════════════════
#  CORE POLL LOGIC
# ═══════════════════════════════════════════

def _poll_and_check(snapshot: dict) -> dict:
    """Single poll cycle. For each activity:

    1. Restore deleted warnings (write to Odoo)
    2. Clean deleted edit logs from snapshot (no write, no restore)
    3. If NO restoration happened → check note hash for genuine edits
    4. Check deadline extension

    Returns updated snapshot.
    """
    global _changes_detected, _reappend_count, _edit_count, _total_polls, _last_poll

    _total_polls += 1
    _last_poll = datetime.now(ICT).strftime("%Y-%m-%d %H:%M:%S ICT")

    activities = _odoo_call(
        "mail.activity", "search_read", [[]],
        {"fields": ["id", "date_deadline", "note", "write_uid"]},
    ) or []

    current_ids = set()
    poll_extensions = 0
    poll_edits = 0

    for act in activities:
        act_id = str(act["id"])
        new_date = act["date_deadline"]
        current_note = act.get("note") or ""
        current_ids.add(act_id)

        # ─── New activity: seed, skip ───
        if act_id not in snapshot:
            snapshot[act_id] = {
                "deadline": new_date,
                "warnings": [],
                "edit_logs": [],
                "note_hash": _note_hash(current_note),
            }
            continue

        entry = snapshot[act_id]
        old_date = entry["deadline"]
        did_restore = False

        # ─── Step 1: Restore deleted warnings & edit logs (Anti-deletion) ───
        to_restore = []

        warnings = entry.get("warnings", [])
        missing_w = [w for w in warnings if w["key"] not in current_note]
        if missing_w:
            for w in missing_w:
                logger.info(f"[DEADLINE] Warning deleted on #{act['id']}: {w['key']} — restoring")
            to_restore.extend(missing_w)
            did_restore = True

        edit_logs = entry.get("edit_logs", [])
        missing_e = [e for e in edit_logs if e["key"] not in current_note]
        if missing_e:
            for e in missing_e:
                logger.info(f"[DEADLINE] Edit log deleted on #{act['id']}: {e['key']} — restoring")
            to_restore.extend(missing_e)
            did_restore = True

        if to_restore:
            result = _append_to_note(act["id"], to_restore)
            if result is not None:
                current_note = result
            _reappend_count += len(to_restore)

        # ─── Step 3: Genuine note edit detection ───
        current_hash = _note_hash(current_note)
        old_hash = entry.get("note_hash")

        if not did_restore and old_hash is not None and old_hash != current_hash:
            # Note changed by user (not by us) → log the edit
            user_name = _get_user_name(act)
            edit_log = _build_edit_log(user_name)
            result = _append_to_note(act["id"], [edit_log])
            if result is not None:
                current_note = result
                current_hash = _note_hash(current_note)
            entry.setdefault("edit_logs", []).append(edit_log)
            poll_edits += 1
            _edit_count += 1
            logger.info(f"[DEADLINE] Note edited on #{act['id']} by {user_name}")

        # ─── Step 4: Deadline extension ───
        if new_date != old_date:
            if new_date > old_date:
                user_name = _get_user_name(act)
                warning = _build_warning(old_date, new_date, user_name)
                result = _append_to_note(act["id"], [warning])
                if result is not None:
                    current_note = result
                    current_hash = _note_hash(current_note)
                entry.setdefault("warnings", []).append(warning)
                poll_extensions += 1
                _changes_detected += 1
                logger.info(f"[DEADLINE] Extension: #{act['id']} {old_date} → {new_date} (by {user_name})")
            else:
                logger.debug(f"[DEADLINE] Earlier: #{act['id']} {old_date} → {new_date} — skip")
            entry["deadline"] = new_date

        # ─── Update hash ───
        # If we restored this cycle, DON'T update hash.
        # This way, if user also added new content alongside deletions,
        # Cycle 2 will see the hash mismatch and create an edit log.
        if not did_restore:
            entry["note_hash"] = _note_hash(current_note)

    # ─── Cleanup stale entries ───
    stale_ids = set(snapshot.keys()) - current_ids
    for sid in stale_ids:
        del snapshot[sid]
    if stale_ids:
        logger.debug(f"[DEADLINE] Cleaned {len(stale_ids)} stale entries")

    if poll_extensions or poll_edits:
        logger.info(f"[DEADLINE] Poll #{_total_polls}: {poll_extensions} extension(s), {poll_edits} edit(s)")

    return snapshot


def _get_user_name(act: dict) -> str:
    """Extract user display name from write_uid field."""
    wu = act.get("write_uid")
    return wu[1] if isinstance(wu, (list, tuple)) and len(wu) > 1 else "Unknown"


# ═══════════════════════════════════════════
#  BACKGROUND THREAD
# ═══════════════════════════════════════════

def _watcher_loop():
    """Background loop: poll every DEADLINE_POLL_INTERVAL seconds."""
    global _watcher_running
    _watcher_running = True

    interval = Config.DEADLINE_POLL_INTERVAL
    logger.info(f"[DEADLINE] Watcher starting — interval: {interval}s")

    time.sleep(10)  # Let server finish starting

    snapshot = _load_snapshot()

    # First run: seed without triggering
    if not snapshot:
        logger.info("[DEADLINE] First run — seeding snapshot")
        try:
            activities = _odoo_call(
                "mail.activity", "search_read", [[]],
                {"fields": ["id", "date_deadline", "note"]},
            ) or []
            for act in activities:
                note = act.get("note") or ""
                snapshot[str(act["id"])] = {
                    "deadline": act["date_deadline"],
                    "warnings": [],
                    "edit_logs": [],
                    "note_hash": _note_hash(note),
                }
            _save_snapshot(snapshot)
            logger.info(f"[DEADLINE] Seeded {len(snapshot)} activities")
        except Exception as e:
            logger.error(f"[DEADLINE] Seed failed: {e}")

    while _watcher_running:
        try:
            snapshot = _poll_and_check(snapshot)
            _save_snapshot(snapshot)
        except Exception as e:
            logger.error(f"[DEADLINE] Poll error: {e}")

        # Interruptible sleep
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and _watcher_running:
            time.sleep(min(5, max(0, deadline - time.monotonic())))


def start_deadline_watcher():
    """Start the deadline watcher background thread."""
    if not _WATCHER_ODOO_UID or not _WATCHER_ODOO_API_KEY:
        logger.warning("[DEADLINE] Credentials not set — watcher disabled")
        return

    thread = threading.Thread(target=_watcher_loop, daemon=True, name="deadline-watcher")
    thread.start()
    logger.info(f"[DEADLINE] Watcher started — every {Config.DEADLINE_POLL_INTERVAL}s")


def get_deadline_watcher_status() -> dict:
    """Health check status."""
    return {
        "active": _watcher_running,
        "poll_interval_seconds": Config.DEADLINE_POLL_INTERVAL,
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_changes_detected": _changes_detected,
        "total_reappends": _reappend_count,
        "total_note_edits": _edit_count,
    }
