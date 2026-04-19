import logging
import os
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, request, jsonify
from supabase import create_client, Client
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
import asyncio

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ADMIN_TELEGRAM_USER_ID = int(os.environ["ADMIN_TELEGRAM_USER_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mithaq-secret-2026")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

flask_app = Flask(__name__)

# Global reference to the telegram bot application
telegram_app = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def profile_button_markup(profile_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("💍 Express Interest", callback_data=f"interest:{profile_id}")]]
    )


def admin_request_markup(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{request_id}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"decline:{request_id}"),
        ]]
    )


def build_profile_text(p: dict) -> str:
    lines = [
        f"📋 *Profile {p['id']}*",
        f"👤 {p['display_name']}",
        f"📍 {p.get('city', '')}, {p.get('country', '')}".strip(", "),
        "",
        f"🕌 Deen: {p.get('deen', 'N/A')}",
        f"🙏 Prayer: {p.get('prayer', 'N/A')}",
        f"📚 Madhab: {p.get('madhab', 'N/A')}",
        "",
        f"💼 Occupation: {p.get('occupation', 'N/A')}",
        f"🎓 Education: {p.get('education', 'N/A')}",
        f"💍 Marital Status: {p.get('marital_status', 'N/A')}",
        f"👶 Children: {p.get('children', 'N/A')}",
        "",
        f"📝 About: {p.get('about', 'N/A')}",
        "",
        f"🔍 Looking for: {p.get('looking_for', 'N/A')}",
    ]
    return "\n".join(lines)


# ── Flask webhook ──────────────────────────────────────────────────────────────

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@flask_app.route("/post_new_profile", methods=["POST"])
def post_new_profile():
    """
    Called by Google Apps Script after inserting a new profile to Supabase.
    Body: { "secret": "...", "profile_id": "MTHAQ-095" }
    """
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "No JSON body"}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorised"}), 401

    profile_id = data.get("profile_id")
    if not profile_id:
        return jsonify({"error": "Missing profile_id"}), 400

    # Fetch profile from Supabase
    result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", profile_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )

    if not result.data:
        return jsonify({"error": f"Profile {profile_id} not found or inactive"}), 404

    p = result.data[0]
    text = build_profile_text(p)

    # Post to Telegram channel using the running event loop
    async def _send():
        await telegram_app.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode="Markdown",
            reply_markup=profile_button_markup(profile_id),
        )

    future = asyncio.run_coroutine_threadsafe(_send(), telegram_app.loop)
    try:
        future.result(timeout=15)
    except Exception as e:
        logging.error(f"Failed to post profile {profile_id}: {e}")
        return jsonify({"error": str(e)}), 500

    logging.info(f"Auto-posted profile {profile_id} to channel.")
    return jsonify({"ok": True, "profile_id": profile_id}), 200


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    user = update.effective_user
    username = user.username

    # Auto-register owner if they have a username
    if username:
        result = (
            supabase.table("profiles")
            .select("id, owner_telegram_username")
            .ilike("owner_telegram_username", username)
            .limit(1)
            .execute()
        )
        if result.data:
            profile = result.data[0]
            supabase.table("profiles").update({
                "owner_telegram_user_id": user.id,
            }).eq("id", profile["id"]).execute()
            logging.info(f"Auto-registered owner {username} → {profile['id']}")

    await update.message.reply_text(
        "Assalamu alaikum! Welcome to Mithaq Marriage bot.\n\n"
        "Browse profiles in the channel and tap 'Express Interest' on any profile you'd like to know more about.\n\n"
        "JazakAllahu khayran 🤲"
    )


async def post_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: /post_profile MTHAQ-001"""
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /post_profile MTHAQ-001")
        return

    profile_id = context.args[0].strip()

    result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", profile_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )

    if not result.data:
        await update.message.reply_text(f"Profile {profile_id} not found or inactive.")
        return

    p = result.data[0]
    text = build_profile_text(p)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=profile_button_markup(profile_id),
    )
    await update.message.reply_text(f"✅ Profile {profile_id} posted to channel.")


async def interest_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    await query.answer()

    _, profile_id = query.data.split(":", 1)

    # Check if user already has an active request
    state_result = (
        supabase.table("user_state")
        .select("*")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )

    if state_result.data and state_result.data[0].get("state") == "locked":
        await context.bot.send_message(
            chat_id=user.id,
            text="⚠️ You already have an active interest request pending. Please wait for a response before expressing interest in another profile."
        )
        return

    # Create the request
    request_result = (
        supabase.table("requests")
        .insert({
            "requester_telegram_user_id": user.id,
            "requester_username": user.username or "unknown",
            "profile_id": profile_id,
            "status": "pending",
        })
        .execute()
    )

    if not request_result.data:
        await context.bot.send_message(
            chat_id=user.id,
            text="Something went wrong. Please try again."
        )
        return

    request_id = request_result.data[0]["id"]

    # Lock the requester
    if state_result.data:
        supabase.table("user_state").update({
            "active_request_id": request_id,
            "state": "locked",
        }).eq("telegram_user_id", user.id).execute()
    else:
        supabase.table("user_state").insert({
            "telegram_user_id": user.id,
            "active_request_id": request_id,
            "state": "locked",
        }).execute()

    # Confirm to requester
    await context.bot.send_message(
        chat_id=user.id,
        text=(
            f"✅ JazakAllahu khayran! Your interest in profile *{profile_id}* has been recorded.\n\n"
            f"The admin will review your request and get back to you insha'Allah. 🤲"
        ),
        parse_mode="Markdown"
    )

    # Route to profile owner first, fall back to admin
    profile_result = (
        supabase.table("profiles")
        .select("owner_telegram_user_id")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    notify_id = ADMIN_TELEGRAM_USER_ID
    if profile_result.data and profile_result.data[0].get("owner_telegram_user_id"):
        notify_id = profile_result.data[0]["owner_telegram_user_id"]

    username_display = f"@{user.username}" if user.username else f"User ID: {user.id}"
    notify_text = (
        f"🔔 *New Interest Request*\n\n"
        f"Request ID: `{request_id}`\n"
        f"Profile: *{profile_id}*\n"
        f"From: {username_display}\n"
        f"Name: {user.first_name or ''} {user.last_name or ''}\n"
        f"Telegram ID: `{user.id}`"
    )

    await context.bot.send_message(
        chat_id=notify_id,
        text=notify_text,
        parse_mode="Markdown",
        reply_markup=admin_request_markup(request_id),
    )

    # Also notify admin if owner was notified instead
    if notify_id != ADMIN_TELEGRAM_USER_ID:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text=notify_text + f"\n\n_(Routed to profile owner)_",
            parse_mode="Markdown",
        )


async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    await query.answer()

    action, request_id_str = query.data.split(":", 1)
    request_id = int(request_id_str)

    req_result = (
        supabase.table("requests")
        .select("*")
        .eq("id", request_id)
        .limit(1)
        .execute()
    )

    if not req_result.data:
        await query.edit_message_text("Request not found.")
        return

    req = req_result.data[0]
    requester_id = req["requester_telegram_user_id"]
    profile_id = req["profile_id"]

    if action == "approve":
        supabase.table("requests").update({
            "status": "approved",
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "decided_by_admin": user.id,
        }).eq("id", request_id).execute()

        await context.bot.send_message(
            chat_id=requester_id,
            text=(
                f"✅ *Alhamdulillah!* Your interest in profile *{profile_id}* has been approved.\n\n"
                f"The admin will be in touch with the next steps insha'Allah. 🤲"
            ),
            parse_mode="Markdown"
        )

        await query.edit_message_text(
            f"✅ Approved request {request_id} for profile {profile_id}."
        )

    elif action == "decline":
        supabase.table("requests").update({
            "status": "declined",
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "decided_by_admin": user.id,
        }).eq("id", request_id).execute()

        supabase.table("user_state").update({
            "active_request_id": None,
            "state": "free",
        }).eq("telegram_user_id", requester_id).execute()

        await context.bot.send_message(
            chat_id=requester_id,
            text=(
                f"JazakAllahu khayran for your interest in profile *{profile_id}*.\n\n"
                f"Unfortunately this match was not taken forward at this time. "
                f"You are welcome to express interest in another profile. 🤲"
            ),
            parse_mode="Markdown"
        )

        await query.edit_message_text(
            f"❌ Declined request {request_id} for profile {profile_id}."
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: /status"""
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    result = (
        supabase.table("requests")
        .select("*")
        .eq("status", "pending")
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )

    if not result.data:
        await update.message.reply_text("No pending requests.")
        return

    lines = ["📋 *Pending Requests:*\n"]
    for r in result.data:
        lines.append(
            f"• Request {r['id']}: Profile *{r['profile_id']}* from @{r.get('requester_username', r['requester_telegram_user_id'])}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def unlock_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: /unlock TELEGRAM_USER_ID"""
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unlock TELEGRAM_USER_ID")
        return

    target_id = int(context.args[0].strip())

    supabase.table("user_state").update({
        "active_request_id": None,
        "state": "free",
    }).eq("telegram_user_id", target_id).execute()

    await update.message.reply_text(f"✅ User {target_id} has been unlocked.")


# ── Main ───────────────────────────────────────────────────────────────────────

def run_flask():
    port = int(os.environ.get("FLASK_PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


def main() -> None:
    global telegram_app

    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("post_profile", post_profile))
    telegram_app.add_handler(CommandHandler("status", status))
    telegram_app.add_handler(CommandHandler("unlock", unlock_user))
    telegram_app.add_handler(CallbackQueryHandler(interest_clicked, pattern=r"^interest:"))
    telegram_app.add_handler(CallbackQueryHandler(admin_decision, pattern=r"^(approve|decline):"))

    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"✅ Flask webhook server started on port {os.environ.get('FLASK_PORT', 8080)}")

    print("✅ Mithaq bot is running...")
    telegram_app.run_polling()


if __name__ == "__main__":
    main()
