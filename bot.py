import logging
import os
import threading
import requests
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


# ── Markup helpers ─────────────────────────────────────────────────────────────

def profile_button_markup(profile_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("💍 Express Interest", callback_data="interest:" + profile_id)]]
    )


def owner_request_markup(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data="approve:" + str(request_id)),
        InlineKeyboardButton("❌ Decline", callback_data="decline:" + str(request_id)),
    ]])


def admin_request_markup(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data="approve:" + str(request_id)),
        InlineKeyboardButton("❌ Decline", callback_data="decline:" + str(request_id)),
    ]])


def interest_confirmation_markup(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Withdraw Interest", callback_data="withdraw:" + str(request_id))]]
    )


def queue_confirmation_markup(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Leave Queue", callback_data="withdraw:" + str(request_id))]]
    )


# ── Profile text builder ───────────────────────────────────────────────────────

def build_profile_text(p: dict) -> str:
    raw = p.get("formatted_text") or ""
    if raw:
        lines = raw.split("\n")
        if lines and "BROTHER" not in lines[0] and "SISTER" not in lines[0]:
            lines = lines[1:]
        return "\n".join(lines)
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


# ── Direct Telegram HTTP (used by Flask thread) ────────────────────────────────

def send_telegram_message(chat_id: str, text: str, reply_markup: dict = None) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=15)
        result = resp.json()
        if not result.get("ok"):
            logging.error(f"Telegram API error: {result}")
            return False
        return True
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False


# ── Queue helper ───────────────────────────────────────────────────────────────

async def advance_queue(profile_id: str, context) -> None:
    next_result = (
        supabase.table("requests")
        .select("*")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .eq("is_active_request", False)
        .order("queue_position", desc=False)
        .limit(1)
        .execute()
    )

    if not next_result.data:
        return

    next_req = next_result.data[0]
    next_request_id = next_req["id"]
    next_requester_id = next_req["requester_telegram_user_id"]
    next_username = next_req.get("requester_username", str(next_requester_id))

    supabase.table("requests").update({
        "is_active_request": True,
    }).eq("id", next_request_id).execute()

    supabase.table("user_state").update({
        "active_request_id": next_request_id,
        "state": "locked",
    }).eq("telegram_user_id", next_requester_id).execute()

    profile_result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    owner_tg_id = None
    owner_username = ""
    if profile_result.data:
        owner_tg_id = profile_result.data[0].get("owner_telegram_user_id")
        owner_username = profile_result.data[0].get("owner_telegram_username", "")

    await context.bot.send_message(
        chat_id=next_requester_id,
        text=(
            "🔔 It's your turn! Your interest in profile " + profile_id + " is now being considered by the profile owner insha'Allah. 🤲\n\n"
            "You will be notified of their decision.\n\n"
            "To withdraw, tap the button below or send /withdraw"
        ),
        reply_markup=interest_confirmation_markup(next_request_id),
    )

    username_display = "@" + next_username if next_username != str(next_requester_id) else "User ID: " + str(next_requester_id)
    request_text = (
        "New Interest Request for your profile " + profile_id + "\n\n"
        "Someone has expressed interest in your profile.\n\n"
        "Request ID: " + str(next_request_id) + "\n"
        "From: " + username_display + "\n\n"
        "Please tap Approve or Decline below."
    )

    admin_text = (
        "🔔 Queue Advanced — New Interest Request\n\n"
        "Profile: " + profile_id + "\n"
        "From: " + username_display + "\n"
        "Owner: @" + owner_username + "\n"
        "Request ID: " + str(next_request_id)
    )

    sent_to_owner = False
    if owner_tg_id:
        try:
            await context.bot.send_message(
                chat_id=owner_tg_id,
                text=request_text,
                reply_markup=owner_request_markup(next_request_id),
            )
            sent_to_owner = True
        except Exception as e:
            logging.warning("Could not message owner: " + str(e))

    admin_text += "\n\n✅ Request sent to owner." if sent_to_owner else "\n\n⚠️ Owner not registered — approve/decline below."

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text=admin_text,
        reply_markup=admin_request_markup(next_request_id),
    )


# ── Flask webhook ──────────────────────────────────────────────────────────────

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@flask_app.route("/post_new_profile", methods=["POST"])
def post_new_profile():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "No JSON body"}), 400
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorised"}), 401

    profile_id = data.get("profile_id")
    if not profile_id:
        return jsonify({"error": "Missing profile_id"}), 400

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
    is_new = not p.get("notified")
    if is_new:
        text = "🆕 NEW PROFILE\n\n" + text

    reply_markup = {
        "inline_keyboard": [[
            {"text": "💍 Express Interest", "callback_data": "interest:" + profile_id}
        ]]
    }

    success = send_telegram_message(CHANNEL_ID, text, reply_markup)

    if not success:
        return jsonify({"error": "Failed to send to Telegram"}), 500

    # Mark as notified
    supabase.table("profiles").update({"notified": True}).eq("id", profile_id).execute()

    # Send welcome message to owner
    owner_tg_id = p.get("owner_telegram_user_id")
    owner_username = p.get("owner_telegram_username", "")
    if is_new:
        welcome_msg = (
            "Assalamu alaikum! 🌸\n\n"
            "JazakAllahu khayran — your Mithaq profile " + profile_id + " is now live in the channel!\n\n"
            "Here's what happens next:\n\n"
            "1️⃣ Channel members can tap Express Interest on your profile\n"
            "2️⃣ You'll receive a message here with Approve and Decline buttons\n"
            "3️⃣ If you Approve, the person receives your contact details\n"
            "4️⃣ If you Decline, they are notified and may look at other profiles\n\n"
            "📌 You are in full control — nothing is shared without your approval\n"
            "📌 Only first name and wali contact are shared upon approval (for sisters)\n\n"
            "Questions? Contact @MithaqAdmin 🤲\n\n"
            "May Allah make it easy for you 🤲"
        )
        if owner_tg_id:
            send_telegram_message(str(owner_tg_id), welcome_msg)
        else:
            send_telegram_message(
                str(ADMIN_TELEGRAM_USER_ID),
                "Could not send welcome to owner of " + profile_id + " (@" + owner_username + ") — they may not have started the bot yet."
            )

    logging.info(f"Auto-posted profile {profile_id} to channel.")
    return jsonify({"ok": True, "profile_id": profile_id}), 200


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    user = update.effective_user
    username = user.username.lower() if user.username else ""

    if username:
        result = (
            supabase.table("profiles")
            .select("id, owner_telegram_user_id")
            .eq("owner_telegram_username", username)
            .limit(1)
            .execute()
        )
        if result.data:
            profile = result.data[0]
            if not profile.get("owner_telegram_user_id"):
                supabase.table("profiles").update({
                    "owner_telegram_user_id": user.id
                }).eq("id", profile["id"]).execute()
                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_USER_ID,
                    text="Owner registered: " + profile["id"] + " @" + user.username + " ID " + str(user.id),
                )

    await update.message.reply_text(
        "Assalamu alaikum! Welcome to Mithaq Marriage 🌸\n\n"
        "Here's how it works:\n\n"
        "1️⃣ Browse profiles in the channel\n"
        "2️⃣ Tap 💍 Express Interest on any profile you like\n"
        "3️⃣ The profile owner will be notified and will Approve or Decline\n"
        "4️⃣ If approved, you'll receive their contact details here\n\n"
        "📌 You can only have one active request at a time\n"
        "📌 If declined, you're free to express interest in another profile\n"
        "📌 You can withdraw your interest at any time — just send /withdraw\n"
        "📌 To check your request status, send /my_request\n\n"
        "📢 Browse profiles here: https://t.me/+ilWsgu9hLb02ODQ0\n\n"
        "Questions or issues? Contact @MithaqAdmin\n\n"
        "JazakAllahu khayran — may Allah make it easy for you 🤲"
    )


async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    state_result = (
        supabase.table("user_state")
        .select("*")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )

    if not state_result.data or state_result.data[0].get("state") not in ["locked", "queued"]:
        await update.message.reply_text("You don't have an active interest request to withdraw.")
        return

    active_request_id = state_result.data[0].get("active_request_id")

    if not active_request_id:
        supabase.table("user_state").update({"state": "free"}).eq("telegram_user_id", user.id).execute()
        await update.message.reply_text("You have been unlocked. You may now express interest in another profile.")
        return

    req_result = (
        supabase.table("requests")
        .select("*")
        .eq("id", active_request_id)
        .limit(1)
        .execute()
    )

    if not req_result.data or req_result.data[0].get("status") != "pending":
        supabase.table("user_state").update({
            "active_request_id": None,
            "state": "free",
        }).eq("telegram_user_id", user.id).execute()
        await update.message.reply_text("Your request has already been decided. You are free to express interest in another profile.")
        return

    profile_id = req_result.data[0]["profile_id"]
    was_active = req_result.data[0].get("is_active_request", False)

    supabase.table("requests").update({
        "status": "withdrawn",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", active_request_id).execute()

    supabase.table("user_state").update({
        "active_request_id": None,
        "state": "free",
    }).eq("telegram_user_id", user.id).execute()

    await update.message.reply_text(
        "Your interest in profile " + profile_id + " has been withdrawn. You are now free to express interest in another profile. 🤲"
    )

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text="Request " + str(active_request_id) + " withdrawn via /withdraw by @" + str(user.username or user.id),
    )

    if was_active:
        await advance_queue(profile_id, context)


async def my_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    state_result = (
        supabase.table("user_state")
        .select("*")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )

    if not state_result.data or state_result.data[0].get("state") not in ["locked", "queued"]:
        await update.message.reply_text("You don't have an active interest request.")
        return

    active_request_id = state_result.data[0].get("active_request_id")
    if not active_request_id:
        await update.message.reply_text("You don't have an active interest request.")
        return

    req_result = (
        supabase.table("requests")
        .select("*")
        .eq("id", active_request_id)
        .limit(1)
        .execute()
    )

    if not req_result.data:
        await update.message.reply_text("No active request found.")
        return

    req = req_result.data[0]
    profile_id = req["profile_id"]
    is_active = req.get("is_active_request", False)
    queue_pos = req.get("queue_position", 1)

    if is_active:
        await update.message.reply_text(
            "Your current interest request:\n\n"
            "Profile: " + profile_id + "\n"
            "Status: ⏳ Pending — waiting for owner response\n\n"
            "To withdraw, tap below or send /withdraw",
            reply_markup=interest_confirmation_markup(active_request_id),
        )
    else:
        await update.message.reply_text(
            "Your current interest request:\n\n"
            "Profile: " + profile_id + "\n"
            "Status: 🔢 In queue (position " + str(queue_pos) + ")\n\n"
            "You'll be notified when it's your turn insha'Allah.\n"
            "To leave the queue, tap below or send /withdraw",
            reply_markup=queue_confirmation_markup(active_request_id),
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
        await update.message.reply_text("Profile " + profile_id + " not found or inactive.")
        return

    p = result.data[0]
    is_new = not p.get("notified")
    text = build_profile_text(p)
    if is_new:
        text = "🆕 NEW PROFILE\n\n" + text

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        reply_markup=profile_button_markup(profile_id),
    )
    await update.message.reply_text("Profile " + profile_id + " posted to channel.")

    if is_new:
        owner_tg_id = p.get("owner_telegram_user_id")
        owner_username = p.get("owner_telegram_username", "")
        welcome_msg = (
            "Assalamu alaikum! 🌸\n\n"
            "JazakAllahu khayran — your Mithaq profile " + profile_id + " is now live in the channel!\n\n"
            "Here's what happens next:\n\n"
            "1️⃣ Channel members can tap Express Interest on your profile\n"
            "2️⃣ You'll receive a message here with Approve and Decline buttons\n"
            "3️⃣ If you Approve, the person receives your contact details\n"
            "4️⃣ If you Decline, they are notified and may look at other profiles\n\n"
            "📌 You are in full control — nothing is shared without your approval\n"
            "📌 Only first name and wali contact are shared upon approval (for sisters)\n\n"
            "Questions? Contact @MithaqAdmin 🤲\n\n"
            "May Allah make it easy for you 🤲"
        )
        sent = False
        if owner_tg_id:
            try:
                await context.bot.send_message(chat_id=owner_tg_id, text=welcome_msg)
                sent = True
            except Exception as e:
                logging.warning("Could not send welcome to owner: " + str(e))
        if not sent:
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text="Could not send welcome to owner of " + profile_id + " (@" + owner_username + ") — they may not have started the bot yet."
            )
        supabase.table("profiles").update({"notified": True}).eq("id", profile_id).execute()


async def interest_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    _, profile_id = query.data.split(":", 1)

    state_result = (
        supabase.table("user_state")
        .select("*")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )

    user_state = state_result.data[0].get("state") if state_result.data else "free"

    if user_state == "locked":
        await query.answer(
            "You already have an active pending request. Send /my_request to see it or /withdraw to cancel it.",
            show_alert=True,
        )
        return

    profile_result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    if not profile_result.data:
        await query.answer("Profile not found.", show_alert=True)
        return

    profile = profile_result.data[0]
    owner_username = profile.get("owner_telegram_username", "")
    owner_tg_id = profile.get("owner_telegram_user_id")

    active_check = (
        supabase.table("requests")
        .select("id")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .eq("is_active_request", True)
        .limit(1)
        .execute()
    )

    queue_count = (
        supabase.table("requests")
        .select("id", count="exact")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .execute()
    )

    queue_position = (queue_count.count or 0) + 1
    is_first_in_queue = len(active_check.data) == 0

    request_result = (
        supabase.table("requests")
        .insert({
            "requester_telegram_user_id": user.id,
            "requester_username": user.username or "unknown",
            "profile_id": profile_id,
            "status": "pending",
            "is_active_request": is_first_in_queue,
            "queue_position": queue_position,
        })
        .execute()
    )

    if not request_result.data:
        await query.answer("Something went wrong. Please try again.", show_alert=True)
        return

    request_id = request_result.data[0]["id"]

    if is_first_in_queue:
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

        await query.answer(
            "✅ Interest sent! You will be notified of the response insha'Allah.",
            show_alert=True,
        )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "JazakAllahu khayran! Your interest in profile " + profile_id + " has been recorded. "
                "The profile owner will be notified and will respond insha'Allah. 🤲\n\n"
                "📌 To withdraw your interest at any time, tap below or send /withdraw\n"
                "📌 To check your request status, send /my_request"
            ),
            reply_markup=interest_confirmation_markup(request_id),
        )

        username_display = "@" + user.username if user.username else "User ID: " + str(user.id)
        request_text = (
            "New Interest Request for your profile " + profile_id + "\n\n"
            "Someone has expressed interest in your profile.\n\n"
            "Request ID: " + str(request_id) + "\n"
            "From: " + username_display + "\n\n"
            "Please tap Approve or Decline below."
        )

        admin_text = (
            "🔔 New Interest Request\n\n"
            "Profile: " + profile_id + "\n"
            "From: " + username_display + "\n"
            "Owner: @" + owner_username + "\n"
            "Request ID: " + str(request_id)
        )

        sent_to_owner = False
        if owner_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=owner_tg_id,
                    text=request_text,
                    reply_markup=owner_request_markup(request_id),
                )
                sent_to_owner = True
            except Exception as e:
                logging.warning("Could not message owner: " + str(e))

        admin_text += "\n\n✅ Request sent to owner. You can also approve/decline below." if sent_to_owner else "\n\n⚠️ Owner not registered — approve/decline below."

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text=admin_text,
            reply_markup=admin_request_markup(request_id),
        )

    else:
        if state_result.data:
            supabase.table("user_state").update({
                "active_request_id": request_id,
                "state": "queued",
            }).eq("telegram_user_id", user.id).execute()
        else:
            supabase.table("user_state").insert({
                "telegram_user_id": user.id,
                "active_request_id": request_id,
                "state": "queued",
            }).execute()

        await query.answer(
            "✅ You've been added to the queue for this profile insha'Allah.",
            show_alert=True,
        )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "JazakAllahu khayran! You have been added to the queue for profile " + profile_id + ". 🤲\n\n"
                "You are number " + str(queue_position) + " in the queue.\n"
                "You will be notified when it's your turn insha'Allah.\n\n"
                "📌 You are free to express interest in other profiles while you wait\n"
                "📌 To leave the queue, tap below or send /withdraw"
            ),
            reply_markup=queue_confirmation_markup(request_id),
        )

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text=(
                "🔢 Queue Update\n\n"
                "Profile: " + profile_id + "\n"
                "@" + str(user.username or user.id) + " added to queue at position " + str(queue_position)
            ),
        )


async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    await query.answer()

    action, request_id_str = query.data.split(":", 1)
    request_id = int(request_id_str)

    if action == "withdraw":
        req_result = (
            supabase.table("requests")
            .select("*")
            .eq("id", request_id)
            .limit(1)
            .execute()
        )

        if not req_result.data or req_result.data[0].get("status") != "pending":
            await query.edit_message_text("This request has already been decided.")
            return

        profile_id = req_result.data[0]["profile_id"]
        was_active = req_result.data[0].get("is_active_request", False)
        requester_id = req_result.data[0]["requester_telegram_user_id"]

        supabase.table("requests").update({
            "status": "withdrawn",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", request_id).execute()

        supabase.table("user_state").update({
            "active_request_id": None,
            "state": "free",
        }).eq("telegram_user_id", requester_id).execute()

        await query.edit_message_text("Your interest request has been withdrawn. You may now express interest in another profile.")

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text="Request " + str(request_id) + " withdrawn by @" + str(user.username or user.id),
        )

        if was_active:
            await advance_queue(profile_id, context)
        return

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

    if req.get("status") != "pending":
        await query.edit_message_text("This request has already been decided.")
        return

    requester_id = req["requester_telegram_user_id"]
    profile_id = req["profile_id"]

    profile_result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    owner_tg_id = None
    owner_username = ""
    if profile_result.data:
        owner_tg_id = profile_result.data[0].get("owner_telegram_user_id")
        owner_username = profile_result.data[0].get("owner_telegram_username", "")

    is_admin = user.id == ADMIN_TELEGRAM_USER_ID
    is_owner = (owner_tg_id and user.id == owner_tg_id) or (user.username and user.username.lower() == owner_username.lower())

    if not is_admin and not is_owner:
        await query.answer("Not authorised.", show_alert=True)
        return

    if action == "approve":
        supabase.table("requests").update({
            "status": "approved",
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "decided_by_admin": user.id,
        }).eq("id", request_id).execute()

        supabase.table("user_state").update({
            "active_request_id": None,
            "state": "free",
        }).eq("telegram_user_id", requester_id).execute()

        p = profile_result.data[0] if profile_result.data else {}
        gender = p.get("gender", "").lower()
        full_name = p.get("full_name", "")
        phone = p.get("phone", "")
        wali = p.get("wali_contact", "")
        tg_username = p.get("owner_telegram_username", "")

        if "sister" in gender or "female" in gender:
            first_name = full_name.split()[0] if full_name else ""
            contact_msg = (
                "Alhamdulillah! Your interest in profile " + profile_id + " has been approved. 🤲\n\n"
                "Here are the contact details:\n"
                "First Name: " + first_name + "\n"
                "Wali Contact: " + wali + "\n\n"
                "Please contact the wali to proceed insha'Allah."
            )
        else:
            contact_msg = (
                "Alhamdulillah! Your interest in profile " + profile_id + " has been approved. 🤲\n\n"
                "Here are the contact details:\n"
                "Name: " + full_name + "\n"
                "Telegram: @" + tg_username + "\n"
                "Phone: " + phone + "\n\n"
                "JazakAllahu khayran."
            )

        await context.bot.send_message(chat_id=requester_id, text=contact_msg)

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text="✅ Approved: profile " + profile_id + " request " + str(request_id) + " from @" + str(req.get("requester_username", requester_id)) + " by @" + str(user.username or user.id),
        )

        await query.edit_message_text("✅ You approved request " + str(request_id) + " for profile " + profile_id + ".")

        # Decline all remaining queue for this profile
        remaining = (
            supabase.table("requests")
            .select("*")
            .eq("profile_id", profile_id)
            .eq("status", "pending")
            .execute()
        )

        for r in (remaining.data or []):
            supabase.table("requests").update({
                "status": "declined",
                "decided_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", r["id"]).execute()

            supabase.table("user_state").update({
                "active_request_id": None,
                "state": "free",
            }).eq("telegram_user_id", r["requester_telegram_user_id"]).execute()

            try:
                await context.bot.send_message(
                    chat_id=r["requester_telegram_user_id"],
                    text="JazakAllahu khayran for your interest in profile " + profile_id + ". Unfortunately this profile is no longer available. You are welcome to express interest in another profile. 🤲"
                )
            except Exception as e:
                logging.warning("Could not notify queued user: " + str(e))

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
            text="JazakAllahu khayran for your interest in profile " + profile_id + ". Unfortunately this match was not taken forward at this time. You are welcome to express interest in another profile. 🤲"
        )

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text="❌ Declined: profile " + profile_id + " request " + str(request_id) + " from @" + str(req.get("requester_username", requester_id)) + " by @" + str(user.username or user.id),
        )

        await query.edit_message_text("❌ You declined request " + str(request_id) + " for profile " + profile_id + ".")

        await advance_queue(profile_id, context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    lines = ["Pending Requests:\n"]
    for r in result.data:
        active = "🔔 Active" if r.get("is_active_request") else "🔢 Queue #" + str(r.get("queue_position", "?"))
        lines.append(active + " — " + r["profile_id"] + " from @" + str(r.get("requester_username", r["requester_telegram_user_id"])))

    await update.message.reply_text("\n".join(lines))


async def unlock_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    await update.message.reply_text("User " + str(target_id) + " has been unlocked.")


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    total_profiles = supabase.table("profiles").select("id", count="exact").execute()
    active_profiles = supabase.table("profiles").select("id", count="exact").eq("is_active", True).execute()
    pending = supabase.table("requests").select("id", count="exact").eq("status", "pending").execute()
    approved = supabase.table("requests").select("id", count="exact").eq("status", "approved").execute()
    declined = supabase.table("requests").select("id", count="exact").eq("status", "declined").execute()
    withdrawn = supabase.table("requests").select("id", count="exact").eq("status", "withdrawn").execute()

    recent = supabase.table("requests").select("*").eq("status", "pending").eq("is_active_request", True).order("created_at", desc=True).limit(5).execute()

    lines = [
        "📊 Mithaq Dashboard\n",
        "👥 Total profiles: " + str(total_profiles.count),
        "✅ Active profiles: " + str(active_profiles.count),
        "",
        "🔔 Pending requests: " + str(pending.count),
        "✅ Approved: " + str(approved.count),
        "❌ Declined: " + str(declined.count),
        "🔄 Withdrawn: " + str(withdrawn.count),
    ]

    if recent.data:
        lines.append("\nActive requests:")
        for r in recent.data:
            lines.append("• " + r["profile_id"] + " — @" + str(r.get("requester_username", r["requester_telegram_user_id"])))

    await update.message.reply_text("\n".join(lines))


# ── Main ───────────────────────────────────────────────────────────────────────

def run_flask():
    port = int(os.environ.get("FLASK_PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


def main() -> None:
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"✅ Flask webhook server started on port {os.environ.get('FLASK_PORT', 8080)}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post_profile", post_profile))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("unlock", unlock_user))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("withdraw", withdraw_command))
    app.add_handler(CommandHandler("my_request", my_request))
    app.add_handler(CallbackQueryHandler(interest_clicked, pattern=r"^interest:"))
    app.add_handler(CallbackQueryHandler(handle_decision, pattern=r"^(approve|decline|withdraw):"))

    print("✅ Mithaq bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
