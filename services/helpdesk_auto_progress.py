"""Odoo Helpdesk Auto Progress Watcher.

Periodically inspects helpdesk.ticket records and transitions tickets
remaining in stage "New" for a random wait time to "In Progress".
Each ticket gets its own wait between MIN and MAX minutes (default 10-25),
derived from the ticket id so it stays the same across polls and restarts.

Key design invariants:
1. No backfill on initial activation: On first start or missing/invalid state,
   a UTC activation cutoff is generated and persisted before any query or write.
   Zero writes are emitted on first startup, ensuring historical New tickets
   are never backfilled.
2. Safe restarts: On subsequent service restarts, the persistent cutoff is loaded
   from disk so tickets created during or before restart (post-activation) remain eligible.
3. Pre-write stage re-check: Immediately before writing, the ticket is re-read.
   If a user or automation moved the ticket out of New, it is skipped and never
   moved back.
4. Mutation minimalism: The only write is {'stage_id': IN_PROGRESS_STAGE_ID}.
   No chatter messages, team changes, or assignee updates are performed.
5. Error isolation & resilience: Single-ticket failures do not block other tickets;
   the loop survives transient Odoo/network exceptions with exponential backoff.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from config import Config
from services.odoo_client import odoo

logger = logging.getLogger(__name__)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ─── Runtime status counters & state ───
_watcher_running = False
_watcher_thread: threading.Thread | None = None
_last_poll: str | None = None
_total_polls = 0
_total_moved = 0
_total_skipped_not_new = 0
_total_errors = 0
_activation_cutoff: str | None = None


def _utc_now_str() -> str:
    """Return current UTC timestamp matching Odoo datetime string format."""
    return datetime.now(timezone.utc).strftime(DATE_FORMAT)


def _load_state() -> dict | None:
    """Load persistent state from disk. Returns None if file is missing or invalid."""
    state_path = Config.HELPDESK_AUTO_PROGRESS_STATE_PATH
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        cutoff = data.get("cutoff")
        if not cutoff or not isinstance(cutoff, str):
            return None
        # Validate format
        datetime.strptime(cutoff, DATE_FORMAT)
        return data
    except Exception as exc:
        logger.warning(
            f"[HELPDESK-PROGRESS] Invalid or corrupt state file ({exc}); will reseed"
        )
        return None


def _save_state(state: dict) -> bool:
    """Atomically save state file using a temporary file and fsync."""
    state_path = Config.HELPDESK_AUTO_PROGRESS_STATE_PATH
    try:
        dir_path = os.path.dirname(state_path) or "."
        os.makedirs(dir_path, exist_ok=True)
        temp_file = f"{state_path}.tmp.{os.getpid()}"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, state_path)
        return True
    except Exception as exc:
        logger.error(
            f"[HELPDESK-PROGRESS] Failed to atomically save state to {state_path}: {exc}"
        )
        return False


def _ticket_wait_minutes(ticket_id: int, min_wait: int, max_wait: int) -> int:
    """Return a pseudo-random wait in [min_wait, max_wait] that is stable per ticket.

    Derived from a hash of the ticket id, so the same ticket always gets the
    same wait on every poll and after restarts (no re-rolling each cycle).
    """
    lo, hi = min(min_wait, max_wait), max(min_wait, max_wait)
    digest = hashlib.sha256(f"helpdesk-auto-progress:{ticket_id}".encode()).digest()
    return lo + int.from_bytes(digest[:8], "big") % (hi - lo + 1)


def _find_eligible_tickets(
    cutoff: str, min_wait: int, max_wait: int, team_ids: list[int]
) -> list[dict]:
    """Search for tickets created after activation cutoff, still in New stage,

    whose own random wait (between min_wait and max_wait) has elapsed.
    """
    now_utc = datetime.now(timezone.utc)
    # Pre-filter in Odoo with the shortest possible wait, then apply each
    # ticket's own wait below.
    eligible_before_str = (
        now_utc - timedelta(minutes=min(min_wait, max_wait))
    ).strftime(DATE_FORMAT)

    domain = [
        ("create_date", ">", cutoff),
        ("stage_id", "=", Config.HELPDESK_AUTO_PROGRESS_NEW_STAGE_ID),
        ("create_date", "<=", eligible_before_str),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))

    # Bounded batch and deterministic ordering
    candidates = odoo.search_read(
        "helpdesk.ticket",
        domain,
        fields=["id", "stage_id", "create_date"],
        limit=200,
        order="create_date asc, id asc",
    ) or []

    due: list[dict] = []
    for ticket in candidates:
        ticket_id = ticket.get("id")
        created_raw = ticket.get("create_date")
        if not ticket_id or not created_raw:
            continue
        try:
            created = datetime.strptime(str(created_raw)[:19], DATE_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.warning(
                f"[HELPDESK-PROGRESS] Ticket #{ticket_id} has unparseable create_date "
                f"{created_raw!r}; skipping"
            )
            continue
        wait = _ticket_wait_minutes(ticket_id, min_wait, max_wait)
        if created <= now_utc - timedelta(minutes=wait):
            ticket["_wait_minutes"] = wait
            due.append(ticket)
        if len(due) >= 50:
            break
    return due


def _process_candidate_tickets(tickets: list[dict]) -> None:
    """Validate stage right before write and update stage to In Progress."""
    global _total_moved, _total_skipped_not_new, _total_errors
    new_stage_id = Config.HELPDESK_AUTO_PROGRESS_NEW_STAGE_ID
    target_stage_id = Config.HELPDESK_AUTO_PROGRESS_IN_PROGRESS_STAGE_ID

    for ticket in tickets:
        ticket_id = ticket.get("id")
        if not ticket_id:
            continue
        try:
            # Immediately before write: re-read the ticket to verify it is still in New stage
            current = odoo.read("helpdesk.ticket", [ticket_id], fields=["stage_id"])
            if not current:
                logger.warning(
                    f"[HELPDESK-PROGRESS] Ticket #{ticket_id} could not be read; skipping"
                )
                _total_skipped_not_new += 1
                continue

            raw_stage = current[0].get("stage_id")
            current_stage_id = (
                raw_stage[0] if isinstance(raw_stage, (list, tuple)) else raw_stage
            )

            if current_stage_id != new_stage_id:
                logger.info(
                    f"[HELPDESK-PROGRESS] Ticket #{ticket_id} no longer in New stage "
                    f"(current stage={current_stage_id}) — skipping"
                )
                _total_skipped_not_new += 1
                continue

            # Still in New stage: write only {'stage_id': configured In Progress ID}
            odoo.write(
                "helpdesk.ticket",
                [ticket_id],
                {"stage_id": target_stage_id},
            )
            _total_moved += 1
            logger.info(
                f"[HELPDESK-PROGRESS] Ticket #{ticket_id} auto-progressed from "
                f"stage {current_stage_id} to stage {target_stage_id} "
                f"after {ticket.get('_wait_minutes', '?')}m wait"
            )
        except Exception as exc:
            _total_errors += 1
            logger.error(
                f"[HELPDESK-PROGRESS] Failed to process ticket #{ticket_id}: {exc}"
            )


def _watcher_loop() -> None:
    """Background polling loop with fail-closed state seeding and backoff."""
    global _watcher_running, _last_poll, _total_polls, _total_errors, _activation_cutoff

    interval = Config.HELPDESK_AUTO_PROGRESS_POLL_INTERVAL
    min_wait = Config.HELPDESK_AUTO_PROGRESS_MIN_WAIT_MINUTES
    max_wait = Config.HELPDESK_AUTO_PROGRESS_MAX_WAIT_MINUTES
    team_ids = Config.HELPDESK_AUTO_PROGRESS_TEAM_IDS

    logger.info(
        f"[HELPDESK-PROGRESS] Watcher starting — interval: {interval}s, "
        f"wait: {min_wait}-{max_wait}m (random per ticket), new_stage: {Config.HELPDESK_AUTO_PROGRESS_NEW_STAGE_ID}, "
        f"in_progress_stage: {Config.HELPDESK_AUTO_PROGRESS_IN_PROGRESS_STAGE_ID}, "
        f"teams: {team_ids if team_ids else 'all'}"
    )

    # 1. State initialization
    state = _load_state()
    is_first_start = False
    if state is None:
        # First run: seed fresh UTC activation cutoff
        cutoff_str = _utc_now_str()
        state = {
            "cutoff": cutoff_str,
            "activated_at": cutoff_str,
        }
        # Fail closed: must persist before querying/writing
        persisted = False
        while _watcher_running:
            if _save_state(state):
                persisted = True
                break
            logger.error(
                "[HELPDESK-PROGRESS] Initial cutoff persistence failed, fail closed — retrying in 10s"
            )
            time.sleep(10)
        if not persisted or not _watcher_running:
            return
        is_first_start = True
        logger.info(
            f"[HELPDESK-PROGRESS] Initialized activation cutoff at {cutoff_str} UTC; "
            f"historical tickets excluded (zero writes on first startup)"
        )

    _activation_cutoff = state["cutoff"]

    # Short stagger delay to prevent boot burst
    deadline = time.monotonic() + 5.0
    while _watcher_running and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    consecutive_errors = 0
    max_backoff = max(interval * 5, 300)

    while _watcher_running:
        _total_polls += 1
        _last_poll = _utc_now_str()
        try:
            if is_first_start:
                # Mandatory first-start guard: emit zero writes on first cycle
                logger.info(
                    "[HELPDESK-PROGRESS] First cycle after seeding: zero writes emitted"
                )
                is_first_start = False
            else:
                candidates = _find_eligible_tickets(
                    _activation_cutoff, min_wait, max_wait, team_ids
                )
                if candidates:
                    logger.info(
                        f"[HELPDESK-PROGRESS] Found {len(candidates)} eligible ticket(s) to progress"
                    )
                    _process_candidate_tickets(candidates)

            consecutive_errors = 0
            delay = interval
        except Exception as exc:
            consecutive_errors += 1
            _total_errors += 1
            logger.exception(
                f"[HELPDESK-PROGRESS] Polling cycle failed (#{consecutive_errors}): {exc}"
            )
            # Exponential backoff with jitter
            backoff = min(max_backoff, interval * (2 ** min(consecutive_errors, 5)))
            jitter = random.uniform(0, min(5.0, backoff * 0.1))
            delay = backoff + jitter

        # Interruptible sleep
        deadline = time.monotonic() + delay
        while _watcher_running and time.monotonic() < deadline:
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


def start_helpdesk_auto_progress_watcher() -> None:
    """Start the Helpdesk auto progress watcher background thread idempotently."""
    global _watcher_running, _watcher_thread

    if not Config.HELPDESK_AUTO_PROGRESS_ENABLED:
        logger.info("[HELPDESK-PROGRESS] Watcher disabled in configuration")
        return

    if not Config.ODOO_UID and not Config.ODOO_USER:
        logger.warning(
            "[HELPDESK-PROGRESS] Odoo credentials not configured; watcher disabled"
        )
        return

    if _watcher_running and _watcher_thread is not None and _watcher_thread.is_alive():
        logger.info("[HELPDESK-PROGRESS] Watcher is already running")
        return

    _watcher_running = True
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        daemon=True,
        name="helpdesk-auto-progress-watcher",
    )
    _watcher_thread.start()
    logger.info(
        f"[HELPDESK-PROGRESS] Watcher started — poll interval: {Config.HELPDESK_AUTO_PROGRESS_POLL_INTERVAL}s"
    )


def get_helpdesk_auto_progress_status() -> dict:
    """Return compact status counters and configuration for /health."""
    cutoff = _activation_cutoff
    if cutoff is None:
        cached_state = _load_state()
        if cached_state:
            cutoff = cached_state.get("cutoff")

    return {
        "active": _watcher_running,
        "enabled": Config.HELPDESK_AUTO_PROGRESS_ENABLED,
        "cutoff": cutoff,
        "last_poll": _last_poll,
        "polls": _total_polls,
        "moved": _total_moved,
        "skipped_not_new": _total_skipped_not_new,
        "errors": _total_errors,
        "min_wait_minutes": Config.HELPDESK_AUTO_PROGRESS_MIN_WAIT_MINUTES,
        "max_wait_minutes": Config.HELPDESK_AUTO_PROGRESS_MAX_WAIT_MINUTES,
        "new_stage_id": Config.HELPDESK_AUTO_PROGRESS_NEW_STAGE_ID,
        "in_progress_stage_id": Config.HELPDESK_AUTO_PROGRESS_IN_PROGRESS_STAGE_ID,
        "team_ids": Config.HELPDESK_AUTO_PROGRESS_TEAM_IDS,
        "poll_interval_seconds": Config.HELPDESK_AUTO_PROGRESS_POLL_INTERVAL,
    }
