import asyncio
import logging
import os
import threading
import requests
from datetime import datetime, timezone, timedelta, date

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
    MessageHandler,
    filters,
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
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
CHANNEL_LINK = "https://t.me/+ilWsgu9hLb02ODQ0"

# ── Repost marker ──────────────────────────────────────────────────────────────
# During the back-catalogue cleanup, every REPOSTED profile carries this header
# so members can instantly tell reposts apart from brand-new profiles. Anything
# WITHOUT the header is a new member by definition. Set MARK_REPOSTS = False
# once the cleanup drip is finished to stop marking reposts.
MARK_REPOSTS = True
REPOST_HEADER = "♻️ Repost — earlier profile, shared again for newer members\n\n"

DECLINE_REASONS = {
    "not_right_fit": "Not the right fit at this time",
    "location": "Location not compatible",
    "age": "Age preference not met",
    "deen": "Looking for a different level of practice",
    "children": "Different preference on children",
    "marital": "Prefers someone who hasn't been married before",
    "istikhara": "Made istikhara — doesn't feel right",
    "break": "Taking a break from searching",
}

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

flask_app = Flask(__name__)


# ── Registration code helper ───────────────────────────────────────────────────

import secrets


def generate_registration_code() -> str:
    """Short, unambiguous code like MTHAQ-7X4K. Avoids confusable characters
    (no 0/O, 1/I/L) so users can't mistype it. Retries until unique."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    for _ in range(10):
        code = "MTHAQ-" + "".join(secrets.choice(alphabet) for _ in range(4))
        # ensure it's not already issued or already on a profile
        a = supabase.table("user_state").select("telegram_user_id").eq("issued_code", code).limit(1).execute()
        b = supabase.table("profiles").select("id").eq("registration_code", code).limit(1).execute()
        if not a.data and not b.data:
            return code
    # extreme fallback — add an extra char
    return "MTHAQ-" + "".join(secrets.choice(alphabet) for _ in range(6))


def link_profile_by_code(profile_row: dict):
    """Given a freshly-submitted profile row that carries a registration_code,
    find the Telegram user that the bot issued that code to (stored in
    user_state.issued_code) and stamp their telegram_user_id onto the profile.
    Returns the telegram_user_id if linked, else None."""
    code = (profile_row.get("registration_code") or "").strip().upper()
    if not code:
        return None
    # already linked? leave it
    if profile_row.get("owner_telegram_user_id"):
        return profile_row.get("owner_telegram_user_id")
    st = (
        supabase.table("user_state")
        .select("telegram_user_id")
        .eq("issued_code", code)
        .limit(1)
        .execute()
    )
    if not st.data:
        return None
    tg_id = st.data[0]["telegram_user_id"]
    supabase.table("profiles").update(
        {"owner_telegram_user_id": tg_id}
    ).eq("id", profile_row["id"]).execute()
    logging.info(f"🔗 Linked {profile_row['id']} to Telegram ID {tg_id} via code {code}")

    # Tell admin — this is the path most signups take (code -> form -> webhook),
    # and it previously fired no notification at all.
    try:
        uname = (profile_row.get("owner_telegram_username") or "").strip()
        send_telegram_message(
            str(ADMIN_TELEGRAM_USER_ID),
            "✅ Profile linked: " + str(profile_row["id"])
            + (" @" + uname if uname else "")
            + " (ID " + str(tg_id) + ") — now receiving notifications."
        )
    except Exception as e:
        logging.warning("Could not send profile-linked admin ping: " + str(e))

    return tg_id


# ── Photo helper ───────────────────────────────────────────────────────────────

def get_photo_ref(profile: dict):
    """Return the best photo reference for a profile, or None.
    Prefers the Telegram file_id (new, private method); falls back to the
    legacy photo_url for existing users. Telegram's send_photo accepts either,
    so callers can pass the result straight to photo=."""
    if not profile:
        return None
    file_id = (profile.get("photo_file_id") or "").strip()
    if file_id:
        return file_id
    url = (profile.get("photo_url") or "").strip()
    if url:
        return url
    return None


# ── Age helper ─────────────────────────────────────────────────────────────────

def calculate_age(dob_value) -> int:
    """Compute age in whole years from a dob value that may be a string
    ('1998-09-30', '30/09/1998', etc.) or a date/datetime. Returns an int,
    or None if the value is missing or cannot be parsed. Guards against
    nonsense (negative / >120) so a bad dob simply hides the age line."""
    if not dob_value:
        return None
    d = None
    if isinstance(dob_value, str):
        s = dob_value.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                d = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
    elif isinstance(dob_value, datetime):
        d = dob_value.date()
    elif isinstance(dob_value, date):
        d = dob_value
    if not d:
        return None
    today = date.today()
    age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
    if age < 0 or age > 120:
        return None
    return age


# ── Markup helpers ─────────────────────────────────────────────────────────────

BOT_USERNAME = os.environ.get("BOT_USERNAME", "Mithaq_Marriage_bot")


def profile_button_markup(profile_id: str) -> InlineKeyboardMarkup:
    # Deep-link button: tapping opens the user's private chat with the bot at
    # /start interest_<profile_id>, where the gender-aware consent + a confirm
    # button are shown before any interest is actually sent. This keeps the
    # channel button clean while ensuring the person sees, in the bot chat,
    # exactly what will be shared if their interest is approved.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "📩 Express Interest",
            url="https://t.me/" + BOT_USERNAME + "?start=interest_" + profile_id,
        )]]
    )


def owner_request_markup(request_id: int, requester_has_photo: bool, owner_has_photo: bool, include_consider: bool = True) -> InlineKeyboardMarkup:
    # Approve and Decline share the top row. When a photo option applies, it goes on
    # its OWN full-width row so the longer "Approve & Share Photos" label isn't
    # truncated by sharing a row with the other two buttons.
    rows = [[
        InlineKeyboardButton("✅ Approve", callback_data="approve:" + str(request_id)),
        InlineKeyboardButton("❌ Decline", callback_data="decline:" + str(request_id)),
    ]]
    if requester_has_photo and owner_has_photo:
        rows.append([InlineKeyboardButton(
            "📷 Approve & also share photos", callback_data="approve_photo:" + str(request_id))])
    if include_consider:
        rows.append([InlineKeyboardButton("🤲 I need time to consider", callback_data="consider:" + str(request_id))])
    return InlineKeyboardMarkup(rows)


def admin_request_markup(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data="approve:" + str(request_id)),
        InlineKeyboardButton("❌ Decline", callback_data="decline:" + str(request_id)),
    ]])


def decline_reason_markup(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Not the right fit", callback_data=f"dr:{request_id}:not_right_fit")],
        [InlineKeyboardButton("📍 Location not compatible", callback_data=f"dr:{request_id}:location")],
        [InlineKeyboardButton("🎂 Age preference not met", callback_data=f"dr:{request_id}:age")],
        [InlineKeyboardButton("🕌 Different level of practice", callback_data=f"dr:{request_id}:deen")],
        [InlineKeyboardButton("👶 Different preference on children", callback_data=f"dr:{request_id}:children")],
        [InlineKeyboardButton("💍 Prefer someone not previously married", callback_data=f"dr:{request_id}:marital")],
        [InlineKeyboardButton("🤲 Istikhara — doesn't feel right", callback_data=f"dr:{request_id}:istikhara")],
        [InlineKeyboardButton("⏸ Taking a break from searching", callback_data=f"dr:{request_id}:break")],
        [InlineKeyboardButton("✏️ Other — type your own reason", callback_data=f"dr:{request_id}:other")],
    ])


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


def available_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Make me available again", callback_data="avail_menu")]]
    )


# ── Welcome message builder ────────────────────────────────────────────────────

def build_welcome_message(profile_id: str) -> str:
    return (
        "Assalamu alaikum, and welcome to Mithaq. 🤍\n\n"
        "JazakAllahu khayran — your profile " + profile_id + " is now live in the channel.\n\n"
        "Mithaq is not a swiping app. It is a serious, dignified path to marriage, built on the "
        "belief that marriage is a mithaq — a solemn covenant. Here, your privacy is protected "
        "and nothing about you is shared without your approval.\n\n"
        "Here's what happens next:\n\n"
        "1️⃣ Members can tap 📩 Express Interest on your profile\n"
        "2️⃣ You'll receive a message here with Approve and Decline buttons\n"
        "3️⃣ If you Approve, contact details are exchanged between both parties\n"
        "4️⃣ If you Decline, they are notified privately and may look at other profiles\n\n"
        "📌 You are in full control — nothing is shared without your approval\n"
        "📌 For sisters, only the wali's contact is shared on approval\n\n"
        "🔔 Please turn ON Telegram notifications for this chat (tap the bot name above → Notifications) — this is how you'll hear about interest in your profile.\n\n"
        "📢 Browse profiles here: " + CHANNEL_LINK + "\n\n"
        "📌 You can pause your profile at any time using the button below.\n\n"
        "📌 If you ever stop receiving notifications, simply type /start again to reactivate your account.\n\n"
        "Questions? Contact @MithaqAdmin. May Allah make it easy for you. 🤲"
    )


# Standalone photo invitation — sent as its own message right after the welcome,
# so it stands out instead of being buried at the bottom of a long message.
def build_photo_invite_message() -> str:
    return (
        "📷 Would you like to add a private photo?\n\n"
        "_Entirely your choice. A photo is only ever shared privately, and only when you "
        "and one other person both approve and both choose to share — never public, never "
        "in the channel._"
    )


def photo_invite_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Yes, add a photo", callback_data="addphoto_start"),
            InlineKeyboardButton("Not now", callback_data="addphoto_notnow"),
        ]]
    )


# ── Profile text builder ───────────────────────────────────────────────────────

def country_flag(country: str) -> str:
    """Return the flag emoji for a country name, or '' if not recognised.
    Handles the common name variants your dropdown may store (e.g. 'United
    States of America' / 'USA' / 'United States'). Case-insensitive. If the
    country isn't found, returns '' so the caller can fall back to the pin.

    Flags are built from the 2-letter ISO code: each letter -> regional
    indicator symbol. So we only need name -> ISO2 here."""
    if not country:
        return ""
    key = country.strip().lower()

    # Common aliases -> ISO2. Extend this map to match your dropdown values.
    ALIASES = {
        "usa": "US", "u.s.a.": "US", "u.s.": "US", "united states": "US",
        "united states of america": "US", "america": "US",
        "uk": "GB", "u.k.": "GB", "united kingdom": "GB",
        "great britain": "GB", "britain": "GB", "england": "GB",
        "scotland": "GB", "wales": "GB", "northern ireland": "GB",
        "uae": "AE", "u.a.e.": "AE", "united arab emirates": "AE",
        "south korea": "KR", "korea": "KR", "north korea": "KP",
        "russia": "RU", "ivory coast": "CI", "cote d'ivoire": "CI",
        "czech republic": "CZ", "czechia": "CZ",
        "democratic republic of the congo": "CD", "dr congo": "CD",
        "republic of the congo": "CG", "congo": "CG",
        "trinidad": "TT", "trinidad and tobago": "TT",
        "bosnia": "BA", "bosnia and herzegovina": "BA",
        "macedonia": "MK", "north macedonia": "MK",
        "palestine": "PS", "palestinian territories": "PS",
        "brunei": "BN", "burma": "MM", "myanmar": "MM",
        "cape verde": "CV", "east timor": "TL", "timor-leste": "TL",
        "swaziland": "SZ", "eswatini": "SZ",
        "tanzania": "TZ", "vietnam": "VN", "laos": "LA", "syria": "SY",
        "iran": "IR", "moldova": "MD", "vatican": "VA",
    }

    # Full name -> ISO2 map (common countries; extend as needed to cover your
    # dropdown exactly).
    NAMES = {
        "afghanistan": "AF", "albania": "AL", "algeria": "DZ", "andorra": "AD",
        "angola": "AO", "argentina": "AR", "armenia": "AM", "australia": "AU",
        "austria": "AT", "azerbaijan": "AZ", "bahamas": "BS", "bahrain": "BH",
        "bangladesh": "BD", "barbados": "BB", "belarus": "BY", "belgium": "BE",
        "belize": "BZ", "benin": "BJ", "bhutan": "BT", "bolivia": "BO",
        "botswana": "BW", "brazil": "BR", "bulgaria": "BG", "burkina faso": "BF",
        "burundi": "BI", "cambodia": "KH", "cameroon": "CM", "canada": "CA",
        "chad": "TD", "chile": "CL", "china": "CN", "colombia": "CO",
        "comoros": "KM", "costa rica": "CR", "croatia": "HR", "cuba": "CU",
        "cyprus": "CY", "denmark": "DK", "djibouti": "DJ", "dominica": "DM",
        "dominican republic": "DO", "ecuador": "EC", "egypt": "EG",
        "el salvador": "SV", "eritrea": "ER", "estonia": "EE", "ethiopia": "ET",
        "fiji": "FJ", "finland": "FI", "france": "FR", "gabon": "GA",
        "gambia": "GM", "georgia": "GE", "germany": "DE", "ghana": "GH",
        "greece": "GR", "grenada": "GD", "guatemala": "GT", "guinea": "GN",
        "guyana": "GY", "haiti": "HT", "honduras": "HN", "hungary": "HU",
        "iceland": "IS", "india": "IN", "indonesia": "ID", "iraq": "IQ",
        "ireland": "IE", "israel": "IL", "italy": "IT", "jamaica": "JM",
        "japan": "JP", "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE",
        "kuwait": "KW", "kyrgyzstan": "KG", "latvia": "LV", "lebanon": "LB",
        "lesotho": "LS", "liberia": "LR", "libya": "LY", "liechtenstein": "LI",
        "lithuania": "LT", "luxembourg": "LU", "madagascar": "MG",
        "malawi": "MW", "malaysia": "MY", "maldives": "MV", "mali": "ML",
        "malta": "MT", "mauritania": "MR", "mauritius": "MU", "mexico": "MX",
        "monaco": "MC", "mongolia": "MN", "montenegro": "ME", "morocco": "MA",
        "mozambique": "MZ", "namibia": "NA", "nepal": "NP", "netherlands": "NL",
        "new zealand": "NZ", "nicaragua": "NI", "niger": "NE", "nigeria": "NG",
        "norway": "NO", "oman": "OM", "pakistan": "PK", "panama": "PA",
        "papua new guinea": "PG", "paraguay": "PY", "peru": "PE",
        "philippines": "PH", "poland": "PL", "portugal": "PT", "qatar": "QA",
        "romania": "RO", "rwanda": "RW", "saudi arabia": "SA", "senegal": "SN",
        "serbia": "RS", "seychelles": "SC", "sierra leone": "SL",
        "singapore": "SG", "slovakia": "SK", "slovenia": "SI", "somalia": "SO",
        "south africa": "ZA", "south sudan": "SS", "spain": "ES",
        "sri lanka": "LK", "sudan": "SD", "suriname": "SR", "sweden": "SE",
        "switzerland": "CH", "taiwan": "TW", "tajikistan": "TJ",
        "thailand": "TH", "togo": "TG", "tunisia": "TN", "turkey": "TR",
        "turkmenistan": "TM", "uganda": "UG", "ukraine": "UA", "uruguay": "UY",
        "uzbekistan": "UZ", "venezuela": "VE", "yemen": "YE", "zambia": "ZM",
        "zimbabwe": "ZW",
    }

    iso = ALIASES.get(key) or NAMES.get(key)
    if not iso or len(iso) != 2:
        return ""
    # Convert ISO2 to regional-indicator flag emoji.
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())


def build_profile_text(p: dict) -> str:
    raw = p.get("formatted_text") or ""
    if raw:
        lines = raw.split("\n")
        if lines and "BROTHER" not in lines[0] and "SISTER" not in lines[0]:
            lines = lines[1:]
        return "\n".join(lines)

    gender = (p.get("gender") or "").lower()
    gender_emoji = "🟣" if ("female" in gender or "sister" in gender) else "🔵"
    gender_label = "SISTER" if ("female" in gender or "sister" in gender) else "BROTHER"

    lines = []

    lines.append(f"{gender_emoji} {gender_label} — {p['id']}")
    if p.get('city') or p.get('country'):
        loc = ', '.join(filter(None, [p.get('city'), p.get('country')]))
        flag = country_flag(p.get('country') or "")
        lines.append(f"{flag} {loc}" if flag else f"📍 {loc}")

    lines.append("")

    # ── Personal details first ──
    _age = calculate_age(p.get('dob'))
    if _age is not None:             lines.append(f"👤 Age: {_age}")
    if p.get('height'):              lines.append(f"📏 Height: {p['height']}")
    if p.get('occupation'):          lines.append(f"💼 Occupation: {p['occupation']}")
    if p.get('education'):           lines.append(f"🎓 Education: {p['education']}")
    if p.get('languages'):           lines.append(f"🗣️ Languages: {p['languages']}")
    if p.get('nationality'):         lines.append(f"🌍 Nationality: {p['nationality']}")
    if p.get('ethnicity'):           lines.append(f"🧬 Ethnicity: {p['ethnicity']}")
    if p.get('marital_status'):      lines.append(f"💍 Marital status: {p['marital_status']}")
    if p.get('children'):            lines.append(f"👶 Has children: {p['children']}")
    if p.get('willing_to_relocate'): lines.append(f"🧳 Willing to relocate: {p['willing_to_relocate']}")

    lines.append("")

    # ── Religious details second ──
    if p.get('deen'):             lines.append(f"🕌 Religious practice: {p['deen']}")
    if p.get('prayer'):           lines.append(f"🙏 Five daily prayers: {p['prayer']}")
    if p.get('hijab'):            lines.append(f"🧕 Hijab: {p['hijab']}")
    if p.get('beard'):            lines.append(f"🧔 Beard: {p['beard']}")
    if p.get('madhab'):           lines.append(f"📚 Madhab: {p['madhab']}")
    if p.get('revert'):           lines.append(f"🕋 Faith background: {p['revert']}")

    lines.append("")

    if p.get('about'):
        lines.append("✨ About:")
        lines.append(p['about'])

    lines.append("")

    if p.get('pref_age_range'):    lines.append(f"🎂 Preferred age: {p['pref_age_range']}")
    if p.get('spouse_deen_level'): lines.append(f"❤️ Spouse deen level: {p['spouse_deen_level']}")
    if p.get('marriage_dynamic'):  lines.append(f"🤝 Marriage dynamic: {p['marriage_dynamic']}")
    if p.get('looking_for'):       lines.append(f"🔍 Looking for: {p['looking_for']}")

    if p.get('additional'):
        lines.append("")
        lines.append(f"ℹ️ Also worth knowing: {p['additional']}")

    return "\n".join(lines)


def repost_text(p: dict) -> str:
    """Profile text for a REPOST (not a brand-new profile). Prepends the repost
    header so members can tell reposts apart from new profiles at a glance.
    Toggle MARK_REPOSTS = False to stop marking once the cleanup is done."""
    header = REPOST_HEADER if MARK_REPOSTS else ""
    return header + build_profile_text(p)


def channel_safe_text(text: str) -> str:
    """Guarantee a channel post fits Telegram's 4096-char message limit.
    Over-length profiles (e.g. a very long About) previously made the send
    throw, silently vanishing the profile from the channel. Truncates with a
    marker instead so the post always goes out."""
    LIMIT = 4000  # hard limit is 4096; leave headroom
    if len(text) <= LIMIT:
        return text
    return text[:LIMIT - 30].rstrip() + "\n… (profile shortened to fit)"


def build_interest_notification(profile_id: str, requester_profile: dict, photo_line: str = "") -> str:
    """Owner notification including the requester's profile, guaranteed to fit within
    Telegram's 4096-char limit. If the full profile would overflow, it is trimmed and
    the owner is pointed to the full profile in the channel."""
    header = (
        "💚 Someone has expressed interest in you\n\n"
        "A member has expressed interest in your profile " + str(profile_id) + ". "
        "Their profile is below for you to consider.\n"
        "───────────────\n"
    )
    footer = "───────────────" + str(photo_line) + "\n\nNothing is shared unless you approve. Requests stay open for up to 5 days, then close gently on their own. 🤲"

    try:
        rendered = build_profile_text(requester_profile) if requester_profile else ""
    except Exception as e:
        logging.warning("Could not render requester profile: " + str(e))
        rendered = ""

    LIMIT = 3900  # Telegram hard limit is 4096; leave headroom.
    budget = LIMIT - len(header) - len(footer)
    if rendered and len(rendered) > budget:
        trimmed = rendered[:max(0, budget - 60)].rstrip()
        rendered = trimmed + "\n… (full profile in the channel — profile " + str(profile_id) + ")"

    body = (rendered + "\n") if rendered else ""
    return header + body + footer


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
            # Markdown parse errors: retry once as plain text so a stray _ or *
            # in someone's profile can never silently kill a message.
            desc = str(result.get("description", "")).lower()
            if "parse" in desc or "entit" in desc:
                payload.pop("parse_mode", None)
                try:
                    resp2 = requests.post(url, json=payload, timeout=15)
                    result2 = resp2.json()
                    if result2.get("ok"):
                        logging.info("Plain-text retry succeeded after parse error.")
                        return True
                    logging.error(f"Telegram API error (plain retry): {result2}")
                except Exception as e2:
                    logging.error(f"Plain-text retry failed: {e2}")
            return False
        return True
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False


# ── Helper: look up a requester's own profile ─────────────────────────────────

def get_requester_profile_id(username: str, tg_id: int = None) -> str:
    # Prefer the reliable Telegram ID; fall back to username (legacy).
    if tg_id:
        r = (
            supabase.table("profiles").select("id")
            .eq("owner_telegram_user_id", tg_id).limit(1).execute()
        )
        if r.data:
            return r.data[0]["id"]
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


def get_requester_profile(username: str, tg_id: int = None) -> dict:
    # Prefer the reliable Telegram ID; fall back to username (legacy).
    if tg_id:
        r = (
            supabase.table("profiles").select("*")
            .eq("owner_telegram_user_id", tg_id).limit(1).execute()
        )
        if r.data:
            return r.data[0]
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


# ── Complete decline helper ────────────────────────────────────────────────────

def format_contact_details(profile: dict):
    """Build the contact-details lines for a profile being shared on approval.
    Returns (lines, wali_missing):
      - lines: the formatted contact text (may be "" if a sister has no wali)
      - wali_missing: True if this is a sister with no usable wali contact, so
        the caller should hold the exchange and alert admin instead of sending.
    Sisters share wali only. Brothers share their direct contact AND (if present)
    their female relative as first point of contact. All fields NULL-safe."""
    p = profile or {}
    gender = (p.get("gender") or "").lower()
    is_sister = ("sister" in gender or "female" in gender)

    if is_sister:
        wali = (p.get("wali_contact") or "").strip()
        no_wali = bool(p.get("no_wali"))
        if not wali or no_wali:
            # No usable wali → signal caller to hold & route to admin.
            return "", True
        return "👤 Wali contact: " + wali, False

    # Brother → direct contact plus optional female-relative contact.
    tg = (p.get("owner_telegram_username") or "").strip()
    phone = (p.get("phone") or "").strip()
    female = (p.get("female_family_contact") or "").strip()

    direct_bits = []
    if tg:
        direct_bits.append("@" + tg)
    if phone:
        direct_bits.append(phone)
    direct = " / ".join(direct_bits) if direct_bits else "(not provided)"

    lines = "📞 Him directly: " + direct
    if female:
        lines += ("\n👩 His female relative (first point of contact): " + female +
                  "\nYou're welcome to make first contact through his female relative if you prefer.")
    return lines, False


async def complete_decline(request_id: int, user, context, reason_text: str) -> None:
    req_result = (
        supabase.table("requests")
        .select("*")
        .eq("id", request_id)
        .limit(1)
        .execute()
    )

    if not req_result.data or req_result.data[0].get("status") != "pending":
        return

    req = req_result.data[0]
    requester_id = req["requester_telegram_user_id"]
    profile_id = req["profile_id"]

    supabase.table("requests").update({
        "status": "declined",
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "decided_by_admin": user.id,
    }).eq("id", request_id).execute()

    supabase.table("user_state").update({
        "active_request_id": None,
        "state": "free",
    }).eq("telegram_user_id", requester_id).execute()

    supabase.table("user_state").update({
        "active_request_id": None,
        "state": "free",
    }).eq("telegram_user_id", user.id).execute()

    try:
        await context.bot.send_message(
            chat_id=requester_id,
            text=(
                "JazakAllahu khayran for your interest in profile " + profile_id + ". "
                "Unfortunately this match was not taken forward at this time.\n\n"
                "Reason: " + reason_text + "\n\n"
                "You are welcome to express interest in another profile. 🤲"
            )
        )
    except Exception as e:
        logging.warning("Could not notify requester of decline: " + str(e))

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text="❌ Declined: profile " + profile_id + " request " + str(request_id) + " by @" + str(user.username or user.id) + "\nReason: " + reason_text,
    )

    await advance_queue(profile_id, context, repost_if_empty=False)


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
        text = repost_text(p)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=channel_safe_text(text),
            reply_markup=profile_button_markup(profile_id),
        )
        logging.info(f"✅ Reposted {profile_id} to channel")

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
        owner_username = (profile_result.data[0].get("owner_telegram_username") or "")
        owner_photo_url = get_photo_ref(profile_result.data[0])

    await context.bot.send_message(
        chat_id=next_requester_id,
        text=(
            "🔔 It's your turn! Your interest in profile " + profile_id + " is now being considered by the profile owner insha'Allah. 🤲\n\n"
            "You will be notified of their decision.\n\n"
            "To withdraw, tap the button below or send /withdraw"
        ),
        reply_markup=interest_confirmation_markup(next_request_id),
    )

    requester_profile = get_requester_profile(next_username, next_requester_id)
    requester_profile_id = requester_profile["id"] if requester_profile else None
    requester_photo_url = get_photo_ref(requester_profile)
    requester_profile_text = "Profile " + requester_profile_id if requester_profile_id else "Anonymous"

    requester_has_photo = bool(requester_photo_url)
    owner_has_photo = bool(owner_photo_url)

    if requester_has_photo and owner_has_photo:
        photo_line = ("\n📷 Both of you have added a photo. When you approve, you can choose "
                      "\"Approve & Share Photos\" to exchange them — or approve without sharing. "
                      "Photos are only ever swapped when you both approve and both choose to share.")
    elif requester_has_photo and not owner_has_photo:
        photo_line = ("\n📷 They have added a photo. Photos are only shared when *both* sides have one "
                      "and both approve — so to enable photo sharing, add yours anytime with /addphoto.")
    elif not requester_has_photo and owner_has_photo:
        photo_line = ("\n📷 They have not added a photo. Photos are only ever shared when *both* sides have "
                      "one and both approve — so there is nothing to exchange unless they add one too.")
    else:
        photo_line = "\n📷 Neither of you has added a photo. Photos are optional and only ever shared privately when both sides add one and both approve."

    request_text = build_interest_notification(profile_id, requester_profile, photo_line)

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


@flask_app.route("/verify_code", methods=["POST"])
def verify_code():
    """Called by the website form on submit to check a registration code BEFORE
    saving the profile. Returns {"valid": true} only if the code was genuinely
    issued by the bot (exists in user_state.issued_code) AND has not already
    been used on an existing profile (single-use). Otherwise {"valid": false}
    with a reason. This keeps all Supabase access on the bot side — the form
    just makes one HTTP call, same as the post_new_profile webhook."""
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"valid": False, "reason": "no_body"}), 400
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"valid": False, "reason": "unauthorised"}), 401

    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"valid": False, "reason": "empty"}), 200

    # normalise: codes are issued uppercase with MTHAQ- prefix
    code_norm = code.upper()

    try:
        # 1) must have been issued by the bot
        issued = (
            supabase.table("user_state")
            .select("telegram_user_id")
            .eq("issued_code", code_norm)
            .limit(1)
            .execute()
        )
        if not issued.data:
            return jsonify({"valid": False, "reason": "not_issued"}), 200

        # 2) must not already be used on a profile (single-use)
        used = (
            supabase.table("profiles")
            .select("id")
            .eq("registration_code", code_norm)
            .limit(1)
            .execute()
        )
        if used.data:
            return jsonify({"valid": False, "reason": "already_used"}), 200

        # valid and unused
        return jsonify({"valid": True}), 200

    except Exception as e:
        logging.warning("verify_code error: " + str(e))
        # On an unexpected error, fail safe by rejecting (better to ask them to
        # retry than to let an unverified code through).
        return jsonify({"valid": False, "reason": "error"}), 200


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

    # ── Normalise the username if the member typed it with a leading @ ──
    # (e.g. "@Asaakius" stored verbatim breaks username-based lookups and would
    # render as @@name in contact shares.) Fix it at source, once.
    _uname_raw = (p.get("owner_telegram_username") or "").strip()
    if _uname_raw.startswith("@"):
        _uname_clean = _uname_raw.lstrip("@").lower()
        try:
            supabase.table("profiles").update(
                {"owner_telegram_username": _uname_clean}
            ).eq("id", profile_id).execute()
            p["owner_telegram_username"] = _uname_clean
            logging.info(f"Normalised username on {profile_id}: '{_uname_raw}' -> '{_uname_clean}'")
        except Exception as e:
            logging.warning("Could not normalise username: " + str(e))

    # ── Link this profile to the Telegram user who was issued its code ──
    # (registration code carried from the form → find the telegram id we gave it to)
    linked_id = link_profile_by_code(p)
    if linked_id and not p.get("owner_telegram_user_id"):
        p["owner_telegram_user_id"] = linked_id

    is_new = not p.get("notified")
    if is_new:
        text = "🆕 NEW PROFILE\n\n" + build_profile_text(p)
    else:
        # A webhook call for an already-notified profile is a repost — mark it.
        text = repost_text(p)

    reply_markup = {
        "inline_keyboard": [[
            {"text": "📩 Express Interest",
             "url": "https://t.me/" + BOT_USERNAME + "?start=interest_" + profile_id}
        ]]
    }

    success = send_telegram_message(CHANNEL_ID, channel_safe_text(text), reply_markup)

    if not success:
        return jsonify({"error": "Failed to send to Telegram"}), 500

    supabase.table("profiles").update({"notified": True}).eq("id", profile_id).execute()

    owner_tg_id = p.get("owner_telegram_user_id")
    owner_username = (p.get("owner_telegram_username") or "")
    if is_new:
        # ── Admin: a new profile has gone live. Fires on SUCCESS, not just failure. ──
        try:
            gender = (p.get("gender") or "").strip() or "?"
            city = (p.get("city") or "").strip()
            country = (p.get("country") or "").strip()
            where = ", ".join([x for x in (city, country) if x]) or "location not given"
            send_telegram_message(
                str(ADMIN_TELEGRAM_USER_ID),
                "📥 New profile posted: " + str(profile_id) + "\n"
                + gender + " · " + where + "\n"
                + ("Linked ✅ @" + owner_username if owner_tg_id and owner_username
                   else ("Linked ✅ (no username)" if owner_tg_id else "⚠️ NOT LINKED — owner will not receive notifications"))
            )
        except Exception as e:
            logging.warning("Could not send new-profile admin ping: " + str(e))

        welcome_msg = build_welcome_message(profile_id)
        if owner_tg_id:
            send_telegram_message(str(owner_tg_id), welcome_msg,
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "⏸ Pause Profile", "callback_data": "pause:" + profile_id}
                    ]]
                }
            )
            # Standalone photo invite, so it isn't buried in the welcome message.
            send_telegram_message(str(owner_tg_id), build_photo_invite_message(),
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "✅ Yes, add a photo", "callback_data": "addphoto_start"},
                        {"text": "Not now", "callback_data": "addphoto_notnow"},
                    ]]
                }
            )
        else:
            send_telegram_message(
                str(ADMIN_TELEGRAM_USER_ID),
                "Could not send welcome to owner of " + profile_id + " (@" + owner_username + ") — they may not have started the bot yet."
            )

        # ── Welcome email (best-effort; never blocks the posting) ──
        owner_email = (p.get("email") or "").strip()
        if owner_email:
            send_welcome_email(owner_email, profile_id)
        else:
            logging.info(f"No email on {profile_id} — welcome email skipped.")

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

        elif arg.startswith("interest_"):
            # ── Deep-link from the channel "Express Interest" button ──
            # Show what will be shared if this interest is approved, then let them
            # confirm. Nothing is sent until they tap the confirm button, which
            # routes to interest_clicked (all eligibility checks live there).
            target_profile_id = arg[len("interest_"):]

            if not username:
                await update.message.reply_text(
                    "Assalamu alaikum!\n\n"
                    "⚠️ To express interest you need a Telegram username set.\n\n"
                    "📱 Go to: Settings → tap your name → Username → set one, "
                    "then tap Express Interest again. 🤲"
                )
                return

            # Requester's own profile → drives gender-aware wording.
            requester = _get_user_profile_by_tg(user.id)
            if not requester and username:
                try:
                    r = (
                        supabase.table("profiles")
                        .select("*")
                        .ilike("owner_telegram_username", username)
                        .limit(1)
                        .execute()
                    )
                    if r.data:
                        requester = r.data[0]
                except Exception as e:
                    logging.warning("interest deep-link: profile lookup failed: " + str(e))

            if not requester:
                await update.message.reply_text(
                    "You need a Mithaq profile to express interest. "
                    "Visit mithaqmarriage.com to submit yours. 🤲"
                )
                return

            r_gender = (requester.get("gender") or "").lower()
            r_is_sister = ("sister" in r_gender or "female" in r_gender)
            r_no_wali = bool(requester.get("no_wali")) or not (requester.get("wali_contact") or "").strip()

            if r_is_sister and r_no_wali:
                warn = (
                    "🤲 Before you express interest\n\n"
                    "Marriage is a mithaq — a solemn covenant. Please proceed only with "
                    "sincere intention.\n\n"
                    "If they approve, Mithaq will speak with you first to arrange your "
                    "introduction with care. Nothing is shared without you."
                )
            elif r_is_sister:
                warn = (
                    "🤲 Before you express interest\n\n"
                    "Marriage is a mithaq — a solemn covenant. Please proceed only with "
                    "sincere intention.\n\n"
                    "If they approve, your wali's contact will be shared so you can be "
                    "introduced through him, in the proper manner."
                )
            else:
                warn = (
                    "🤲 Before you express interest\n\n"
                    "Marriage is a mithaq — a solemn covenant. Please proceed only with "
                    "sincere intention.\n\n"
                    "If they approve, your contact details will be shared so you can connect."
                )

            await update.message.reply_text(
                warn,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Send my interest", callback_data="interest:" + target_profile_id)],
                    [InlineKeyboardButton("↩️ Cancel", callback_data="interest_cancel")],
                ]),
            )
            return

    try:
        existing_state = (
            supabase.table("user_state")
            .select("telegram_user_id")
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

    if not username:
        await update.message.reply_text(
            "Assalamu alaikum!\n\n"
            "⚠️ We noticed you don't have a Telegram username set.\n\n"
            "To use Mithaq you need a Telegram username.\n\n"
            "📱 Go to: Settings → tap your name → Username → set one\n\n"
            "Once done, type /start again and you'll be all set insha'Allah. 🤲"
        )
        return

    # ── 1) Already registered? Match by Telegram ID first (most reliable) ──
    by_id = (
        supabase.table("profiles")
        .select("id, owner_telegram_user_id, is_paused, is_matched, photo_file_id, photo_url")
        .eq("owner_telegram_user_id", user.id)
        .limit(1)
        .execute()
    )
    if by_id.data:
        await _show_registered_status(update, by_id.data[0])
        return

    # ── 2) Quick single check: does a profile already match this username?
    #       (legacy path — user filled the form before pressing /start.) If so,
    #       we may need the retry loop in case the form is mid-submit. If NOT,
    #       this is a brand-new code-first user and we issue the code instantly
    #       with no waiting. ──
    by_name_now = (
        supabase.table("profiles")
        .select("id, owner_telegram_user_id, is_paused, is_matched, photo_file_id, photo_url")
        .eq("owner_telegram_username", username)
        .limit(1)
        .execute()
    )

    # Has this user already been issued a code before (e.g. pressing /start again)?
    st_now = (
        supabase.table("user_state")
        .select("issued_code")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )
    existing_code = st_now.data[0].get("issued_code") if st_now.data else None

    is_legacy_pending = bool(by_name_now.data) or bool(existing_code)

    if not is_legacy_pending:
        # ── 3) BRAND-NEW code-first user → issue a code IMMEDIATELY (no 30s wait) ──
        try:
            code = generate_registration_code()
            supabase.table("user_state").update(
                {"issued_code": code}
            ).eq("telegram_user_id", user.id).execute()

            await update.message.reply_text(
                "Assalamu alaikum! 🌸\n\n"
                "Here is your Mithaq registration code:\n\n"
                "🔑 `" + code + "`\n\n"
                "Please paste this code into the *Telegram Registration Code* box on the "
                "form, then submit your profile.\n\n"
                "As soon as your profile is reviewed and posted, this code links it to "
                "your account here — so you'll receive interest notifications directly. 🤲",
                parse_mode="Markdown",
            )
        except Exception as e:
            # Never leave the user hanging silently — tell them, and alert admin.
            logging.warning("Could not issue registration code: " + str(e))
            try:
                await update.message.reply_text(
                    "Assalamu alaikum!\n\n"
                    "Something went wrong while setting up your registration code. "
                    "Please try /start again in a moment, or contact @MithaqAdmin and "
                    "we'll sort it out for you insha'Allah. 🤲"
                )
            except Exception:
                pass
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_USER_ID,
                    text="⚠️ Code issuance FAILED for @" + username + " (ID " + str(user.id) + "): " + str(e),
                )
            except Exception:
                pass
        return

    # ── LEGACY PATH: a username-matched or already-coded user may have a profile
    #    arriving via the form right now. Acknowledge, then retry a few times. ──
    await update.message.reply_text(
        "Assalamu alaikum!\n\nJazakAllahu khayran — we're setting up your account, please wait a moment insha'Allah..."
    )

    profile = None
    for attempt in range(6):
        # 2a) code we issued to this telegram id, now present on a profile
        my_code = existing_code
        if not my_code:
            st = (
                supabase.table("user_state")
                .select("issued_code")
                .eq("telegram_user_id", user.id)
                .limit(1)
                .execute()
            )
            my_code = st.data[0].get("issued_code") if st.data else None
        if my_code:
            by_code = (
                supabase.table("profiles")
                .select("id, owner_telegram_user_id, is_paused, is_matched, photo_file_id, photo_url")
                .eq("registration_code", my_code)
                .limit(1)
                .execute()
            )
            if by_code.data:
                profile = by_code.data[0]
                break

        # 2b) legacy fallback: match by username
        by_name = (
            supabase.table("profiles")
            .select("id, owner_telegram_user_id, is_paused, is_matched, photo_file_id, photo_url")
            .eq("owner_telegram_username", username)
            .limit(1)
            .execute()
        )
        if by_name.data:
            profile = by_name.data[0]
            break

        logging.info(f"No profile yet for @{username} (ID {user.id}) — attempt {attempt + 1}/6")
        await asyncio.sleep(5)

    if profile:
        profile_id = profile["id"]
        # stamp the telegram id on if it's not there (this is the real link)
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
                reply_markup=resume_markup(profile_id) if profile.get("is_paused") else pause_markup(profile_id),
            )
            # Standalone photo invite so it isn't buried in the welcome message.
            await update.message.reply_text(
                build_photo_invite_message(),
                parse_mode="Markdown",
                reply_markup=photo_invite_markup(),
            )
        else:
            await _show_registered_status(update, profile)
        return

    # ── 3b) Still no profile (username-matched user whose row never arrived, OR
    #        a re-/start where the coded profile isn't posted yet) → re-show the
    #        code they already have, or issue one, so they can finish the form. ──
    try:
        code = existing_code or generate_registration_code()
        if not existing_code:
            supabase.table("user_state").update(
                {"issued_code": code}
            ).eq("telegram_user_id", user.id).execute()

        await update.message.reply_text(
            "Assalamu alaikum! 🌸\n\n"
            "Here is your Mithaq registration code:\n\n"
            "🔑 `" + code + "`\n\n"
            "Please paste this code into the *Telegram Registration Code* box on the "
            "form, then submit your profile.\n\n"
            "As soon as your profile is reviewed and posted, this code links it to "
            "your account here — so you'll receive interest notifications directly. 🤲",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.warning("Could not issue registration code (legacy path): " + str(e))
        try:
            await update.message.reply_text(
                "Assalamu alaikum!\n\n"
                "Something went wrong while setting up your registration code. "
                "Please try /start again in a moment, or contact @MithaqAdmin and "
                "we'll sort it out for you insha'Allah. 🤲"
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text="⚠️ Code issuance FAILED (legacy) for @" + username + " (ID " + str(user.id) + "): " + str(e),
            )
        except Exception:
            pass
    return


async def _show_registered_status(update: Update, profile: dict) -> None:
    """Show an already-registered user their current profile status."""
    profile_id = profile["id"]
    is_paused = profile.get("is_paused", False)
    is_matched = profile.get("is_matched", False)
    if is_matched:
        status_text = "💬 You're currently in a conversation through Mithaq, so your profile is on hold."
        markup = available_menu_markup()
    elif is_paused:
        status_text = "⏸ Your profile is currently *paused*."
        markup = resume_markup(profile_id)
    else:
        status_text = "✅ Your profile is currently *active*."
        markup = pause_markup(profile_id)

    # Gentle, one-line photo reminder — only if they have no photo yet.
    photo_line = ""
    if not get_photo_ref(profile):
        photo_line = "\n\n📷 You haven't added a photo yet. If you'd like to, type /addphoto — it's optional, and only ever shared privately on mutual approval."

    await update.message.reply_text(
        "📋 Your profile: *" + profile_id + "*\n\n" + status_text + "\n\n"
        "📢 Browse profiles here: " + CHANNEL_LINK + "\n\n"
        "📌 If you are not receiving notifications, please type /start again."
        + photo_line,
        parse_mode="Markdown",
        reply_markup=markup,
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

    # Also clear ANY other stray pending requests this user owns, so orphaned
    # requests can't linger and keep falsely blocking them from expressing interest.
    try:
        supabase.table("requests").update({
            "status": "withdrawn",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }).eq("requester_telegram_user_id", user.id).eq("status", "pending").execute()
    except Exception as e:
        logging.warning("Could not clear stray pending requests on withdraw: " + str(e))

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
        await advance_queue(profile_id, context, repost_if_empty=False)


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
            "You're in the queue 🕰️\n\n"
            "Profile: " + profile_id + "\n"
            "Your position: " + str(queue_pos) + "\n\n"
            "Someone else is currently being considered. Your place is private — held with "
            "patience — and you'll be notified the moment it's your turn, insha'Allah. "
            "One person at a time, always. 🤲\n"
            "To leave the queue, tap below or send /withdraw",
            reply_markup=queue_confirmation_markup(active_request_id),
        )


async def available_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    await _make_available(user, context, update.message.reply_text)


async def _make_available(user, context, reply_func) -> None:
    profile_result = (
        supabase.table("profiles")
        .select("id, is_matched, is_paused")
        .eq("owner_telegram_user_id", user.id)
        .execute()
    )

    if not profile_result.data:
        uname = user.username.lower() if user.username else ""
        if uname:
            profile_result = (
                supabase.table("profiles")
                .select("id, is_matched, is_paused")
                .eq("owner_telegram_username", uname)
                .execute()
            )

    if not profile_result.data:
        await reply_func("We couldn't find your profile. Please contact @MithaqAdmin.")
        return

    updated_ids = []
    for prof in profile_result.data:
        supabase.table("profiles").update({
            "is_matched": False,
            "is_paused": False,
        }).eq("id", prof["id"]).execute()
        updated_ids.append(prof["id"])

    supabase.table("user_state").update({
        "state": "free",
        "active_request_id": None,
    }).eq("telegram_user_id", user.id).execute()

    await reply_func(
        "Done — you're active again and back in the channel. You can express interest in "
        "profiles now. May Allah grant you what is best. 🤲\n\n"
        "📢 Browse here: " + CHANNEL_LINK
    )

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text="🔄 Reactivation via /available — " + " & ".join(updated_ids) + " are available again (match ended).",
    )


# ── Photo upload flow ──────────────────────────────────────────────────────────

async def addphoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    profile = _get_user_profile_by_tg(user.id)
    if not profile:
        await update.message.reply_text(
            "We couldn't find your profile. Please contact @MithaqAdmin. 🤲"
        )
        return
    context.user_data["awaiting_photo_for"] = profile["id"]
    await update.message.reply_text(
        "📷 Please send the photo you'd like on your profile (as a normal photo, "
        "not a file).\n\n"
        "_It will never be shown publicly — only shared privately when you and "
        "another member both approve and both choose to share photos. You can "
        "replace it any time by sending /addphoto again, or remove it with "
        "/removephoto._",
        parse_mode="Markdown",
    )


async def removephoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    profile = _get_user_profile_by_tg(user.id)
    if not profile:
        await update.message.reply_text(
            "We couldn't find your profile. Please contact @MithaqAdmin. 🤲"
        )
        return
    supabase.table("profiles").update({"photo_file_id": None}).eq("id", profile["id"]).execute()
    context.user_data.pop("awaiting_photo_for", None)
    await update.message.reply_text(
        "Done — your photo has been removed from your profile. You can add one "
        "again any time with /addphoto. 🤲"
    )


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message or not update.message.photo:
        return
    profile_id = context.user_data.get("awaiting_photo_for")
    if not profile_id:
        profile = _get_user_profile_by_tg(user.id)
        if not profile:
            return
        profile_id = profile["id"]
        context.user_data["awaiting_photo_for"] = profile_id

    file_id = update.message.photo[-1].file_id

    supabase.table("profiles").update({"photo_file_id": file_id}).eq("id", profile_id).execute()
    context.user_data.pop("awaiting_photo_for", None)

    await update.message.reply_text(
        "✅ Photo saved to your profile (" + profile_id + ").\n\n"
        "_It will only ever be shared privately, on mutual approval, and only if "
        "the other member has also added a photo. Replace it any time with "
        "/addphoto, or remove it with /removephoto._",
        parse_mode="Markdown",
    )


def _get_user_profile_by_tg(tg_id: int) -> dict:
    r = (
        supabase.table("profiles").select("*")
        .eq("owner_telegram_user_id", tg_id).limit(1).execute()
    )
    return r.data[0] if r.data else None


# ── Admin commands ─────────────────────────────────────────────────────────────

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    total = supabase.table("profiles").select("id", count="exact").execute()
    active = supabase.table("profiles").select("id", count="exact").eq("is_active", True).eq("is_paused", False).eq("is_matched", False).execute()
    paused = supabase.table("profiles").select("id", count="exact").eq("is_paused", True).execute()
    matched = supabase.table("profiles").select("id", count="exact").eq("is_matched", True).execute()
    pending = supabase.table("requests").select("id", count="exact").eq("status", "pending").execute()
    approved = supabase.table("requests").select("id", count="exact").eq("status", "approved").execute()
    declined = supabase.table("requests").select("id", count="exact").eq("status", "declined").execute()
    withdrawn = supabase.table("requests").select("id", count="exact").eq("status", "withdrawn").execute()

    active_requests = (
        supabase.table("requests")
        .select("*")
        .eq("status", "pending")
        .eq("is_active_request", True)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )

    lines = [
        "📊 Mithaq Dashboard",
        "",
        "👥 Total profiles: " + str(total.count),
        "✅ Active profiles: " + str(active.count),
        "⏸ Paused profiles: " + str(paused.count),
        "💬 In conversation (matched): " + str(matched.count),
        "",
        "🔔 Pending requests: " + str(pending.count),
        "✅ Approved: " + str(approved.count),
        "❌ Declined: " + str(declined.count),
        "🔄 Withdrawn: " + str(withdrawn.count),
    ]

    if active_requests.data:
        lines.append("")
        lines.append("Active requests:")
        for r in active_requests.data:
            lines.append("• " + r["profile_id"] + " — @" + str(r.get("requester_username", "?")))

    await update.message.reply_text("\n".join(lines))


async def post_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /post_profile MTHAQ-001")
        return

    profile_id = context.args[0].upper()

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
    text = "🆕 NEW PROFILE\n\n" + build_profile_text(p)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=channel_safe_text(text),
        reply_markup=profile_button_markup(profile_id),
    )

    supabase.table("profiles").update({"notified": True}).eq("id", profile_id).execute()
    await update.message.reply_text("✅ Profile " + profile_id + " posted to channel.")


async def bump_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /bump MTHAQ-001")
        return

    profile_id = context.args[0].upper()

    result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", profile_id)
        .eq("is_active", True)
        .eq("is_paused", False)
        .limit(1)
        .execute()
    )

    if not result.data:
        await update.message.reply_text("Profile " + profile_id + " not found, inactive, or paused.")
        return

    p = result.data[0]
    text = repost_text(p)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=channel_safe_text(text),
        reply_markup=profile_button_markup(profile_id),
    )

    await update.message.reply_text("✅ Profile " + profile_id + " bumped to channel.")


async def set_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    if len(context.args) < 3:
        await update.message.reply_text("Usage: /set_field MTHAQ-001 field_name value")
        return

    profile_id = context.args[0].upper()
    field = context.args[1].lower()
    value = " ".join(context.args[2:])

    ALLOWED = [
        "additional", "beard", "children", "city", "country", "deen", "dob",
        "education", "email", "ethnicity", "female_family_contact", "height",
        "hijab", "languages", "looking_for", "madhab", "marital_status",
        "marriage_dynamic", "nationality", "occupation", "phone", "prayer",
        "pref_age_range", "revert", "spouse_deen_level", "wali_contact",
        "willing_to_relocate",
    ]

    if field not in ALLOWED:
        await update.message.reply_text(
            "Field '" + field + "' not allowed. Fields: " + ", ".join(ALLOWED)
        )
        return

    supabase.table("profiles").update({field: value}).eq("id", profile_id).execute()
    await update.message.reply_text("✅ " + field + " updated for " + profile_id + ".")


async def edit_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    text = update.message.text or ""
    parts = text.split(None, 2)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /edit_about MTHAQ-001 <new about text>")
        return

    profile_id = parts[1].upper()
    about = parts[2]

    supabase.table("profiles").update({"about": about, "formatted_text": None}).eq("id", profile_id).execute()
    await update.message.reply_text(
        "✅ About updated for " + profile_id + " (" + str(len(about)) + " chars).\n"
        "Run /bump " + profile_id + " to refresh the channel post (delete the old post first)."
    )


async def set_wali(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /set_wali MTHAQ-001 +447123456789")
        return

    profile_id = context.args[0].upper()
    wali = " ".join(context.args[1:])

    supabase.table("profiles").update({
        "wali_contact": wali,
        "no_wali": False,
    }).eq("id", profile_id).execute()

    # If an approved request was waiting on this wali, release the details now.
    waiting = (
        supabase.table("requests")
        .select("*")
        .eq("profile_id", profile_id)
        .eq("status", "approved")
        .order("decided_at", desc=True)
        .limit(1)
        .execute()
    )

    released = False
    if waiting.data:
        req = waiting.data[0]
        requester_id = req["requester_telegram_user_id"]
        try:
            await context.bot.send_message(
                chat_id=requester_id,
                text=(
                    "💚 Alhamdulillah — the introduction details for profile " + profile_id + " are ready.\n\n"
                    "👤 Wali contact: " + wali + "\n\n"
                    "Please reach out with respect, patience, and good character. "
                    "From here, it is between you both, your walis, and Allah. May He put barakah in it. 🤲"
                ),
            )
            released = True
        except Exception as e:
            logging.warning("Could not release wali details: " + str(e))

    if released:
        await update.message.reply_text("✅ Wali contact saved for " + profile_id + " and sent to the waiting party.")
    else:
        await update.message.reply_text("✅ Wali contact saved for " + profile_id + ". (No one currently waiting on an approved match.)")


async def rehome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /rehome MTHAQ-001")
        return

    profile_id = context.args[0].upper()

    supabase.table("profiles").update({
        "is_active": False,
        "owner_telegram_user_id": None,
        "registration_code": None,
    }).eq("id", profile_id).execute()

    await update.message.reply_text(
        "✅ " + profile_id + " retired: deactivated, Telegram unlinked, code cleared."
    )


async def resend_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /resend_requests MTHAQ-001")
        return

    profile_id = context.args[0].upper()

    pending = (
        supabase.table("requests")
        .select("*")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .eq("is_active_request", True)
        .limit(1)
        .execute()
    )

    if not pending.data:
        await update.message.reply_text("No active pending request on " + profile_id + ".")
        return

    req = pending.data[0]
    request_id = req["id"]
    requester_username = req.get("requester_username", "")
    requester_id = req["requester_telegram_user_id"]

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

    owner_tg_id = profile_result.data[0].get("owner_telegram_user_id")
    owner_photo_url = get_photo_ref(profile_result.data[0])
    if not owner_tg_id:
        await update.message.reply_text("Owner of " + profile_id + " is not linked — cannot resend.")
        return

    requester_profile = get_requester_profile(requester_username, requester_id)
    requester_photo_url = get_photo_ref(requester_profile)

    requester_has_photo = bool(requester_photo_url)
    owner_has_photo = bool(owner_photo_url)

    if requester_has_photo and owner_has_photo:
        photo_line = ("\n📷 Both of you have added a photo. When you approve, you can choose "
                      "\"Approve & Share Photos\" to exchange them — or approve without sharing.")
    else:
        photo_line = ""

    request_text = build_interest_notification(profile_id, requester_profile, photo_line)

    try:
        await context.bot.send_message(
            chat_id=owner_tg_id,
            text=request_text,
            reply_markup=owner_request_markup(request_id, requester_has_photo, owner_has_photo),
        )
        await update.message.reply_text("✅ Request resent to owner of " + profile_id + ".")
    except Exception as e:
        await update.message.reply_text("Could not resend: " + str(e))


async def reset_all_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        return

    if not context.args or context.args[0] != "CONFIRM":
        await update.message.reply_text(
            "⚠️ This clears ALL pending/queued interest requests across the entire system "
            "and frees every user's state. Approved matches that are already completed are "
            "not affected.\n\n"
            "To proceed, send: /reset_all_requests CONFIRM"
        )
        return

    supabase.table("requests").update({
        "status": "withdrawn",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }).eq("status", "pending").execute()

    supabase.table("user_state").update({
        "active_request_id": None,
        "state": "free",
    }).neq("state", "free").execute()

    await update.message.reply_text("✅ All pending requests cleared; all users freed.")


# ── Interest button handler ────────────────────────────────────────────────────

async def interest_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    username = user.username.lower() if user.username else ""

    profile_id = query.data.split(":", 1)[1]

    if not username:
        await query.answer(
            "⚠️ You need a Telegram username to express interest. Go to Settings → Username, set one, then try again.",
            show_alert=True,
        )
        return

    # ── Self-interest guard ──
    own_profile = get_requester_profile_id(username, user.id)
    if own_profile == profile_id:
        await query.answer("This is your own profile. 🤲", show_alert=True)
        return

    state_result = (
        supabase.table("user_state")
        .select("*")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )

    if state_result.data and state_result.data[0].get("state") == "locked":
        await query.answer(
            "You already have an active interest request. One at a time — withdraw it first (/withdraw) if you'd like to change. 🤲",
            show_alert=True,
        )
        return

    if not state_result.data:
        supabase.table("user_state").insert({
            "telegram_user_id": user.id,
            "state": "free",
        }).execute()

    profile_result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    if not profile_result.data:
        await query.answer("This profile no longer exists.", show_alert=True)
        return

    profile = profile_result.data[0]

    if not profile.get("is_active") or profile.get("is_paused") or profile.get("is_matched"):
        await query.answer(
            "This profile is not currently available for new interest. 🤲",
            show_alert=True,
        )
        return

    # ── Is someone already being considered? Then this user joins the queue. ──
    active_result = (
        supabase.table("requests")
        .select("id", count="exact")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .eq("is_active_request", True)
        .execute()
    )

    someone_active = (active_result.count or 0) > 0

    queue_count = (
        supabase.table("requests")
        .select("id", count="exact")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .eq("is_active_request", True)
        .execute()
    )
    queue_position = (queue_count.count or 0) + 1

    requester_profile = get_requester_profile(username, user.id)
    requester_profile_id = requester_profile["id"] if requester_profile else None
    requester_photo_url = get_photo_ref(requester_profile)
    requester_profile_text = "Profile " + requester_profile_id if requester_profile_id else "Anonymous"

    if not requester_profile_id:
        await query.answer(
            "You need a Mithaq profile to express interest. Visit mithaqmarriage.com to submit yours. 🤲",
            show_alert=True,
        )
        return

    insert_result = supabase.table("requests").insert({
        "requester_telegram_user_id": user.id,
        "requester_username": username,
        "profile_id": profile_id,
        "status": "pending",
        "queue_position": queue_position,
        "is_active_request": not someone_active,
    }).execute()

    request_id = insert_result.data[0]["id"]

    supabase.table("user_state").update({
        "active_request_id": request_id,
        "state": "locked" if not someone_active else "queued",
    }).eq("telegram_user_id", user.id).execute()

    if someone_active:
        await query.answer("You've been added to the queue. 🤲", show_alert=True)
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "🕰️ You're in the queue for profile " + profile_id + "\n\n"
                "Someone else is currently being considered for this profile. Mithaq works "
                "one introduction at a time, out of respect for everyone involved.\n\n"
                "Your place is held. If the current introduction doesn't proceed, you'll be "
                "notified the moment it's your turn insha'Allah. 🤲\n\n"
                "You are free to express interest in other profiles while you wait.\n"
                "To leave this queue, tap below or send /withdraw"
            ),
            reply_markup=queue_confirmation_markup(request_id),
        )

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text=(
                "🔢 Queue Update\n\n"
                "Profile: " + profile_id + "\n"
                "@" + username + " (" + requester_profile_text + ") added to queue at position " + str(queue_position)
            ),
        )
        return

    await query.answer("Your interest has been submitted. 🤲", show_alert=True)

    owner_tg_id = profile.get("owner_telegram_user_id")
    owner_username = (profile.get("owner_telegram_username") or "")
    owner_photo_url = get_photo_ref(profile)

    await context.bot.send_message(
        chat_id=user.id,
        text=(
            "🕊️ Your interest in profile " + profile_id + " has been submitted.\n\n"
            "The profile owner will be notified insha'Allah, and you will hear back once "
            "they have made a decision. This may take a little time — may Allah reward "
            "your patience. 🤲\n\n"
            "📌 To withdraw your interest at any time, tap below or send /withdraw\n"
            "📌 To check your request status, send /my_request"
        ),
        reply_markup=interest_confirmation_markup(request_id),
    )

    requester_has_photo = bool(requester_photo_url)
    owner_has_photo = bool(owner_photo_url)

    if requester_has_photo and owner_has_photo:
        photo_line = ("\n📷 Both of you have added a photo. When you approve, you can choose "
                      "\"Approve & Share Photos\" to exchange them — or approve without sharing. "
                      "Photos are only ever swapped when you both approve and both choose to share.")
    elif requester_has_photo and not owner_has_photo:
        photo_line = ("\n📷 They have added a photo. Photos are only shared when *both* sides have one "
                      "and both approve — so to enable photo sharing, add yours anytime with /addphoto.")
    elif not requester_has_photo and owner_has_photo:
        photo_line = ("\n📷 They have not added a photo. Photos are only ever shared when *both* sides have "
                      "one and both approve — so there is nothing to exchange unless they add one too.")
    else:
        photo_line = "\n📷 Neither of you has added a photo. Photos are optional and only ever shared privately when both sides add one and both approve."

    request_text = build_interest_notification(profile_id, requester_profile, photo_line)

    admin_text = (
        "🔔 New Interest Request\n\n"
        "Profile: " + profile_id + "\n"
        "From: @" + username + " (" + requester_profile_text + ")\n"
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

    if sent_to_owner:
        admin_text += "\n\n✅ Request sent to owner. You can also approve/decline below."
    else:
        admin_text += "\n\n⚠️ OWNER NOT LINKED — this profile owner has not activated their bot, so they could NOT be notified. Please handle this request via the buttons below, or contact them directly."

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text=admin_text,
        reply_markup=admin_request_markup(request_id),
    )


# ── Decision handler ───────────────────────────────────────────────────────────

async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    data = query.data

    action, request_id_str = data.split(":", 1)
    request_id = int(request_id_str)

    req_result = (
        supabase.table("requests")
        .select("*")
        .eq("id", request_id)
        .limit(1)
        .execute()
    )

    if not req_result.data:
        await query.answer("This request no longer exists.", show_alert=True)
        return

    req = req_result.data[0]

    if req.get("status") != "pending":
        await query.answer("This request has already been handled.", show_alert=True)
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

    profile = profile_result.data[0] if profile_result.data else {}
    owner_tg_id = profile.get("owner_telegram_user_id")

    is_owner = owner_tg_id and user.id == owner_tg_id
    is_admin = user.id == ADMIN_TELEGRAM_USER_ID

    if not (is_owner or is_admin):
        await query.answer("Only the profile owner or admin can decide this.", show_alert=True)
        return

    if action == "consider":
        supabase.table("requests").update({
            "consideration_started_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", request_id).execute()

        gender = (profile.get("gender") or "").lower()
        is_sister = ("sister" in gender or "female" in gender)
        consult_line = "consult your family and pray istikhara" if is_sister else "pray istikhara and reflect"

        await query.answer("Take your time. 🤲")
        await context.bot.send_message(
            chat_id=user.id,
            text="JazakAllah khayran. Take the time you need to " + consult_line + ". May Allah guide you to what is best. 🤲\n\n"
                 "📌 So you know how it works: the request stays open for 5 days. If no decision is made by then, it closes gently on its own — no blame on anyone, and they're freed to look elsewhere.",
        )
        return

    if action in ("approve", "approve_photo"):
        share_photos = (action == "approve_photo")

        contact_lines, wali_missing = format_contact_details(profile)

        gender = (profile.get("gender") or "").lower()
        is_sister = ("sister" in gender or "female" in gender)

        supabase.table("requests").update({
            "status": "approved",
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "decided_by_admin": user.id if is_admin and not is_owner else None,
        }).eq("id", request_id).execute()

        supabase.table("profiles").update({
            "is_paused": True,
            "is_matched": True,
        }).eq("id", profile_id).execute()

        requester_own_profile = get_requester_profile(requester_username, requester_id)
        if requester_own_profile:
            supabase.table("profiles").update({
                "is_paused": True,
                "is_matched": True,
            }).eq("id", requester_own_profile["id"]).execute()

        supabase.table("user_state").update({
            "active_request_id": None,
            "state": "matched",
        }).eq("telegram_user_id", requester_id).execute()

        if owner_tg_id:
            supabase.table("user_state").update({
                "active_request_id": None,
                "state": "matched",
            }).eq("telegram_user_id", owner_tg_id).execute()

        pause_note = (
            "\n\n⚠️ Important: while you're in this introduction, your profile is paused and "
            "you won't see or receive new interest. If it doesn't progress to marriage, "
            "please come back and tap the 'Make me available again' button below (or send "
            "/available) so you return to the pool. Otherwise you'll stay paused."
        )

        if wali_missing:
            try:
                await context.bot.send_message(
                    chat_id=requester_id,
                    text=(
                        "💚 Alhamdulillah — your interest has been approved.\n\n"
                        "Mithaq will be in touch with the introduction details shortly, insha'Allah."
                        + pause_note
                    ),
                    reply_markup=available_menu_markup(),
                )
            except Exception as e:
                logging.warning("Could not notify requester (wali hold): " + str(e))

            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text=(
                    "⚠️ HOLD — approval on " + profile_id + " (request " + str(request_id) + ") but the sister has NO wali contact on file.\n"
                    "Contact her, then run /set_wali " + profile_id + " +NUMBER to release the details."
                ),
            )
        else:
            photo_swap_done = False
            if share_photos:
                requester_photo = get_photo_ref(requester_own_profile)
                owner_photo = get_photo_ref(profile)
                if requester_photo and owner_photo:
                    try:
                        await context.bot.send_photo(chat_id=requester_id, photo=owner_photo,
                            caption="📷 Shared with their approval — profile " + profile_id)
                        await context.bot.send_photo(chat_id=owner_tg_id, photo=requester_photo,
                            caption="📷 Shared with their approval — " + (requester_own_profile["id"] if requester_own_profile else ""))
                        photo_swap_done = True
                    except Exception as e:
                        logging.warning("Photo swap failed: " + str(e))

            try:
                await context.bot.send_message(
                    chat_id=requester_id,
                    text=(
                        "💚 Alhamdulillah — your interest has been approved.\n\n"
                        "Below are their contact details. Please reach out with respect, patience, "
                        "and good character.\n\n"
                        + contact_lines + "\n\n"
                        "From here, it is between you both, your walis, and Allah. May He put barakah in it. 🤲"
                        + pause_note
                    ),
                    reply_markup=available_menu_markup(),
                )
            except Exception as e:
                logging.warning("Could not notify requester of approval: " + str(e))

        if owner_tg_id and (is_owner or is_admin):
            requester_contact_lines, _ = format_contact_details(requester_own_profile) if requester_own_profile else ("", False)
            owner_msg = (
                "💚 Alhamdulillah — you approved the interest from "
                + (requester_own_profile["id"] if requester_own_profile else "the member") + ". "
                "Contact details have been exchanged.\n\n"
            )
            if requester_contact_lines:
                owner_msg += "Here are their contact details:\n" + requester_contact_lines + "\n\n"
            owner_msg += "May Allah put barakah in it. 🤲" + pause_note
            try:
                await context.bot.send_message(
                    chat_id=owner_tg_id,
                    text=owner_msg,
                    reply_markup=available_menu_markup(),
                )
            except Exception as e:
                logging.warning("Could not send owner approval summary: " + str(e))

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text="✅ Approved: profile " + profile_id + " request " + str(request_id) + " from @" + requester_username + " by @" + str(user.username or user.id),
        )

        # ── Close remaining queue: this profile is now in an introduction ──
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
                    text=(
                        "JazakAllahu khayran for your interest in profile " + profile_id + ". "
                        "Unfortunately this profile is no longer available. "
                        "You are welcome to express interest in another profile. 🤲"
                    ),
                )
            except Exception as e:
                logging.warning("Could not notify queued requester: " + str(e))

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer("Approved. 🤲")
        return

    if action == "decline":
        if is_owner and not is_admin:
            await query.answer()
            await context.bot.send_message(
                chat_id=user.id,
                text="Please choose a reason (it is shared kindly with the other member):",
                reply_markup=decline_reason_markup(request_id),
            )
        else:
            await complete_decline(request_id, user, context, "Not the right fit at this time")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.answer("Declined.")
        return


async def decline_reason_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    data = query.data

    _, request_id_str, reason_key = data.split(":", 2)
    request_id = int(request_id_str)

    if reason_key == "other":
        context.user_data["awaiting_decline_reason_for"] = request_id
        await query.answer()
        await context.bot.send_message(
            chat_id=user.id,
            text="Please type your reason in a short message (it will be shared kindly with the other member):",
        )
        return

    reason_text = DECLINE_REASONS.get(reason_key, "Not the right fit at this time")
    await complete_decline(request_id, user, context, reason_text)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer("Declined with reason shared. 🤲")


async def text_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    awaiting = context.user_data.get("awaiting_decline_reason_for")
    if awaiting:
        reason_text = (update.message.text or "").strip()
        if not reason_text:
            await update.message.reply_text("Please type a short reason.")
            return
        context.user_data.pop("awaiting_decline_reason_for", None)
        await complete_decline(awaiting, user, context, reason_text)
        await update.message.reply_text("Declined — your reason was shared kindly. 🤲")
        return


async def withdraw_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    request_id = int(query.data.split(":", 1)[1])

    req_result = (
        supabase.table("requests")
        .select("*")
        .eq("id", request_id)
        .limit(1)
        .execute()
    )

    if not req_result.data or req_result.data[0].get("status") != "pending":
        await query.answer("This request has already been handled.", show_alert=True)
        return

    req = req_result.data[0]

    if req["requester_telegram_user_id"] != user.id:
        await query.answer("This is not your request.", show_alert=True)
        return

    profile_id = req["profile_id"]
    was_active = req.get("is_active_request", False)

    supabase.table("requests").update({
        "status": "withdrawn",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", request_id).execute()

    supabase.table("user_state").update({
        "active_request_id": None,
        "state": "free",
    }).eq("telegram_user_id", user.id).execute()

    await query.answer("Interest withdrawn.")
    await context.bot.send_message(
        chat_id=user.id,
        text="Your interest in profile " + profile_id + " has been withdrawn. You are free to express interest in another profile. 🤲",
    )

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text="Request " + str(request_id) + " withdrawn by @" + str(user.username or user.id),
    )

    if was_active:
        await advance_queue(profile_id, context, repost_if_empty=False)


async def pause_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    profile_id = query.data.split(":", 1)[1]

    profile_result = (
        supabase.table("profiles")
        .select("owner_telegram_user_id, owner_telegram_username")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    if not profile_result.data:
        await query.answer("Profile not found.", show_alert=True)
        return

    owner_id = profile_result.data[0].get("owner_telegram_user_id")
    is_admin = user.id == ADMIN_TELEGRAM_USER_ID
    if owner_id != user.id and not is_admin:
        await query.answer("This is not your profile.", show_alert=True)
        return

    supabase.table("profiles").update({"is_paused": True}).eq("id", profile_id).execute()

    await query.answer("Profile paused.")
    await context.bot.send_message(
        chat_id=user.id,
        text=(
            "⏸ Your profile " + profile_id + " is now paused. It will stay in the channel, "
            "but new Express Interest taps will be politely turned away.\n\n"
            "Resume any time with the button below. 🤲"
        ),
        reply_markup=resume_markup(profile_id),
    )

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text="⏸ Profile " + profile_id + " paused by owner.",
    )


async def resume_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    profile_id = query.data.split(":", 1)[1]

    profile_result = (
        supabase.table("profiles")
        .select("owner_telegram_user_id")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    if not profile_result.data:
        await query.answer("Profile not found.", show_alert=True)
        return

    owner_id = profile_result.data[0].get("owner_telegram_user_id")
    is_admin = user.id == ADMIN_TELEGRAM_USER_ID
    if owner_id != user.id and not is_admin:
        await query.answer("This is not your profile.", show_alert=True)
        return

    supabase.table("profiles").update({"is_paused": False}).eq("id", profile_id).execute()

    await query.answer("Profile resumed. 🤲")
    await context.bot.send_message(
        chat_id=user.id,
        text="▶️ Your profile " + profile_id + " is active again. May Allah send the right one your way. 🤲",
        reply_markup=pause_markup(profile_id),
    )

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text="▶️ Profile " + profile_id + " resumed by owner.",
    )


async def avail_menu_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    await query.answer()

    async def _reply(text):
        await context.bot.send_message(chat_id=user.id, text=text)

    await _make_available(user, context, _reply)


async def addphoto_start_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    await query.answer()
    profile = _get_user_profile_by_tg(user.id)
    if not profile:
        await context.bot.send_message(
            chat_id=user.id,
            text="We couldn't find your profile. Please contact @MithaqAdmin. 🤲",
        )
        return
    context.user_data["awaiting_photo_for"] = profile["id"]
    await context.bot.send_message(
        chat_id=user.id,
        text=(
            "📷 Please send the photo you'd like on your profile (as a normal photo, "
            "not a file).\n\n"
            "_It will never be shown publicly — only shared privately when you and "
            "another member both approve and both choose to share photos._"
        ),
        parse_mode="Markdown",
    )


async def addphoto_notnow_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("No problem — you can add one any time with /addphoto. 🤲", show_alert=False)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def interest_cancel_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("No interest sent. 🤲")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# ── Reminder / consideration background jobs ──────────────────────────────────

async def check_pending_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 30 min: find active pending requests older than 2 hours where no
    reminder has been sent, remind the owner (Telegram + email), mark sent."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        stale = (
            supabase.table("requests")
            .select("*")
            .eq("status", "pending")
            .eq("is_active_request", True)
            .eq("reminder_sent", False)
            .lt("created_at", cutoff)
            .execute()
        )
        for req in (stale.data or []):
            # Skip if consideration explicitly started — the consider flow has
            # its own gentler rhythm (2-day nudge, 5-day close).
            if req.get("consideration_started_at"):
                supabase.table("requests").update({"reminder_sent": True}).eq("id", req["id"]).execute()
                continue

            profile_id = req["profile_id"]
            prof = (
                supabase.table("profiles").select("*")
                .eq("id", profile_id).limit(1).execute()
            )
            if not prof.data:
                continue
            owner_tg = prof.data[0].get("owner_telegram_user_id")
            owner_email = (prof.data[0].get("email") or "").strip()

            if owner_tg:
                try:
                    await context.bot.send_message(
                        chat_id=owner_tg,
                        text=(
                            "🔔 A gentle reminder: someone has expressed interest in your profile "
                            + profile_id + " and is waiting for your response.\n\n"
                            "You can Approve, Decline, or take time to consider — the buttons are on "
                            "the original message above. JazakAllahu khayran. 🤲"
                        ),
                    )
                except Exception as e:
                    logging.warning("Reminder TG send failed: " + str(e))

            if owner_email:
                send_reminder_email(owner_email, profile_id)

            supabase.table("requests").update({"reminder_sent": True}).eq("id", req["id"]).execute()
            logging.info("Reminder sent for request " + str(req["id"]))
    except Exception as e:
        logging.warning("check_pending_reminders error: " + str(e))


async def check_consideration(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 6 hours: nudge owners 2 days into consideration; auto-close at 5 days."""
    try:
        now = datetime.now(timezone.utc)
        considering = (
            supabase.table("requests")
            .select("*")
            .eq("status", "pending")
            .not_.is_("consideration_started_at", "null")
            .execute()
        )
        for req in (considering.data or []):
            started_raw = req.get("consideration_started_at")
            try:
                started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
            except Exception:
                continue
            age = now - started

            profile_id = req["profile_id"]
            prof = (
                supabase.table("profiles").select("*")
                .eq("id", profile_id).limit(1).execute()
            )
            owner_tg = prof.data[0].get("owner_telegram_user_id") if prof.data else None

            # ── 5-day close ──
            if age >= timedelta(days=5):
                supabase.table("requests").update({
                    "status": "closed",
                    "decided_at": now.isoformat(),
                }).eq("id", req["id"]).execute()

                supabase.table("user_state").update({
                    "active_request_id": None,
                    "state": "free",
                }).eq("telegram_user_id", req["requester_telegram_user_id"]).execute()

                try:
                    await context.bot.send_message(
                        chat_id=req["requester_telegram_user_id"],
                        text=(
                            "JazakAllah khayran for your interest in profile " + profile_id + " and for your patience. "
                            "This one didn't move forward to a decision in time, so it has now been closed. "
                            "You are free to express interest in another profile whenever you're ready. "
                            "May Allah guide you to what is best for you. 🤲"
                        ),
                    )
                except Exception as e:
                    logging.warning("Close notify (requester) failed: " + str(e))

                if owner_tg:
                    try:
                        await context.bot.send_message(
                            chat_id=owner_tg,
                            text=(
                                "The interest on your profile " + profile_id + " has now closed as time passed "
                                "without a decision. No action needed — if it was meant for you, Allah will "
                                "bring what is best. 🤲"
                            ),
                        )
                    except Exception as e:
                        logging.warning("Close notify (owner) failed: " + str(e))

                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_USER_ID,
                    text="⌛ Expired: considering request " + str(req["id"]) + " (profile " + profile_id + ") closed after 5 days.",
                )

                await advance_queue(profile_id, context, repost_if_empty=False)
                continue

            # ── 2-day nudge ──
            if age >= timedelta(days=2) and not req.get("consideration_nudge_sent"):
                if owner_tg:
                    try:
                        await context.bot.send_message(
                            chat_id=owner_tg,
                            text=(
                                "🤲 A gentle reminder: you asked for time to consider the interest on your "
                                "profile " + profile_id + ". The Approve and Decline buttons are still here "
                                "whenever you're ready.\n\n"
                                "📌 The request stays open for another 3 days — if it's still undecided then, "
                                "it will close gently on its own, with no blame on anyone. May Allah "
                                "guide you to what is best."
                            ),
                        )
                    except Exception as e:
                        logging.warning("Consideration nudge failed: " + str(e))
                supabase.table("requests").update({"consideration_nudge_sent": True}).eq("id", req["id"]).execute()
                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_USER_ID,
                    text="🔔 2-day nudge sent for considering request " + str(req["id"]) + " (profile " + profile_id + ").",
                )
    except Exception as e:
        logging.warning("check_consideration error: " + str(e))


# ── SendGrid email helpers ─────────────────────────────────────────────────────

def send_welcome_email(to_email: str, profile_id: str) -> None:
    if not SENDGRID_API_KEY:
        return
    try:
        requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": "Bearer " + SENDGRID_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": "info@mithaqmarriage.com", "name": "Mithaq Marriage"},
                "subject": "Welcome to Mithaq — your profile " + profile_id + " is live",
                "content": [{
                    "type": "text/plain",
                    "value": (
                        "Assalamu alaikum,\n\n"
                        "JazakAllahu khayran — your Mithaq profile " + profile_id + " is now live.\n\n"
                        "Next step: open Telegram and start our bot so you receive interest "
                        "notifications directly:\n"
                        "https://t.me/" + BOT_USERNAME + "\n\n"
                        "Browse profiles in the private channel:\n"
                        + CHANNEL_LINK + "\n\n"
                        "You are in full control — nothing about you is shared without your approval.\n\n"
                        "May Allah make it easy for you.\n"
                        "— Mithaq Marriage\n"
                        "mithaqmarriage.com"
                    ),
                }],
            },
            timeout=15,
        )
        logging.info("Welcome email sent to " + to_email)
    except Exception as e:
        logging.warning("Welcome email failed: " + str(e))


def send_reminder_email(to_email: str, profile_id: str) -> None:
    if not SENDGRID_API_KEY:
        return
    try:
        requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": "Bearer " + SENDGRID_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": "info@mithaqmarriage.com", "name": "Mithaq Marriage"},
                "subject": "Someone has expressed interest in your Mithaq profile",
                "content": [{
                    "type": "text/plain",
                    "value": (
                        "Assalamu alaikum,\n\n"
                        "Someone has expressed interest in your Mithaq profile " + profile_id + " "
                        "and is waiting for your response.\n\n"
                        "Open Telegram and check your messages from our bot to Approve, Decline, "
                        "or take time to consider:\n"
                        "https://t.me/" + BOT_USERNAME + "\n\n"
                        "JazakAllahu khayran,\n"
                        "— Mithaq Marriage"
                    ),
                }],
            },
            timeout=15,
        )
        logging.info("Reminder email sent to " + to_email)
    except Exception as e:
        logging.warning("Reminder email failed: " + str(e))


# ── Wiring ─────────────────────────────────────────────────────────────────────

def run_flask():
    port = int(os.environ.get("FLASK_PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(CommandHandler("my_request", my_request))
    application.add_handler(CommandHandler("available", available_command))
    application.add_handler(CommandHandler("addphoto", addphoto_command))
    application.add_handler(CommandHandler("removephoto", removephoto_command))
    application.add_handler(CommandHandler("dashboard", dashboard))
    application.add_handler(CommandHandler("post_profile", post_profile))
    application.add_handler(CommandHandler("bump", bump_profile))
    application.add_handler(CommandHandler("set_field", set_field))
    application.add_handler(CommandHandler("edit_about", edit_about))
    application.add_handler(CommandHandler("set_wali", set_wali))
    application.add_handler(CommandHandler("rehome", rehome))
    application.add_handler(CommandHandler("resend_requests", resend_requests))
    application.add_handler(CommandHandler("reset_all_requests", reset_all_requests))

    application.add_handler(CallbackQueryHandler(interest_clicked, pattern="^interest:"))
    application.add_handler(CallbackQueryHandler(interest_cancel_clicked, pattern="^interest_cancel$"))
    application.add_handler(CallbackQueryHandler(handle_decision, pattern="^(approve|approve_photo|decline|consider):"))
    application.add_handler(CallbackQueryHandler(decline_reason_clicked, pattern="^dr:"))
    application.add_handler(CallbackQueryHandler(withdraw_clicked, pattern="^withdraw:"))
    application.add_handler(CallbackQueryHandler(pause_clicked, pattern="^pause:"))
    application.add_handler(CallbackQueryHandler(resume_clicked, pattern="^resume:"))
    application.add_handler(CallbackQueryHandler(avail_menu_clicked, pattern="^avail_menu$"))
    application.add_handler(CallbackQueryHandler(addphoto_start_clicked, pattern="^addphoto_start$"))
    application.add_handler(CallbackQueryHandler(addphoto_notnow_clicked, pattern="^addphoto_notnow$"))

    application.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_received))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_message_router))

    if application.job_queue:
        application.job_queue.run_repeating(check_pending_reminders, interval=1800, first=60)
        application.job_queue.run_repeating(check_consideration, interval=21600, first=120)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logging.info("Mithaq bot starting (polling + Flask webhook)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
