"""
handlers/log_channel.py — centralised helpers for posting activity events to the log channel.

All functions silently swallow errors so a log failure never disrupts the user flow.
"""

import html
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot

from config import LOG_CHANNEL_ID

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def get_user_contact_link(user_id: int, username: str | None = None) -> str:
    """Return a direct Telegram URL for the exact user."""
    username = str(username or "").strip().lstrip("@")
    return f"https://t.me/{username}" if username else f"tg://user?id={user_id}"


def _contact_line(user_id: int, username: str | None = None) -> str:
    href = html.escape(get_user_contact_link(user_id, username), quote=True)
    return f'<a href="{href}">👤 Contact User</a>'


def _now_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


async def _send(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=LOG_CHANNEL_ID, text=text)
    except Exception:
        logger.exception("Failed to post to log channel")


async def log_new_user(bot: Bot, user_id: int, first_name: str, username: str | None) -> None:
    uname = f"@{html.escape(username)}" if username else "None"
    await _send(
        bot,
        "🆕 <b>New User</b>\n\n"
        f"👤 Name: {html.escape(first_name)}\n"
        f"📛 Username: {uname}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"🕒 Time: {_now_ist()}\n\n"
        f"{_contact_line(user_id, username)}",
    )


async def log_plan_selected(
    bot: Bot,
    user_id: int,
    first_name: str,
    plan_title: str = "",
    price: str = "",
    username: str | None = None,
) -> None:
    await _send(
        bot,
        "💎 <b>Plan Selected</b>\n\n"
        f"👤 Name: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Plan: {html.escape(plan_title)}\n"
        f"💰 Price: {html.escape(price)}\n\n"
        f"{_contact_line(user_id, username)}",
    )


async def log_payment_started(
    bot: Bot,
    user_id: int,
    first_name: str,
    plan_title: str = "",
    username: str | None = None,
) -> None:
    await _send(
        bot,
        "💳 <b>Payment Started</b>\n\n"
        f"👤 Name: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Plan: {html.escape(plan_title)}\n\n"
        f"{_contact_line(user_id, username)}",
    )


async def log_payment_success(
    bot: Bot,
    user_id: int,
    first_name: str,
    plan_name: str = "",
    amount: str = "",
    order_id: str = "",
    username: str | None = None,
) -> None:
    await _send(
        bot,
        "🎉 <b>Payment Successful</b>\n\n"
        f"👤 User: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Plan: {html.escape(plan_name)}\n"
        f"💰 Amount: ₹{html.escape(amount)}\n"
        f"🆔 Order: <code>{html.escape(order_id)}</code>\n\n"
        "✅ Subscription Activated\n\n"
        f"{_contact_line(user_id, username)}",
    )


async def log_payment_failed(
    bot: Bot,
    user_id: int,
    first_name: str,
    plan_name: str = "",
    amount: str = "",
    order_id: str = "",
    reason: str = "",
    username: str | None = None,
) -> None:
    await _send(
        bot,
        "❌ <b>Payment Failed</b>\n\n"
        f"👤 User: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Plan: {html.escape(plan_name)}\n"
        f"💰 Amount: ₹{html.escape(amount)}\n"
        f"🆔 Order: <code>{html.escape(order_id)}</code>\n\n"
        f"Reason: {html.escape(reason)}\n\n"
        f"{_contact_line(user_id, username)}",
    )


async def log_payment_expired(
    bot: Bot,
    user_id: int,
    first_name: str,
    order_id: str = "",
    username: str | None = None,
) -> None:
    await _send(
        bot,
        "⏰ <b>Payment Expired</b>\n\n"
        f"👤 User: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"🆔 Order: <code>{html.escape(order_id)}</code>\n\n"
        f"{_contact_line(user_id, username)}",
    )


async def log_payment_cancelled(
    bot: Bot,
    user_id: int,
    first_name: str,
    order_id: str = "",
    username: str | None = None,
) -> None:
    await _send(
        bot,
        "❌ <b>Payment Cancelled</b>\n\n"
        f"👤 User: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"🆔 Order: <code>{html.escape(order_id)}</code>\n\n"
        f"{_contact_line(user_id, username)}",
    )
