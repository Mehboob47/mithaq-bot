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
    footer = "───────────────" + str(photo_line) + "\n\nNothing is shared unless you approve. 🤲"

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
        text = build_profile_text(p)

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
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

    # ── Link this profile to the Telegram user who was issued its code ──
    # (registration code carried from the form → find the telegram id we gave it to)
    linked_id = link_profile_by_code(p)
    if linked_id and not p.get("owner_telegram_user_id"):
        p["owner_telegram_user_id"] = linked_id

    text = build_profile_text(p)
    is_new = not p.get("notified")
    if is_new:
        text = "🆕 NEW PROFILE\n\n" + text

    reply_markup = {
        "inline_keyboard": [[
            {"text": "📩 Express Interest",
             "url": "https://t.me/" + BOT_USERNAME + "?start=interest_" + profile_id}
        ]]
    }

    success = send_telegram_message(CHANNEL_ID, text, reply_markup)

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
        owner_username = (p.get("owner_telegram_username") or "")
        welcome_msg = build_welcome_message(profile_id)
        sent = False
        if owner_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=owner_tg_id,
                    text=welcome_msg,
                    reply_markup=pause_markup(profile_id),
                )
                # Standalone photo invite so it isn't buried in the welcome message.
                await context.bot.send_message(
                    chat_id=owner_tg_id,
                    text=build_photo_invite_message(),
                    parse_mode="Markdown",
                    reply_markup=photo_invite_markup(),
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


async def bump_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /bump MTHAQ-001")
        return

    profile_id = context.args[0].strip()

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
        await update.message.reply_text("Profile " + profile_id + " not found, inactive or paused.")
        return

    p = result.data[0]
    text = build_profile_text(p)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        reply_markup=profile_button_markup(profile_id),
    )
    await update.message.reply_text("✅ Profile " + profile_id + " bumped to channel.")


async def repost_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    await update.message.reply_text("⏳ Reposting all active profiles. This will take a few minutes...")

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
            await asyncio.sleep(2)
        except Exception as e:
            fail_count += 1
            logging.warning(f"Failed to repost {profile_id}: {str(e)}")
            await asyncio.sleep(3)

    await update.message.reply_text(
        f"✅ Repost complete.\n✅ Success: {success_count}\n❌ Failed: {fail_count}"
    )


async def interest_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    prefix, profile_id = query.data.split(":", 1)

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
            .select("telegram_user_id")
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

    requester_profile = get_requester_profile(user.username, user.id)
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

    # ── Photo note is now shown in the "interest sent" popup (below), so a
    #    photo-having user gets it right there in the channel — no blocking DM. ──

    state_result = (
        supabase.table("user_state")
        .select("*")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )

    user_state = state_result.data[0].get("state") if state_result.data else "free"
    active_request_id = state_result.data[0].get("active_request_id") if state_result.data else None

    if user_state == "locked":
        # Only block if there is ACTUALLY a live pending request. State can get
        # orphaned at "locked" (e.g. a flow was interrupted and never reset), which
        # would otherwise wrongly block the user forever. If no genuine pending
        # request exists, self-heal the state to free and let them proceed.
        real_pending = None
        try:
            if active_request_id:
                rp = (
                    supabase.table("requests")
                    .select("id,status")
                    .eq("id", active_request_id)
                    .limit(1)
                    .execute()
                )
                if rp.data and rp.data[0].get("status") == "pending":
                    real_pending = rp.data[0]
            if real_pending is None:
                # Fallback: any pending request they own as requester?
                rp2 = (
                    supabase.table("requests")
                    .select("id")
                    .eq("requester_telegram_user_id", user.id)
                    .eq("status", "pending")
                    .limit(1)
                    .execute()
                )
                if rp2.data:
                    real_pending = rp2.data[0]
        except Exception as e:
            logging.warning("pending-check lookup failed: " + str(e))

        if real_pending is not None:
            await query.answer(
                "You already have an active pending request. Send /my_request to see it or /withdraw to cancel it.",
                show_alert=True,
            )
            return
        else:
            # Orphaned "locked" state with no real pending request — heal it.
            try:
                supabase.table("user_state").update(
                    {"state": "free", "active_request_id": None}
                ).eq("telegram_user_id", user.id).execute()
                logging.info("Healed orphaned 'locked' state for user " + str(user.id))
            except Exception as e:
                logging.warning("Could not heal orphaned state: " + str(e))

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

    if profile.get("is_matched"):
        await query.answer(
            "This person is currently in a conversation through Mithaq and isn't receiving new "
            "interest right now. Please feel free to express interest in another profile. 🤲",
            show_alert=True,
        )
        return

    if profile.get("is_paused"):
        await query.answer(
            "This profile is paused at the moment and isn't receiving interest. "
            "Please check back later. 🤲",
            show_alert=True,
        )
        return

    # ── Consent now lives on the channel button itself ──
    # The channel "Express Interest" button reads "(contacts shared if approved)",
    # so tapping it is itself the consent — no separate confirmation step and no
    # switching to the bot DM (that screen-switch was the friction we removed).
    # Expressing interest shares nothing yet and is reversible via /withdraw, so a
    # pre-send "are you sure" gate isn't needed here. The weightier, irreversible
    # moment — approval, where contact actually goes out — keeps its confirmation.

    owner_username = (profile.get("owner_telegram_username") or "")
    owner_tg_id = profile.get("owner_telegram_user_id")
    owner_photo_url = get_photo_ref(profile)

    active_check = (
        supabase.table("requests")
        .select("id,requester_telegram_user_id")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .eq("is_active_request", True)
        .limit(1)
        .execute()
    )

    # Guard against a GHOST active request: a pending+active request whose requester
    # is no longer actually waiting on it (their user_state doesn't point to it, or
    # was reset). Such ghosts would otherwise queue everyone new behind them forever.
    if active_check.data:
        ghost_id = active_check.data[0]["id"]
        ghost_requester = active_check.data[0].get("requester_telegram_user_id")
        is_ghost = False
        try:
            if ghost_requester:
                gs = (
                    supabase.table("user_state")
                    .select("active_request_id,state")
                    .eq("telegram_user_id", ghost_requester)
                    .limit(1)
                    .execute()
                )
                if not gs.data:
                    is_ghost = True
                else:
                    gs_active = gs.data[0].get("active_request_id")
                    gs_state = gs.data[0].get("state")
                    # Ghost if the requester's state no longer references this request
                    # as their locked active request.
                    if gs_active != ghost_id or gs_state not in ("locked", "queued"):
                        is_ghost = True
            else:
                is_ghost = True
        except Exception as e:
            logging.warning("ghost-check lookup failed: " + str(e))

        if is_ghost:
            try:
                supabase.table("requests").update(
                    {"status": "closed", "is_active_request": False,
                     "decided_at": datetime.now(timezone.utc).isoformat()}
                ).eq("id", ghost_id).execute()
                logging.info("Cleared ghost active request " + str(ghost_id) + " on " + profile_id)
                active_check.data = []  # profile is now free
            except Exception as e:
                logging.warning("Could not clear ghost request: " + str(e))

    queue_count = (
        supabase.table("requests")
        .select("id", count="exact")
        .eq("profile_id", profile_id)
        .eq("status", "pending")
        .eq("is_active_request", True)
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
    requester_photo_url = get_photo_ref(requester_profile)
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

        # Best-effort popup. In the deep-link flow the callback query can expire
        # before we get here, making answer() fail with 400 — that must NOT crash
        # the flow, because the confirmation is also sent as a normal message below.
        try:
            await query.answer(
                ("✅ Your interest has been sent. You'll be notified of their response insha'Allah."
                 + ("\n\n📷 Your photo is only shared if you both approve — never public."
                    if requester_has_photo else "")
                 + "\n\n↩️ To withdraw: tap Withdraw in the message the bot just sent you."),
                show_alert=True,
            )
        except Exception as e:
            logging.info("interest-sent popup skipped (callback likely expired): " + str(e))

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

        # ── Owner notification (best-effort) ──
        # Include the requester's FULL profile so the owner can consider them
        # properly, right here, without hunting for the profile in the channel.
        request_text = build_interest_notification(profile_id, requester_profile, str(photo_line))

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

        # ── Admin notification (ALWAYS fires, no matter what happened above) ──
        # This is the safety net: if the owner isn't linked (NULL owner) or the
        # owner send failed, the interest must still reach admin so it's never lost.
        try:
            if sent_to_owner:
                owner_status = "\n\n✅ Request sent to owner. You can also approve/decline below."
            elif not owner_tg_id:
                owner_status = ("\n\n⚠️ OWNER NOT LINKED — this profile owner has not activated "
                                "their bot, so they could NOT be notified. Please handle this "
                                "request via the buttons below, or contact them directly.")
            else:
                owner_status = "\n\n⚠️ Owner send FAILED — please approve/decline below."

            admin_text = (
                "🔔 New Interest Request\n\n"
                "Profile: " + str(profile_id) + "\n"
                "From: @" + str(user.username or user.id) + " (" + str(requester_profile_text) + ")\n"
                "Owner: @" + str(owner_username or "(no username)")
                + owner_status
            )
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text=admin_text,
                reply_markup=admin_request_markup(request_id),
            )
        except Exception as e:
            # Even if building/sending the rich admin message fails, get SOMETHING to admin.
            logging.warning("Admin notification build failed: " + str(e))
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_USER_ID,
                    text=("🔔 New interest on " + str(profile_id) + " (request " + str(request_id)
                          + ") — details failed to render, please check. Owner linked: "
                          + ("yes" if owner_tg_id else "NO")),
                    reply_markup=admin_request_markup(request_id),
                )
            except Exception as e2:
                logging.warning("Fallback admin notification ALSO failed: " + str(e2))

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
            ("✅ You've been added to the queue for this profile insha'Allah."
             + ("\n\n📷 Your photo is shared only if you both approve — never public."
                if requester_has_photo else "")
             + "\n\n↩️ To leave: tap Withdraw in the message the bot just sent you."),
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


async def handle_decline_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return

    _, request_id_str, reason_code = parts
    request_id = int(request_id_str)

    if reason_code == "other":
        supabase.table("user_state").update({
            "state": "awaiting_decline_reason",
            "active_request_id": request_id,
        }).eq("telegram_user_id", user.id).execute()
        await query.edit_message_text(
            "Please type your reason for declining below. 👇\n\n"
            "⚠️ This will be sent privately to the person who expressed interest, "
            "so please keep it kind and respectful. A gentle word is a sadaqah. 🤲"
        )
        return

    reason_text = DECLINE_REASONS.get(reason_code, "Not the right fit")
    await complete_decline(request_id, user, context, reason_text)
    await query.edit_message_text("Done — your reason has been sent privately to them. JazakAllahu khayran. 🤲")


async def handle_free_text_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return

    state_result = (
        supabase.table("user_state")
        .select("*")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )

    if not state_result.data:
        return

    state = state_result.data[0].get("state")
    if state != "awaiting_decline_reason":
        return

    request_id = state_result.data[0].get("active_request_id")
    if not request_id:
        return

    reason_text = update.message.text.strip()
    await complete_decline(request_id, user, context, reason_text)
    await update.message.reply_text("Done — your reason has been sent privately to them. JazakAllahu khayran. 🤲")


# ── Photo feature ──────────────────────────────────────────────────────────────

PHOTO_INTRO = (
    "📷 *Adding your photo*\n\n"
    "Before you send it, here's exactly how your photo is used:\n\n"
    "• It is *never* shown publicly or posted in the channel.\n"
    "• It is only ever shared *privately with one person*, and only when you *both* approve interest and *both* choose to share — it's always mutual, never one-sided.\n"
    "• Even after you add it, approving someone does *not* automatically share it — you choose \"Approve & Share Photos\" each time, or approve without sharing.\n"
    "• Your photo isn't posted to any public link or website — it stays within Telegram.\n"
    "• You can remove it anytime with /removephoto.\n\n"
    "If you're happy with that, send me the photo now. Or send /cancel to stop. 🤲"
)


def _get_user_profile_by_tg(tg_id: int):
    """Find the profile owned by this Telegram user (by ID, the reliable key)."""
    res = (
        supabase.table("profiles")
        .select("id, owner_telegram_user_id, photo_file_id, photo_url")
        .eq("owner_telegram_user_id", tg_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


async def _begin_addphoto(user, send) -> None:
    """Shared logic for starting the photo flow, used by both the /addphoto
    command and the 'Add a private photo' button. `send` is an async callable
    that takes (text, **kwargs) and delivers a message to the user."""
    # Must own a profile to attach a photo to it.
    profile = _get_user_profile_by_tg(user.id)
    if not profile:
        await send(
            "I couldn't find your profile yet. Please make sure you've completed the form and sent /start first. 🤲"
        )
        return

    # Mark this user as awaiting a photo, preserving their current state so a
    # queued/locked status isn't lost. Stored as "awaiting_photo|<prev_state>".
    st = (
        supabase.table("user_state")
        .select("state")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )
    prev_state = (st.data[0].get("state") if st.data else "free") or "free"
    if prev_state.startswith("awaiting_photo"):
        prev_state = prev_state.split("|", 1)[1] if "|" in prev_state else "free"

    supabase.table("user_state").update(
        {"state": "awaiting_photo|" + prev_state}
    ).eq("telegram_user_id", user.id).execute()

    await send(PHOTO_INTRO, parse_mode="Markdown")


async def addphoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    await _begin_addphoto(user, update.message.reply_text)


async def addphoto_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'Add a private photo' button on the welcome photo invite."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    await query.answer()

    async def send(text, **kwargs):
        await context.bot.send_message(chat_id=user.id, text=text, **kwargs)

    await _begin_addphoto(user, send)


async def addphoto_notnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'Not now' button on the photo invite."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    try:
        await query.edit_message_text(
            "No problem — you can add a private photo anytime with /addphoto. 🤲"
        )
    except Exception:
        pass


async def interest_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles 'Cancel' on the express-interest consent step (no request created)."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    try:
        await query.edit_message_text(
            "No problem — no interest was sent. You can express interest anytime. 🤲"
        )
    except Exception:
        pass


async def removephoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    profile = _get_user_profile_by_tg(user.id)
    if not profile:
        await update.message.reply_text(
            "I couldn't find your profile yet. Please send /start first. 🤲"
        )
        return

    supabase.table("profiles").update(
        {"photo_file_id": None}
    ).eq("id", profile["id"]).execute()

    # Note: we only clear the Telegram file_id. Legacy photo_url (if any) is left
    # as-is; if you want to clear that too, it can be added here.
    await update.message.reply_text(
        "✅ Your Telegram photo has been removed. You can add one again anytime with /addphoto. 🤲"
    )


async def reset_all_requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: clear ALL pending/queued requests across the whole system and free
    every user's state. Use this to get a clean slate after heavy testing has left
    a tangle of interconnected requests between many test profiles.
    Usage: /reset_all_requests CONFIRM"""
    user = update.effective_user
    if not user or not update.message:
        return
    if user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    if not context.args or context.args[0].strip().upper() != "CONFIRM":
        await update.message.reply_text(
            "⚠️ This clears ALL pending/queued interest requests across the entire system "
            "and frees every user's state. Approved matches that are already completed are "
            "not affected.\n\n"
            "To proceed, send: /reset_all_requests CONFIRM"
        )
        return

    cleared = 0
    freed = 0
    try:
        pend = (
            supabase.table("requests")
            .select("id")
            .eq("status", "pending")
            .execute()
        )
        for row in (pend.data or []):
            supabase.table("requests").update({
                "status": "withdrawn",
                "is_active_request": False,
                "decided_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", row["id"]).execute()
            cleared += 1

        # Free any user_state that is locked/queued.
        states = (
            supabase.table("user_state")
            .select("telegram_user_id,state")
            .in_("state", ["locked", "queued"])
            .execute()
        )
        for row in (states.data or []):
            supabase.table("user_state").update({
                "state": "free",
                "active_request_id": None,
            }).eq("telegram_user_id", row["telegram_user_id"]).execute()
            freed += 1
    except Exception as e:
        await update.message.reply_text("Error during global reset: " + str(e))
        return

    await update.message.reply_text(
        "✅ Global reset complete.\n"
        "Cleared " + str(cleared) + " pending/queued request(s) and freed "
        + str(freed) + " user state(s).\n"
        "The whole system is now a clean slate for testing."
    )


async def reset_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: force-clear ALL pending requests and reset state for a user, to recover
    a test account stuck with orphaned 'pending' requests from earlier testing.
    Usage: /reset_user @username  or  /reset_user <telegram_user_id>
    With no argument, resets the admin's own account."""
    user = update.effective_user
    if not user or not update.message:
        return
    if user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    target_tg_id = None
    target_username = None
    if context.args:
        arg = context.args[0].strip()
        # Accept a profile ID (e.g. MTHAQ-225), a @username, or a numeric telegram id.
        if arg.upper().startswith("MTHAQ-") or arg.upper().startswith("MTHAQ"):
            pid = arg.upper()
            if pid.startswith("MTHAQ") and not pid.startswith("MTHAQ-"):
                pid = "MTHAQ-" + pid[len("MTHAQ"):]
            try:
                pr = (
                    supabase.table("profiles")
                    .select("owner_telegram_user_id")
                    .eq("id", pid)
                    .limit(1)
                    .execute()
                )
                if pr.data and pr.data[0].get("owner_telegram_user_id"):
                    target_tg_id = pr.data[0]["owner_telegram_user_id"]
                else:
                    await update.message.reply_text(
                        "Profile " + pid + " has no linked Telegram account (owner_telegram_user_id is empty), "
                        "so there's no user state to reset."
                    )
                    return
            except Exception as e:
                await update.message.reply_text("Lookup failed: " + str(e))
                return
        elif arg.lstrip("-").isdigit():
            target_tg_id = int(arg)
        else:
            target_username = arg.replace("@", "").strip().lower()
    else:
        target_tg_id = user.id

    if target_tg_id is None and target_username:
        try:
            pr = (
                supabase.table("profiles")
                .select("owner_telegram_user_id")
                .ilike("owner_telegram_username", target_username)
                .limit(1)
                .execute()
            )
            if pr.data and pr.data[0].get("owner_telegram_user_id"):
                target_tg_id = pr.data[0]["owner_telegram_user_id"]
        except Exception as e:
            await update.message.reply_text("Lookup failed: " + str(e))
            return

    if target_tg_id is None:
        await update.message.reply_text(
            "Couldn't resolve that user. Try /reset_user <telegram_user_id>."
        )
        return

    cleared_requests = 0
    try:
        pend = (
            supabase.table("requests")
            .select("id")
            .eq("requester_telegram_user_id", target_tg_id)
            .eq("status", "pending")
            .execute()
        )
        for row in (pend.data or []):
            supabase.table("requests").update({
                "status": "withdrawn",
                "decided_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", row["id"]).execute()
            cleared_requests += 1

        st = (
            supabase.table("user_state")
            .select("telegram_user_id")
            .eq("telegram_user_id", target_tg_id)
            .limit(1)
            .execute()
        )
        if st.data:
            supabase.table("user_state").update({
                "active_request_id": None,
                "state": "free",
            }).eq("telegram_user_id", target_tg_id).execute()
    except Exception as e:
        await update.message.reply_text("Reset error: " + str(e))
        return

    await update.message.reply_text(
        "✅ Reset done for user " + str(target_tg_id) + ".\n"
        "Withdrew " + str(cleared_requests) + " pending request(s) and set state to free.\n"
        "They can now express interest cleanly."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    st = (
        supabase.table("user_state")
        .select("state")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )
    state = (st.data[0].get("state") if st.data else "") or ""
    if state.startswith("awaiting_photo"):
        prev = state.split("|", 1)[1] if "|" in state else "free"
        supabase.table("user_state").update(
            {"state": prev}
        ).eq("telegram_user_id", user.id).execute()
        await update.message.reply_text("No problem — photo upload cancelled. 🤲")
    else:
        await update.message.reply_text("Nothing to cancel. 🤲")


async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture a photo sent by a user who is in the awaiting_photo state, store
    its Telegram file_id on their profile, and restore their previous state."""
    user = update.effective_user
    if not user or not update.message or not update.message.photo:
        return

    st = (
        supabase.table("user_state")
        .select("state")
        .eq("telegram_user_id", user.id)
        .limit(1)
        .execute()
    )
    state = (st.data[0].get("state") if st.data else "") or ""
    if not state.startswith("awaiting_photo"):
        # Not in the photo flow — ignore stray photos quietly.
        return

    profile = _get_user_profile_by_tg(user.id)
    if not profile:
        await update.message.reply_text(
            "I couldn't find your profile to attach this to. Please send /start first. 🤲"
        )
        return

    # The largest available size is the last entry in update.message.photo.
    file_id = update.message.photo[-1].file_id

    supabase.table("profiles").update(
        {"photo_file_id": file_id}
    ).eq("id", profile["id"]).execute()

    # Restore the user's previous state.
    prev = state.split("|", 1)[1] if "|" in state else "free"
    supabase.table("user_state").update(
        {"state": prev}
    ).eq("telegram_user_id", user.id).execute()

    await update.message.reply_text(
        "✅ JazakAllahu khayran — your photo has been saved.\n\n"
        "Remember: it stays private and is only ever shared when you both approve "
        "*and* you choose to share it. You're always in control — change it anytime "
        "with /addphoto, or remove it with /removephoto. 🤲",
        parse_mode="Markdown",
    )


async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not query or not user or not query.data:
        return

    await query.answer()

    action, request_id_str = query.data.split(":", 1)
    request_id_or_profile = request_id_str

    # ── Owner approval confirmation plumbing ──
    # A confirmed owner approval (second tap) maps back to the real approve action.
    # approval_confirmed prevents the gate from re-showing (which would loop).
    approval_confirmed = False
    if action == "confirm_approve":
        action = "approve"
        approval_confirmed = True
    elif action == "confirm_approve_photo":
        action = "approve_photo"
        approval_confirmed = True
    elif action == "approveback":
        # Owner tapped "Go back" — restore the original request message + buttons.
        req_r = supabase.table("requests").select("*").eq("id", int(request_id_str)).limit(1).execute()
        if not req_r.data or req_r.data[0].get("status") != "pending":
            await query.edit_message_text("This request has already been handled.")
            return
        r = req_r.data[0]
        pid = r["profile_id"]
        pr = supabase.table("profiles").select("*").eq("id", pid).limit(1).execute()
        o_photo = get_photo_ref(pr.data[0]) if pr.data else None
        rq_prof = get_requester_profile(r.get("requester_username", ""), r.get("requester_telegram_user_id"))
        rq_photo = get_photo_ref(rq_prof)
        rq_pid = rq_prof["id"] if rq_prof else None
        await query.edit_message_text(
            build_interest_notification(pid, rq_prof, ""),
            reply_markup=owner_request_markup(int(request_id_str), bool(rq_photo), bool(o_photo)),
        )
        return

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
                text="⏸ Your profile " + profile_id + " has been paused. 🤲"
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
                text="▶️ Your profile " + profile_id + " has been resumed. 🤲"
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
            await advance_queue(profile_id, context, repost_if_empty=False)
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
    requester_username = (req.get("requester_username") or "")
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
    owner_profile_full = None
    if profile_result.data:
        owner_profile_full = profile_result.data[0]
        owner_tg_id = profile_result.data[0].get("owner_telegram_user_id")
        owner_username = (profile_result.data[0].get("owner_telegram_username") or "")
        owner_photo_url = get_photo_ref(profile_result.data[0])

    is_admin = user.id == ADMIN_TELEGRAM_USER_ID
    is_owner = (owner_tg_id and user.id == owner_tg_id) or (user.username and user.username.lower() == owner_username.lower())

    if not is_admin and not is_owner:
        await query.answer("Not authorised.", show_alert=True)
        return

    if action in ("approve", "approve_photo"):
        share_photos = (action == "approve_photo")

        # ── Owner-only approval confirmation gate ──
        # Approving releases contact and pauses both profiles — an irreversible,
        # weighty step. If the OWNER is approving (not admin) and hasn't yet
        # confirmed, ask them to confirm first, with gender-aware wording about
        # what of theirs is shared. Admin approvals are deliberate and skip this.
        if is_owner and not is_admin and not approval_confirmed:
            o_gender = (owner_profile_full.get("gender") or "").lower() if owner_profile_full else ""
            o_is_sister = ("sister" in o_gender or "female" in o_gender)
            o_no_wali = (bool(owner_profile_full.get("no_wali")) or not (owner_profile_full.get("wali_contact") or "").strip()) if owner_profile_full else False
            if o_is_sister and o_no_wali:
                confirm_text = (
                    "⚠️ Please confirm.\n\n"
                    "Approving pauses both profiles. As you don't have a wali, Mithaq "
                    "will speak with you first to agree how the introduction is made "
                    "before your contact is shared. Only approve if you intend to take "
                    "it forward."
                )
            elif o_is_sister:
                confirm_text = (
                    "⚠️ Please confirm.\n\n"
                    "Approving shares your wali's contact so you can be introduced "
                    "through him, and pauses both profiles. Only approve if you "
                    "intend to take it forward."
                )
            else:
                confirm_text = (
                    "⚠️ Please confirm.\n\n"
                    "Approving shares your contact details and pauses both profiles. "
                    "Only approve if you intend to take it forward."
                )
            base = "approve_photo" if share_photos else "approve"
            await query.edit_message_text(
                confirm_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, approve", callback_data="confirm_" + base + ":" + str(request_id))],
                    [InlineKeyboardButton("↩️ Go back", callback_data="approveback:" + str(request_id))],
                ]),
            )
            return

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

        contact_lines, wali_missing = format_contact_details(p)

        if wali_missing:
            # Sister has no usable wali → don't send broken details. Tell the
            # requester it's being arranged, and alert admin to sort it out.
            hold_msg = (
                "🤍 Alhamdulillah — your interest in profile " + profile_id + " has been approved.\n\n"
                "This sister is being introduced through Mithaq. We'll be in touch with the "
                "details shortly, insha'Allah — nothing is needed from you right now.\n\n"
                "May Allah make it easy for you both, and put barakah in it. 🤲"
            )
            await context.bot.send_message(chat_id=requester_id, text=hold_msg)
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_USER_ID,
                    text=("⚠️ WALI NEEDED — profile " + profile_id + " approved a match but has "
                          "NO wali contact (no_wali/blank). Requester @" +
                          str(req.get("requester_username", requester_id)) + " is waiting.\n\n"
                          "Arrange the wali contact, then run:\n"
                          "/set_wali " + profile_id + " <number>\n"
                          "…to send it to the waiting requester."),
                )
            except Exception as e:
                logging.warning("Could not send wali-needed admin alert: " + str(e))
        else:
            contact_msg = (
                "🤍 Alhamdulillah — your interest has been approved.\n\n"
                "Below are their contact details. Please reach out with respect, patience, "
                "and good character.\n\n"
                + contact_lines + "\n\n"
                "From here, it is between you both, your walis, and Allah. May He put "
                "barakah in it. 🤲\n\n"
                "─────────────\n"
                "⚠️ Important: while you're in this introduction, your profile is paused and "
                "you won't see or receive new interest. If it doesn't progress to marriage, "
                "please come back and tap the 'Make me available again' button below (or send /available) "
                "so you return to the pool. Otherwise you'll stay paused."
            )
            await context.bot.send_message(
                chat_id=requester_id, text=contact_msg, reply_markup=available_menu_markup())


        requester_profile_full = get_requester_profile(requester_username, requester_id)

        # ── take BOTH parties out of circulation while they talk ──
        supabase.table("profiles").update(
            {"is_paused": True, "is_matched": True}).eq("id", profile_id).execute()
        if requester_profile_full:
            supabase.table("profiles").update(
                {"is_paused": True, "is_matched": True}).eq("id", requester_profile_full["id"]).execute()

        if requester_profile_full and owner_tg_id:
            req_profile_id = (requester_profile_full.get("id") or "")
            req_lines, req_wali_missing = format_contact_details(requester_profile_full)

            if req_wali_missing:
                # The requester is a sister with no wali → hold their side too.
                try:
                    await context.bot.send_message(
                        chat_id=owner_tg_id,
                        text=("✅ You approved the interest from " + req_profile_id + ".\n\n"
                              "She does not currently have a wali on file, so Mithaq is helping "
                              "arrange the introduction — you'll be contacted with the details "
                              "shortly insha'Allah.\n\nMay Allah make it easy for you both. 🤲"),
                    )
                except Exception as e:
                    logging.warning("Could not send hold message to owner: " + str(e))
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_TELEGRAM_USER_ID,
                        text=("⚠️ WALI NEEDED — requester " + req_profile_id + " (a sister) has NO "
                              "wali contact. Owner of " + profile_id + " is waiting.\n\n"
                              "Arrange it, then run:\n/set_wali " + req_profile_id + " <number>"),
                    )
                except Exception as e:
                    logging.warning("Could not send wali-needed admin alert (owner side): " + str(e))
            else:
                owner_contact_msg = (
                    "🤍 Alhamdulillah — you approved the interest from " + req_profile_id + ". "
                    "Contact details have been exchanged.\n\n"
                    "Here are their contact details:\n"
                    + req_lines + "\n\n"
                    "May Allah put barakah in it. 🤲\n\n"
                    "─────────────\n"
                    "⚠️ Important: while you're in this introduction, your profile is paused and "
                    "you won't see or receive new interest. If it doesn't progress to marriage, "
                    "please come back and tap the 'Make me available again' button below (or send /available) "
                    "so you return to the pool. Otherwise you'll stay paused."
                )
                try:
                    await context.bot.send_message(
                        chat_id=owner_tg_id, text=owner_contact_msg, reply_markup=available_menu_markup())
                except Exception as e:
                    logging.warning("Could not send requester contact to owner: " + str(e))
        elif owner_tg_id:
            try:
                await context.bot.send_message(
                    chat_id=owner_tg_id,
                    text=("🤍 Alhamdulillah — you approved the interest request. Your contact details "
                          "have been shared with them. May Allah put barakah in it. 🤲\n\n"
                          "─────────────\n"
                          "⚠️ Important: while you're in this introduction, your profile is paused and "
                          "you won't see or receive new interest. If it doesn't progress to marriage, "
                          "please come back and tap the 'Make me available again' button below (or send /available) "
                          "so you return to the pool. Otherwise you'll stay paused."),
                    reply_markup=available_menu_markup(),
                )
            except Exception as e:
                logging.warning("Could not send confirmation to owner: " + str(e))

        if share_photos:
            requester_profile = get_requester_profile(requester_username, requester_id)
            requester_photo = get_photo_ref(requester_profile)
            owner_photo = owner_photo_url  # already resolved via get_photo_ref above

            if requester_photo and owner_photo:
                try:
                    await context.bot.send_photo(
                        chat_id=requester_id,
                        photo=owner_photo,
                        caption="📷 Photo shared by profile " + profile_id,
                    )
                    if owner_tg_id:
                        await context.bot.send_photo(
                            chat_id=owner_tg_id,
                            photo=requester_photo,
                            caption="📷 Photo shared by the person interested in your profile",
                        )
                except Exception as e:
                    logging.warning("Could not send photos: " + str(e))

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text="✅ Approved" + (" with photos" if share_photos else "") + ": profile " + profile_id + " request " + str(request_id) + " from @" + str(req.get("requester_username", requester_id)) + " by @" + str(user.username or user.id),
        )

        await query.edit_message_text(
            "🤍 Alhamdulillah — you've approved this introduction"
            + (", and your photos have been shared." if share_photos else ".")
            + " Their contact details are on their way to you now. May Allah put barakah in it. 🤲"
        )

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
        supabase.table("user_state").update({
            "state": "awaiting_decline_reason",
            "active_request_id": request_id,
        }).eq("telegram_user_id", user.id).execute()

        await query.edit_message_text(
            "Please choose a reason for declining. 👇\n"
            "(This is shared privately with the other person, so it's worded kindly.)"
        )
        await context.bot.send_message(
            chat_id=user.id,
            text="Why are you declining this request?",
            reply_markup=decline_reason_markup(request_id),
        )

    elif action == "consider":
        # Owner is taking time to make istikhara / consult. Request stays pending and
        # open; requester stays held (locked) but may withdraw. A 2-day nudge and a
        # 5-day auto-expire are handled by the check_consideration job.
        supabase.table("requests").update({
            "consideration_started_at": datetime.now(timezone.utc).isoformat(),
            "consideration_nudge_sent": False,
            "reminder_sent": True,  # suppress the unrelated 2-hour "still waiting" reminder
        }).eq("id", request_id).execute()

        requester_profile_c = get_requester_profile(requester_username, requester_id)
        requester_has_photo = bool(get_photo_ref(requester_profile_c))
        owner_has_photo = bool(owner_photo_url)

        # update the request message to a "considering" status, KEEPING Approve/Decline
        try:
            await query.edit_message_text(
                "⏳ You're taking time to consider this interest for profile " + profile_id + ".\n\n"
                "The Approve and Decline buttons below remain available whenever you're ready. 🤲",
                reply_markup=owner_request_markup(request_id, requester_has_photo, owner_has_photo, include_consider=False),
            )
        except Exception as e:
            logging.warning("Could not update consider message: " + str(e))

        # warm, gender-aware confirmation to the owner
        owner_gender = (profile_result.data[0].get("gender") or "").lower() if profile_result.data else ""
        if "sister" in owner_gender or "female" in owner_gender:
            consult_line = "make istikhara and consult your wali and family"
            hold_line = "She is taking time to make istikhara and consult her wali before deciding."
        else:
            consult_line = "make istikhara and consult your family"
            hold_line = "He is taking time to make istikhara and consult his family before deciding."

        await context.bot.send_message(
            chat_id=user.id,
            text="JazakAllah khayran. Take the time you need to " + consult_line + ". May Allah guide you to what is best. 🤲",
        )

        # dignified holding message to the requester (still held, may withdraw)
        try:
            await context.bot.send_message(
                chat_id=requester_id,
                text=(
                    "JazakAllah khayran for your interest in profile " + profile_id + ". "
                    + hold_line + " Please be patient — you'll be notified of their response "
                    "insha'Allah.\n\nYour interest remains held with them. If you'd prefer not to "
                    "wait, you can withdraw at any time by sending /withdraw. 🤲"
                ),
                reply_markup=interest_confirmation_markup(request_id),
            )
        except Exception as e:
            logging.warning("Could not send holding message to requester: " + str(e))

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_USER_ID,
            text="⏳ Considering: profile " + profile_id + " request " + str(request_id) + " — owner is taking time to decide.",
        )


# ── /available — let a paused/matched user (and their partner) return ──────────

async def _offer_available(user, reply, context) -> None:
    """Find the user's profile and offer the confirm prompt. Shared by the
    /available command and the button on the match message."""
    profile = None
    res = (supabase.table("profiles").select("*")
           .eq("owner_telegram_user_id", user.id).limit(1).execute())
    if res.data:
        profile = res.data[0]
    elif user.username:
        res = (supabase.table("profiles").select("*")
               .eq("owner_telegram_username", user.username.lower()).limit(1).execute())
        profile = res.data[0] if res.data else None

    if not profile:
        await reply("I couldn't find your profile linked to this account. Please contact @MithaqAdmin. 🤲")
        return

    if profile.get("is_active") and not profile.get("is_paused") and not profile.get("is_matched"):
        await reply(
            "Your profile is already active — you can express interest in profiles now. 🤲\n\n"
            "📢 Browse here: " + CHANNEL_LINK)
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, make me available", callback_data="avail_yes:" + profile["id"]),
        InlineKeyboardButton("❌ Cancel", callback_data="avail_no:" + profile["id"]),
    ]])
    await reply(
        "This will put you back into circulation so you can express interest in profiles again, "
        "and it will end your current match (the other person will be made available too).\n\n"
        "Are you sure you'd like to continue?",
        reply_markup=keyboard,
    )


async def available_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    await _offer_available(user, update.message.reply_text, context)


async def available_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    await query.answer()
    await _offer_available(user, query.message.reply_text, context)


async def available_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    await query.answer()

    action, profile_id = query.data.split(":", 1)

    if action == "avail_no":
        await query.edit_message_text("No problem — nothing has changed. 🤲")
        return

    # 1) reactivate this user's profile + free their state
    # (No channel repost: their original profile post remains in the channel, so
    # they're still findable. Reposting on every unmatch just floods the channel.)
    supabase.table("profiles").update(
        {"is_active": True, "is_paused": False, "is_matched": False}).eq("id", profile_id).execute()
    supabase.table("user_state").update(
        {"state": "free", "active_request_id": None}).eq("telegram_user_id", user.id).execute()

    # 2) find the match partner via the most recent approved request (either direction)
    partner_profile_id = None
    partner_tg_id = None
    closed_request_id = None

    res = (supabase.table("requests").select("*")
           .eq("profile_id", profile_id).eq("status", "approved")
           .order("decided_at", desc=True).limit(1).execute())
    if res.data:                                   # this user was the OWNER
        req = res.data[0]
        closed_request_id = req.get("id")
        partner_tg_id = req.get("requester_telegram_user_id")
        partner_prof = get_requester_profile(req.get("requester_username", ""), req.get("requester_telegram_user_id"))
        partner_profile_id = partner_prof["id"] if partner_prof else None
    else:                                          # this user was the REQUESTER
        res = (supabase.table("requests").select("*")
               .eq("requester_telegram_user_id", user.id).eq("status", "approved")
               .order("decided_at", desc=True).limit(1).execute())
        if res.data:
            req = res.data[0]
            closed_request_id = req.get("id")
            partner_profile_id = req.get("profile_id")
            owner_res = (supabase.table("profiles").select("owner_telegram_user_id")
                         .eq("id", partner_profile_id).limit(1).execute())
            partner_tg_id = owner_res.data[0].get("owner_telegram_user_id") if owner_res.data else None

    # Close the old approved request so it is never re-found as a "current" match.
    # (Without this, the same approved request keeps being picked up, leaving both
    # sides in a stale-matched state even after they've been made available again.)
    if closed_request_id is not None:
        supabase.table("requests").update({
            "status": "closed",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", closed_request_id).execute()

    # 3) reactivate the partner too, free their state, notify gently (no repost)
    if partner_profile_id:
        supabase.table("profiles").update(
            {"is_active": True, "is_paused": False, "is_matched": False}).eq("id", partner_profile_id).execute()
        if partner_tg_id:
            supabase.table("user_state").update(
                {"state": "free", "active_request_id": None}).eq("telegram_user_id", partner_tg_id).execute()
            try:
                await context.bot.send_message(
                    chat_id=partner_tg_id,
                    text=("Your previous conversation on Mithaq has been closed, and your profile is "
                          "active again. You're free to express interest in profiles whenever you're "
                          "ready. 🤲\n\n📢 Browse here: " + CHANNEL_LINK))
            except Exception as e:
                logging.warning("Could not notify partner of reactivation: " + str(e))

    # 4) confirm to the user
    await query.edit_message_text(
        "Done — you're active again and back in the channel. You can express interest in "
        "profiles now. May Allah grant you what is best. 🤲\n\n📢 Browse here: " + CHANNEL_LINK)

    # 5) tell admin
    who = profile_id + ((" & " + partner_profile_id) if partner_profile_id else "")
    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_USER_ID,
        text="🔄 Reactivation via /available — " + who + " are available again (match ended).")


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


async def set_wali(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: fill in a missing wali contact for a sister's profile and, if
    someone is waiting on an approved match with her, release the details now."""
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /set_wali MTHAQ-217 +447700900123")
        return

    profile_id = context.args[0].strip()
    wali_number = " ".join(context.args[1:]).strip()

    # 1) Save the wali contact (and clear the no_wali flag).
    prof = (
        supabase.table("profiles").select("*").eq("id", profile_id).limit(1).execute()
    )
    if not prof.data:
        await update.message.reply_text("Profile " + profile_id + " not found.")
        return

    sister_tg_id = prof.data[0].get("owner_telegram_user_id")

    supabase.table("profiles").update(
        {"wali_contact": wali_number, "no_wali": False}
    ).eq("id", profile_id).execute()

    # 2) Find an approved match this sister is part of, to release the details.
    #    She may be the OWNER (a brother approved into her) or the REQUESTER.
    #    Both lookups use the reliable Telegram ID / profile ID — never the
    #    username, which is stored with original casing on requests and is often
    #    blank on code-first profiles. That username mismatch was why the release
    #    silently failed and the waiting party was never sent the details.
    released_to = []

    # 2a) She is the OWNER → the requester on the approved request is waiting.
    as_owner = (
        supabase.table("requests").select("*")
        .eq("profile_id", profile_id).eq("status", "approved")
        .order("decided_at", desc=True).limit(1).execute()
    )
    if as_owner.data:
        waiting_id = as_owner.data[0].get("requester_telegram_user_id")
        if waiting_id:
            try:
                await context.bot.send_message(
                    chat_id=waiting_id,
                    text=("🤍 Alhamdulillah — the wali's contact for profile " + profile_id +
                          " is ready. You can now be introduced through him. Please reach out "
                          "with respect and good character.\n\n"
                          "👤 Wali contact: " + wali_number + "\n\n"
                          "May Allah put barakah in it. 🤲"),
                )
                released_to.append(str(waiting_id))
            except Exception as e:
                logging.warning("Could not release wali to waiting requester: " + str(e))

    # 2b) She is the REQUESTER → the owner she was approved into is waiting.
    #     Match on her Telegram ID against requester_telegram_user_id (reliable),
    #     NOT on username (which was case-mismatched / blank and silently failed).
    if sister_tg_id:
        as_req = (
            supabase.table("requests").select("*")
            .eq("requester_telegram_user_id", sister_tg_id)
            .eq("status", "approved")
            .order("decided_at", desc=True).limit(1).execute()
        )
        if as_req.data:
            owner_profile_id = as_req.data[0].get("profile_id")
            owner_res = (
                supabase.table("profiles").select("owner_telegram_user_id")
                .eq("id", owner_profile_id).limit(1).execute()
            )
            owner_wid = owner_res.data[0].get("owner_telegram_user_id") if owner_res.data else None
            if owner_wid:
                try:
                    await context.bot.send_message(
                        chat_id=owner_wid,
                        text=("🤍 Alhamdulillah — the wali's contact for " + profile_id +
                              " (who expressed interest in your profile) is ready. You can now be "
                              "introduced through him. Please reach out with respect and good character.\n\n"
                              "👤 Wali contact: " + wali_number + "\n\n"
                              "May Allah put barakah in it. 🤲"),
                    )
                    released_to.append(str(owner_wid))
                except Exception as e:
                    logging.warning("Could not release wali to waiting owner: " + str(e))

    if released_to:
        await update.message.reply_text(
            "✅ Wali contact saved for " + profile_id + " and sent to the waiting party.")
    else:
        await update.message.reply_text(
            "✅ Wali contact saved for " + profile_id + ". (No one currently waiting on an approved match.)")


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_TELEGRAM_USER_ID:
        await update.message.reply_text("Not authorised.")
        return

    total_profiles = supabase.table("profiles").select("id", count="exact").execute()
    active_profiles = supabase.table("profiles").select("id", count="exact").eq("is_active", True).execute()
    paused_profiles = supabase.table("profiles").select("id", count="exact").eq("is_paused", True).execute()
    matched_profiles = supabase.table("profiles").select("id", count="exact").eq("is_matched", True).execute()
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
        "💬 In conversation (matched): " + str(matched_profiles.count),
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
            "✅ Affiliate created!\n\nName: " + name + "\nCode: " + code + "\nLink: " + link
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
            "✅ Referral marked as converted!\n\nUser: @" + str(referral.get("telegram_username", referral["telegram_user_id"])) + "\nAffiliate: " + referral["affiliate_code"]
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
    owner_photo_url = get_photo_ref(profile)

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
    requester_username = (req.get("requester_username") or "")

    requester_profile = get_requester_profile(requester_username, req.get("requester_telegram_user_id"))
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

    try:
        await context.bot.send_message(
            chat_id=owner_tg_id,
            text=request_text,
            reply_markup=owner_request_markup(request_id, requester_has_photo, owner_has_photo),
        )
        await update.message.reply_text("✅ Request resent to owner of " + profile_id + " successfully.")
    except Exception as e:
        await update.message.reply_text("❌ Could not send to owner: " + str(e))


# ── Email helper (SendGrid) ────────────────────────────────────────────────────

def _send_email(to_email: str, subject: str, body: str, label: str = "email") -> bool:
    """Shared SendGrid sender. Returns True on success, False on any failure.
    Never raises — email is best-effort and must never break the caller."""
    if not SENDGRID_API_KEY or not to_email:
        return False
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": "info@mithaqmarriage.com", "name": "Mithaq Marriage"},
        "reply_to": {"email": "info@mithaqmarriage.com", "name": "Mithaq Marriage"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": "Bearer " + SENDGRID_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code == 202:
            return True
        logging.warning(f"SendGrid {label} failed: {resp.status_code} — {resp.text}")
        return False
    except Exception as e:
        logging.warning("Could not send " + label + ": " + str(e))
        return False


def send_welcome_email(to_email: str, profile_id: str) -> bool:
    """Sent once, when a profile first goes live in the channel.
    Its real job is to get Telegram notifications switched on."""
    subject = "Your Mithaq profile is live — " + str(profile_id)
    body = (
        "Assalamu alaikum,\n\n"
        "Your profile (" + str(profile_id) + ") is now live in the Mithaq channel.\n\n"
        "ONE THING TO CHECK\n"
        "Please make sure Telegram notifications are switched on for the Mithaq bot. "
        "That is how you will hear when someone expresses interest in you — and it is "
        "easy to miss if the chat is muted.\n\n"
        "Browse profiles here:\n" + CHANNEL_LINK + "\n\n"
        "You can pause your profile, add a photo, or check your requests at any time "
        "in the bot.\n\n"
        "May Allah make it easy for you.\n\n"
        "The Mithaq Team\n"
        "info@mithaqmarriage.com\n"
        "mithaqmarriage.com"
    )
    return _send_email(to_email, subject, body, "welcome email")


def send_reminder_email(to_email: str, profile_id: str, requester_profile_text: str) -> bool:
    if not SENDGRID_API_KEY or not to_email:
        return False

    subject = "Pending response needed — Mithaq"

    body = (
        "Assalamu alaikum,\n\n"
        + requester_profile_text + " expressed interest in your profile (" + profile_id + ") "
        "2 hours ago and is still waiting for your Approve or Decline.\n\n"
        "They remain interested for now, but the longer this stays unanswered, the more likely "
        "they are to withdraw and move on to other profiles.\n\n"
        "Please check Telegram when you can to Approve or Decline.\n\n"
        "The Mithaq Team\n"
        "info@mithaqmarriage.com\n"
        "mithaqmarriage.com"
    )

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": "info@mithaqmarriage.com", "name": "Mithaq Marriage"},
        "reply_to": {"email": "info@mithaqmarriage.com", "name": "Mithaq Marriage"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }

    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": "Bearer " + SENDGRID_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code == 202:
            return True
        logging.warning(f"SendGrid reminder email failed: {resp.status_code} — {resp.text}")
        return False
    except Exception as e:
        logging.warning("Could not send reminder email: " + str(e))
        return False


# ── Pending request reminder check (runs every 30 minutes) ────────────────────

async def check_pending_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - (2 * 60 * 60)  # 2 hours ago
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()

        result = (
            supabase.table("requests")
            .select("*")
            .eq("status", "pending")
            .eq("is_active_request", True)
            .eq("reminder_sent", False)
            .lt("created_at", cutoff_iso)
            .execute()
        )

        if not result.data:
            return

        for req in result.data:
            request_id = req["id"]
            profile_id = req["profile_id"]
            requester_username = (req.get("requester_username") or "")

            profile_result = (
                supabase.table("profiles")
                .select("*")
                .eq("id", profile_id)
                .limit(1)
                .execute()
            )

            if not profile_result.data:
                continue

            profile = profile_result.data[0]
            owner_tg_id = profile.get("owner_telegram_user_id")
            owner_email = profile.get("email")

            requester_profile = get_requester_profile(requester_username, req.get("requester_telegram_user_id"))
            requester_profile_id = requester_profile["id"] if requester_profile else None
            requester_profile_text = "Profile " + requester_profile_id if requester_profile_id else "Someone"

            # Telegram reminder
            if owner_tg_id:
                try:
                    await context.bot.send_message(
                        chat_id=owner_tg_id,
                        text=(
                            "🔔 Reminder: " + requester_profile_text + " expressed interest in your profile "
                            + profile_id + " 2 hours ago and is still waiting for your Approve or Decline.\n\n"
                            "They remain interested for now, but the longer this stays unanswered, the more likely "
                            "they are to withdraw and move on to other profiles."
                        ),
                    )
                except Exception as e:
                    logging.warning("Could not send Telegram reminder: " + str(e))

            # Email reminder
            if owner_email:
                send_reminder_email(owner_email, profile_id, requester_profile_text)

            # Mark as reminded so it never fires again for this request
            supabase.table("requests").update({"reminder_sent": True}).eq("id", request_id).execute()

            logging.info(f"✅ Sent 2hr reminder for request {request_id} (profile {profile_id})")

    except Exception as e:
        logging.warning("ERROR in check_pending_reminders: " + str(e))


# ── Consideration check: 2-day nudge + 5-day auto-expire (runs every 30 min) ──

async def check_consideration(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        now = datetime.now(timezone.utc)
        two_days_ago = (now - timedelta(days=2)).isoformat()
        five_days_ago = (now - timedelta(days=5)).isoformat()

        # ── 1) AUTO-EXPIRE anything still pending after 5 days of "considering" ──
        expired = (
            supabase.table("requests")
            .select("*")
            .eq("status", "pending")
            .lt("consideration_started_at", five_days_ago)
            .execute()
        )

        for req in (expired.data or []):
            request_id = req["id"]
            profile_id = req["profile_id"]
            requester_id = req["requester_telegram_user_id"]

            supabase.table("requests").update({
                "status": "expired",
                "decided_at": now.isoformat(),
            }).eq("id", request_id).execute()

            supabase.table("user_state").update({
                "active_request_id": None,
                "state": "free",
            }).eq("telegram_user_id", requester_id).execute()

            # gentle, blameless close to the requester
            try:
                await context.bot.send_message(
                    chat_id=requester_id,
                    text=(
                        "JazakAllah khayran for your interest in profile " + profile_id + " and for your "
                        "patience. This one didn't move forward to a decision in time, so it has now been "
                        "closed. You are free to express interest in another profile whenever you're ready. "
                        "May Allah guide you to what is best for you. 🤲"
                    ),
                )
            except Exception as e:
                logging.warning("Could not notify requester of expiry: " + str(e))

            # soft note to the owner
            profile_res = (
                supabase.table("profiles").select("owner_telegram_user_id")
                .eq("id", profile_id).limit(1).execute()
            )
            owner_tg_id = profile_res.data[0].get("owner_telegram_user_id") if profile_res.data else None
            if owner_tg_id:
                try:
                    await context.bot.send_message(
                        chat_id=owner_tg_id,
                        text=(
                            "The interest on your profile " + profile_id + " has now closed as time passed "
                            "without a decision. No action needed — if it was meant for you, Allah will "
                            "bring what is best. 🤲"
                        ),
                    )
                except Exception as e:
                    logging.warning("Could not notify owner of expiry: " + str(e))

            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text="⌛ Expired: considering request " + str(request_id) + " (profile " + profile_id + ") closed after 5 days.",
            )

            # bring forward anyone queued on this profile
            await advance_queue(profile_id, context, repost_if_empty=False)

            logging.info(f"⌛ Auto-expired considering request {request_id} (profile {profile_id})")

        # ── 2) 2-DAY NUDGE for those still considering (and not yet expired) ──
        nudge = (
            supabase.table("requests")
            .select("*")
            .eq("status", "pending")
            .eq("consideration_nudge_sent", False)
            .lt("consideration_started_at", two_days_ago)
            .gte("consideration_started_at", five_days_ago)
            .execute()
        )

        for req in (nudge.data or []):
            request_id = req["id"]
            profile_id = req["profile_id"]

            profile_res = (
                supabase.table("profiles").select("owner_telegram_user_id")
                .eq("id", profile_id).limit(1).execute()
            )
            owner_tg_id = profile_res.data[0].get("owner_telegram_user_id") if profile_res.data else None

            if owner_tg_id:
                try:
                    await context.bot.send_message(
                        chat_id=owner_tg_id,
                        text=(
                            "🤲 A gentle reminder: you asked for time to consider the interest on your "
                            "profile " + profile_id + ". Whenever you're ready, the Approve and Decline "
                            "buttons are still here — no rush, just so it doesn't slip your mind. May Allah "
                            "guide you to what is best."
                        ),
                        reply_markup=admin_request_markup(request_id),
                    )
                except Exception as e:
                    logging.warning("Could not send consideration nudge: " + str(e))

            supabase.table("requests").update({"consideration_nudge_sent": True}).eq("id", request_id).execute()

            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_USER_ID,
                text="🔔 2-day nudge sent for considering request " + str(request_id) + " (profile " + profile_id + ").",
            )

            logging.info(f"🔔 Sent 2-day consideration nudge for request {request_id} (profile {profile_id})")

    except Exception as e:
        logging.warning("ERROR in check_consideration: " + str(e))


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
    app.add_handler(CommandHandler("bump", bump_profile))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("unlock", unlock_user))
    app.add_handler(CommandHandler("set_wali", set_wali))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("withdraw", withdraw_command))
    app.add_handler(CommandHandler("my_request", my_request))
    app.add_handler(CommandHandler("available", available_command))
    app.add_handler(CommandHandler("add_affiliate", add_affiliate))
    app.add_handler(CommandHandler("affiliate_stats", affiliate_stats))
    app.add_handler(CommandHandler("convert", convert_referral))
    app.add_handler(CommandHandler("resend_requests", resend_requests))
    app.add_handler(CommandHandler("repost_all", repost_all))
    app.add_handler(CommandHandler("addphoto", addphoto_command))
    app.add_handler(CallbackQueryHandler(addphoto_button, pattern=r"^addphoto_start$"))
    app.add_handler(CallbackQueryHandler(addphoto_notnow, pattern=r"^addphoto_notnow$"))
    app.add_handler(CommandHandler("removephoto", removephoto_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("reset_user", reset_user_command))
    app.add_handler(CommandHandler("reset_all_requests", reset_all_requests_command))
    app.add_handler(CallbackQueryHandler(interest_clicked, pattern=r"^interest:"))
    app.add_handler(CallbackQueryHandler(interest_clicked, pattern=r"^interest_confirm:"))
    app.add_handler(CallbackQueryHandler(interest_cancel, pattern=r"^interest_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_decline_reason, pattern=r"^dr:"))
    app.add_handler(CallbackQueryHandler(available_menu, pattern=r"^avail_menu$"))
    app.add_handler(CallbackQueryHandler(available_callback, pattern=r"^avail_(yes|no):"))
    app.add_handler(CallbackQueryHandler(handle_decision, pattern=r"^(approve|approve_photo|decline|withdraw|pause|resume|consider|confirm_approve|confirm_approve_photo|approveback):"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text_reason))

    # Run pending-request reminder check every 30 minutes
    app.job_queue.run_repeating(check_pending_reminders, interval=1800, first=60)
    app.job_queue.run_repeating(check_consideration, interval=1800, first=90)

    print("✅ Mithaq bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
