"""
Auto-Workflow — Flask Application
Replaces 8 n8n workflows with Python endpoints.

Routes:
  POST /webhook/shopify/customer-create  → Create Contact Odoo
  POST /webhook/fsm                       → Delivery Tracking
  POST /webhook/hdsd-eng                  → ZNS HDSD English
  POST /webhook/hdsd-vie                  → ZNS HDSD Vietnamese
  POST /webhook/rating-ord-eng            → ZNS Rating English
  POST /webhook/rating-ord-vie            → ZNS Rating Vietnamese
  POST /webhook/rating                    → ZNS BON Rating
  GET  /webhook/zns-done                  → Zalo OAuth Callback
  GET  /health                            → Health check

Telegram bot runs in a background thread for RFID reconciliation.
"""

import io
import os
import sys
import logging
import threading
from datetime import datetime

from flask import Flask, request, jsonify

# Setup logging before importing config
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("auto-workflow.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("auto-workflow")

# Reduce noise from transient Telegram polling errors (auto-retried by library)
# and httpx getUpdates spam
class _TelegramTransientFilter(logging.Filter):
    """Downgrade transient Telegram polling errors from ERROR to DEBUG."""
    _TRANSIENT = ("RemoteProtocolError", "Bad Gateway", "Server disconnected")
    def filter(self, record):
        if record.levelno >= logging.ERROR:
            msg = record.getMessage()
            if any(t in msg for t in self._TRANSIENT):
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
        return True

logging.getLogger("telegram.ext.Updater").addFilter(_TelegramTransientFilter())
# httpx logs every getUpdates poll at INFO — reduce to WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)

from config import Config
from utils.phone import normalize_phone_zalo
from services.zalo_zns import send_zns, handle_authorization_callback, start_auto_refresh, get_token_status
from services.shopify_contact import sync_shopify_customer
from services.shopify_contact_bon import sync_bonario_customer
from services.delivery_tracking import track_delivery
from services.rfid_reconciliation import reconcile
from services.auto_conducted import run_auto_conducted, start_conducted_scheduler, get_conducted_status
from services.deadline_watcher import start_deadline_watcher, get_deadline_watcher_status
from services.crm_lost_watcher import start_crm_lost_watcher, get_crm_lost_status
from services.completion_days import start_completion_days_scheduler, get_completion_days_status
from services.dashboard_259 import start_dashboard_259_scheduler, get_dashboard_259_status
from services.kho_mau_ctl import start_ctl_scheduler, get_ctl_status
from services.checklist_overdue import start_checklist_overdue_scheduler, get_checklist_overdue_status
from services.dashboard_247 import start_dashboard_247_scheduler, get_dashboard_247_status
from services.section3_timeline import start_section3_timeline_scheduler, get_section3_timeline_status
from services.section5_2 import start_section5_2_scheduler, get_section5_2_status

app = Flask(__name__)


# ═══════════════════════════════════════════
#  HEALTH CHECK
# ═══════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "auto-workflow",
        "timestamp": datetime.now().isoformat(),
        "zns_tokens": get_token_status(),
        "conducted": get_conducted_status(),
        "deadline_watcher": get_deadline_watcher_status(),
        "crm_lost_watcher": get_crm_lost_status(),
        "completion_days": get_completion_days_status(),
        "dashboard_259": get_dashboard_259_status(),
        "kho_mau_ctl": get_ctl_status(),
        "checklist_overdue": get_checklist_overdue_status(),
        "dashboard_247": get_dashboard_247_status(),
        "section3_timeline": get_section3_timeline_status(),
        "section5_2": get_section5_2_status(),
        "routes": [
            "/webhook/shopify/customer-create",
            "/webhook/fsm",
            "/webhook/hdsd-eng",
            "/webhook/hdsd-vie",
            "/webhook/rating-ord-eng",
            "/webhook/rating-ord-vie",
            "/webhook/rating",
            "/webhook/zns-done",
            "/webhook/conducted",
        ],
    })


# ═══════════════════════════════════════════
#  SHOPIFY → ODOO CONTACT SYNC
# ═══════════════════════════════════════════

@app.route("/webhook/shopify/customer-create", methods=["POST"])
def shopify_customer_create():
    """
    Webhook endpoint for Shopify customer/create event.
    Replaces: Create Contact Odoo workflow.
    """
    try:
        data = request.get_json(force=True)
        logger.info(f"Shopify webhook payload keys: {list(data.keys())}")
        logger.info(f"Shopify webhook metafields in payload: {data.get('metafields', 'NOT FOUND')}")
        result = sync_shopify_customer(data)
        logger.info(f"Shopify sync result: {result}")
        return jsonify(result), 200
    except Exception as e:
        logger.exception("Error in shopify_customer_create")
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/shopify-bonario/customer-create", methods=["POST"])
def shopify_bonario_customer_create():
    """
    Webhook endpoint for Shopify Bonario customer/create event.
    Replaces: Create Contact Odoo Bon workflow.
    """
    try:
        data = request.get_json(force=True)
        logger.info(f"Bonario Shopify webhook payload keys: {list(data.keys())}")
        logger.info(f"Bonario Shopify webhook metafields in payload: {data.get('metafields', 'NOT FOUND')}")
        result = sync_bonario_customer(data)
        logger.info(f"Bonario Shopify sync result: {result}")
        return jsonify(result), 200
    except Exception as e:
        logger.exception("Error in shopify_bonario_customer_create")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════
#  DELIVERY TRACKING
# ═══════════════════════════════════════════

@app.route("/webhook/fsm", methods=["POST"])
def delivery_tracking():
    """
    Webhook endpoint for delivery tracking queries.
    Replaces: Delivery Tracking workflow.
    """
    try:
        data = request.get_json(force=True)
        phone = data.get("phone", "")
        result = track_delivery(phone)
        return jsonify(result), 200
    except Exception as e:
        logger.exception("Error in delivery_tracking")
        return jsonify({"success": False, "error": str(e)}), 500


# ═══════════════════════════════════════════
#  ZNS — 5 ROUTES (UNIFIED HANDLER)
# ═══════════════════════════════════════════

ZNS_ROUTES = ["hdsd-eng", "hdsd-vie", "rating-ord-eng", "rating-ord-vie", "rating"]


def _handle_zns(template_type: str):
    """Unified ZNS handler for all 5 ZNS webhook routes."""
    try:
        data = request.get_json(force=True)
        body = data.get("body", data)  # Support both wrapped and flat payloads

        # Extract fields (from Odoo webhook payload)
        phone_raw = body.get("x_studio_phone", "")
        order_code = body.get("name", "")
        customer_name = body.get("x_studio_tn_khch_hng", "")
        date_order = body.get("date_order", "")

        # Normalize phone
        phone = normalize_phone_zalo(phone_raw)

        # Format date to DD/MM/YYYY
        date_formatted = ""
        if date_order:
            try:
                d = datetime.strptime(date_order.replace("T", " ").split(".")[0], "%Y-%m-%d %H:%M:%S")
                date_formatted = d.strftime("%d/%m/%Y")
            except ValueError:
                try:
                    d = datetime.strptime(date_order[:10], "%Y-%m-%d")
                    date_formatted = d.strftime("%d/%m/%Y")
                except ValueError:
                    date_formatted = date_order

        result = send_zns(
            template_type=template_type,
            phone=phone,
            order_code=order_code,
            order_date=date_formatted,
            customer_name=customer_name,
        )

        return jsonify({"status": "sent", "zns_response": result}), 200

    except Exception as e:
        logger.exception(f"Error in ZNS [{template_type}]")
        return jsonify({"error": str(e)}), 500


# Register all 5 ZNS routes
for route in ZNS_ROUTES:
    app.add_url_rule(
        f"/webhook/{route}",
        endpoint=f"zns_{route.replace('-', '_')}",
        view_func=lambda rt=route: _handle_zns(rt),
        methods=["POST"],
    )


# ═══════════════════════════════════════════
#  ZALO OAUTH CALLBACK
# ═══════════════════════════════════════════

@app.route("/webhook/zns-done", methods=["GET"])
def zalo_oauth_callback():
    """
    Handle Zalo OAuth2 authorization callback.
    Used to initially obtain tokens for ORD or BON apps.
    The `state` parameter determines which app: state=bon → BON, else ORD
    """
    code = request.args.get("code", "")
    state = request.args.get("state", "ord")
    app_name = "bon" if state == "bon" else "ord"

    if not code:
        return jsonify({"error": "Missing 'code' parameter"}), 400

    # PKCE code_verifiers per app
    CODE_VERIFIERS = {
        "ord": "GO6IIHAxCuiyr0DB8t8ELOgEmqfH5XcAOAGZBx0Nawl",
        "bon": "y5kJmPmJIkcfAWdBiQSoVoym26Q1E5YWEjaBUbkdZKM",
    }

    try:
        result = handle_authorization_callback(
            code=code,
            code_verifier=CODE_VERIFIERS.get(app_name, ""),
            app=app_name,
        )
        return jsonify({"status": "ok", "app": app_name, "result": result}), 200
    except Exception as e:
        logger.exception(f"Error in zalo_oauth_callback [{app_name}]")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════
#  AUTO-CONDUCTED MANUAL TRIGGER
# ═══════════════════════════════════════════

@app.route("/webhook/conducted", methods=["POST"])
def manual_conducted():
    """
    Manual trigger for auto-conducted.
    Use ?dry_run=true to preview without writing.
    """
    try:
        dry_run = request.args.get("dry_run", "false").lower() == "true"
        result = run_auto_conducted(dry_run=dry_run)
        return jsonify(result), 200
    except Exception as e:
        logger.exception("Error in manual_conducted")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════
#  RFID TELEGRAM BOT (Background Thread)
# ═══════════════════════════════════════════

def _start_telegram_bot():
    """Start the Telegram bot in a background thread for RFID reconciliation."""
    try:
        import asyncio
        import time
        from telegram import Update
        from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
        from telegram.request import HTTPXRequest

        async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Handle incoming XLS/XLSX document from Telegram."""
            doc = update.message.document
            if not doc:
                await update.message.reply_text("❌ Vui lòng gửi file Excel (.xls/.xlsx)")
                return

            # Check file extension
            fname = doc.file_name or ""
            if not fname.lower().endswith((".xls", ".xlsx")):
                await update.message.reply_text("❌ Chỉ chấp nhận file .xls hoặc .xlsx")
                return

            await update.message.reply_text("📥 Đang xử lý file RFID...")

            try:
                # Download file with retry logic
                file_bytes = None
                last_err = None
                for attempt in range(3):
                    try:
                        tg_file = await context.bot.get_file(doc.file_id)
                        file_bytes = await tg_file.download_as_bytearray()
                        break
                    except Exception as dl_err:
                        last_err = dl_err
                        if attempt < 2:
                            wait = 3 * (2 ** attempt)
                            logger.warning(
                                f"Telegram file download failed (attempt {attempt + 1}/3): "
                                f"{dl_err} — retrying in {wait}s"
                            )
                            await asyncio.sleep(wait)
                        else:
                            raise last_err

                # Run reconciliation
                result = reconcile(bytes(file_bytes))

                # Send summary text
                await update.message.reply_text(result["summary_text"])

                # Send XLSX report
                if result["xlsx_bytes"]:
                    await update.message.reply_document(
                        document=io.BytesIO(result["xlsx_bytes"]),
                        filename="missing_stock.xlsx",
                        caption=f"📊 Báo cáo hàng thiếu — {result['missing_count']} items",
                    )

            except Exception as e:
                logger.exception("Error processing RFID file")
                await update.message.reply_text(f"❌ Lỗi: {str(e)}")

        async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Handle text messages."""
            await update.message.reply_text(
                "📦 RFID Stock Checker Bot\n\n"
                "Gửi file Excel (.xls/.xlsx) chứa dữ liệu RFID scan để kiểm kê.\n"
                "Bot sẽ so sánh với Odoo và trả báo cáo hàng thiếu."
            )

        async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
            """Handle errors in PTB — suppress transient network errors."""
            err = context.error
            err_msg = str(err) if err else ""
            transient_markers = (
                "RemoteProtocolError", "Bad Gateway", "Server disconnected",
                "NetworkError", "TimedOut", "Connection reset",
            )
            if any(m in err_msg for m in transient_markers):
                logger.debug(f"Telegram transient error (auto-retried): {err}")
            else:
                logger.error(f"Telegram error: {err}", exc_info=err)

        async def run_bot():
            # Use custom HTTPXRequest with longer timeouts for file operations
            # Also detect proxy settings from environment for networks where
            # Telegram is blocked (HTTPS_PROXY / HTTP_PROXY)
            proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
            custom_request = HTTPXRequest(
                connect_timeout=20.0,
                read_timeout=60.0,
                write_timeout=30.0,
                pool_timeout=10.0,
                connection_pool_size=8,
                proxy=proxy_url,
            )
            # Separate request object for getUpdates long-polling:
            # - read_timeout must be > poll_interval (default 10s from start_polling)
            #   to avoid premature timeouts during long-poll waits
            # - connection_pool_size=2 is sufficient for polling
            get_updates_req = HTTPXRequest(
                connect_timeout=20.0,
                read_timeout=30.0,
                pool_timeout=10.0,
                connection_pool_size=2,
            )
            app_tg = (
                ApplicationBuilder()
                .token(Config.TELEGRAM_BOT_TOKEN)
                .request(custom_request)
                .get_updates_request(get_updates_req)
                .build()
            )
            app_tg.add_handler(MessageHandler(filters.Document.ALL, handle_document))
            app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
            # Register error handler to suppress "No error handlers are registered" warning
            app_tg.add_error_handler(error_handler)

            # Use manual init/start instead of run_polling() to avoid
            # signal handler issues in daemon threads (Linux/Docker)
            await app_tg.initialize()
            await app_tg.start()
            await app_tg.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message"],
                poll_interval=1.0,
                # python-telegram-bot's network_retry_loop handles transient
                # errors (NetworkError, RemoteProtocolError, etc.) automatically
                # via exponential backoff — no custom error_callback needed.
            )
            logger.info("🤖 Telegram RFID bot started (polling)")

            # Keep running forever
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
            finally:
                await app_tg.updater.stop()
                await app_tg.stop()
                await app_tg.shutdown()

        # Run with retry on failure (network blips, Telegram blocks, etc.)
        retry_delay = 30
        while True:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(run_bot())
                break  # Normal exit (shouldn't happen — run_bot loops forever)
            except Exception as e:
                logger.warning(f"Telegram bot crashed, retrying in {retry_delay}s: {e}")
                try:
                    loop.close()
                except Exception:
                    pass
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 600)  # Max 10 min backoff

    except ImportError:
        logger.warning("python-telegram-bot not installed — RFID bot disabled")
    except Exception as e:
        logger.exception(f"Telegram bot error: {e}")


# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  Auto-Workflow Server Starting...")
    logger.info(f"  Port: {Config.FLASK_PORT}")
    logger.info(f"  Odoo: {Config.ODOO_URL}")
    logger.info(f"  Shopify: {Config.SHOPIFY_STORE}")
    logger.info("=" * 60)

    # Start Telegram bot in background thread
    if Config.TELEGRAM_BOT_TOKEN:
        bot_thread = threading.Thread(target=_start_telegram_bot, daemon=True)
        bot_thread.start()
        logger.info("🤖 Telegram RFID bot thread started")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — RFID bot disabled")

    # Start ZNS auto-refresh (keeps token alive forever)
    start_auto_refresh()

    # Start Conducted scheduler (08:00 ICT daily)
    start_conducted_scheduler()

    # Start Deadline Watcher (polls mail.activity for deadline extensions)
    start_deadline_watcher()

    # Start CRM Lost Watcher (cancels activities on lost leads)
    start_crm_lost_watcher()

    # Start Completion Days scheduler (Monday 08:00 ICT weekly)
    start_completion_days_scheduler()

    # Start Dashboard 259 Approval scheduler (1st of month 06:00 ICT)
    start_dashboard_259_scheduler()

    # Start KHO MAU CTL Overdue scheduler (daily 11:00 ICT)
    start_ctl_scheduler()

    # Start Checklist Overdue scheduler (1st of month 00:00 ICT)
    start_checklist_overdue_scheduler()

    # Start Dashboard 247 SC Activities scheduler (1st of month 06:00 ICT)
    start_dashboard_247_scheduler()

    # Start Section 3 Timeline scheduler (1st of month 06:00 ICT)
    start_section3_timeline_scheduler()

    # Start Section 5.2 SO-to-FSM Violations scheduler (1st of month 06:00 ICT)
    start_section5_2_scheduler()

    # Start Flask with a production-ready WSGI server
    try:
        from waitress import serve
        logger.info(f"Starting waitress WSGI server on 0.0.0.0:{Config.FLASK_PORT}")
        serve(app, host="0.0.0.0", port=Config.FLASK_PORT, threads=4)
    except ImportError:
        logger.warning("waitress not installed — falling back to Flask dev server")
        app.run(
            host="0.0.0.0",
            port=Config.FLASK_PORT,
            debug=Config.FLASK_DEBUG,
        )
