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
        f"🕒 Time: {_now_ist()}",
    )


async def log_plan_selected(
    bot: Bot,
    user_id: int,
    first_name: str,
    plan_title: str = "",
    price: str = "",
) -> None:
    await _send(
        bot,
        "💎 <b>Plan Selected</b>\n\n"
        f"👤 Name: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Plan: {html.escape(plan_title)}\n"
        f"💰 Price: {html.escape(price)}",
    )


async def log_payment_started(
    bot: Bot,
    user_id: int,
    first_name: str,
    plan_title: str = "",
) -> None:
    await _send(
        bot,
        "💳 <b>Payment Started</b>\n\n"
        f"👤 Name: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Plan: {html.escape(plan_title)}",
    )


async def log_payment_success(
    bot: Bot,
    user_id: int,
    first_name: str,
    plan_name: str = "",
    amount: str = "",
    order_id: str = "",
) -> None:
    await _send(
        bot,
        "🎉 <b>Payment Successful</b>\n\n"
        f"👤 User: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Plan: {html.escape(plan_name)}\n"
        f"💰 Amount: ₹{html.escape(amount)}\n"
        f"🆔 Order: <code>{html.escape(order_id)}</code>\n\n"
        "✅ Subscription Activated",
    )


async def log_payment_failed(
    bot: Bot,
    user_id: int,
    first_name: str,
    plan_name: str = "",
    amount: str = "",
    order_id: str = "",
    reason: str = "",
) -> None:
    await _send(
        bot,
        "❌ <b>Payment Failed</b>\n\n"
        f"👤 User: {html.escape(first_name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"📦 Plan: {html.escape(plan_name)}\n"
        f"💰 Amount: ₹{html.escape(amount)}\n"
        f"🆔 Order: <code>{html.escape(order_id)}</code>\n\n"
        f"Reason: {html.escape(reason)}",
    )
