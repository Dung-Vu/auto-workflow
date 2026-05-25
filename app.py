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

from config import Config
from utils.phone import normalize_phone_zalo
from services.zalo_zns import send_zns, handle_authorization_callback, start_auto_refresh, get_token_status
from services.shopify_contact import sync_shopify_customer
from services.delivery_tracking import track_delivery
from services.rfid_reconciliation import reconcile
from services.auto_conducted import run_auto_conducted, start_conducted_scheduler, get_conducted_status

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
        from telegram import Update
        from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

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
                # Download file
                tg_file = await context.bot.get_file(doc.file_id)
                file_bytes = await tg_file.download_as_bytearray()

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

        async def run_bot():
            app_tg = ApplicationBuilder().token(Config.TELEGRAM_BOT_TOKEN).build()
            app_tg.add_handler(MessageHandler(filters.Document.ALL, handle_document))
            app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

            # Use manual init/start instead of run_polling() to avoid
            # signal handler issues in daemon threads (Linux/Docker)
            await app_tg.initialize()
            await app_tg.start()
            await app_tg.updater.start_polling(drop_pending_updates=True)
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

        # Run in event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_bot())

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

    # Start Flask
    app.run(
        host="0.0.0.0",
        port=Config.FLASK_PORT,
        debug=Config.FLASK_DEBUG,
    )
