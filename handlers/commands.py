import logging
import os
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from config import SUPPORT_GROUP_URL
from database import get_all_plans, get_user_active_subscription, get_setting, get_user_referral_info
from keyboards.menu import plan_detail_keyboard, plans_list_keyboard, refer_share_keyboard

_IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger(__name__)

router = Router()

BUILD_VERSION = "replit-build-20260715"
_STARTUP_TIME = datetime.now(timezone.utc)


# ── /debug ────────────────────────────────────────────────────────────────────

@router.message(Command("debug", ignore_case=True))
async def cmd_debug(message: Message) -> None:
    logger.info("/debug from user %s", message.from_user.id)
    uptime_str = _STARTUP_TIME.strftime("%Y-%m-%d %H:%M:%S UTC")
    vc_key_loaded  = "✅ Yes" if os.environ.get("VC_API_KEY")  else "❌ No"
    vc_url_loaded  = "✅ Yes" if os.environ.get("VC_API_URL")  else "❌ No"
    await message.answer(
        "<b>🔧 Debug Info</b>\n\n"
        f"<b>Build version:</b> <code>{BUILD_VERSION}</code>\n"
        f"<b>Startup time:</b> <code>{uptime_str}</code>\n"
        f"<b>VC_API_KEY loaded:</b> {vc_key_loaded}\n"
        f"<b>VC_API_URL loaded:</b> {vc_url_loaded}"
    )


# ── /plans ────────────────────────────────────────────────────────────────────

@router.message(Command("plans", ignore_case=True))
async def cmd_plans(message: Message) -> None:
    logger.info("/plans from user %s", message.from_user.id)
    plans = await get_all_plans()
    if not plans:
        await message.answer("📦 No plans are available right now. Check back later.")
        return

    for plan in plans:
        text = (
            f"📦 <b>{plan['name']}</b>\n"
            f"💰 ₹{plan['price']} / {plan['validity']}"
        )
        await message.answer(text, reply_markup=plan_detail_keyboard(plan["id"]))


# ── /status ───────────────────────────────────────────────────────────────────

@router.message(Command("status", ignore_case=True))
async def cmd_status(message: Message) -> None:
    logger.info("/status from user %s", message.from_user.id)

    sub = await get_user_active_subscription(message.from_user.id)
    if not sub:
        await message.answer(
            "❌ <b>No active subscription found.</b>\n\n"
            "Use /plans to view available plans."
        )
        return

    try:
        end_utc = datetime.fromisoformat(sub["subscription_end"])
        end_ist = end_utc.astimezone(_IST)
        expiry_str = end_ist.strftime("%d %b %Y")
    except Exception:
        expiry_str = sub.get("subscription_end", "—")

    await message.answer(
        "✅ <b>Active Subscription</b>\n\n"
        f"📦 <b>Plan:</b> {sub['plan_name']}\n"
        f"💰 <b>Price:</b> ₹{sub['plan_price']}\n"
        f"⏳ <b>Validity:</b> {sub['plan_validity']}\n"
        f"📅 <b>Expires:</b> {expiry_str}"
    )


# ── /help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help", ignore_case=True))
async def cmd_help(message: Message) -> None:
    logger.info("/help from user %s", message.from_user.id)
    await message.answer(
        "❓ <b>Help Center</b>\n\n"
        "• Use /plans to view available plans.\n"
        "• Select a plan and complete payment.\n"
        "• After payment, check your subscription status using /status.\n"
        "• If you face any issue, use /contact."
    )


# ── /refer & Get Discount button ─────────────────────────────────────────────

from aiogram.types import CallbackQuery as _CQ  # local import to avoid circular

_REFERRAL_UNAVAILABLE = "⚠️ Referral program is currently unavailable."


async def _referral_enabled() -> bool:
    return (await get_setting("referral_enabled", "1")) == "1"


async def _send_refer_page(target, bot: Bot) -> None:
    """Shared logic for /refer and the 🏷️ Get Discount button."""
    if not await _referral_enabled():
        if isinstance(target, _CQ):
            await target.message.answer(_REFERRAL_UNAVAILABLE)
        else:
            await target.answer(_REFERRAL_UNAVAILABLE)
        return

    user_id = target.from_user.id
    bot_info = await bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start={user_id}"

    info = await get_user_referral_info(user_id)
    total_referrals   = info["total_referrals"]
    referral_discount = info["referral_discount"]

    text = (
        "🎉 <b>Refer Friends &amp; Earn Discounts!</b>\n\n"
        "Invite your friends using your personal referral link.\n\n"
        "Every valid referral gives you a <b>5% discount</b> on your next purchase.\n\n"
        "Share this link:\n\n"
        f"<code>{referral_link}</code>\n\n"
        f"👥 <b>Total Referrals:</b> {total_referrals}\n"
        f"🎁 <b>Current Discount:</b> {referral_discount}%"
    )
    kb = refer_share_keyboard(referral_link)

    if isinstance(target, _CQ):
        await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data == "open_refer")
async def cb_open_refer(call: _CQ, bot: Bot) -> None:
    """🏷️ Get Discount inline button."""
    await call.answer()
    await _send_refer_page(call, bot)


@router.message(Command("refer", ignore_case=True))
async def cmd_refer(message: Message, bot: Bot) -> None:
    """Refer & Earn — show the user their referral link and stats."""
    logger.info("/refer from user %s", message.from_user.id)
    await _send_refer_page(message, bot)


# ── /contact ──────────────────────────────────────────────────────────────────

@router.message(Command("contact", ignore_case=True))
async def cmd_contact(message: Message) -> None:
    logger.info("/contact from user %s", message.from_user.id)
    # DB setting takes priority; fall back to env var
    support_url = (await get_setting("support_group_url")) or SUPPORT_GROUP_URL
    await message.answer(
        "📞 <b>Support</b>\n\n"
        "Need help?\n\n"
        "Join our official support group:\n\n"
        f"{support_url}"
    )
