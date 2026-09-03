"""
Zalo ZNS Service — manages OAuth2 tokens and sends ZNS messages.
Replaces 5 n8n ZNS workflows.

Supports DUAL APPS: ORD (Ordinaire) and BON (Bonario).
Each app has separate credentials, token files, and auto-refresh loops.

Token storage uses DATA_DIR (Docker volume mountable) to persist
refresh_token across container rebuilds.

Concurrency & Caching:
- Per-app threading.Lock prevents race conditions when refreshing tokens or reading/writing files.
- Smart caching reuses access tokens with a 30-minute safety buffer before expiry.

Auto-refresh: Background thread refreshes token every 24h with retry and exponential backoff
so the refresh_token chain never expires (each refresh returns a new refresh_token with 3-month expiry).
"""

import json
import os
import logging
import threading
import time
import requests
from datetime import datetime
from config import Config
from utils.pii import mask_phone

logger = logging.getLogger(__name__)

# Token file path: DATA_DIR (Docker volume) or project root data fallback
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

# Separate token files per app
TOKEN_FILES = {
    "ord": os.path.join(DATA_DIR, "zalo_tokens.json"),
    "bon": os.path.join(DATA_DIR, "zalo_tokens_bon.json"),
}

# App credentials lookup
APP_CREDENTIALS = {
    "ord": {
        "app_id": Config.ZALO_APP_ID,
        "secret_key": Config.ZALO_SECRET_KEY,
    },
    "bon": {
        "app_id": Config.ZALO_BON_APP_ID,
        "secret_key": Config.ZALO_BON_SECRET_KEY,
    },
}

# ─── Concurrency Control (R1) ───
# Per-app threading locks to prevent concurrent token refresh / file access race conditions
_APP_LOCKS = {
    "ord": threading.Lock(),
    "bon": threading.Lock(),
}
_LOCKS_MUTEX = threading.Lock()

# ─── Token Caching Configuration (R2) ───
DEFAULT_EXPIRES_IN = 90000  # Default 25 hours (Zalo OA token lifetime in seconds)
TOKEN_EXPIRY_BUFFER = 1800  # 30 minutes in seconds


def _get_app_lock(app: str = "ord") -> threading.Lock:
    """Get or create a dedicated threading.Lock for the specified app."""
    app_key = app.lower() if isinstance(app, str) else "ord"
    if app_key not in _APP_LOCKS:
        with _LOCKS_MUTEX:
            if app_key not in _APP_LOCKS:
                _APP_LOCKS[app_key] = threading.Lock()
    return _APP_LOCKS[app_key]


def _token_file(app: str = "ord") -> str:
    """Get token file path for the given app."""
    app_key = app.lower() if isinstance(app, str) else "ord"
    return TOKEN_FILES.get(app_key, TOKEN_FILES.get("ord", os.path.join(DATA_DIR, "zalo_tokens.json")))


def _load_tokens(app: str = "ord") -> dict:
    """Load Zalo tokens from local JSON file safely."""
    tf = _token_file(app)
    if os.path.exists(tf):
        try:
            with open(tf, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    logger.warning(f"Token file {tf} is empty")
                    return {"access_token": "", "refresh_token": ""}
                data = json.loads(content)
                if isinstance(data, dict):
                    return data
                logger.warning(f"Token file {tf} content is not a JSON object (type: {type(data).__name__})")
        except Exception as e:
            logger.error(f"Error reading token file {tf}: {e}")
    return {"access_token": "", "refresh_token": ""}


def _save_tokens(tokens: dict, app: str = "ord"):
    """
    Persist Zalo tokens to local JSON file atomically.
    Writes to a temporary file in the same directory and renames atomically to prevent corruption.
    """
    if not isinstance(tokens, dict):
        raise TypeError(f"tokens must be a dict, got {type(tokens).__name__}")

    app_key = app.lower() if isinstance(app, str) else "ord"
    tokens_to_save = dict(tokens)
    tokens_to_save["updated_at"] = datetime.now().isoformat()
    tf = _token_file(app_key)
    target_dir = os.path.dirname(tf)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    tmp_file = f"{tf}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(tokens_to_save, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, tf)
        logger.info(f"Zalo [{app_key.upper()}] tokens saved → {tf}")
    except Exception as e:
        logger.error(f"Error writing token file {tf}: {e}")
        if os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except OSError:
                pass
        raise


def _is_token_valid(tokens: dict, buffer_seconds: int = TOKEN_EXPIRY_BUFFER) -> bool:
    """
    Check if the cached access token is present and valid for at least buffer_seconds.
    Returns False if token is missing, expired, or expiring within buffer_seconds.
    """
    if not isinstance(tokens, dict):
        return False
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return False
    expires_at = tokens.get("expires_at")
    if expires_at is None:
        return False
    try:
        exp_ts = float(expires_at)
        buf = float(buffer_seconds)
        if buf < 0:
            buf = 0.0
        return (time.time() + buf) < exp_ts
    except (ValueError, TypeError):
        return False


def _refresh_access_token_locked(app: str = "ord", tokens: dict = None) -> str:
    """
    Execute actual OAuth2 refresh token request to Zalo API.
    Assumes caller holds the per-app lock.
    """
    app_key = app.lower() if isinstance(app, str) else "ord"
    if tokens is None or not isinstance(tokens, dict):
        tokens = _load_tokens(app_key)
    refresh_token = tokens.get("refresh_token", "")
    creds = APP_CREDENTIALS.get(app_key, APP_CREDENTIALS["ord"])

    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise ValueError(
            f"No refresh_token found for [{app_key.upper()}]. Please authorize at: "
            "https://oauth.zaloapp.com/v4/oa/permission?app_id={}&redirect_uri={}".format(
                creds["app_id"], Config.ZALO_REDIRECT_URI
            )
        )

    try:
        resp = requests.post(
            "https://oauth.zaloapp.com/v4/oa/access_token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "secret_key": creds["secret_key"],
            },
            data={
                "app_id": creds["app_id"],
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
    except Exception as e:
        logger.error(f"Zalo [{app_key.upper()}] token refresh network error: {e}")
        raise

    try:
        data = resp.json()
    except Exception as e:
        logger.error(f"Zalo [{app_key.upper()}] token refresh non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}")
        raise ValueError(f"Zalo OAuth API returned non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}") from e

    if not isinstance(data, dict):
        raise ValueError(f"Zalo OAuth API returned invalid response format: {type(data).__name__}")

    if not data.get("access_token"):
        error_msg = data.get("error_description") or data.get("message") or data.get("name") or str(data)
        logger.error(f"Zalo [{app_key.upper()}] token refresh failed: {error_msg}")
        raise ValueError(f"Zalo token refresh failed: {error_msg}")

    expires_in_raw = data.get("expires_in")
    try:
        expires_in = int(expires_in_raw) if expires_in_raw is not None else DEFAULT_EXPIRES_IN
    except (ValueError, TypeError):
        expires_in = DEFAULT_EXPIRES_IN
    if expires_in <= 0:
        expires_in = DEFAULT_EXPIRES_IN

    expires_at = time.time() + expires_in

    # Save both tokens and expiration metadata (retain existing refresh_token if new one omitted)
    new_tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or refresh_token,
        "expires_in": expires_in,
        "expires_at": expires_at,
    }
    _save_tokens(new_tokens, app_key)

    logger.info(f"Zalo [{app_key.upper()}] access token refreshed successfully (expires in {expires_in}s)")
    return data["access_token"]


def get_access_token(app: str = "ord", force_refresh: bool = False) -> str:
    """
    Get a valid access token for the specified app (R2: Smart Caching).
    Reuses existing cached token if valid; refreshes only when expired or forced.
    Thread-safe across all callers (R1: Concurrency Control).
    """
    app_key = app.lower() if isinstance(app, str) else "ord"
    lock = _get_app_lock(app_key)
    with lock:
        tokens = _load_tokens(app_key)
        if not force_refresh and _is_token_valid(tokens):
            return tokens["access_token"]
        return _refresh_access_token_locked(app_key, tokens)


def refresh_access_token(app: str = "ord", force: bool = True) -> str:
    """
    Refresh Zalo OA access token using refresh_token.
    If force=False, returns cached token if still valid.
    """
    return get_access_token(app=app, force_refresh=force)


def get_token_status() -> dict:
    """Get current token status for health checks — all apps."""
    status = {}
    for app_name in TOKEN_FILES:
        lock = _get_app_lock(app_name)
        with lock:
            tokens = _load_tokens(app_name)
        updated = tokens.get("updated_at", "never")
        refresh_tok = tokens.get("refresh_token")
        access_tok = tokens.get("access_token")
        has_refresh = bool(isinstance(refresh_tok, str) and refresh_tok.strip())
        has_access = bool(isinstance(access_tok, str) and access_tok.strip())
        is_valid = _is_token_valid(tokens)
        expires_at = tokens.get("expires_at")
        status[app_name] = {
            "has_refresh_token": has_refresh,
            "has_access_token": has_access,
            "is_token_valid": is_valid,
            "expires_at": expires_at,
            "last_refreshed": updated,
            "token_file": _token_file(app_name),
        }
    status["auto_refresh_active"] = bool(_auto_refresh_running)
    return status


# ─── Auto-Refresh Scheduler with Retry & Exponential Backoff (R3) ───
_auto_refresh_running = False
_auto_refresh_thread = None
_auto_refresh_lock = threading.Lock()
_stop_event = threading.Event()
AUTO_REFRESH_INTERVAL = 24 * 60 * 60  # 24 hours in seconds
RETRY_BACKOFFS = [60, 120, 300]  # 1m, 2m, 5m backoff delays


def _refresh_app_with_retry(app_name: str, max_retries: int = 3, backoffs: list = None) -> bool:
    """
    Attempt to refresh an app's token with exponential backoff retries (R3).
    Returns True if refreshed successfully, False otherwise.
    """
    if not backoffs:
        backoffs = RETRY_BACKOFFS

    for attempt in range(1, max_retries + 1):
        if _stop_event.is_set():
            return False
        try:
            tokens = _load_tokens(app_name)
            refresh_tok = tokens.get("refresh_token")
            if not isinstance(refresh_tok, str) or not refresh_tok.strip():
                logger.info(f"[AUTO-REFRESH] [{app_name.upper()}] no refresh_token yet — skipping")
                return False

            refresh_access_token(app_name, force=True)
            logger.info(f"[AUTO-REFRESH] [{app_name.upper()}] token refreshed successfully (attempt {attempt}/{max_retries})")
            return True
        except Exception as e:
            if attempt < max_retries:
                delay = backoffs[attempt - 1] if (attempt - 1) < len(backoffs) else backoffs[-1]
                logger.warning(
                    f"[AUTO-REFRESH] [{app_name.upper()}] refresh attempt {attempt}/{max_retries} failed: {e}. "
                    f"Retrying in {delay}s..."
                )
                if _stop_event.wait(timeout=delay):
                    return False
            else:
                logger.error(
                    f"[AUTO-REFRESH] [{app_name.upper()}] all {max_retries} refresh attempts failed. Last error: {e}"
                )
                return False
    return False


def _auto_refresh_loop(initial_wait: int = 60, interval: int = AUTO_REFRESH_INTERVAL):
    """
    Background loop that refreshes ALL Zalo app tokens every 24h with retry on failure.

    Each refresh returns a NEW refresh_token with a fresh 3-month expiry.
    By refreshing every 24h, the token chain never expires.
    """
    global _auto_refresh_running
    _auto_refresh_running = True

    # Wait initial delay before first attempt (let server start)
    if initial_wait > 0:
        if _stop_event.wait(timeout=initial_wait):
            _auto_refresh_running = False
            return

    while _auto_refresh_running and not _stop_event.is_set():
        for app_name in list(TOKEN_FILES.keys()):
            if _stop_event.is_set() or not _auto_refresh_running:
                break
            try:
                _refresh_app_with_retry(app_name)
            except Exception as e:
                logger.error(f"[AUTO-REFRESH] Unexpected error processing [{app_name.upper()}]: {e}")

        if _stop_event.wait(timeout=interval):
            break

    _auto_refresh_running = False


def start_auto_refresh():
    """Start the background auto-refresh thread safely (idempotent, call once at app startup)."""
    global _auto_refresh_running, _auto_refresh_thread
    with _auto_refresh_lock:
        if _auto_refresh_thread is not None and _auto_refresh_thread.is_alive():
            logger.info("[AUTO-REFRESH] Thread already running — ignoring start call")
            return
        _stop_event.clear()
        _auto_refresh_running = True
        _auto_refresh_thread = threading.Thread(target=_auto_refresh_loop, daemon=True, name="zalo-auto-refresh")
        _auto_refresh_thread.start()
        logger.info(f"[AUTO-REFRESH] Started — interval: 24h, apps: {list(TOKEN_FILES.keys())}")


def stop_auto_refresh():
    """Stop the background auto-refresh loop and wait briefly for thread exit."""
    global _auto_refresh_running, _auto_refresh_thread
    with _auto_refresh_lock:
        _auto_refresh_running = False
        _stop_event.set()
        if _auto_refresh_thread is not None and _auto_refresh_thread.is_alive():
            if threading.current_thread() != _auto_refresh_thread:
                _auto_refresh_thread.join(timeout=1.0)
        _auto_refresh_thread = None


def send_zns(
    template_type: str,
    phone: str,
    order_code: str = "",
    order_date: str = "",
    customer_name: str = "",
    tracking_id: str = None,
    mode: str = None,
) -> dict:
    """
    Send a ZNS message via Zalo OpenAPI.
    Automatically routes to the correct Zalo app (ORD or BON) based on template_type.
    Uses cached access token if valid, avoiding redundant token refresh calls.

    Args:
        template_type: One of 'hdsd-eng', 'hdsd-vie', 'rating-ord-eng',
                       'rating-ord-vie', 'rating'
        phone: Customer phone (should already be normalized to +84xxx)
        order_code: BG/order code (e.g. "BG-202508-1234")
        order_date: Date in DD/MM/YYYY format
        customer_name: Customer name
        tracking_id: Optional tracking identifier (max 48 chars) for webhook reconciliation
        mode: Optional mode ('development' or 'production')
    """
    template_id = Config.ZNS_TEMPLATES.get(template_type)
    if not template_id:
        raise ValueError(f"Unknown template type: {template_type}")

    # Determine which Zalo app to use
    app = Config.ZNS_TEMPLATE_APP.get(template_type, "ord")

    template_config = Config.ZNS_TEMPLATE_DATA_KEYS.get(template_type, {})
    name_key = template_config.get("name_key", "name")
    include_date = template_config.get("include_date", True)

    # Get valid access token for the correct app (smart caching: reuses token if valid)
    access_token = get_access_token(app)

    # Build template_data
    template_data = {
        "order_code": order_code,
        name_key: customer_name,
    }
    if include_date:
        template_data["order_date"] = order_date

    payload = {
        "phone": phone,
        "template_id": template_id,
        "template_data": template_data,
    }
    if tracking_id:
        payload["tracking_id"] = str(tracking_id)[:48]
    if mode:
        payload["mode"] = mode

    try:
        resp = requests.post(
            "https://business.openapi.zalo.me/message/template",
            headers={
                "Content-Type": "application/json",
                "access_token": access_token,
            },
            json=payload,
            timeout=30,
        )
    except Exception as e:
        logger.error(f"ZNS send request network error to {mask_phone(phone)}: {e}")
        raise

    try:
        result = resp.json()
    except Exception as e:
        logger.error(f"ZNS API returned non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}")
        raise ValueError(f"ZNS API returned non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}") from e

    logger.info(f"ZNS sent [{template_type}→{app.upper()}] to {mask_phone(phone)}: {result}")
    return result



def handle_authorization_callback(code: str, code_verifier: str = None, app: str = "ord") -> dict:
    """
    Handle Zalo OAuth2 authorization callback to get initial tokens.
    Ported from: ZNS ORD / Webhook1 → HTTP Request1 chain (disabled in n8n).

    Use this to initially set up tokens:
    1. Visit: https://oauth.zaloapp.com/v4/oa/permission?app_id={ZALO_APP_ID}&redirect_uri={REDIRECT_URI}
    2. After authorization, it redirects with ?code=xxx
    3. Call this function with that code
    """
    app_key = app.lower() if isinstance(app, str) else "ord"
    creds = APP_CREDENTIALS.get(app_key, APP_CREDENTIALS["ord"])
    data = {
        "app_id": creds["app_id"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": Config.ZALO_REDIRECT_URI,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    try:
        resp = requests.post(
            "https://oauth.zaloapp.com/v4/oa/access_token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "secret_key": creds["secret_key"],
            },
            data=data,
            timeout=30,
        )
    except Exception as e:
        logger.error(f"Zalo [{app_key.upper()}] authorization request network error: {e}")
        raise

    try:
        result = resp.json()
    except Exception as e:
        logger.error(f"Zalo [{app_key.upper()}] authorization callback non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}")
        raise ValueError(f"Zalo OAuth API returned non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}") from e

    if isinstance(result, dict) and result.get("access_token"):
        expires_in_raw = result.get("expires_in")
        try:
            expires_in = int(expires_in_raw) if expires_in_raw is not None else DEFAULT_EXPIRES_IN
        except (ValueError, TypeError):
            expires_in = DEFAULT_EXPIRES_IN
        if expires_in <= 0:
            expires_in = DEFAULT_EXPIRES_IN
        expires_at = time.time() + expires_in
        lock = _get_app_lock(app_key)
        with lock:
            _save_tokens({
                "access_token": result["access_token"],
                "refresh_token": result.get("refresh_token", ""),
                "expires_in": expires_in,
                "expires_at": expires_at,
            }, app_key)
        logger.info(f"Zalo [{app_key.upper()}] initial authorization successful")

    return result
