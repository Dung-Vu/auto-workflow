# External OP round robin migration

`services/op_round_robin.py` replaces the polling behavior in the legacy
`op-round-robin/app/main.py`; it installs no Odoo code or automation.
Importing it does not start threads, register signals or contact Odoo.

## Contract

- `sale.order` draft + unset `x_studio_op_in_charge` only, ascending order ID.
- OP relation IDs **23 → 6 → 27**, never 25; these are **not res.users IDs**.
- Global rotation across ALL companies, including unknown-company fallback.
  Missing company skips assignment.
- For EACH candidate, find the highest order ID with ANY assigned OP, excluding
  the candidate, across all companies and states. Next eligible OP follows it;
  absent/excluded/unrecognized last OP restarts at 23. No timestamp cursor.
  A higher-ID assigned order therefore can cause several older drafts to receive
  the same next OP: this intentional legacy behavior is preserved.
- Optional `OP_ROUND_ROBIN_EXCLUDE_OP_ID` excludes one further ID (default 25).
- Recheck draft/unset immediately before write, reducing stale-poll overwrites.
  This is NOT an atomic compare-and-set: a concurrent human/worker can still
  change the order between recheck and write. Single-writer cutover is mandatory.

## Configuration and lifecycle

`OP_ROUND_ROBIN_ENABLED=false` by default. Explicitly enable after cutover approval.
Poll 30s + 0–15s jitter; initial stagger 10–30s. Optional separate
`OP_ROUND_ROBIN_ODOO_UID` / `OP_ROUND_ROBIN_ODOO_API_KEY` fall back to existing Odoo
credentials; use the same visibility/permissions as the legacy worker to retain
its effective global scope. RPC socket timeout defaults to 30s. No transparent
RPC retries/replays (an ambiguous write is reconciled by the next poll).

Errors abort the remaining batch, increment the consecutive error count and
back off: ordinary 2^n seconds capped at 600; 429 uses max(60, 30*2^min(n,4)),
capped at 600. Both add up to 25% jitter (legacy cap applies before jitter).
Proxy recreation begins after five consecutive errors. Success resets errors.
Shutdown interrupts stagger/backoff/idle and stops between orders; an in-flight
order may finish its remaining calls. Socket timeouts are not a strict end-to-end
wall-clock deadline (DNS or a trickling peer can take longer).

App `__main__` starts the worker alongside existing daemons and stops it on
SIGTERM/SIGINT/atexit. `ENVIRONMENT=test` or `ZNS_DISABLE_BACKGROUND_DAEMONS=true`
suppresses startup. Import-only WSGI does not start it, consistent with the app's
existing lifecycle. `/health` adds `op_round_robin` with enabled/running/stopping,
OP IDs, consecutive error count and last successful poll; no secrets or RPC
fault text. Health remains the app's existing summary, not a readiness gate.

Importing with OP disabled also works on platforms without `fcntl` (e.g. native
Windows). Enabling the worker there fails closed before any file, thread or RPC
is created; there is no unlocked fallback. Worker operation requires `fcntl`.

A thread lock and nonblocking filesystem flock prevent repeated starts and
multiple workers sharing `OP_ROUND_ROBIN_LOCK_PATH` (default
`DATA_DIR/op_round_robin.lock`). Keep the same shared volume/path across replicas.
The lease remains held until the worker truly exits, even if bounded shutdown
join expires. This cannot coordinate the legacy service, separate volumes or
independent hosts without a shared locking filesystem.

## Parent-controlled cutover (not executed here)

1. Review/test and reconcile the deployed CS-handover baseline before release.
2. Deploy this implementation with `OP_ROUND_ROBIN_ENABLED=false`; check health.
3. Stop/disable legacy op-round-robin (including restart policy and any replicas),
   confirm its process and in-flight requests have ended. Retain source for comparison.
4. Set `OP_ROUND_ROBIN_ENABLED=true`, `OP_ROUND_ROBIN_EXCLUDE_OP_ID=25`,
   `OP_ROUND_ROBIN_POLL_INTERVAL=30`, `OP_ROUND_ROBIN_POLL_JITTER=15`;
   use matching legacy DB/credentials and a shared DATA_DIR lock path.
5. Restart auto-workflow; confirm only one active worker, health running, and
   observe assignments without adding an alternate writer.
6. Rollback: disable/restart auto-workflow and confirm worker termination BEFORE
   restarting legacy. Never overlap the two workers.

This lane permits local task commits and merging fetched `origin/main` only.
Preserve the original CS-handover baseline (`e650fa3`) in branch ancestry and
retain upstream `COMMISSION_REVENUE_SKIP_SO_IDS`. No push, GitHub merge, Odoo
writes or deployment is authorized here. Production inventory/drift reconciliation
and independent review remain parent-controlled release gates.

## Hermetic checks

Run `python -m pytest -q tests/test_op_round_robin.py` using the project's venv.
Tests use local fake RPC only. Do not run live Odoo/API tests for this migration.
