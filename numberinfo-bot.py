# -*- coding: utf-8 -*-
"""
Telegram Bot (Termux‑ready) with:
- Lifetime Admins (∞ credits, never expire)
- Member management (add/remove, credits, expiry)
- Mobile/Email OSINT search for both admin & member
- Status display with proper formatting (admin sees all members)
- Robust error handling: API/server errors
- Auto‑retry on internet/network issues
- Compatible with python-telegram-bot v13.x

How to run (Termux example):
  pkg install python git -y
  pip install python-telegram-bot==13.15 requests pytz
  export BOT_TOKEN="<your_telegram_bot_token>"
  export API_TOKEN="<your_osint_api_token>"
  export ADMIN_IDS="123456789,987654321"  # comma-separated Telegram user IDs
  python bot.py

Note: Do NOT hardcode secrets in the file; use environment variables above.
"""

import os
import re
import json
import time
import math
import requests
from datetime import datetime, timedelta
import pytz

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram.error import NetworkError, TelegramError

import warnings
warnings.filterwarnings("ignore")

# ========= CONFIG =========
BOT_TOKEN     = os.getenv("BOT_TOKEN", "8082482810:AAGg0-3oDRDfc5e127iCAh8YVr8Byxhx1Qk")
API_TOKEN     = os.getenv("API_TOKEN", "")
ADMIN_IDS     = list({int(x) for x in os.getenv("ADMIN_IDS", "7917120388").split(",") if x.strip().isdigit()})
LANG          = os.getenv("LANG", "en")
LIMIT         = int(os.getenv("LIMIT", "10000"))
URL           = os.getenv("OSINT_URL", "https://leakosintapi.com/")
MEMBERS_FILE  = os.getenv("MEMBERS_FILE", "members.json")

HTTP_TIMEOUT = 15
HTTP_RETRIES = 3
HTTP_RETRY_SLEEP = 3
CREDIT_COST_PER_QUERY = 1
SEARCH_COOLDOWN_SECONDS = 3

MEMBERS = {}        # uid -> {"expiry": datetime, "credit": int/float('inf'), "name": str}
LAST_QUERY_AT = {}  # uid -> datetime (cooldown)

# ========= UTILITIES =========
def get_ist_time() -> str:
    tz = pytz.timezone("Asia/Kolkata")
    return datetime.now(tz).strftime("%d-%m-%Y %I:%M:%S %p")


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def _dt_to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_to_dt(s: str) -> datetime:
    # backward compatible parser
    try:
        return datetime.fromisoformat(s)
    except Exception:
        # fallback: treat as now if broken
        return datetime.now()


def load_members():
    """Load members.json into MEMBERS dict."""
    global MEMBERS
    if not os.path.exists(MEMBERS_FILE):
        MEMBERS = {}
        return
    try:
        with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        tmp = {}
        for uid_str, info in raw.items():
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            expiry = _iso_to_dt(info.get("expiry", datetime.now().isoformat()))
            credit_raw = info.get("credit", 0)
            if isinstance(credit_raw, str) and credit_raw == "inf":
                credit = float("inf")
            else:
                try:
                    credit = int(credit_raw)
                except Exception:
                    credit = 0
            name = info.get("name", "")
            tmp[uid] = {"expiry": expiry, "credit": credit, "name": name}
        MEMBERS = tmp
    except Exception:
        MEMBERS = {}


def save_members():
    data = {}
    for uid, info in MEMBERS.items():
        credit_val = info.get("credit", 0)
        credit_out = "inf" if (isinstance(credit_val, float) and math.isinf(credit_val)) else credit_val
        data[str(uid)] = {
            "expiry": _dt_to_iso(info.get("expiry", datetime.now())),
            "credit": credit_out,
            "name": info.get("name", "")
        }
    with open(MEMBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def ensure_lifetime_admins():
    changed = False
    for aid in ADMIN_IDS:
        current = MEMBERS.get(aid)
        if not current:
            MEMBERS[aid] = {"expiry": datetime.max, "credit": float("inf"), "name": MEMBERS.get(aid, {}).get("name", "")}
            changed = True
        else:
            upd = False
            if current.get("expiry") != datetime.max:
                current["expiry"] = datetime.max
                upd = True
            if not (isinstance(current.get("credit"), float) and math.isinf(current.get("credit", 0))):
                current["credit"] = float("inf")
                upd = True
            if upd:
                changed = True
    if changed:
        save_members()


def cleanup_expired_members():
    now = datetime.now()
    removed = False
    for uid in list(MEMBERS.keys()):
        if uid in ADMIN_IDS:
            continue
        member = MEMBERS[uid]
        credit = member.get("credit", 0)
        if member.get("expiry", now) <= now or (not math.isinf(credit) and credit <= 0):
            del MEMBERS[uid]
            removed = True
    if removed:
        save_members()


def clean_input(query: str):
    q = (query or "").strip()
    # email
    if re.match(r"^[\w\.-]+@[\w\.-]+\.[A-Za-z]{2,}$", q):
        return q
    # phone (normalize spaces, hyphens, commas, dots)
    q2 = re.sub(r"[ \-.,]", "", q)
    if q2.isdigit() and len(q2) == 10:
        return "+91" + q2
    if q2.startswith("+91") and len(q2) == 13 and q2[1:].isdigit():
        return q2
    return None


def post_with_retry(url: str, json_payload: dict):
    last_error = None
    for _ in range(HTTP_RETRIES):
        try:
            resp = requests.post(url, json=json_payload, timeout=HTTP_TIMEOUT)
            return resp
        except requests.exceptions.RequestException as e:
            last_error = e
            time.sleep(HTTP_RETRY_SLEEP)
    return None

# Pretty labels for known keys
KEY_EMOJI_MAP = {
    
    
    "FatherName": "👤 Full Name",
    "FullName": "🧑‍🤝‍🧑 Father/Spouse",
    "FirstName": "🧑 First Name",
    "NickName": "💟 Nickname",
    "Gender": "🚻 Gender",
    "Age": "🧮 Age",
    "Password": "✍️ Password",
    "Phone": "☎️ Mobile",
    "Phone2": "📱 Alt Mobile", "Phone3": "📱 Alt Mobile", "Phone4": "📱 Alt Mobile",
    "Phone5": "📱 Alt Mobile", "Phone6": "📱 Alt Mobile", "Phone7": "📱 Alt Mobile",
    "Phone8": "📱 Alt Mobile", "Phone9": "📱 Alt Mobile", "Phone10": "📱 Alt Mobile",
    "Mobile": "☎️ Mobile",
    "Mobile2": "📱 Alt Mobile", "Mobile3": "📱 Alt Mobile", "Mobile4": "📱 Alt Mobile",
    "Mobile5": "📱 Alt Mobile", "Mobile6": "📱 Alt Mobile", "Mobile7": "📱 Alt Mobile",
    "Mobile8": "📱 Alt Mobile", "Mobile9": "📱 Alt Mobile", "Mobile10": "📱 Alt Mobile",
    "Email": "📧 Email",
    "DocNumber": "🪪 Aadhar/PAN",
    "PassportNumber": "🪪 Aadhar No. ",
    "Address": "🔎 Address",
    "Address1": "🔎 Alt Address", "Address2": "🔎 Alt Address", "Address3": "🔎 Alt Address",
    "Address4": "🔎 Alt Address", "Address5": "🔎 Alt Address", "Address6": "🔎 Alt Address",
    "Address7": "🔎 Alt Address", "Address8": "🔎 Alt Address", "Address9": "🔎 Alt Address",
    "Address10": "🔎 Alt Address",
    "CompanyName": "🏢 Company",
    "City": "🏙️ City",
    "District": "🗺️ District",
    "IndianState": "🧭 State",
    "State": "🧭 State",
    "Country": "🌍 Country",
    "Region": "📍 Circle",
    "MobileOperator": "📡 Operator",
    "IP": "🌐 IP",
    "RegDate": "📅 Registered On",
    "Salt": "🧂 Salt",
    "TimeTaken": "⏱️ Time Taken",
    "Whatsapp": "💬 WhatsApp",
}


def _append_line(lines, label, val):
    if val is None:
        return
    s = str(val).strip()
    if not s or s.lower() == "null":
        return
    lines.append(f"{label}: {s}")


def format_entry(entry: dict, idx: int) -> str:
    lines = [f"— Result {idx} —"]
    # known keys first (in this order)
    for key, label in KEY_EMOJI_MAP.items():
        if key in entry:
            _append_line(lines, label, entry.get(key))
    # any extra keys
    for key in entry:
        if key not in KEY_EMOJI_MAP:
            _append_line(lines, key, entry.get(key))
    return "\n".join(lines)


def generate_report(query: str) -> str:
    payload = {"token": API_TOKEN, "request": query.strip(), "limit": LIMIT, "lang": LANG}
    try:
        resp = post_with_retry(URL, payload)
        if not resp:
            return "🚫 Server Problem, Please Contact Bot Owner"
        if resp.status_code != 200:
            return f"🚫 Server Error: HTTP {resp.status_code}"
        data = resp.json()
    except Exception:
        return "🚫 Server Problem, Please Contact Bot Owner"

    # Expected shape: {"List": {<db>: {"Data": [ {...}, ... ]}, ...}}
    if not data or "List" not in data or not data["List"]:
        return "🚫 No results found for this number or email."

    results = []
    count = 1
    try:
        for _db, block in data["List"].items():
            for entry in (block or {}).get("Data", []):
                results.append(format_entry(entry, count))
                count += 1
    except Exception:
        return "🚫 Response format changed. Please contact bot owner."

    return "\n\n".join(results) if results else "🚫 No results found for this number or email."


def member_keyboard():
    return ReplyKeyboardMarkup([["Mobile/Email", "Status"], ["Help"]], resize_keyboard=True, one_time_keyboard=False)


def admin_keyboard():
    return ReplyKeyboardMarkup([["Mobile/Email", "Status"], ["Add Member", "Remove Member", "Update API Token"], ["Help"]], resize_keyboard=True, one_time_keyboard=False)


def duration_keyboard():
    return ReplyKeyboardMarkup([["1 Day (10 Credit)", "3 Days (35 Credit)"], ["7 Days (90 Credit)"]], resize_keyboard=True, one_time_keyboard=True)


def safe_send(bot, chat_id, text, reply_markup=None):
    try:
        bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:
        time.sleep(2)
        try:
            bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception:
            pass

# ========= HANDLERS =========

def start(update: Update, context: CallbackContext):
    load_members()
    ensure_lifetime_admins()
    cleanup_expired_members()

    uid = update.effective_user.id
    name = (update.effective_user.first_name or "").strip()

    # store/refresh user name for status list
    info = MEMBERS.get(uid)
    if info:
        if name and info.get("name") != name:
            info["name"] = name
            save_members()
    else:
        # not a member; name will be saved later upon activation
        pass

    if is_admin(uid):
        update.message.reply_text("Admin Panel:", reply_markup=admin_keyboard())
    else:
        update.message.reply_text("Welcome! Use the buttons below:", reply_markup=member_keyboard())


def _admin_status_text() -> str:
    if not MEMBERS:
        return "No members yet."
    now = datetime.now()
    lines = ["👑 Admin Status — All Members"]
    for uid, info in sorted(MEMBERS.items(), key=lambda kv: kv[0]):
        credit = info.get("credit", 0)
        credit_text = "∞" if (isinstance(credit, float) and math.isinf(credit)) else str(credit)
        expiry = info.get("expiry", now)
        lifetime = expiry == datetime.max or (isinstance(credit, float) and math.isinf(credit))
        if lifetime:
            left_text = "∞"
            exp_text = "Lifetime"
        else:
            remaining = max(expiry - now, timedelta(seconds=0))
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            left_text = f"{hours}h {minutes}m"
            exp_text = expiry.strftime("%d-%m-%Y %I:%M:%S %p")
        role = "(Admin)" if uid in ADMIN_IDS else "(Member)"
        name = info.get("name", "")
        display = f"{uid} {role} {('- ' + name) if name else ''}".strip()
        lines.append(f"🟢 {display}\n💳 Credits: {credit_text} | ⏳ Left: {left_text}\n📆 Exp: {exp_text}")
    return "\n\n".join(lines)


def handle_message(update: Update, context: CallbackContext):
    load_members()  # ensure latest
    ensure_lifetime_admins()
    cleanup_expired_members()

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    first_name = update.effective_user.first_name or ""

    # persist name whenever we see the user
    if user_id in MEMBERS:
        if first_name and MEMBERS[user_id].get("name") != first_name:
            MEMBERS[user_id]["name"] = first_name
            save_members()

    # ===== ADMIN FLOW =====
    if is_admin(user_id):
        # Commands for admin
        if text == "Add Member":
            context.user_data['awaiting_member_id'] = True
            update.message.reply_text("Enter Member Chat ID:")
            return

        if context.user_data.get('awaiting_member_id'):
            try:
                mid = int(text)
                context.user_data['new_member_id'] = mid
                context.user_data.pop('awaiting_member_id', None)
                context.user_data['awaiting_duration'] = True
                update.message.reply_text("Select subscription duration:", reply_markup=duration_keyboard())
            except Exception:
                update.message.reply_text("Invalid ID. Try again.")
            return

        if context.user_data.get('awaiting_duration'):
            dur_map = {
                "1 Day (10 Credit)": (1, 10),
                "3 Days (35 Credit)": (3, 35),
                "7 Days (90 Credit)": (7, 90),
            }
            chosen = dur_map.get(text)
            if chosen and 'new_member_id' in context.user_data:
                days, credits = chosen
                mid = context.user_data['new_member_id']
                name = MEMBERS.get(mid, {}).get("name", "")
                MEMBERS[mid] = {"expiry": datetime.now() + timedelta(days=days), "credit": credits, "name": name}
                save_members()
                update.message.reply_text(f"✅ Member {mid} activated for {days} day(s), credits: {credits}.", reply_markup=admin_keyboard())
                context.user_data.pop('awaiting_duration', None)
                context.user_data.pop('new_member_id', None)
            else:
                update.message.reply_text("Invalid selection. Try again.")
            return

        if text == "Remove Member":
            context.user_data['awaiting_remove_id'] = True
            update.message.reply_text("Enter Member Chat ID to remove:")
            return

        if context.user_data.get('awaiting_remove_id'):
            try:
                mid = int(text)
                if mid in MEMBERS:
                    del MEMBERS[mid]
                    save_members()
                    update.message.reply_text(f"✅ Member {mid} removed.", reply_markup=admin_keyboard())
                else:
                    update.message.reply_text("Member not found.", reply_markup=admin_keyboard())
                context.user_data.pop('awaiting_remove_id', None)
            except Exception:
                update.message.reply_text("Invalid ID.", reply_markup=admin_keyboard())
            return

        if text == "Update API Token":
            context.user_data['awaiting_api_token'] = True
            update.message.reply_text("Enter new API Token:")
            return

        if context.user_data.get('awaiting_api_token'):
            new_token = text.strip()
            if new_token:
                global API_TOKEN
                API_TOKEN = new_token
                context.user_data.pop('awaiting_api_token', None)
                update.message.reply_text("✅ API Token updated!", reply_markup=admin_keyboard())
            else:
                update.message.reply_text("Invalid token.", reply_markup=admin_keyboard())
            return

        if text == "Status":
            safe_send(context.bot, update.effective_chat.id, _admin_status_text(), reply_markup=admin_keyboard())
            return

        if text == "Help":
            update.message.reply_text(
                "Admin Guide:\n• Add Member → Activate with days & credits\n• Remove Member → Remove by chat ID\n• Update API Token → Set new OSINT token\n• Status → List all members (credits/expiry)\n• Mobile/Email → Run search\n• Help → This guide",
                reply_markup=admin_keyboard(),
            )
            return

        if text == "Mobile/Email":
            context.user_data['awaiting_query'] = True
            update.message.reply_text("Enter Mobile Number or Email:")
            return

        if context.user_data.get('awaiting_query'):
            last = LAST_QUERY_AT.get(user_id)
            if last and (datetime.now() - last).total_seconds() < SEARCH_COOLDOWN_SECONDS:
                update.message.reply_text("⏳ Wait before next query.", reply_markup=admin_keyboard())
                return
            query = clean_input(text)
            if not query:
                update.message.reply_text("❌ Invalid input.", reply_markup=admin_keyboard())
                return
            result = generate_report(query)
            safe_send(context.bot, update.effective_chat.id, result, reply_markup=admin_keyboard())
            LAST_QUERY_AT[user_id] = datetime.now()
            context.user_data.pop('awaiting_query', None)
            return

        update.message.reply_text("Choose option:", reply_markup=admin_keyboard())
        return

    # ===== MEMBER FLOW =====
    if user_id not in MEMBERS:
        update.message.reply_text("❌ Not an active member.", reply_markup=member_keyboard())
        return

    now = datetime.now()
    info = MEMBERS[user_id]
    credit = info.get("credit", 0)
    if isinstance(credit, str) and credit == "inf":
        credit = float("inf")
    expired = info.get("expiry", now) <= now or (not math.isinf(credit) and credit <= 0)
    remaining = max(info.get("expiry", now) - now, timedelta(seconds=0))
    hours, remainder = divmod(int(remaining.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)

    if text == "Status":
        exp_text = "Lifetime" if math.isinf(credit) or info.get("expiry") == datetime.max else info.get("expiry").strftime("%d-%m-%Y %I:%M:%S %p")
        credit_text = "∞" if math.isinf(credit) else str(credit)
        safe_send(
            context.bot,
            update.effective_chat.id,
            f"🟢 Active: Left: {('∞' if math.isinf(credit) or info.get('expiry') == datetime.max else f'{hours}h {minutes}m')}\n🆔 {user_id} ({first_name})\n💳 Credits: {credit_text}\n📆 Exp: {exp_text}",
            reply_markup=member_keyboard(),
        )
        return

    if text == "Help":
        update.message.reply_text(
            "Member Guide:\n• Mobile/Email → Run OSINT search\n• Status → See credits & expiry\n• Help → This guide",
            reply_markup=member_keyboard(),
        )
        return

    if text == "Mobile/Email":
        if expired:
            update.message.reply_text("❌ Membership expired. Contact admin.", reply_markup=member_keyboard())
            return
        context.user_data['awaiting_query'] = True
        update.message.reply_text("Enter Mobile Number or Email:")
        return

    if context.user_data.get('awaiting_query'):
        if expired:
            update.message.reply_text("❌ Membership expired. Contact admin.", reply_markup=member_keyboard())
            context.user_data.pop('awaiting_query', None)
            return
        last = LAST_QUERY_AT.get(user_id)
        if last and (datetime.now() - last).total_seconds() < SEARCH_COOLDOWN_SECONDS:
            update.message.reply_text("⏳ Wait before next query.", reply_markup=member_keyboard())
            return
        query = clean_input(text)
        if not query:
            update.message.reply_text("❌ Invalid input.", reply_markup=member_keyboard())
            return
        result = generate_report(query)
        # deduct credits only if member, results appear meaningful
        if not is_admin(user_id):
            if result and not result.startswith("🚫") and not math.isinf(credit):
                MEMBERS[user_id]["credit"] = max(int(credit) - CREDIT_COST_PER_QUERY, 0)
                save_members()
        safe_send(context.bot, update.effective_chat.id, result, reply_markup=member_keyboard())
        LAST_QUERY_AT[user_id] = datetime.now()
        context.user_data.pop('awaiting_query', None)
        return

    update.message.reply_text("Choose option:", reply_markup=member_keyboard())


# ========= MAIN =========

def main():
    if not BOT_TOKEN:
        print("[ERROR] BOT_TOKEN is not set. Export BOT_TOKEN and restart.")
        return
    if not API_TOKEN:
        print("[WARN] API_TOKEN is not set. You can still start, but queries will fail until you set it.")

    load_members()
    ensure_lifetime_admins()
    cleanup_expired_members()

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Commands
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", lambda u, c: handle_message(u, c)))  # map /help to same flow

    # All text messages
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    print("Bot running… Press Ctrl+C to stop.")
    try:
        updater.start_polling()
        updater.idle()
    except (NetworkError, TelegramError) as e:
        print(f"[ERROR] Telegram error: {e}")


if __name__ == "__main__":
    main()
