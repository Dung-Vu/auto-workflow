# Zalo ZNS Service Upgrade Handoff Report

> [!WARNING] **Skepticism Disclaimer**
> High confidence in unit test coverage and thread safety under mock conditions; real-world Zalo token refresh and network edge cases must be monitored under live production traffic.

## 1. What I changed
- `services/zalo_zns.py`:
  - **R1 Concurrency Control**: Added per-app `threading.Lock()` for `ord` and `bon` apps (`_get_app_lock(app)`). All token file reads/writes and OAuth2 refresh API calls are strictly synchronized per app to prevent race conditions and inadvertent token invalidation.
  - **R2 Smart Token Caching**: Implemented `_is_token_valid(tokens, buffer_seconds=1800)` checking `expires_at` with a 30-minute safety buffer. `get_access_token(app)` reuses cached valid access tokens and only triggers refresh when expired or within buffer. `send_zns` now calls `get_access_token(app)` instead of forcing a refresh on every send.
  - **R3 Auto-Refresh Daemon with Retry & Exponential Backoff**: Enhanced `_refresh_app_with_retry` to retry failed refresh attempts up to 3 times with backoff intervals (`[60, 120, 300]` seconds). Added `stop_auto_refresh()` and `_stop_event` for graceful thread termination.
  - **Status & Diagnostics**: Updated `get_token_status()` to return `is_token_valid` and `expires_at` for each app alongside token presence and timestamps.
- `tests/test_zalo_zns.py`:
  - Created automated test suite with 22 test cases covering token caching, buffer validation, concurrency & multi-threaded race conditions (20 concurrent threads), multi-app independence (`ord` vs `bon`), auto-refresh retry backoff, ZNS routing & message formatting, OAuth authorization callback, and Flask endpoint integration.

## 2. Why
- Previously, `send_zns()` triggered a refresh API call on every single message dispatch, causing unnecessary network latency, hitting Zalo API rate limits, and introducing race conditions where concurrent requests could invalidate the refresh token chain.
- The auto-refresh daemon lacked retry logic, meaning a single transient network timeout at the 24h interval could cause token expiration.

## 3. Verification Record
- **Deep Verification (ran actual tests):**
  - Ran `pytest tests/test_zalo_zns.py` (22/22 passed in 3.01s).
  - Ran `pytest tests/test_lot_serial_unit.py tests/test_zalo_zns.py` (73/73 passed in 1.72s).
  - Ran `python -m unittest discover tests` (73/73 passed).
  - Ran `python tests/test_watcher_mocked.py` (all scenarios passed).
  - Verified Flask endpoints `/health`, `/webhook/hdsd-*`, `/webhook/rating*`, `/webhook/zns-done` via test client.
- **Shallow Verification (manual run only):**
  - Inspected log outputs for correct format, error descriptions, and retry backoff logs.
- **Unverified aspects:**
  - Live HTTP requests to Zalo OA OAuth server `oauth.zaloapp.com` and ZNS OpenAPI `business.openapi.zalo.me` (requires live credentials and active Zalo OA account).

## 4. Known Issues
- `None` (All 73 unit tests pass cleanly; no functional regressions found in local verification).

## 5. Untested Edge Cases & Next Step
- **Untested edge cases**:
  - Behavior when Zalo OAuth server returns non-JSON HTTP 502/504 Bad Gateway responses during a cloud outage.
  - Token refresh when local disk filesystem is full or read-only.
- **Next Step**: Deploy to staging container with real Zalo credentials to monitor live token refresh cycle over 24h.
