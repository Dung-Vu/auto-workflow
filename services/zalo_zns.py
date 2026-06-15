"""
Zalo ZNS Service — manages OAuth2 tokens and sends ZNS messages.
Replaces 5 n8n ZNS workflows.

Supports DUAL APPS: ORD (Ordinaire) and BON (Bonario).
Each app has separate credentials, token files, and auto-refresh loops.

Token storage uses DATA_DIR (Docker volume mountable) to persist
refresh_token across container rebuilds.

Auto-refresh: Background thread refreshes token every 24h so the
refresh_token chain never expires (each refresh returns a new
refresh_token with 3-month expiry).
"""

import json
import os
import logging
import threading
import time
import requests
from datetime import datetime
from config import Config

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


def _token_file(app: str = "ord") -> str:
    """Get token file path for the given app."""
    return TOKEN_FILES.get(app, TOKEN_FILES["ord"])


def _load_tokens(app: str = "ord") -> dict:
    """Load Zalo tokens from local JSON file."""
    tf = _token_file(app)
    if os.path.exists(tf):
        with open(tf, "r") as f:
            return json.load(f)
    return {"access_token": "", "refresh_token": ""}


def _save_tokens(tokens: dict, app: str = "ord"):
    """Persist Zalo tokens to local JSON file."""
    tokens["updated_at"] = datetime.now().isoformat()
    tf = _token_file(app)
    with open(tf, "w") as f:
        json.dump(tokens, f, indent=2)
    logger.info(f"Zalo [{app.upper()}] tokens saved → {tf}")


def refresh_access_token(app: str = "ord") -> str:
    """
    Refresh Zalo OA access token using refresh_token.
    Ported from: ZNS ORD / Get Token → Get Refresh Token → Get Refresh chain.
    """
    tokens = _load_tokens(app)
    refresh_token = tokens.get("refresh_token", "")
    creds = APP_CREDENTIALS.get(app, APP_CREDENTIALS["ord"])

    if not refresh_token:
        raise ValueError(
            f"No refresh_token found for [{app.upper()}]. Please authorize at: "
            "https://oauth.zaloapp.com/v4/oa/permission?app_id={}&redirect_uri={}".format(
                creds["app_id"], Config.ZALO_REDIRECT_URI
            )
        )

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
    )

    data = resp.json()

    if "access_token" not in data:
        error_msg = data.get("error_description", data.get("message", str(data)))
        logger.error(f"Zalo [{app.upper()}] token refresh failed: {error_msg}")
        raise ValueError(f"Zalo token refresh failed: {error_msg}")

    # Save both tokens (Zalo returns new refresh_token on every refresh)
    new_tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
    }
    _save_tokens(new_tokens, app)

    logger.info(f"Zalo [{app.upper()}] access token refreshed successfully")
    return data["access_token"]


def get_token_status() -> dict:
    """Get current token status for health checks — all apps."""
    status = {}
    for app_name in TOKEN_FILES:
        tokens = _load_tokens(app_name)
        updated = tokens.get("updated_at", "never")
        has_refresh = bool(tokens.get("refresh_token", ""))
        has_access = bool(tokens.get("access_token", ""))
        status[app_name] = {
            "has_refresh_token": has_refresh,
            "has_access_token": has_access,
            "last_refreshed": updated,
            "token_file": _token_file(app_name),
        }
    status["auto_refresh_active"] = _auto_refresh_running
    return status


# ─── Auto-Refresh Scheduler ───
_auto_refresh_running = False
AUTO_REFRESH_INTERVAL = 24 * 60 * 60  # 24 hours in seconds


def _auto_refresh_loop():
    """Background loop that refreshes ALL Zalo app tokens every 24h.
    
    Each refresh returns a NEW refresh_token with a fresh 3-month expiry.
    By refreshing every 24h, the token chain never expires — user only
    needs to do OAuth ONCE, ever.
    """
    global _auto_refresh_running
    _auto_refresh_running = True
    
    # Wait 60s before first attempt (let server start)
    time.sleep(60)
    
    while _auto_refresh_running:
        for app_name in TOKEN_FILES:
            try:
                tokens = _load_tokens(app_name)
                if tokens.get("refresh_token"):
                    refresh_access_token(app_name)
                    logger.info(f"[AUTO-REFRESH] [{app_name.upper()}] token refreshed — next in 24h")
                else:
                    logger.info(f"[AUTO-REFRESH] [{app_name.upper()}] no refresh_token yet — skipping")
            except Exception as e:
                logger.error(f"[AUTO-REFRESH] [{app_name.upper()}] failed: {e}")
        
        time.sleep(AUTO_REFRESH_INTERVAL)


def start_auto_refresh():
    """Start the background auto-refresh thread (call once at app startup)."""
    thread = threading.Thread(target=_auto_refresh_loop, daemon=True, name="zalo-auto-refresh")
    thread.start()
    logger.info(f"[AUTO-REFRESH] Started — interval: 24h, apps: {list(TOKEN_FILES.keys())}")


def send_zns(
    template_type: str,
    phone: str,
    order_code: str = "",
    order_date: str = "",
    customer_name: str = "",
) -> dict:
    """
    Send a ZNS message via Zalo OpenAPI.
    Automatically routes to the correct Zalo app (ORD or BON) based on template_type.

    Args:
        template_type: One of 'hdsd-eng', 'hdsd-vie', 'rating-ord-eng',
                       'rating-ord-vie', 'rating'
        phone: Customer phone (should already be normalized to +84xxx)
        order_code: BG/order code (e.g. "BG-202508-1234")
        order_date: Date in DD/MM/YYYY format
        customer_name: Customer name
    """
    template_id = Config.ZNS_TEMPLATES.get(template_type)
    if not template_id:
        raise ValueError(f"Unknown template type: {template_type}")

    # Determine which Zalo app to use
    app = Config.ZNS_TEMPLATE_APP.get(template_type, "ord")

    template_config = Config.ZNS_TEMPLATE_DATA_KEYS.get(template_type, {})
    name_key = template_config.get("name_key", "name")
    include_date = template_config.get("include_date", True)

    # Get fresh access token for the correct app
    access_token = refresh_access_token(app)

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

    resp = requests.post(
        "https://business.openapi.zalo.me/message/template",
        headers={
            "Content-Type": "application/json",
            "access_token": access_token,
        },
        json=payload,
    )

    result = resp.json()
    logger.info(f"ZNS sent [{template_type}→{app.upper()}] to {phone}: {result}")
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
    creds = APP_CREDENTIALS.get(app, APP_CREDENTIALS["ord"])
    data = {
        "app_id": creds["app_id"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": Config.ZALO_REDIRECT_URI,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    resp = requests.post(
        "https://oauth.zaloapp.com/v4/oa/access_token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "secret_key": creds["secret_key"],
        },
        data=data,
    )

    result = resp.json()
    if "access_token" in result:
        _save_tokens({
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
        }, app)
        logger.info(f"Zalo [{app.upper()}] initial authorization successful")

    return result
