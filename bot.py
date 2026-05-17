import asyncio
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
CHANNEL_LINK = "https://t.me/+ilWsgu9hLb02ODQ0"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

flask_app = Flask(__name__)


# ── Markup helpers ─────────────────────────────────────────────────────────────

def profile_button_markup(profile_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📩 Express Interest", callback_data="interest:" + profile_id)]]
    )


def owner_request_markup(request_id: int, requester_has_photo: bool, owner_has_photo: bool) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("✅ Approve", callback_data="approve:" + str(request_id)),
    ]
    if requester_has_photo and owner_has_photo:
        buttons.append(InlineKeyboardButton("📷 Approve & Share Photos", callback_data="approve_photo:" + str(request_id)))
    buttons.append(InlineKeyboardButton("❌ Decline", callback_data="decline:" + str(request_id)))
    return InlineKeyboardMarkup([buttons])


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


def pause_markup(profile_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏸ Pause Profile", callback_data="pause:" + profile_id)]]
    )


def resume_markup(profile_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Resume Profile", callback_data="resume:" + profile_id)]]
    )


# ── Welcome message builder ────────────────────────────────────────────────────

def build_welcome_message(profile_id: str) -> str:
    return (
        "Assalamu alaikum! 🌸\n\n"
        "JazakAllahu khayran — your Mithaq profile " + profile_id + " is now live in the channel!\n\n"
        "Here's what happens next:\n\n"
        "1️⃣ Channel members can tap 📩 Express Interest on your profile\n"
        "2️⃣ You'll receive a message here with Approve and Decline buttons\n"
        "3️⃣ If you Approve, contact details are exchanged between both parties\n"
        "4️⃣ If you Decline, they are notified and may look at other profiles\n\n"
        "📌 You are in full control — nothing is shared without your approval\n"
        "📌 Only first name and wali contact are shared upon approval (for sisters)\n\n"
        "📢 Browse profiles and express interest here: " + CHANNEL_LINK + "\n\n"
        "📌 You can pause your profile at any time using the button below.\n\n"
        "📌 If you ever stop receiving notifications, simply type /start again to reactivate your account.\n\n"
        "Questions? Contact @MithaqAdmin 🤲\n\n"
        "May Allah make it easy for you 🤲"
    )


# ── Profile text builder ───────────────────────────────────────────────────────

def build_profile_text(p: dict) -> str:
    raw = p.get("formatted_text") or ""
    if raw:
        lines = raw.split("\n")
        if lines and "BROTHER" not in lines[0] and "SISTER" not in lines[0]:
            lines = lines[1:]
        return "\n".join(lines)

    gender = p.get("gender", "").lower()
    gender_emoji = "🟣" if ("female" in gender or "sister" in gender) else "🔵"
    gender_label = "SISTER" if ("female" in gender or "sister" in gender) else "BROTHER"

    lines = []

    lines.append(f"{gender_emoji} {gender_label} — {p['id']}")
    lines.append(f"👤 {p.get('display_name', '')}")
    if p.get('city') or p.get('country'):
        lines.append(f"📍 {', '.join(filter(None, [p.get('city'), p.get('country')]))}")

    lines.append("")

    if p.get('deen'):             lines.append(f"🕌 Deen: {p['deen']}")
    if p.get('prayer'):           lines.append(f"🙏 Prayer: {p['prayer']}")
    if p.get('madhab'):           lines.append(f"📚 Madhab: {p['madhab']}")
    if p.get('islamic_classes'):  lines.append(f"📖 Islamic classes: {p['islamic_classes']}")
    if p.get('revert'):           lines.append(f"🔄 Revert: {p['revert']}")

    lines.append("")

    if p.get('occupation'):       lines.append(f"💼 Occupation: {p['occupation']}")
    if p.get('education'):        lines.append(f"🎓 Education: {p['education']}")
    if p.get('languages'):        lines.append(f"🗣️ Languages: {p['languages']}")
    if p.get('nationality'):      lines.append(f"🌍 Nationality: {p['nationality']}")
    if p.get('ethnicity'):        lines.append(f"🌺 Ethnicity: {p['ethnicity']}")
    if p.get('marital_status'):   lines.append(f"💍 Marital Status: {p['marital_status']}")
    if p.get('children'):         lines.append(f"👶 Children: {p['children']}")

    lines.append("")

    if p.get('about'):
        lines.append("✨ About:")
        lines.append(p['about'])

    lines.append("")

    if p.get('personality_traits'): lines.append(f"🌱 Personality Traits: {p['personality_traits']}")
    if p.get('interests'):          lines.append(f"🎯 Interests: {p['interests']}")
    if p.get('goals'):              lines.append(f"🏡 Goals: {p['goals']}")

    lines.append("")

    if p.get('looking_for'):      lines.append(f"🔍 Looking for: {p['looking_for']}")

    lines.append("")

    if p.get('spouse_deen_level'): lines.append(f"❤️ Spouse Deen Level: {p['spouse_deen_level']}")
    if p.get('marriage_dynamic'):  lines.append(f"🤝 Marriage Dynamic: {p['marriage_dynamic']}")

    if p.get('additional'):
        lines.append("")
        lines.append(f"📋 Additional: {p['additional']}")

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


# ── Helper: look up a requester's own profile ─────────────────────────────────

def get_requester_profile_id(username: str) -> str:
    if not username:
        return None
    result = (
        supabase.table("profiles")
        .select("id")
        .eq("owner_telegram_username", username.lower())
        .limit(1)
        .execute()
    )
    return result.data[0]["id"] if result.data else None


def get_requester_profile(username: str) -> dict:
    if not username:
        return None
    result = (
        supabase.table("profiles")
        .select("*")
        .eq("owner_telegram_username", username.lower())
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ── Repost profile helper ──────────────────────────────────────────────────────

async def repost_profile(profile_id: str, context) -> None:
    try:
        profile_result = (
            supabase.table("profiles")
            .select("*")
            .eq("id", profile_id)
            .eq("is_active", True)
            .eq("is_paused", False)
            .limit(1)
            .execute()
        )

        if not profile_result.data:
            logging.info(f"Skipping repost for {profile_id} — not active or paused")
            return

        p = profile_result.data[0]
        text = build_profile_text(p)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            reply_markup=profile_button_markup(profile_id),
        )
        logging.info(f"✅ Reposted {profile_id} to channel after becoming available")

    except Exception as e:
        logging.warning(f"Could not repost {profile_id}: {str(e)}")


# ── Queue helper ───────────────────────────────────────────────────────────────

async def advance_queue(profile_id: str, context, repost_if_empty: bool = False) -> None:
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
        if repost_if_empty:
            await repost_profile(profile_id, context)
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
    owner_photo_url = None
    if profile_result.data:
        owner_tg_id = profile_result.data[0].get("owner_telegram_user_id")
        owner_username = profile_result.data[0].get("owner_telegram_username", "")
        owner_photo_url = profile_result.data[0].get("photo_url")

    await context.bot.send_message(
        chat_id=next_requester_id,
        text=(
            "🔔 It's your turn! Your interest in profile " + profile_id + " is now being considered by the profile owner insha'Allah. 🤲\n\n"
            "You will be notified of their decision.\n\n"
            "To withdraw, tap the button below or send /withdraw"
        ),
        reply_markup=interest_confirmation_markup(next_request_id),
    )

    requester_profile = get_requester_profile(next_username)
    requester_profile_id = requester_profile["id"] if requester_profile else None
    requester_photo_url = requester_profile["photo_url"] if requester_profile else None
    requester_profile_text = "Profile " + requester_profile_id if requester_profile_id else "Anonymous"

    requester_has_photo = bool(requester_photo_url)
    owner_has_photo = bool(owner_photo_url)

    photo_line = "\n📷 They have a photo to share." if requester_has_photo else "\n📷 They have not uploaded a photo."

    request_text = (
        "New Interest Request for your profile " + profile_id + "\n\n"
        + requester_profile_text + " has expressed interest in your profile."
        + photo_line + "\n\n"
        "Please tap Approve or Decline below."
    )

    admin_text = (
        "🔔 Queue Advanced — New Interest Request\n\n"
        "Profile: " + profile_id + "\n"
        "From: @" + next_username + " (" + requester_profile_text + ")\n"
        "Owner: @" + owner_username
    )

    sent_to_owner = False
    if owner_tg_id:
        try:
            await context.bot.send_message(
                chat_id=owner_tg_id,
                text=request_text,
                reply_markup=owner_request_markup(next_request_id, requester_has_photo, owner_has_photo),
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
            {"text": "📩 Express Interest", "callback_data": "interest:" + profile_id}
        ]]
    }

    success = send_telegram_message(CHANNEL_ID, text, reply_markup)

    if not success:
        return jsonify({"error": "Failed to send to Telegram"}), 500

    supabase.table("profiles").update({"notified": True}).eq("id", profile_id).execute()

    owner_tg_id = p.get("owner_telegram_user_id")
    owner_username = p.get("owner_telegram_username", "")
    if is_new:
        welcome_msg = build_welcome_message(profile_id)
        if owner_tg_id:
            send_telegram_message(str(owner_tg_id), welcome_msg,
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "⏸ Pause Profile", "callback_data": "pause:" + profile_id}
                    ]]
                }
            )
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

    if context.args:
        arg = context.args[0]
        if arg.startswith("aff_"):
            affiliate_code = arg[4:]
            try:
                aff_result = (
                    supabase.table("affiliates")
                    .select("code")
                    .eq("code", affiliate_code)
                    .limit(1)
                    .execute()
                )
                if aff_result.data:
                    existing = (
                        supabase.table("referrals")
                        .select("id")
                        .eq("telegram_user_id", user.id)
                        .limit(1)
                        .execute()
                    )
                    if not existing.data:
                        supabase.table("referrals").insert({
                            "affiliate_code": affiliate_code,
                            "telegram_user_id": user.id,
                            "telegram_username": username,
                        }).execute()
                        logging.info(f"Referral recorded: {user.id} via {affiliate_code}")
            except Exception as e:
                logging.warning("Could not record referral: " + str(e))

    try:
        existing_state = (
            supabase.table("user_state")
            .select("id")
            .eq("telegram_user_id", user.id)
            .limit(1)
            .execute()
        )
        if not existing_state.data:
            supabase.table("user_state").insert({
                "telegram_user_id": user.id,
                "state": "free",
            }).execute()
            logging.info(f"user_state created for {user.id} @{username}")
    except Exception as e:
        logging.warning("Could not create user_state: " + str(e))

    if username:
        result = (
            supabase.table("profiles")
            .select("id, owner_telegram_user_id, is_paused")
            .eq("owner_telegram_username", username)
            .limit(1)
            .execute()
        )
        if result.data:
            profile = result.data[0]
            profile_id = profile["id"]
            is_paused = profile.get("is_paused", False)

            if not profile.get("owner_telegram_user_id"):
                supabase.table("profiles").update({
                    "owner_telegram_user_id": user.id
                }).eq("id", profile_id).execute()
                logging.info(f"Owner registered on start: {profile_id} @{username} ID {user.id}")
                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_USER_ID,
                    text="✅ Owner registered: " + profile_id + " @" + username + " ID " + str(user.id),
                )
                welcome_msg = build_welcome_message(profile_id)
                await update.message.reply_text(
                    welcome_msg,
                    reply_markup=resume_markup(profile_id) if is_paused else pause_markup(profile_id),
                )
            else:
                status_text = "⏸ Your profile is currently *paused*." if is_paused else "✅ Your profile is currently *active*."
                await update.message.reply_text(
                    "📋 Your profile: *" + profile_id + "*\n\n" + status_text + "\n\n"
                    "📢 Browse profiles here: " + CHANNEL_LINK + "\n\n"
                    "📌 If you are not receiving notifications, please type /start again.",
                    parse_mode="Markdown",
                    reply_markup=resume_markup(profile_id) if is_paused else pause_markup(profile_id),
                )
            return

    if not username:
        await update.message.reply_text(
            "Assalamu alaikum! 🌸\n\n"
            "⚠️ We noticed you don't have a Telegram username set.\n\n"
            "To use Mithaq you need a Telegram username.\n\n"
            "📱 Go to: Settings → tap your name → Username → set one\n\n"
            "Once done, type /start again and you'll be all set insha'Allah. 🤲"
        )
        return

    await update.message.reply_text(
        "Assalamu alaikum! Welcome to Mithaq Marriage 🌸\n\n"
        "Here's how it works:\n\n"
        "1️⃣ Browse profiles in the channel\n"
        "2️⃣ Tap 📩 Express Interest on any profile you like\n"
        "3️⃣ The profile owner will be notified and will Approve or Decline\n"
        "4️⃣ If approved, contact details are exchanged between both parties\n\n"
        "📌 You can only have one active request at a time\n"
        "📌 If declined, you're free to express interest in another profile\n"
        "📌 You can withdraw your interest at any time — just send /withdraw\n"
        "📌 To check your request status, send /my_request\n"
        "📌 If you ever stop receiving notifications, simply type /start again\n\n"
        "📢 Browse profiles here: " + CHANNEL_LINK + "\n\n"
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
        await update.message.reply_text("Your request has already been handled. You are free to express interest in another profile.")
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
        await advance_queue(profile_id, context, repost_if_empty=True)


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
        welcome_msg = build_welcome_message(profile_id)
        sent = False
        if owner_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=owner_tg_id,
                    text=welcome_msg,
                    reply_markup=pause_markup(profile_id),
                )
                sent = True
            except Exception as e:
                logging.warning("Could not send welcome to owner: " + str(e))
        if not sent:
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text="Could not send welcome to owner of " + profile_id + " (@" + owner_username + ") — they may not have started the bot yet."
            )
        supabase.table("profiles").update({"notified": True}).eq("id", profile_id).execute()


async def repost_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    await update.message.reply_text("⏳ Reposting all active profiles with updated format. This will take a few minutes...")

    result = (
        supabase.table("profiles")
        .select("*")
        .eq("is_active", True)
        .eq("is_paused", False)
        .order("id", desc=False)
        .execute()
    )

    if not result.data:
        await update.message.reply_text("No active profiles found.")
        return

    success_count = 0
    fail_count = 0

    for p in result.data:
        profile_id = p["id"]
        try:
            text = build_profile_text(p)
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                reply_markup=profile_button_markup(profile_id),
            )
            success_count += 1
            logging.info(f"Reposted {profile_id}")
            await asyncio.sleep(2)
        except Exception as e:
            fail_count += 1
            logging.warning(f"Failed to repost {profile_id}: {str(e)}")
            await asyncio.sleep(3)

    await update.message.reply_text(
        f"✅ Repost complete.\n\n"
        f"✅ Success: {success_count}\n"
        f"❌ Failed: {fail_count}\n\n"
        f"All active profiles have been reposted with the full updated format."
    )


async def interest_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    _, profile_id = query.data.split(":", 1)

    if not user.username:
        await query.answer(
            "You need a Telegram username to use Mithaq. Go to Settings → Username to set one.",
            show_alert=True,
        )
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "To use Mithaq you need a Telegram username.\n\n"
                    "📱 Go to: Settings → tap your name → Username → set one\n\n"
                    "Once done, type /start and then you can express interest in profiles insha'Allah. 🤲"
                )
            )
        except Exception:
            pass
        return

    try:
        user_started = (
            supabase.table("user_state")
            .select("id")
            .eq("telegram_user_id", user.id)
            .limit(1)
            .execute()
        )
        if not user_started.data:
            await query.answer(
                "Please start the Mithaq bot first. Open @Mithaq_Marriage_bot, type /start and send it — then you can express interest insha'Allah.",
                show_alert=True,
            )
            return
    except Exception as e:
        logging.warning("Could not check user_state: " + str(e))

    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user.id)
        if member.status in ("left", "kicked", "banned"):
            await query.answer(
                "You must submit a profile to Mithaq before expressing interest.",
                show_alert=True,
            )
            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=(
                        "To express interest in profiles, you must first submit your own profile to Mithaq.\n\n"
                        "📝 Submit your profile here: mithaqmarriage.com\n\n"
                        "Once your profile is live, you'll be able to express interest in others insha'Allah. 🤲"
                    )
                )
            except Exception:
                pass
            return
    except Exception as e:
        logging.warning("Could not check channel membership: " + str(e))

    requester_profile = get_requester_profile(user.username)
    if not requester_profile:
        await query.answer(
            "You need a Mithaq profile to express interest. Visit mithaqmarriage.com to submit yours.",
            show_alert=True,
        )
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "You don't have a Mithaq profile yet.\n\n"
                    "📝 Submit your profile here: mithaqmarriage.com\n\n"
                    "Once your profile is live, you'll be able to express interest in others insha'Allah. 🤲"
                )
            )
        except Exception:
            pass
        return

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

    if profile.get("is_paused"):
        await query.answer(
            "This profile is temporarily paused. Please check back later.",
            show_alert=True,
        )
        return

    owner_username = profile.get("owner_telegram_username", "")
    owner_tg_id = profile.get("owner_telegram_user_id")
    owner_photo_url = profile.get("photo_url")

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

    requester_profile_id = requester_profile["id"] if requester_profile else None
    requester_photo_url = requester_profile.get("photo_url") if requester_profile else None
    requester_profile_text = "Profile " + requester_profile_id if requester_profile_id else "Anonymous"

    requester_has_photo = bool(requester_photo_url)
    owner_has_photo = bool(owner_photo_url)

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

        photo_line = "\n📷 They have a photo to share." if requester_has_photo else "\n📷 They have not uploaded a photo."

        request_text = (
            "New Interest Request for your profile " + profile_id + "\n\n"
            + requester_profile_text + " has expressed interest in your profile."
            + photo_line + "\n\n"
            "Please tap Approve or Decline below."
        )

        admin_text = (
            "🔔 New Interest Request\n\n"
            "Profile: " + profile_id + "\n"
            "From: @" + str(user.username or user.id) + " (" + requester_profile_text + ")\n"
            "Owner: @" + owner_username
        )

        sent_to_owner = False
        if owner_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=owner_tg_id,
                    text=request_text,
                    reply_markup=owner_request_markup(request_id, requester_has_photo, owner_has_photo),
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
                "@" + str(user.username or user.id) + " (" + requester_profile_text + ") added to queue at position " + str(queue_position)
            ),
        )


async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    await query.answer()

    action, request_id_str = query.data.split(":", 1)
    request_id_or_profile = request_id_str

    if action in ("pause", "resume"):
        profile_id = request_id_or_profile

        profile_result = (
            supabase.table("profiles")
            .select("*")
            .eq("id", profile_id)
            .limit(1)
            .execute()
        )

        if not profile_result.data:
            await query.edit_message_text("Profile not found.")
            return

        profile = profile_result.data[0]
        owner_tg_id = profile.get("owner_telegram_user_id")
        is_admin = user.id == ADMIN_TELEGRAM_USER_ID
        is_owner = (owner_tg_id and user.id == owner_tg_id)

        if not is_admin and not is_owner:
            await query.answer("Not authorised.", show_alert=True)
            return

        if action == "pause":
            supabase.table("profiles").update({"is_paused": True}).eq("id", profile_id).execute()
            await query.edit_message_reply_markup(reply_markup=resume_markup(profile_id))
            await context.bot.send_message(
                chat_id=user.id,
                text="⏸ Your profile " + profile_id + " has been paused. No new interest requests will be accepted until you resume. 🤲"
            )
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text="⏸ Profile " + profile_id + " paused by owner."
            )
        elif action == "resume":
            supabase.table("profiles").update({"is_paused": False}).eq("id", profile_id).execute()
            await query.edit_message_reply_markup(reply_markup=pause_markup(profile_id))
            await context.bot.send_message(
                chat_id=user.id,
                text="▶️ Your profile " + profile_id + " has been resumed. You will now receive interest requests again. 🤲"
            )
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text="▶️ Profile " + profile_id + " resumed by owner."
            )
            await advance_queue(profile_id, context)
        return

    request_id = int(request_id_or_profile)

    if action == "withdraw":
        req_result = (
            supabase.table("requests")
            .select("*")
            .eq("id", request_id)
            .limit(1)
            .execute()
        )

        if not req_result.data or req_result.data[0].get("status") != "pending":
            await query.edit_message_text("This request has already been handled.")
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
            await advance_queue(profile_id, context, repost_if_empty=True)
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
        await query.edit_message_text("This request has already been handled.")
        return

    requester_id = req["requester_telegram_user_id"]
    requester_username = req.get("requester_username", "")
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
    owner_photo_url = None
    if profile_result.data:
        owner_tg_id = profile_result.data[0].get("owner_telegram_user_id")
        owner_username = profile_result.data[0].get("owner_telegram_username", "")
        owner_photo_url = profile_result.data[0].get("photo_url")

    is_admin = user.id == ADMIN_TELEGRAM_USER_ID
    is_owner = (owner_tg_id and user.id == owner_tg_id) or (user.username and user.username.lower() == owner_username.lower())

    if not is_admin and not is_owner:
        await query.answer("Not authorised.", show_alert=True)
        return

    if action in ("approve", "approve_photo"):
        share_photos = (action == "approve_photo")

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

        # ── Send profile owner's contact details to requester ──
        if "sister" in gender or "female" in gender:
            first_name = full_name.split()[0] if full_name else ""
            contact_msg = (
                "Alhamdulillah! Your interest in profile " + profile_id + " has been approved. 🤲\n\n"
                "Here are their contact details:\n"
                "First Name: " + first_name + "\n"
                "Wali Contact: " + wali + "\n\n"
                "Please contact the wali to proceed insha'Allah.\n\n"
                "May Allah make it easy for you both. 🤲"
            )
        else:
            contact_msg = (
                "Alhamdulillah! Your interest in profile " + profile_id + " has been approved. 🤲\n\n"
                "Here are their contact details:\n"
                "Name: " + full_name + "\n"
                "Telegram: @" + tg_username + "\n"
                "Phone: " + phone + "\n\n"
                "May Allah make it easy for you both. 🤲"
            )

        await context.bot.send_message(chat_id=requester_id, text=contact_msg)

        # ── Send requester's contact details to profile owner ──
        requester_profile_full = get_requester_profile(requester_username)
        if requester_profile_full and owner_tg_id:
            req_gender = requester_profile_full.get("gender", "").lower()
            req_full_name = requester_profile_full.get("full_name", "")
            req_phone = requester_profile_full.get("phone", "")
            req_wali = requester_profile_full.get("wali_contact", "")
            req_tg_username = requester_profile_full.get("owner_telegram_username", "")
            req_profile_id = requester_profile_full.get("id", "")

            if "sister" in req_gender or "female" in req_gender:
                req_first_name = req_full_name.split()[0] if req_full_name else ""
                owner_contact_msg = (
                    "✅ You approved the interest from " + req_profile_id + ". Contact details have been exchanged.\n\n"
                    "Here are their contact details:\n"
                    "First Name: " + req_first_name + "\n"
                    "Wali Contact: " + req_wali + "\n\n"
                    "May Allah make it easy for you both. 🤲"
                )
            else:
                owner_contact_msg = (
                    "✅ You approved the interest from " + req_profile_id + ". Contact details have been exchanged.\n\n"
                    "Here are their contact details:\n"
                    "Name: " + req_full_name + "\n"
                    "Telegram: @" + req_tg_username + "\n"
                    "Phone: " + req_phone + "\n\n"
                    "May Allah make it easy for you both. 🤲"
                )
            try:
                await context.bot.send_message(chat_id=owner_tg_id, text=owner_contact_msg)
            except Exception as e:
                logging.warning("Could not send requester contact to owner: " + str(e))
        elif owner_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=owner_tg_id,
                    text="✅ You approved the interest request. Your contact details have been shared with them. May Allah make it easy for you both. 🤲"
                )
            except Exception as e:
                logging.warning("Could not send confirmation to owner: " + str(e))

        # ── Share photos if approve_photo ──
        if share_photos:
            requester_profile = get_requester_profile(requester_username)
            requester_photo_url = requester_profile.get("photo_url") if requester_profile else None

            if requester_photo_url and owner_photo_url:
                try:
                    await context.bot.send_photo(
                        chat_id=requester_id,
                        photo=owner_photo_url,
                        caption="📷 Photo shared by profile " + profile_id,
                    )
                    if owner_tg_id:
                        await context.bot.send_photo(
                            chat_id=owner_tg_id,
                            photo=requester_photo_url,
                            caption="📷 Photo shared by the person interested in your profile",
                        )
                except Exception as e:
                    logging.warning("Could not send photos: " + str(e))

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text="✅ Approved" + (" with photos" if share_photos else "") + ": profile " + profile_id + " request " + str(request_id) + " from @" + str(req.get("requester_username", requester_id)) + " by @" + str(user.username or user.id),
        )

        await query.edit_message_text("✅ You approved request " + str(request_id) + " for profile " + profile_id + (" with photos shared." if share_photos else "."))

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

        await advance_queue(profile_id, context, repost_if_empty=True)


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
    paused_profiles = supabase.table("profiles").select("id", count="exact").eq("is_paused", True).execute()
    pending = supabase.table("requests").select("id", count="exact").eq("status", "pending").execute()
    approved = supabase.table("requests").select("id", count="exact").eq("status", "approved").execute()
    declined = supabase.table("requests").select("id", count="exact").eq("status", "declined").execute()
    withdrawn = supabase.table("requests").select("id", count="exact").eq("status", "withdrawn").execute()

    recent = supabase.table("requests").select("*").eq("status", "pending").eq("is_active_request", True).order("created_at", desc=True).limit(5).execute()

    lines = [
        "📊 Mithaq Dashboard\n",
        "👥 Total profiles: " + str(total_profiles.count),
        "✅ Active profiles: " + str(active_profiles.count),
        "⏸ Paused profiles: " + str(paused_profiles.count),
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


async def add_affiliate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add_affiliate code name\nExample: /add_affiliate ahmed123 Ahmed Ali")
        return

    code = context.args[0].strip().lower()
    name = " ".join(context.args[1:]).strip()

    try:
        supabase.table("affiliates").insert({
            "code": code,
            "name": name,
        }).execute()

        link = f"https://mithaqmarriage.com?ref={code}"

        await update.message.reply_text(
            "✅ Affiliate created!\n\n"
            "Name: " + name + "\n"
            "Code: " + code + "\n"
            "Link: " + link
        )
    except Exception as e:
        await update.message.reply_text("❌ Error: " + str(e))


async def affiliate_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    affiliates = supabase.table("affiliates").select("*").order("created_at", desc=False).execute()

    if not affiliates.data:
        await update.message.reply_text("No affiliates yet.")
        return

    lines = ["📊 Affiliate Stats\n"]
    for aff in affiliates.data:
        code = aff["code"]
        name = aff["name"]

        referrals = (
            supabase.table("referrals")
            .select("id", count="exact")
            .eq("affiliate_code", code)
            .execute()
        )
        conversions = (
            supabase.table("referrals")
            .select("id", count="exact")
            .eq("affiliate_code", code)
            .eq("converted", True)
            .execute()
        )

        lines.append(
            f"👤 {name} ({code})\n"
            f"   Referrals: {referrals.count} | Conversions: {conversions.count}\n"
        )

    await update.message.reply_text("\n".join(lines))


async def convert_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /convert @username or /convert telegram_user_id")
        return

    target = context.args[0].strip().replace("@", "").lower()

    try:
        result = (
            supabase.table("referrals")
            .select("*")
            .eq("telegram_username", target)
            .limit(1)
            .execute()
        )

        if not result.data and target.isdigit():
            result = (
                supabase.table("referrals")
                .select("*")
                .eq("telegram_user_id", int(target))
                .limit(1)
                .execute()
            )

        if not result.data:
            await update.message.reply_text("No referral found for " + target)
            return

        referral = result.data[0]

        if referral.get("converted"):
            await update.message.reply_text("This referral is already marked as converted.")
            return

        supabase.table("referrals").update({
            "converted": True,
            "converted_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", referral["id"]).execute()

        await update.message.reply_text(
            "✅ Referral marked as converted!\n\n"
            "User: @" + str(referral.get("telegram_username", referral["telegram_user_id"])) + "\n"
            "Affiliate: " + referral["affiliate_code"]
        )
    except Exception as e:
        await update.message.reply_text("❌ Error: " + str(e))


async def resend_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /resend_requests MTHAQ-001")
        return

    profile_id = context.args[0].strip()

    profile_result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    if not profile_result.data:
        await update.message.reply_text("Profile " + profile_id + " not found.")
        return

    profile = profile_result.data[0]
    owner_tg_id = profile.get("owner_telegram_user_id")
    owner_photo_url = profile.get("photo_url")

    if not owner_tg_id:
        await update.message.reply_text(
            "Owner of " + profile_id + " is not registered yet — they need to start the bot first."
        )
        return

    pending_requests = (
        supabase.table("requests")
        .select("*")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .eq("is_active_request", True)
        .limit(1)
        .execute()
    )

    if not pending_requests.data:
        await update.message.reply_text("No active pending requests for " + profile_id + ".")
        return

    req = pending_requests.data[0]
    request_id = req["id"]
    requester_username = req.get("requester_username", "")

    requester_profile = get_requester_profile(requester_username)
    requester_profile_id = requester_profile["id"] if requester_profile else None
    requester_photo_url = requester_profile.get("photo_url") if requester_profile else None
    requester_profile_text = "Profile " + requester_profile_id if requester_profile_id else "Anonymous"

    requester_has_photo = bool(requester_photo_url)
    owner_has_photo = bool(owner_photo_url)

    photo_line = "\n📷 They have a photo to share." if requester_has_photo else "\n📷 They have not uploaded a photo."

    request_text = (
        "New Interest Request for your profile " + profile_id + "\n\n"
        + requester_profile_text + " has expressed interest in your profile."
        + photo_line + "\n\n"
        "Please tap Approve or Decline below."
    )

    try:
        await context.bot.send_message(
            chat_id=owner_tg_id,
            text=request_text,
            reply_markup=owner_request_markup(request_id, requester_has_photo, owner_has_photo),
        )
        await update.message.reply_text(
            "✅ Request resent to owner of " + profile_id + " successfully."
        )
    except Exception as e:
        await update.message.reply_text(
            "❌ Could not send to owner: " + str(e)
        )


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
    app.add_handler(CommandHandler("add_affiliate", add_affiliate))
    app.add_handler(CommandHandler("affiliate_stats", affiliate_stats))
    app.add_handler(CommandHandler("convert", convert_referral))
    app.add_handler(CommandHandler("resend_requests", resend_requests))
    app.add_handler(CommandHandler("repost_all", repost_all))
    app.add_handler(CallbackQueryHandler(interest_clicked, pattern=r"^interest:"))
    app.add_handler(CallbackQueryHandler(handle_decision, pattern=r"^(approve|approve_photo|decline|withdraw|pause|resume):"))

    print("✅ Mithaq bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
