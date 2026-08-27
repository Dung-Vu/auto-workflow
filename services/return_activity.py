"""
Return Activity Watcher — Background Polling Service.

When a **new** return picking is created on stock.picking (return_id set),
creates a To-Do activity "Return mới cần xem" assigned to a fixed user
(default: BON SC, TRẦN THIỆN NHÂN) as a notification / reminder for
manager followers to review.

Hard guarantee — no historical backfill:
  On first run the watcher records:
    - seed_max_id   = max existing return picking id (or 0)
    - cutoff_create_date = UTC timestamp at seed moment
  Only pickings with id > seed_max_id AND create_date > cutoff get an activity.
  Restarts re-load this snapshot; deleting snapshot re-seeds (still no backfill).

Scope:
  - Incoming returns (customer return of delivery → receipt)
  - Internal returns (e.g. CTL / warehouse internal reverse)
  - Activity is attached on the return picking itself (not the SO)
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

# ─── Config ───
_POLL_INTERVAL = Config.RETURN_ACTIVITY_POLL_INTERVAL
_ACTIVITY_TYPE_ID = Config.RETURN_ACTIVITY_TYPE_ID
_USER_IDS = Config.RETURN_ACTIVITY_USER_IDS
_SUMMARY = Config.RETURN_ACTIVITY_SUMMARY
_DEADLINE_DAYS = Config.RETURN_ACTIVITY_DEADLINE_DAYS
_PICKING_CODES = set(Config.RETURN_ACTIVITY_PICKING_CODES)

# ─── Runtime state ───
_watcher_running = False
_last_poll = None
_total_polls = 0
_total_activities_created = 0
_total_returns_seen = 0
_total_skipped_existing = 0
_total_skipped_before_cutoff = 0

# ─── Cached ir.model id for stock.picking ───
_picking_model_id = None

# ─── Snapshot persistence ───
SNAPSHOT_FILE = os.path.join(
    os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")),
    "return_activity_snapshot.json",
)


def _utc_now_str() -> str:
    """Odoo-friendly UTC datetime string (naive, second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_odoo_dt(value) -> datetime | None:
    """Parse Odoo datetime string to aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("T", " ").strip()
    # strip fractional seconds / timezone suffix if present
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s[:26].split("+")[0].split("Z")[0].strip(), fmt).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _empty_snapshot() -> dict:
    return {
        "seeded": False,
        "seed_max_id": 0,
        "cutoff_create_date": None,
        "processed_ids": [],
    }


def _load_snapshot() -> dict:
    """Load snapshot. Requires seed_max_id + cutoff for eligibility."""
    if not os.path.exists(SNAPSHOT_FILE):
        logger.info("[RETURN-ACT] No snapshot file — will seed on first poll (no backfill)")
        return _empty_snapshot()
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        snap = _empty_snapshot()
        snap["seeded"] = bool(data.get("seeded", False))
        snap["seed_max_id"] = int(data.get("seed_max_id") or 0)
        snap["cutoff_create_date"] = data.get("cutoff_create_date") or None
        snap["processed_ids"] = list(data.get("processed_ids") or [])

        # Migrate old snapshot shape (only processed_ids list) → hard cutoff now
        if snap["seeded"] and (not snap["cutoff_create_date"] or not snap["seed_max_id"]):
            ids = [int(x) for x in snap["processed_ids"] if str(x).isdigit()]
            if not snap["seed_max_id"] and ids:
                snap["seed_max_id"] = max(ids)
            if not snap["cutoff_create_date"]:
                snap["cutoff_create_date"] = _utc_now_str()
            logger.warning(
                f"[RETURN-ACT] Migrated old snapshot → seed_max_id={snap['seed_max_id']}, "
                f"cutoff={snap['cutoff_create_date']} (still no historical backfill)"
            )

        logger.info(
            f"[RETURN-ACT] Loaded snapshot: seeded={snap['seeded']}, "
            f"seed_max_id={snap['seed_max_id']}, cutoff={snap['cutoff_create_date']}, "
            f"processed={len(snap['processed_ids'])}"
        )
        return snap
    except Exception as e:
        logger.warning(f"[RETURN-ACT] Failed to load snapshot: {e} — will re-seed")
        return _empty_snapshot()


def _save_snapshot(snapshot: dict):
    """Persist snapshot; keep processed_ids list bounded."""
    try:
        ids = snapshot.get("processed_ids") or []
        if len(ids) > 5000:
            # only need recent post-seed ids for idempotency
            snapshot["processed_ids"] = ids[-5000:]
        os.makedirs(os.path.dirname(SNAPSHOT_FILE) or ".", exist_ok=True)
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[RETURN-ACT] Failed to save snapshot: {e}")


def _ensure_model_id() -> int:
    """Cache ir.model id for stock.picking (needed for mail.activity res_model_id)."""
    global _picking_model_id
    if _picking_model_id is not None:
        return _picking_model_id
    ir = odoo.search_read(
        "ir.model",
        [["model", "=", "stock.picking"]],
        fields=["id", "model"],
        limit=1,
    )
    if not ir:
        raise RuntimeError("ir.model 'stock.picking' not found")
    _picking_model_id = ir[0]["id"]
    logger.info(f"[RETURN-ACT] Cached ir.model id for stock.picking: {_picking_model_id}")
    return _picking_model_id


def _return_domain() -> list:
    """Domain for return pickings in scope (incoming + internal by default)."""
    domain = [
        ["return_id", "!=", False],
        ["state", "!=", "cancel"],
    ]
    if _PICKING_CODES:
        domain.append(["picking_type_code", "in", list(_PICKING_CODES)])
    return domain


def _is_eligible(picking: dict, snapshot: dict) -> bool:
    """
    True only if this return was created AFTER the seed moment.

    Both conditions required:
      1) id > seed_max_id
      2) create_date > cutoff_create_date  (when cutoff is set)
    """
    seed_max = int(snapshot.get("seed_max_id") or 0)
    pid = int(picking["id"])
    if pid <= seed_max:
        return False

    cutoff_raw = snapshot.get("cutoff_create_date")
    if cutoff_raw:
        created = _parse_odoo_dt(picking.get("create_date"))
        cutoff = _parse_odoo_dt(cutoff_raw)
        if created is None or cutoff is None:
            # fail closed: without parseable dates, require id > seed only already passed
            # but if cutoff set and create_date unreadable, skip to be safe
            logger.warning(
                f"[RETURN-ACT]   skip #{pid}: cannot parse dates "
                f"create_date={picking.get('create_date')!r} cutoff={cutoff_raw!r}"
            )
            return False
        if created <= cutoff:
            return False

    return True


def _fetch_candidate_returns(snapshot: dict, limit: int = 100) -> list:
    """
    Fetch only returns that can possibly be new:
      return scope AND id > seed_max_id
    """
    seed_max = int(snapshot.get("seed_max_id") or 0)
    domain = _return_domain() + [["id", ">", seed_max]]
    return odoo.search_read(
        "stock.picking",
        domain,
        fields=[
            "id",
            "name",
            "state",
            "picking_type_id",
            "picking_type_code",
            "origin",
            "partner_id",
            "sale_id",
            "return_id",
            "create_date",
            "user_id",
            "activity_ids",
        ],
        limit=limit,
        order="id asc",  # process oldest-first among new
    )


def _has_existing_activity(picking_id: int, user_id: int) -> bool:
    """True if an activity with same summary already exists on this picking for the given user."""
    existing = odoo.search(
        "mail.activity",
        [
            ["res_model", "=", "stock.picking"],
            ["res_id", "=", picking_id],
            ["summary", "=", _SUMMARY],
            ["user_id", "=", user_id],
        ],
        limit=1,
    )
    return bool(existing)


def _create_return_activities(picking: dict) -> list[int]:
    """Create To-Do activities on the return picking for ALL configured users.

    Returns list of created activity IDs.
    """
    model_id = _ensure_model_id()
    name = picking.get("name") or f"#{picking['id']}"
    origin = picking.get("origin") or ""
    partner = picking.get("partner_id")
    partner_name = (
        partner[1]
        if isinstance(partner, (list, tuple)) and len(partner) > 1
        else "?"
    )
    sale = picking.get("sale_id")
    sale_name = (
        sale[1] if isinstance(sale, (list, tuple)) and len(sale) > 1 else ""
    )
    ret_of = picking.get("return_id")
    ret_of_name = (
        ret_of[1] if isinstance(ret_of, (list, tuple)) and len(ret_of) > 1 else "?"
    )
    ptype = picking.get("picking_type_id")
    ptype_name = (
        ptype[1] if isinstance(ptype, (list, tuple)) and len(ptype) > 1 else "?"
    )

    note_parts = [
        "<p><strong>📦 Return mới cần xem</strong></p>",
        "<ul>",
        f"<li>Phiếu return: <strong>{name}</strong></li>",
        f"<li>Return of: {ret_of_name}</li>",
        f"<li>Type: {ptype_name} ({picking.get('picking_type_code') or '?'})</li>",
        f"<li>Origin: {origin or '—'}</li>",
        f"<li>Customer: {partner_name}</li>",
    ]
    if sale_name:
        note_parts.append(f"<li>SO: <strong>{sale_name}</strong></li>")
    note_parts.append("</ul>")
    note = "".join(note_parts)

    deadline = (date.today() + timedelta(days=_DEADLINE_DAYS)).isoformat()

    created_ids = []
    for uid in _USER_IDS:
        if _has_existing_activity(picking["id"], uid):
            logger.info(
                f"[RETURN-ACT]   skip {name}: activity '{_SUMMARY}' already exists for uid={uid}"
            )
            continue
        vals = {
            "res_model": "stock.picking",
            "res_model_id": model_id,
            "res_id": picking["id"],
            "activity_type_id": _ACTIVITY_TYPE_ID,
            "summary": _SUMMARY,
            "note": note,
            "date_deadline": deadline,
            "user_id": uid,
        }
        activity_id = odoo.create("mail.activity", vals)
        created_ids.append(activity_id)
        logger.info(
            f"[RETURN-ACT]   ✅ Activity #{activity_id} → uid={uid} "
            f"on {name} (return of {ret_of_name})"
        )
    return created_ids


def _seed_snapshot(snapshot: dict) -> dict:
    """
    First boot: lock the baseline. NO activities created.

    Records:
      - seed_max_id: highest return picking id currently in Odoo
      - cutoff_create_date: UTC now — only create_date AFTER this is eligible
    """
    cutoff = _utc_now_str()

    # Highest existing return id (any in scope)
    rows = odoo.search_read(
        "stock.picking",
        _return_domain(),
        fields=["id", "create_date", "name"],
        limit=1,
        order="id desc",
    )
    seed_max_id = int(rows[0]["id"]) if rows else 0

    snapshot["seeded"] = True
    snapshot["seed_max_id"] = seed_max_id
    snapshot["cutoff_create_date"] = cutoff
    snapshot["processed_ids"] = []

    logger.info(
        f"[RETURN-ACT] ✅ SEED complete — NO historical activities. "
        f"seed_max_id={seed_max_id} "
        f"(latest={rows[0].get('name') if rows else 'none'}), "
        f"cutoff_create_date={cutoff} UTC. "
        f"Only returns with id>{seed_max_id} AND create_date>{cutoff} get activities."
    )
    return snapshot


def _poll_and_create_activities(snapshot: dict) -> dict:
    """Poll for NEW return pickings (after seed) and create activities."""
    global _total_activities_created, _total_returns_seen
    global _total_skipped_existing, _total_skipped_before_cutoff, _last_poll

    poll_start = datetime.now()
    _last_poll = poll_start.strftime("%Y-%m-%d %H:%M:%S") + " ICT"

    # Must have a complete seed before creating anything
    if (
        not snapshot.get("seeded")
        or not snapshot.get("cutoff_create_date")
        or snapshot.get("seed_max_id") is None
    ):
        return _seed_snapshot(snapshot)

    processed = set(str(x) for x in (snapshot.get("processed_ids") or []))
    seed_max = int(snapshot.get("seed_max_id") or 0)

    candidates = _fetch_candidate_returns(snapshot, limit=100)
    if not candidates:
        logger.debug(
            f"[RETURN-ACT] Poll #{_total_polls}: no returns with id>{seed_max}"
        )
        return snapshot

    created_this_poll = 0
    for picking in candidates:
        pid = picking["id"]
        pid_str = str(pid)
        name = picking.get("name") or f"#{pid}"

        if pid_str in processed:
            continue

        # Hard gate: only post-seed returns
        if not _is_eligible(picking, snapshot):
            _total_skipped_before_cutoff += 1
            processed.add(pid_str)  # don't re-check forever
            logger.info(
                f"[RETURN-ACT]   skip {name} id={pid}: before cutoff "
                f"(create_date={picking.get('create_date')}, "
                f"seed_max_id={seed_max}, cutoff={snapshot.get('cutoff_create_date')})"
            )
            continue

        _total_returns_seen += 1

        try:
            created_ids = _create_return_activities(picking)
            _total_activities_created += len(created_ids)
            created_this_poll += len(created_ids)
            if not created_ids:
                _total_skipped_existing += 1
        except Exception as e:
            logger.error(f"[RETURN-ACT]   ❌ Failed on {name}: {e}")
            # Do not mark processed — retry next poll
            continue

        processed.add(pid_str)

    if created_this_poll:
        logger.info(
            f"[RETURN-ACT] Poll #{_total_polls}: created {created_this_poll} activity(ies)"
        )

    snapshot["processed_ids"] = list(processed)
    return snapshot


# ═══════════════════════════════════════════
#  BACKGROUND THREAD
# ═══════════════════════════════════════════

def _watcher_loop():
    """Background loop: poll every RETURN_ACTIVITY_POLL_INTERVAL seconds."""
    global _watcher_running, _total_polls
    _watcher_running = True

    interval = _POLL_INTERVAL
    logger.info(
        f"[RETURN-ACT] Watcher starting — interval: {interval}s, "
        f"user_ids={_USER_IDS}, summary='{_SUMMARY}', "
        f"type_id={_ACTIVITY_TYPE_ID}, codes={sorted(_PICKING_CODES)}, "
        f"deadline_days={_DEADLINE_DAYS}. "
        f"Policy: ONLY returns created AFTER seed (id>seed_max_id AND create_date>cutoff)."
    )

    time.sleep(12)  # Stagger start vs other watchers

    snapshot = _load_snapshot()
    consecutive_errors = 0
    max_backoff = max(interval * 5, 600)

    while _watcher_running:
        _total_polls += 1
        try:
            snapshot = _poll_and_create_activities(snapshot)
            _save_snapshot(snapshot)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            err_str = str(e)
            is_rate_limited = "429" in err_str or "Too Many Requests" in err_str
            if is_rate_limited:
                backoff = min(
                    max_backoff, max(90, 45 * (2 ** min(consecutive_errors, 4)))
                )
            else:
                backoff = min(max_backoff, 10 * (2 ** min(consecutive_errors, 5)))
            backoff += random.uniform(0, max(1, backoff * 0.2))
            log_level = logging.WARNING if is_rate_limited else logging.ERROR
            logger.log(
                log_level,
                f"[RETURN-ACT] Poll error (#{consecutive_errors}): {e} "
                f"— retrying in {backoff:.0f}s",
            )
            time.sleep(min(backoff, max_backoff))
            continue

        jitter = random.uniform(0, min(2.0, max(0.05, interval * 0.25)))
        deadline = time.monotonic() + interval + jitter
        while time.monotonic() < deadline and _watcher_running:
            time.sleep(
                min(0.5 if interval <= 2 else 5, max(0, deadline - time.monotonic()))
            )


def start_return_activity_watcher():
    """Start the return activity watcher background thread."""
    if not Config.RETURN_ACTIVITY_ENABLED:
        logger.warning("[RETURN-ACT] RETURN_ACTIVITY_ENABLED not true — watcher disabled")
        return
    if not Config.ODOO_UID and not Config.ODOO_USER:
        logger.warning("[RETURN-ACT] Odoo credentials not set — watcher disabled")
        return
    if not _USER_IDS:
        logger.warning("[RETURN-ACT] RETURN_ACTIVITY_USER_IDS not set — watcher disabled")
        return

    thread = threading.Thread(
        target=_watcher_loop, daemon=True, name="return-activity-watcher"
    )
    thread.start()
    logger.info(f"[RETURN-ACT] Watcher started — every {_POLL_INTERVAL}s")


def get_return_activity_status() -> dict:
    """Health check status."""
    snap = {}
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                snap = json.load(f)
    except Exception:
        pass
    return {
        "active": _watcher_running,
        "poll_interval_seconds": _POLL_INTERVAL,
        "user_ids": _USER_IDS,
        "summary": _SUMMARY,
        "activity_type_id": _ACTIVITY_TYPE_ID,
        "picking_codes": sorted(_PICKING_CODES),
        "deadline_days": _DEADLINE_DAYS,
        "policy": "only returns with id>seed_max_id AND create_date>cutoff (no historical backfill)",
        "seed_max_id": snap.get("seed_max_id"),
        "cutoff_create_date": snap.get("cutoff_create_date"),
        "seeded": snap.get("seeded"),
        "last_poll": _last_poll,
        "total_polls": _total_polls,
        "total_returns_seen": _total_returns_seen,
        "total_activities_created": _total_activities_created,
        "total_skipped_existing": _total_skipped_existing,
        "total_skipped_before_cutoff": _total_skipped_before_cutoff,
    }
