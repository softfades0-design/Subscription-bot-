"""
reminder_scheduler.py — background loop for abandoned-payment reminders.

The bot is a single-process aiogram poller with MongoDB and no task queue, so
reminders are handled with a simple asyncio loop: every CHECK_INTERVAL_SECONDS
it looks in the `reminders` collection for schedules whose first/second
reminder is due and not yet sent, sends them, and marks them sent.

Schedules are created/replaced in handlers/payment.py (callback_buy) and
cancelled in database.py (approve_order) and handlers/payment.py (cancel_order).
This module only sends what's already due.

Launch with `asyncio.create_task(reminder_scheduler.run(bot))` — it never
returns on its own.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta

from aiogram import Bot

from database import (
    get_setting,
    get_due_first_reminders,
    get_due_second_reminders,
    mark_first_reminder_sent,
    mark_second_reminder_sent,
    get_due_referral_reminders,
    mark_referral_reminder_sent,
    has_any_approved_order,
    get_due_plan_interest_reminders,
    mark_plan_interest_sent,
    user_has_active_plan,
    get_due_start_reminders,
    advance_start_reminder,
    cancel_start_reminders,
    get_all_plans,
    get_due_demo_sessions,
    claim_demo_expiry,
    complete_demo_deletion,
    expire_due_orders,
    get_user_info,
    cancel_reminder,
)
from keyboards.menu import (
    reminder_buy_now_keyboard,
    referral_reminder_keyboard,
    plan_interest_reminder_keyboard,
    regenerate_payment_qr_keyboard,
)
from keyboards.menu import plans_list_keyboard
from handlers.log_channel import log_payment_expired

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30

_DEFAULT_REMINDER_FIRST_MESSAGE = (
    "🔔 <b>Reminder</b>\n\n"
    "You still haven't completed payment for <b>{plan_name}</b>.\n\n"
    "💰 <b>Price:</b> ₹{plan_price}\n"
    "⏳ <b>Validity:</b> {plan_validity}\n\n"
    "Tap <b>Buy Now</b> below and complete your payment to activate your plan!"
)

_DEFAULT_REMINDER_SECOND_MESSAGE = (
    "🔔 <b>Last Chance!</b>\n\n"
    "Your payment for <b>{plan_name}</b> is still incomplete.\n\n"
    "💰 <b>Price:</b> ₹{plan_price}\n"
    "⏳ <b>Validity:</b> {plan_validity}\n\n"
    "Tap <b>Buy Now</b> below before this offer slips away!"
)

_START_REMINDER_RANGES = (
    ("start_reminder_second_min", "start_reminder_second_max", 120, 240),
    ("start_reminder_third_min", "start_reminder_third_max", 720, 1440),
    ("start_reminder_fourth_min", "start_reminder_fourth_max", 1440, 2880),
)


def _render(template: str, doc: dict, default: str) -> str:
    kwargs = dict(
        plan_name=doc.get("plan_name", ""),
        plan_price=doc.get("plan_price", ""),
        plan_validity=doc.get("plan_validity", ""),
    )
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        logger.warning("reminder message template is malformed — using default")
        return default.format(**kwargs)


async def run(bot: Bot) -> None:
    """Background loop — never returns. Launch with asyncio.create_task()."""
    logger.info("Payment reminder scheduler started (interval=%ss)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await _tick(bot)
        except Exception:
            logger.exception("Reminder scheduler tick failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _tick(bot: Bot) -> None:
    await _tick_demo_sessions(bot)

    # Expire payment orders independently of reminder settings. The database
    # update is atomic, so an approval racing this sweep cannot win afterward.
    for order in await expire_due_orders():
        await cancel_reminder(order["user_id"], order["_id"])
        user = await get_user_info(order["user_id"])
        await log_payment_expired(
            bot,
            user_id=order["user_id"],
            first_name=(user or {}).get("first_name", "User"),
            order_id=order["_id"],
            username=(user or {}).get("username"),
        )
        for message_id in (order.get("qr_message_id"), order.get("payment_message_id")):
            if not message_id:
                continue
            try:
                await bot.delete_message(chat_id=order["user_id"], message_id=message_id)
            except Exception:
                logger.info("Expired payment message %s was already deleted or unavailable", message_id)
        try:
            await bot.send_message(
                chat_id=order["user_id"],
                text=(
                    "⏰ Payment QR Expired\n\n"
                    "Your payment session has expired.\n"
                    "Please generate a new QR to continue your purchase."
                ),
                reply_markup=regenerate_payment_qr_keyboard(order["_id"]),
            )
        except Exception:
            logger.warning("Failed to send expiry message for order %s", order["_id"])

    enabled = (await get_setting("reminder_enabled", "1")) == "1"
    if not enabled:
        return

    now = datetime.now(timezone.utc)
    first_template = (await get_setting("reminder_first_message")) or _DEFAULT_REMINDER_FIRST_MESSAGE
    second_template = (await get_setting("reminder_second_message")) or _DEFAULT_REMINDER_SECOND_MESSAGE

    for doc in await get_due_first_reminders(now):
        user_id = doc["_id"]
        order_id = doc.get("order_id")
        plan_id = doc.get("plan_id")
        try:
            await bot.send_message(
                chat_id=user_id,
                text=_render(first_template, doc, _DEFAULT_REMINDER_FIRST_MESSAGE),
                reply_markup=reminder_buy_now_keyboard(plan_id) if plan_id is not None else None,
            )
        except Exception:
            logger.warning("Failed to send first reminder to user %s", user_id)
        # Mark sent regardless of delivery outcome — a blocked/invalid chat
        # should not be retried forever. Scoped to order_id so a schedule
        # replaced by a repeat Buy Now in the meantime is left untouched.
        await mark_first_reminder_sent(user_id, order_id)

    for doc in await get_due_second_reminders(now):
        user_id = doc["_id"]
        order_id = doc.get("order_id")
        plan_id = doc.get("plan_id")
        try:
            await bot.send_message(
                chat_id=user_id,
                text=_render(second_template, doc, _DEFAULT_REMINDER_SECOND_MESSAGE),
                reply_markup=reminder_buy_now_keyboard(plan_id) if plan_id is not None else None,
            )
        except Exception:
            logger.warning("Failed to send final reminder to user %s", user_id)
        await mark_second_reminder_sent(user_id, order_id)

    # ── One-time referral reminder ────────────────────────────────────────────
    for user_id in await get_due_referral_reminders(now):
        # Skip if the user has already made a purchase.
        if await has_any_approved_order(user_id):
            await mark_referral_reminder_sent(user_id)
            continue
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "🎁 <b>Want to save on your purchase?</b>\n\n"
                    "Invite your friends and earn discounts automatically.\n\n"
                    "🏷️ Every valid referral gives you a discount on your next purchase.\n\n"
                    "Tap below to start earning 👇"
                ),
                reply_markup=referral_reminder_keyboard(),
            )
        except Exception:
            logger.warning("Failed to send referral reminder to user %s", user_id)
        # Mark sent regardless of delivery — never retry this reminder.
        await mark_referral_reminder_sent(user_id)

    # ── Plan-interest reminder ────────────────────────────────────────────────
    for item in await get_due_plan_interest_reminders(now):
        user_id = item["user_id"]
        plan_id = item["plan_id"]
        # Skip if the user has already purchased this specific plan.
        if await user_has_active_plan(user_id, plan_id):
            await mark_plan_interest_sent(user_id)
            continue
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "🤔 <b>Still thinking?</b>\n\n"
                    "The plan you viewed is still waiting for you.\n\n"
                    "💰 Don't forget—you can reduce the price by using "
                    "<b>🏷️ Get Discount</b> before purchasing.\n\n"
                    "✨ Your referral discount will be applied automatically during checkout."
                ),
                reply_markup=plan_interest_reminder_keyboard(plan_id),
            )
        except Exception:
            logger.warning("Failed to send plan-interest reminder to user %s", user_id)
        # Mark sent regardless of delivery outcome — never retry this reminder.
        await mark_plan_interest_sent(user_id)

    # Start follow-up reminders for users who have not selected a plan.
    for item in await get_due_start_reminders(now):
        user_id = item["user_id"]
        step = item["step"]
        if await has_any_approved_order(user_id):
            await cancel_start_reminders(user_id)
            continue

        plans = await get_all_plans()
        if not plans:
            continue

        next_due = None
        if step < len(_START_REMINDER_RANGES):
            min_key, max_key, default_min, default_max = _START_REMINDER_RANGES[step]
            try:
                delay_min = max(1, int((await get_setting(min_key, str(default_min))) or default_min))
                delay_max = max(delay_min, int((await get_setting(max_key, str(default_max))) or default_max))
            except (TypeError, ValueError):
                delay_min, delay_max = default_min, default_max
            next_due = now + timedelta(minutes=random.randint(delay_min, delay_max))

        if not await advance_start_reminder(user_id, step, next_due):
            continue
        try:
            await bot.send_message(
                chat_id=user_id,
                text="✨ Choose a plan below to continue 👇",
                reply_markup=plans_list_keyboard(plans),
            )
        except Exception:
            logger.warning("Failed to send start follow-up reminder to user %s", user_id)


async def _tick_demo_sessions(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    for due in await get_due_demo_sessions(now):
        session = await claim_demo_expiry(due["_id"])
        if not session:
            continue

        for message_id in session.get("message_ids", []):
            try:
                await bot.delete_message(chat_id=session["user_id"], message_id=message_id)
            except Exception:
                logger.info("Demo message %s was already deleted or unavailable", message_id)

        await complete_demo_deletion(session["_id"])
