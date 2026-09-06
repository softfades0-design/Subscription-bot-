import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta

from aiogram import Router, Bot
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery

from database import (
    save_user, save_referral, get_user_referral_info,
    get_all_plans, get_plan, get_setting, get_start_demo,
    schedule_referral_reminder,
    save_plan_interest,
    schedule_start_reminders,
    cancel_start_reminders,
    has_any_approved_order,
    create_demo_session,
    activate_demo_session,
    discard_demo_session,
)
from keyboards.menu import plans_list_keyboard, plan_detail_keyboard, main_menu_keyboard
from handlers.log_channel import log_new_user, log_plan_selected

logger = logging.getLogger(__name__)

router = Router()

# ── Fallback texts (used only when DB setting is empty) ───────────────────────

_DEFAULT_WELCOME = (
    "🎉 <b>Welcome to Premium Bot!</b>\n\n"
    "✨ Get exclusive access to premium content\n"
    "💰 Affordable plans available\n"
    "✨ Daily New Uploads\n"
    "✨ High Quality Content\n\n"
    "✨ <b>SELECT A PLAN TO GET STARTED</b> ✨"
)

PRODUCT_TEXT = "Hello, {first_name} 👋\n\nChoose a plan to get started 💫"

_DEFAULT_BUY_MESSAGE = (
    "📦 <b>{plan_name}</b>\n\n"
    "💰 <b>Price:</b> ₹{plan_price}\n"
    "⏳ <b>Validity:</b> {plan_validity}\n\n"
    "Tap <b>Buy Now</b> to proceed with payment."
)

NO_PLANS_TEXT = (
    "⚠️ No plans are available right now.\n\n"
    "Please check back later or contact support."
)


# ── Start demo video sender ───────────────────────────────────────────────────

async def _copy_demo_messages(bot: Bot, chat_id: int, source: str, msg_ids: list[int]) -> list[int]:
    sent_ids: list[int] = []
    try:
        copied = await bot.copy_messages(chat_id=chat_id, from_chat_id=source, message_ids=msg_ids)
        sent_ids.extend(item.message_id for item in copied)
        return sent_ids
    except Exception:
        logger.exception("copy_messages() failed — falling back to individual sends")

    for message_id in msg_ids:
        try:
            copied = await bot.copy_message(chat_id=chat_id, from_chat_id=source, message_id=message_id)
            sent_ids.append(copied.message_id)
        except Exception:
            logger.exception("Failed to copy message %s from channel %s", message_id, source)
        await asyncio.sleep(0.25)
    return sent_ids


async def send_start_demo_videos(bot: Bot, chat_id: int, user_id: int) -> None:
    """
    Send the global start demo videos to the user on /start, if enabled.
    Reads message IDs and source channel from MongoDB — never downloads media.
    Returns immediately (no-op) when disabled or no IDs are configured.
    """
    cfg = await get_start_demo()
    if not cfg["enabled"] or not cfg["ids"]:
        return

    source  = cfg["source"]
    msg_ids = cfg["ids"]

    session_id = await create_demo_session(user_id)
    sent_ids = await _copy_demo_messages(bot, chat_id, source, msg_ids)
    if sent_ids:
        await activate_demo_session(session_id, sent_ids, datetime.now(timezone.utc) + timedelta(minutes=10))
    else:
        await discard_demo_session(session_id)


# ── Plan demo video sender ────────────────────────────────────────────────────

async def send_demo_videos(bot: Bot, chat_id: int, user_id: int, plan: dict) -> tuple[str, list[int]] | None:
    """
    Copy demo messages from the plan's source channel to the user.
    Stores only message IDs — no media is downloaded locally.
    """
    source = plan.get("source_channel_id", "")
    msg_ids = plan.get("demo_message_ids", [])

    if not source or not msg_ids:
        logger.warning("Plan id=%s has no demo videos configured.", plan.get("id"))
        return

    session_id = await create_demo_session(user_id)
    sent_ids = await _copy_demo_messages(bot, chat_id, source, msg_ids)
    if sent_ids:
        return session_id, sent_ids
    await discard_demo_session(session_id)
    return None


def _render_plan_text(plan: dict) -> str:
    buy_tpl = plan.get("buy_message") or _DEFAULT_BUY_MESSAGE
    try:
        return buy_tpl.format(
            plan_name=plan["name"],
            plan_price=plan["price"],
            plan_validity=plan["validity"],
        )
    except (KeyError, IndexError, ValueError):
        logger.exception("Invalid placeholder in buy_message template")
        return _DEFAULT_BUY_MESSAGE.format(
            plan_name=plan["name"],
            plan_price=plan["price"],
            plan_validity=plan["validity"],
        )


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    logger.info("/start from user %s", message.from_user.id)
    user = message.from_user

    # Clear any active payment/proof state so the user is never stuck.
    # This does not touch MongoDB — orders remain intact.
    from handlers.payment import clear_payment_state
    clear_payment_state(user.id)

    # Parse deep-link referral argument: /start <referrer_user_id>
    referrer_id: int | None = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        try:
            referrer_id = int(parts[1].strip())
        except ValueError:
            referrer_id = None

    try:
        is_new = await save_user(user.id, user.username, user.first_name)
    except Exception:
        logger.exception("Failed to save user %s", user.id)
        is_new = False

    # Record referral only for brand-new users referred by someone else
    if is_new and referrer_id is not None:
        try:
            referral_counted = await save_referral(user.id, referrer_id)
        except Exception:
            logger.exception("Failed to save referral for user %s from %s", user.id, referrer_id)
            referral_counted = False

        if referral_counted:
            try:
                info = await get_user_referral_info(referrer_id)
                await bot.send_message(
                    chat_id=referrer_id,
                    text=(
                        "🎉 <b>Congratulations!</b>\n\n"
                        "A new user joined using your referral link.\n\n"
                        "🎁 You earned <b>5% referral discount</b>.\n\n"
                        f"👥 <b>Total Referrals:</b> {info['total_referrals']}\n"
                        f"💰 <b>Current Discount:</b> {info['referral_discount']}%\n\n"
                        "Keep sharing your referral link and save more on your next purchase! 🚀"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Failed to send referral notification to user %s", referrer_id)

    if is_new:
        await log_new_user(bot, user.id, user.first_name, user.username)
        # Schedule the one-time referral reminder 30 minutes after first start.
        due_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        try:
            await schedule_referral_reminder(user.id, due_at)
        except Exception:
            logger.exception("Failed to schedule referral reminder for user %s", user.id)

    # Send start demo videos (if enabled by admin) — always, regardless of plans
    await send_start_demo_videos(bot, message.chat.id, user.id)

    plans = await get_all_plans()

    if not plans:
        welcome_text = (await get_setting("welcome_message")) or _DEFAULT_WELCOME
        await message.answer(welcome_text)
        await message.answer(NO_PLANS_TEXT)
        return
    if not await has_any_approved_order(user.id):
        try:
            first_min = max(1, int((await get_setting("start_reminder_first_min", "15")) or 15))
            first_max = max(first_min, int((await get_setting("start_reminder_first_max", "30")) or 30))
            await schedule_start_reminders(
                user.id,
                datetime.now(timezone.utc) + timedelta(minutes=random.randint(first_min, first_max)),
            )
        except (TypeError, ValueError):
            logger.exception("Invalid start reminder timing configuration")
        except Exception:
            logger.exception("Failed to schedule start reminders for user %s", user.id)
    welcome_text = (await get_setting("welcome_message")) or _DEFAULT_WELCOME
    await message.answer(welcome_text)
    await message.answer(
        PRODUCT_TEXT.format(first_name=user.first_name),
        reply_markup=plans_list_keyboard(plans),
    )


# ── Show plans (from "View Plans" button fallback) ────────────────────────────

@router.callback_query(lambda c: c.data == "show_plans")
async def cb_show_plans(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.answer(NO_PLANS_TEXT)
        return
    await call.message.answer(
        PRODUCT_TEXT.format(first_name=call.from_user.first_name),
        reply_markup=plans_list_keyboard(plans),
    )


# ── Plan selected (plan:{plan_id}) ────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("plan:"))
async def callback_plan(call: CallbackQuery, bot: Bot) -> None:
    """User tapped a plan button — send demo videos then plan detail."""
    await call.answer()
    try:
        plan_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.message.answer("⚠️ Invalid plan. Please try again.")
        return

    plan = await get_plan(plan_id)
    if not plan:
        await call.message.answer("⚠️ Plan not found. It may have been removed.")
        return

    await cancel_start_reminders(call.from_user.id)

    await log_plan_selected(
        bot,
        call.from_user.id,
        call.from_user.first_name,
        plan_title=plan["name"],
        price=f"₹{plan['price']} / {plan['validity']}",
    )

    demo_result = await send_demo_videos(bot, call.message.chat.id, call.from_user.id, plan)
    await call.message.answer(
        _render_plan_text(plan),
        reply_markup=plan_detail_keyboard(plan_id),
    )
    if demo_result:
        session_id, demo_ids = demo_result
        await activate_demo_session(
            session_id,
            demo_ids,
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )

    # Record plan interest: if the user never clicks Buy Now, a reminder fires
    # 30 minutes from now.  Viewing a different plan overwrites this safely.
    due_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    try:
        await save_plan_interest(call.from_user.id, plan_id, due_at)
    except Exception:
        logger.exception("Failed to save plan interest for user %s plan %s", call.from_user.id, plan_id)


# ── Back to plan list ─────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "back")
async def callback_back(call: CallbackQuery) -> None:
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.answer(NO_PLANS_TEXT)
        return
    await call.message.answer(
        PRODUCT_TEXT.format(first_name=call.from_user.first_name),
        reply_markup=plans_list_keyboard(plans),
    )


# ── Main menu ─────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "main_menu")
async def callback_main_menu(call: CallbackQuery) -> None:
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.answer(NO_PLANS_TEXT)
        return
    await call.message.answer(
        PRODUCT_TEXT.format(first_name=call.from_user.first_name),
        reply_markup=plans_list_keyboard(plans),
    )
