# Zalo ZNS Service Upgrade Reviewer 1 Handoff Report

> [!WARNING] **Skepticism Disclaimer**
> 100% test pass rate achieved across all 84 unit and integration tests under mock and edge-case conditions; live OAuth and ZNS token refresh must be monitored under real production network conditions.

## 1. What the prior attempt got wrong
1. **Non-Atomic Token Persistence (`_save_tokens`)**:
   - *Input*: Token refresh or OAuth authorization callback writing to JSON file during process termination or filesystem write failure.
   - *Expected*: Atomic write where file is updated via temporary file replace and fsync; original file remains uncorrupted if write fails.
   - *Actual*: Directly opened target file in `'w'` mode and dumped JSON. If interrupted or failing halfway, the file could be truncated or corrupted.
   - *Root Cause*: Lack of atomic temp-file + `os.replace` pattern with `fsync`.

2. **Unhandled Non-JSON 502/504 HTTP Errors (`oauth.zaloapp.com` & `openapi.zalo.me`)**:
   - *Input*: Cloud proxy / gateway outage returning HTML 502 Bad Gateway or 504 Gateway Timeout.
   - *Expected*: Gracefully caught non-JSON responses with structured `ValueError` detailing HTTP status code and response snippet.
   - *Actual*: Directly invoked `resp.json()`, raising raw unhandled `json.decoder.JSONDecodeError`.
   - *Root Cause*: Direct call to `resp.json()` without try/except handling and status code validation.

3. **Vulnerability to `null` or String `expires_in`**:
   - *Input*: Zalo OAuth API returning `{"expires_in": null}` or `{"expires_in": "7200"}`.
   - *Expected*: Resilient parsing defaulting to `DEFAULT_EXPIRES_IN` (90000s) on `null` and converting string integers.
   - *Actual*: `int(data.get("expires_in", DEFAULT_EXPIRES_IN))` raised `TypeError` when `"expires_in"` key was present with value `None`.
   - *Root Cause*: `.get(key, default)` returns the value (`None`) if the key exists in dict.

4. **Corrupt / Non-Dict JSON File Crash (`_load_tokens`)**:
   - *Input*: Corrupted token file containing `null` or JSON array `[]`.
   - *Expected*: Safe fallback to empty dict `{"access_token": "", "refresh_token": ""}`.
   - *Actual*: Returned raw `None` or `[]`, causing subsequent `.get()` calls to crash with `AttributeError`.
   - *Root Cause*: Lack of `isinstance(data, dict)` check in `_load_tokens`.

5. **`start_auto_refresh` Idempotency**:
   - *Input*: Multiple calls to `start_auto_refresh()` during app reloads.
   - *Expected*: Single daemon thread running at any time.
   - *Actual*: Spawned duplicate background threads on every invocation.
   - *Root Cause*: Lack of thread liveness check before spawning.

6. **Pytest Discovery Error on `tests/test_api.py`**:
   - *Input*: Running full repository test suite with `pytest`.
   - *Expected*: Clean execution of all tests.
   - *Actual*: `test_api.py::test_store` errored out with missing fixtures because the standalone script lacked `__test__ = False`.
   - *Root Cause*: Pytest test runner discovered `test_store` as a unit test.

## 2. What I changed
- `services/zalo_zns.py`:
  - Implemented atomic file saving (`_save_tokens`) using temporary files, `os.fsync`, and atomic `os.replace`.
  - Added robust response parsing with try/except around `resp.json()` across `_refresh_access_token_locked`, `send_zns`, and `handle_authorization_callback`, formatting clear descriptive errors when non-JSON 500/502/504 responses are returned.
  - Added type-safe extraction for `expires_in` handling `None`, strings, and missing fields.
  - Hardened `_load_tokens` to validate `isinstance(data, dict)` and gracefully handle corrupted JSON.
  - Added thread synchronization and idempotency guard to `start_auto_refresh()` and graceful join in `stop_auto_refresh()`.
- `tests/test_api.py`:
  - Added `__test__ = False` so pytest ignores the standalone Shopify credential checker.
- `tests/test_zalo_zns.py`:
  - Added 11 new unit tests (33 tests total, up from 22) in `TestTokenStorageAndIO`, `TestZaloAPIEdgeCases`, and auto-refresh idempotency suites.

## 3. Verification Record
- **Deep Verification (ran actual tests):**
  - `PYTHONPATH=. ./.venv/bin/pytest tests/test_zalo_zns.py` (33/33 passed in 0.87s)
  - `PYTHONPATH=. ./.venv/bin/pytest` (84/84 passed in 1.35s)
  - `PYTHONPATH=. ./.venv/bin/python -m unittest discover tests` (84/84 passed in 0.655s)
  - `PYTHONPATH=. ./.venv/bin/python tests/test_watcher_mocked.py` (all scenarios passed)
- **Shallow Verification (manual only):**
  - Inspected atomic temp file creation and cleanup behavior.
  - Validated Flask endpoints `/health`, `/webhook/hdsd-*`, `/webhook/rating*`, `/webhook/zns-done` via test client.
- **Unverified aspects:**
  - Live HTTP calls to external Zalo servers `oauth.zaloapp.com` and `business.openapi.zalo.me` (requires live credentials).

## 4. Known Issues
- `None` (All 84 unit and integration tests pass cleanly; zero regressions).

## 5. Remaining risk & next step
- Codebase implementation is complete and hardened against concurrency race conditions, file corruption, cloud outage non-JSON responses, and auto-refresh failures.
- Ready for live staging deployment with real Zalo OA credentials to observe continuous 24h refresh cycle.
