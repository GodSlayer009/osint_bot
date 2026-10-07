from __future__ import annotations

import asyncio
import copy
import html
import io
import json
import logging
import os
import re
import threading
from datetime import datetime, time, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram import StopPropagation
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import (
    ChatJoinRequest,
    Message,
)

from tracker import OsintBot
from flask import Flask

# Keep literal placeholders such as ${NUMBER} in endpoint templates. Without
# this, python-dotenv expands them while reading .env and sends an empty value.
load_dotenv(interpolate=False)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


web_app = Flask(__name__)

@web_app.get("/")
def home():
    return "Telegram bot is running"


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing {name}. Add it to your .env file.")
    return value


osint_bot = OsintBot(
    mongo_uri=required("MONGO_URI"),
    database_name=os.getenv("MONGO_DATABASE", "osint_bot"),
)

def chat_id_from_env(name: str) -> int | str:
    value = required(name)
    return int(value) if value.lstrip("-").isdigit() else value


REQUIRED_CHANNELS = (
    (chat_id_from_env("CHANNEL_1_ID"), required("CHANNEL_1_URL"), "Channel 1"),
    (chat_id_from_env("CHANNEL_2_ID"), required("CHANNEL_2_URL"), "Channel 2"),
    (chat_id_from_env("CHANNEL_3_ID"), required("CHANNEL_3_URL"), "Channel 3"),
)
PENDING_JOIN_REQUESTS: dict[int | str, set[int]] = {}


def admin_user_ids() -> frozenset[int]:
    """Read one or more comma-separated Telegram administrator IDs."""
    raw_ids = os.getenv("ADMIN_USER_ID", "")
    ids: set[int] = set()
    for raw_id in raw_ids.split(","):
        raw_id = raw_id.strip()
        if not raw_id or raw_id == "0":
            continue
        if raw_id.isdigit():
            ids.add(int(raw_id))
        else:
            LOGGER.warning("Ignoring invalid ADMIN_USER_ID value")
    return frozenset(ids)


ADMIN_USER_IDS = admin_user_ids()
BOT_USERNAME = os.getenv("BOT_USERNAME", "OsintYutabot").lstrip("@")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_USER_IDS)


async def admin_target(identifier: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(osint_bot.find_user, identifier)


app = Client(
    "osint_bot",
    api_id=int(required("API_ID")),
    api_hash=required("API_HASH"),
    bot_token=required("BOT_TOKEN"),
)


@app.on_message(filters.private, group=-1)
async def block_guard(_: Client, message: Message) -> None:
    if message.from_user and await asyncio.to_thread(osint_bot.is_blocked, message.from_user.id):
        await message.reply_text("<b>You are blocked.</b> Contact @Ezzyseller to be unblocked.")
        raise StopPropagation


ADMIN_HELP = (
    "<b>Admin commands</b>\n\n"
    "/info &lt;username or user_id&gt; — <i>show user account information</i>\n"
    "/stats — <i>bot statistics</i>\n"
    "/resetcredit — <i>set all users below 2 credits to 2</i>\n"
    "/broadcast &lt;message&gt; — <i>send text or a replied message</i>\n"
    "/protect &lt;phone_number&gt; — <i>prevent a number lookup</i>\n"
    "\n"
    "<b>Premium Management:</b>\n\n"
    "/premium &lt;username or user_id&gt; &lt;dd-mm-yyyy&gt; — <i>grant premium</i>\n"
    "/rmpremium &lt;username or user_id&gt; — <i>remove premium</i>\n"
    "/premiumusers — <i>list active premium users</i>\n"
    "\n"
    "<b>Credit Management:</b>\n"
    "/setcredit &lt;username or user_id&gt; &lt;amount&gt; — <i>set credits</i>\n"
    "/checkcredit &lt;username or user_id&gt; — <i>check credits</i>\n"
    "\n"
    "<b>Block Management:</b>\n"
    "/block &lt;username or user_id&gt; — <i>block a user</i>\n"
    "/unblock &lt;username or user_id&gt; — <i>unblock a user</i>\n"
    "/blockedusers — <i>list blocked users</i>"
)


@app.on_message(filters.command("admin") & filters.private)
async def admin_help_command(_: Client, message: Message) -> None:
    if not is_admin(message):
        await message.reply_text("You are not authorized to use this command.")
        return
    await message.reply_text(ADMIN_HELP)


@app.on_message(filters.command("stats") & filters.private)
async def stats_command(_: Client, message: Message) -> None:
    if not is_admin(message):
        await message.reply_text("You are not authorized to use this command.")
        return
    stats = await asyncio.to_thread(osint_bot.statistics)
    await message.reply_text(
        "<b>Bot statistics</b>\n"
        f"Users: {stats['users']}\nActive premium: {stats['premium']}\n"
        f"Blocked users: {stats['blocked']}\nTotal credits: {stats['credits']}"
    )


@app.on_message(filters.command("resetcredit") & filters.private)
async def reset_credit_command(_: Client, message: Message) -> None:
    if not is_admin(message):
        await message.reply_text("You are not authorized to use this command.")
        return
    count = await asyncio.to_thread(osint_bot.set_credits_minimum, 2)
    await message.reply_text(f"Set credits to at least 2 for {count} user(s).")


@app.on_message(filters.command(["info", "checkcredit", "setcredit", "rmpremium", "block", "unblock"]) & filters.private)
async def account_admin_commands(_: Client, message: Message) -> None:
    if not is_admin(message):
        await message.reply_text("You are not authorized to use this command.")
        return
    command = (message.command or [""])[0].split("@", 1)[0].lower()
    parts = message.text.split(maxsplit=2) if message.text else []
    if len(parts) < 2 or (command == "setcredit" and len(parts) != 3):
        usage = {
            "info": "/info <username|user_id>", "checkcredit": "/checkcredit <username|user_id>",
            "setcredit": "/setcredit <username|user_id> <amount>", "rmpremium": "/rmpremium <username|user_id>",
            "block": "/block <username|user_id>", "unblock": "/unblock <username|user_id>",
        }[command]
        await message.reply_text(f"Usage: {usage}")
        return
    user = await admin_target(parts[1])
    if not user:
        await message.reply_text("User not found. They must have started the bot first.")
        return
    user_id = int(user["telegram_id"])
    if command == "info":
        expiry = user.get("premium_expires_at")
        expiry_text = expiry.strftime("%d-%m-%Y") if isinstance(expiry, datetime) else "inactive"
        await message.reply_text(
            f"<b>User account</b>\nID: <code>{user_id}</code>\n"
            f"Username: @{html.escape(user.get('username') or 'none')}\n"
            f"Name: {html.escape(' '.join(filter(None, [user.get('first_name'), user.get('last_name')])) or 'unknown')}\n"
            f"Credits: {int(user.get('credits', 0))}\n"
            f"Premium expiry: {expiry_text}\n"
            f"Blocked: {'yes' if user.get('blocked') else 'no'}"
        )
    elif command == "checkcredit":
        await message.reply_text(f"User <code>{user_id}</code> has {int(user.get('credits', 0))} credit(s).")
    elif command == "setcredit":
        try:
            amount = int(parts[2])
            if amount < 0: raise ValueError
        except ValueError:
            await message.reply_text("Credit amount must be a non-negative whole number.")
            return
        await asyncio.to_thread(osint_bot.set_credits, user_id, amount)
        await message.reply_text(f"Set credits for <code>{user_id}</code> to {amount}.")
    elif command == "rmpremium":
        await asyncio.to_thread(osint_bot.remove_premium, user_id)
        await message.reply_text(f"Premium removed for <code>{user_id}</code>.")
    else:
        await asyncio.to_thread(osint_bot.block_user if command == "block" else osint_bot.unblock_user, user_id)
        await message.reply_text(f"User <code>{user_id}</code> {'blocked' if command == 'block' else 'unblocked' }.")


@app.on_message(filters.command(["premiumusers", "blockedusers"]) & filters.private)
async def list_users_admin_command(_: Client, message: Message) -> None:
    if not is_admin(message):
        await message.reply_text("You are not authorized to use this command.")
        return
    command = (message.command or [""])[0].split("@", 1)[0].lower()
    users = await asyncio.to_thread(osint_bot.premium_users if command == "premiumusers" else osint_bot.blocked_users)
    if not users:
        await message.reply_text("No matching users.")
        return
    rows = []
    for user in users:
        name = f"@{user['username']}" if user.get("username") else html.escape(user.get("first_name") or "")
        extra = f" — until {user['premium_expires_at']:%d-%m-%Y}" if command == "premiumusers" else ""
        rows.append(f"<code>{user['telegram_id']}</code> {name}{extra}")
    await message.reply_text(f"<b>{'Premium users' if command == 'premiumusers' else 'Blocked users'} ({len(rows)})</b>\n" + "\n".join(rows))

WELCOME_TEXT = (
    "<b>🕵️‍♂️ Yuta OSINT Bot</b>\n\n"
    "I can help you find information using OSINT commands.\n\n"
    "<b>💳 Credit:</b> {credits}{premium_expiry}\n\n"
    "Send a 10-digit mobile number to look up number information.\n\n"
    "── ── ── ── ── ── ── ── ── ── ── ── ── ── ──\n\n"
    "<b>⚠️ Important:</b>\n"
    "• This bot is for educational purposes only\n"
    "• Do not use for illegal activities\n"
    "• Users are responsible for their actions\n"
    "• Unauthorized use is strictly prohibited\n"
    "• Lookup messages will be auto-deleted after 300 seconds"
)

PREMIUM_REQUIRED_TEXT = (
    "<b>🔒 Premium Required</b>\n\n"
    "<i>This lookup is available to premium users only.</i>\n\n"
    "<b>Please contact the admin to buy Premium for your account.</b>\n\n"
    "<b>Admin Contact:</b> @Ezzyseller "
)
PREMIUM_REQUIRED_ALERT = (
    "Premium required. Contact @Ezzyseller to buy Premium."
)

JOIN_TEXT = (
    "<b>OSINT BOT</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>Must Join all channels then You can use this bot</b>"
)


async def missing_required_channels(user_id: int) -> tuple[tuple[int | str, str, str], ...]:
    """Return channels where the user is neither joined nor awaiting approval."""
    inactive_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
    missing: list[tuple[int | str, str, str]] = []
    for channel in REQUIRED_CHANNELS:
        chat_id, _, _ = channel
        try:
            member = await app.get_chat_member(chat_id, user_id)
            if member.status not in inactive_statuses:
                PENDING_JOIN_REQUESTS.get(chat_id, set()).discard(user_id)
                await asyncio.to_thread(osint_bot.clear_pending_join_request, chat_id, user_id)
            else:
                if not await has_pending_join_request(chat_id, user_id):
                    missing.append(channel)
        except RPCError:
            # A non-member raises an RPC error, so check the pending-request list too.
            if not await has_pending_join_request(chat_id, user_id):
                missing.append(channel)
    return tuple(missing)


async def has_pending_join_request(chat_id: int | str, user_id: int) -> bool:
    """Check a pending request received by this bot, including before a restart."""
    return (
        user_id in PENDING_JOIN_REQUESTS.get(chat_id, set())
        or await asyncio.to_thread(osint_bot.has_pending_join_request, chat_id, user_id)
    )


@app.on_chat_join_request()
async def process_join_request(_: Client, request: ChatJoinRequest) -> None:
    """Record pending access only for invite links that require approval."""
    invite_link = request.invite_link
    if not invite_link or not invite_link.creates_join_request:
        return

    for chat_id, _, _ in REQUIRED_CHANNELS:
        if request.chat.id == chat_id:
            PENDING_JOIN_REQUESTS.setdefault(chat_id, set()).add(request.from_user.id)
            await asyncio.to_thread(osint_bot.record_pending_join_request, chat_id, request.from_user.id)
            LOGGER.info("Granted bot access for pending request from user %s in chat %s", request.from_user.id, chat_id)
            return


async def welcome_text(user_id: int) -> str:
    """Build the welcome message, including premium expiry when applicable."""
    credits = await asyncio.to_thread(
        osint_bot.get_credits, user_id
    )
    is_premium = await asyncio.to_thread(
        osint_bot.is_premium, user_id
    )
    premium_expiry = ""
    if is_premium:
        expiry = await asyncio.to_thread(osint_bot.premium_expiry, user_id)
        if expiry:
            premium_expiry = f"\n<b>📅 Premium expires:</b> {expiry:%d-%m-%Y}"
    return WELCOME_TEXT.format(
        credits="Unlimited" if is_premium else credits,
        premium_expiry=premium_expiry,
    )


async def send_start_message(message: Message, user_id: int | None = None) -> None:
    if user_id is None and not message.from_user:
        return
    target_user_id = user_id if user_id is not None else message.from_user.id
    await message.reply_text(
        await welcome_text(target_user_id),
    )


async def referral_view_text(user_id: int) -> str:
    """Build the referral screen shown from the inline menu."""
    referrals, earned_credits = await asyncio.to_thread(osint_bot.referral_stats, user_id)
    progress = referrals % 2
    percent = progress * 50
    progress_bar = "■" * (progress * 5) + "▱" * (10 - progress * 5)
    remaining = 2 - progress
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    return (
        "<b>🤝 REFER &amp; EARN</b>\n\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{referral_link}</code>\n\n"
        "📊 <b>Your Stats:</b>\n"
        f"• Total Successful Referrals: <b>{referrals}</b>\n"
        f"• Credits Earned from Referrals: <b>{earned_credits}</b>\n\n"
        "🎯 <b>Progress to Next Credit:</b>\n"
        f"{progress_bar} {percent}%\n"
        f"┗ ➤ {remaining} more referral(s) needed\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How it works:</b>\n"
        "• Share your referral link with friends.\n"
        "• When they start the bot using your link and join all required channels, they become your referral.\n"
        "• For every 2 successful referrals, you earn <b>1 FREE Credit</b>!\n"
        "• Referrals are counted only after channel membership is verified.\n\n"
        "🚀 <b>Start sharing now and earn free credits!</b>"
    )


async def remove_loading_message(message: Message) -> None:
    """Delete a temporary loading reply without hiding the final result."""
    try:
        await message.delete()
    except RPCError:
        LOGGER.debug("Could not delete loading message %s", message.id)


async def notify_referrer(referrer_id: int | None, referred_user: Any) -> None:
    """Tell the referrer when their referral completes channel verification."""
    if referrer_id is None:
        return
    name = html.escape(referred_user.first_name or referred_user.username or "A user")
    try:
        await app.send_message(
            referrer_id,
            f"{name} successfully joined the bot with your referral link.",
        )
    except RPCError:
        LOGGER.warning("Could not notify referrer %s", referrer_id)


SENSITIVE_KEYS = {"aadhar", "aadhaar", "aadharno", "aadhaarno", "uid", "uidai"}
REMOVED_KEYS = {"owner", "metadata"}


def sanitize(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(item): sanitize(child, str(item))
            for item, child in value.items()
            if re.sub(r"[^a-z0-9]", "", str(item).lower()) not in REMOVED_KEYS
        }

    if isinstance(value, list):
        return [sanitize(item, key) for item in value]

    return value


def format_result(data: Any) -> str:
    safe_data = sanitize(data)
    return json.dumps(safe_data, indent=2, ensure_ascii=True, default=str)


def result_file(data: Any, filename: str) -> io.BytesIO:
    document = io.BytesIO(format_result(data).encode("utf-8"))
    document.name = filename
    return document


def json_message(data: Any) -> str:
    return "```json\n" + format_result(data) + "\n```"


RESULT_ALIASES = {
    "name": {"name", "fullname", "personname"},
    "father": {"father", "fathername", "fname", "fathersname", "father_name"},
    "address": {"address", "fulladdress", "completeaddress"},
    "circle": {"circle", "telecomcircle", "operatorcircle"},
    "email": {"email", "emailaddress", "mail"},
    "aadhar": {"aadhar", "aadhaar", "aadharno", "aadhaarno", "uid", "uidai"},
    "alternate": {"alt", "alternate", "alternatenumber", "alternatenumbers"},
    "number": {
        "num", "number", "mobile", "mobileno", "mobilenumber", "phone", "phoneno", "phonenumber"
    },
}


def normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def scalar_value(value: Any) -> str | None:
    if isinstance(value, (str, int, float)) and str(value).strip():
        return str(value).strip()
    return None


def field_value(record: Any, field: str) -> str | None:
    aliases = {normalized_key(alias) for alias in RESULT_ALIASES[field]}
    if isinstance(record, dict):
        for key, value in record.items():
            if normalized_key(key) in aliases:
                found = scalar_value(value)
                if found:
                    return found
        for value in record.values():
            found = field_value(value, field)
            if found:
                return found
    elif isinstance(record, list):
        for value in record:
            found = field_value(value, field)
            if found:
                return found
    return None


def looks_like_result(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {normalized_key(key) for key in value}
    result_keys = {
        normalized_key(alias)
        for aliases in RESULT_ALIASES.values()
        for alias in aliases
    }
    return bool(keys & result_keys)


def result_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if looks_like_result(value):
            records.append(value)
        else:
            for child in value.values():
                records.extend(result_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(result_records(child))
    return records


def format_lookup_record(record: dict[str, Any], query: str, command: str) -> str:
    number = field_value(record, "number") or (query if command == "num" else "Not found")

    def text(field: str, fallback: str = "Not found") -> str:
        return html.escape(field_value(record, field) or fallback)

    lines = [
        f"👤 Name: {text('name')}",
        f"👨‍👦 Father: {text('father')}",
        f"📍 Address: {text('address')}",
        f"📡 Circle: {text('circle')}",
    ]
    email = field_value(record, "email")
    aadhar = field_value(record, "aadhar")
    alternate = field_value(record, "alternate")
    if email:
        lines.append(f"📧 Email: {html.escape(email)}")
    if aadhar:
        lines.append(f"🆔 Aadhar: {html.escape(aadhar)}")
    if alternate:
        lines.append(f"📞 Alternate: {html.escape(alternate)}")
    lines.append(f"📱 Number: {html.escape(number)}")
    return "\n".join(lines)


def lookup_message_chunks(query: str, data: Any, command: str) -> list[str]:
    records = result_records(data)
    if not records and isinstance(data, dict):
        records = [data]
    title = "📱 NUMBER SEARCH" if command == "num" else "🆔 AADHAR SEARCH"
    chunks: list[str] = []
    current: list[str] = []
    for index, record in enumerate(records, start=1):
        block = f"📌 Record #{index}\n{format_lookup_record(record, query, command)}"
        if current and (len(current) >= 5 or len("\n───────────────────────────────────\n".join(current + [block])) + len(title) + 20 > 4096):
            chunks.append(f"🔍 {title}\n═══════════════════════════════════\n\n" + "\n───────────────────────────────────\n".join(current))
            current = []
        current.append(block)
    if current:
        chunks.append(f"🔍 {title}\n═══════════════════════════════════\n\n" + "\n───────────────────────────────────\n".join(current))
    return chunks


def normalise_phone_number(value: str) -> str | None:
    """Return the 10-digit Indian mobile number expected by the configured API."""
    digits = re.sub(r"[\s-]", "", value)
    if digits.startswith("+"):
        digits = digits[1:]
    if not digits.isdigit():
        return None
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if len(digits) == 10 else None


def text_result_chunks(data: Any, title: str) -> list[str]:
    """Render a nested API response as readable Telegram text rather than JSON."""
    lines: list[str] = []
    hidden_metadata = {
        "success",
        "credit",
        "usedtoday",
        "dailylimit",
        "validdays",
        "expireson",
        "query",
    }

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if normalized_key(key) in hidden_metadata:
                    continue
                walk(child, f"{path} {key}".strip())
        elif isinstance(value, list):
            for index, child in enumerate(value, start=1):
                walk(child, f"{path} {index}".strip())
        elif value is not None:
            # Many APIs wrap their fields in a `result` object. It is only a
            # transport wrapper, so do not show that word in user-facing labels.
            label_path = re.sub(r"(?<![A-Za-z0-9])result(?![A-Za-z0-9])", "", path, flags=re.IGNORECASE)
            label = re.sub(r"\s+", " ", label_path.replace("_", " ")).strip().title() or "Value"
            lines.append(f"<b>{html.escape(label)}:</b> {html.escape(str(value))}")

    walk(sanitize(data))
    if not lines:
        return []

    header = f"<b>{html.escape(title)}</b>\n━━━━━━━━━━━━━━\n"
    chunks: list[str] = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 4096:
            chunks.append(current.rstrip())
            current = header
        current += line + "\n"
    if current != header:
        chunks.append(current.rstrip())
    return chunks


def api_error_message(data: Any) -> str | None:
    """Return a safe, user-facing message from a failed JSON API response."""
    if not isinstance(data, dict) or data.get("status") is not False:
        return None
    message = data.get("message")
    return message if isinstance(message, str) and message else "No data found."


def combine_lookup_results(number_data: Any, aadhar_data: Any) -> Any:
    if aadhar_data:
        return {"number_info": number_data, "aadhaar_info": aadhar_data}
    return number_data


def is_no_result_payload(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    status = data.get("status")
    message = data.get("message")
    if status is False and isinstance(message, str):
        normalized = re.sub(r"[^a-z0-9]", "", message.lower())
        if "nonumberdatafound" in normalized or "numberdatafound" in normalized and "no" in normalized:
            return True
    # Some number APIs return a successful top-level response while putting the
    # actual no-result state in an item under ``result``.
    result = data.get("result")
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            item_status = item.get("status")
            if isinstance(item_status, str):
                normalized = re.sub(r"[^a-z0-9]", "", item_status.lower())
                if normalized == "nodatafound":
                    return True
    return False


def build_api_url(template: str, placeholder: str, value: str, parameter: str) -> str:
    encoded_value = quote(value, safe="")
    expanded = template.replace("${" + placeholder + "}", encoded_value)
    if "${" + placeholder + "}" in expanded:
        raise ValueError(f"Unresolved API placeholder: {placeholder}")

    parts = urlsplit(expanded)
    if parameter not in parts.query:
        separator = "&" if parts.query else ""
        expanded = urlunsplit(parts._replace(query=parts.query + separator + f"{parameter}={encoded_value}"))
    return expanded


def start_referrer_id(message: Message) -> int | None:
    """Extract a numeric referrer ID from a /start deep-link command."""
    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) != 2:
        return None
    value = parts[1].strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


async def lookup_number(message: Message, number: str) -> None:
    if not message.from_user:
        return

    if await asyncio.to_thread(osint_bot.is_protected_number, number):
        await message.reply_text("This number was protected. Try another number.")
        return

    missing_channels = await missing_required_channels(message.from_user.id)

    if missing_channels:
        links = "\n".join(url for _, url, _ in missing_channels)
        await message.reply_text(JOIN_TEXT + "\n\n" + links + "\nJoin the channels, then send /start again.")
        return

    await asyncio.to_thread(osint_bot.register_user, message.from_user)

    api_template = os.getenv("NUM_TO_INFO", "").strip()
    if not api_template:
        await message.reply_text("The number lookup API is not configured.")
        return

    has_credit = await asyncio.to_thread(osint_bot.consume_credit, message.from_user.id)
    if not has_credit:
        await message.reply_text("<b><i>Contact the admin to buy credits and premium to your account before using this command.\n\n Admin Contact: @Ezzyseller</b></i>")
        return

    loading_message = await message.reply_text("Fetching the number information…")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            url = build_api_url(api_template, "NUMBER", number, "number")
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            error_message = api_error_message(data)
            if error_message:
                await remove_loading_message(loading_message)
                await message.reply_text(error_message)
                return
    except httpx.HTTPStatusError as error:
        LOGGER.warning("Number API returned HTTP %s", error.response.status_code)
        await remove_loading_message(loading_message)
        await message.reply_text("The lookup service rejected the request. Please try again later.")
        return
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOGGER.warning("Lookup failed: %s", type(error).__name__)
        await remove_loading_message(loading_message)
        await message.reply_text("Lookup failed. Please try again later.")
        return

    if is_no_result_payload(data):
        await remove_loading_message(loading_message)
        await message.reply_text("❌ No data found")
        return
    chunks = lookup_message_chunks(number, data, "num")
    if not chunks:
        await remove_loading_message(loading_message)
        await message.reply_text("❌ No data found")
        return

    await remove_loading_message(loading_message)
    for chunk in chunks:
        await message.reply_text(chunk)


@app.on_message(filters.command("refer") & filters.private)
async def refer_command(_: Client, message: Message) -> None:
    """Show a user's referral link and completed-referral progress."""
    if not message.from_user:
        return
    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        links = "\n".join(url for _, url, _ in missing_channels)
        await message.reply_text(JOIN_TEXT + "\n\n" + links + "\nJoin the channels, then send /start again.")
        return

    await asyncio.to_thread(osint_bot.register_user, message.from_user)
    referrals, earned_credits = await asyncio.to_thread(
        osint_bot.referral_stats, message.from_user.id
    )
    credits = await asyncio.to_thread(osint_bot.get_credits, message.from_user.id)
    is_premium = await asyncio.to_thread(osint_bot.is_premium, message.from_user.id)
    progress = referrals % 2
    percent = progress * 50
    progress_bar = "▰" * (progress * 5) + "▱" * (10 - progress * 5)
    remaining = 2 - progress
    referral_link = f"https://t.me/{BOT_USERNAME}?start={message.from_user.id}"
    await message.reply_text(
        "<b>🤝 REFER &amp; EARN</b>\n\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{referral_link}</code>\n\n"
        "📊 <b>Your Stats:</b>\n"
        f"• Total Successful Referrals: <b>{referrals}</b>\n"
        f"• Credits Earned from Referrals: <b>{earned_credits}</b>\n\n"
        "🎯 <b>Progress to Next Credit:</b>\n"
        f"{progress_bar} {percent}%\n"
        f"┗ ➤ {remaining} more referral(s) needed\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How it works:</b>\n"
        "• Share your referral link with friends.\n"
        "• When they start the bot using your link and join all required channels, they become your referral.\n"
        "• For every 2 successful referrals, you earn <b>1 FREE Credit</b>!\n"
        "• Referrals are counted only after channel membership is verified.\n\n"
        "🚀 <b>Start sharing now and earn free credits!</b>"
        ,
    )


@app.on_message(filters.command("protect") & filters.private)
async def protect_number_command(_: Client, message: Message) -> None:
    """Allow an administrator to prevent a number from being looked up."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    parts = message.text.split(maxsplit=1) if message.text else []
    number = normalise_phone_number(parts[1]) if len(parts) == 2 else None
    if not number:
        await message.reply_text("Usage: /protect &lt;10-digit mobile number&gt;")
        return

    added = await asyncio.to_thread(osint_bot.protect_number, number, message.from_user.id)
    if added:
        await message.reply_text(f"Number <code>{number}</code> is now protected.")
    else:
        await message.reply_text(f"Number <code>{number}</code> is already protected.")


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_command(_: Client, message: Message) -> None:
    """Send text or forward a replied message to every registered bot user."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    reply = message.reply_to_message
    text = ""
    entities = None
    if not reply:
        command_match = re.match(r"^/broadcast(?:@\w+)?\s+", message.text or "")
        if not command_match:
            await message.reply_text(
                "Usage: /broadcast &lt;message&gt;\n\n"
                "To preserve any message type and formatting exactly, reply to it with /broadcast."
            )
            return
        body_offset = command_match.end()
        text = (message.text or "")[body_offset:]
        if not text:
            await message.reply_text("Please provide a message to broadcast.")
            return

        entities = []
        for entity in message.entities or []:
            entity_end = entity.offset + entity.length
            if entity.offset >= body_offset:
                adjusted = copy.copy(entity)
                adjusted.offset -= body_offset
                entities.append(adjusted)
            elif entity_end > body_offset:
                # A format spanning the command boundary cannot be sent safely.
                LOGGER.warning("Ignoring broadcast entity spanning the command prefix")

    user_ids = await asyncio.to_thread(osint_bot.user_ids)
    sent = 0
    failed = 0
    await message.reply_text(f"Broadcast started for {len(user_ids)} user(s).")

    for user_id in user_ids:
        try:
            if reply:
                await app.forward_messages(user_id, reply.chat.id, reply.id)
            else:
                await app.send_message(user_id, text, entities=entities)
            sent += 1
        except FloodWait as error:
            await asyncio.sleep(error.value)
            try:
                if reply:
                    await app.forward_messages(user_id, reply.chat.id, reply.id)
                else:
                    await app.send_message(user_id, text, entities=entities)
                sent += 1
            except RPCError:
                failed += 1
        except RPCError:
            failed += 1

    await message.reply_text(f"Broadcast complete. Sent: {sent}; failed: {failed}.")


@app.on_message(filters.command("premium") & filters.private)
async def premium_command(_: Client, message: Message) -> None:
    """Grant a user unlimited access through the supplied expiry date."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    parts = message.text.split(maxsplit=2) if message.text else []
    if len(parts) != 3:
        await message.reply_text("Usage: /premium &lt;username|user_id&gt; &lt;dd-mm-yyyy&gt;")
        return

    try:
        expiry_date = datetime.strptime(parts[2], "%d-%m-%Y").date()
    except ValueError:
        await message.reply_text("Use the expiry format dd-mm-yyyy, for example 31-12-2026.")
        return

    expires_at = datetime.combine(expiry_date, time.max, tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        await message.reply_text("The premium expiry date must be in the future.")
        return

    target = await admin_target(parts[1])
    if not target:
        await message.reply_text("User not found. They must have started the bot first.")
        return
    user_id = int(target["telegram_id"])
    await asyncio.to_thread(osint_bot.set_premium, user_id, expires_at)
    await message.reply_text(
        f"Premium enabled for <code>{user_id}</code> until <b>{expiry_date:%d-%m-%Y}</b>."
    )


@app.on_message(filters.private & filters.text & ~filters.regex(r"^/"))
async def lookup_number_input(_: Client, message: Message) -> None:
    if not message.from_user or not message.text:
        return
    number = normalise_phone_number(message.text)
    if not number:
        await message.reply_text("Send a valid 10-digit mobile number.")
        return
    await lookup_number(message, number)


@app.on_message(filters.command("start") & filters.private)
async def start_command(_: Client, message: Message) -> None:
    if not message.from_user:
        return
    referrer_id = start_referrer_id(message)
    await asyncio.to_thread(osint_bot.register_user, message.from_user, referrer_id)
    missing_channels = await missing_required_channels(message.from_user.id)
    if missing_channels:
        links = "\n".join(url for _, url, _ in missing_channels)
        await message.reply_text(JOIN_TEXT + "\n\n" + links + "\nJoin the channels, then send /start again.")
        return
    referrer_id = await asyncio.to_thread(
        osint_bot.complete_referral, message.from_user.id
    )
    await notify_referrer(referrer_id, message.from_user)
    await send_start_message(message)


if __name__ == "__main__":
    LOGGER.info("Starting bot")
    threading.Thread(
        target=lambda: web_app.run(
            host="0.0.0.0",
            port=int(os.getenv("PORT", "5000")),
            use_reloader=False,
        ),
        daemon=True,
    ).start()
    app.run()
