"""
handlers/payment.py — Buy Now flow with automatic VC Store payment verification.

Sequence:
  1. callback_buy         → load plan from DB, generate order, show payment details
  2. callback_i_have_paid → call VC Store API to verify payment automatically
       success  → instantly approve order, send access link
       pending  → tell user payment is still being processed
       failed   → ask user to complete payment and try again
  3. cancel callbacks     → cancel order, return to main menu
"""

import html
import logging
import random
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import aiohttp

from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, Message

from config import VC_API_KEY, VC_API_URL, LOG_CHANNEL_ID, ADMIN_IDS
from database import (
    create_order,
    update_order_status,
    approve_order,
    get_order,
    get_order_final_price,
    user_has_active_plan,
    get_plan,
    get_all_plans,
    get_setting,
    get_user_referral_info,
    set_pending_reminder,
    cancel_reminder,
    clear_plan_interest,
)
from keyboards.menu import (
    payment_details_keyboard,
    manual_payment_keyboard,
    manual_review_keyboard,
    main_menu_keyboard,
    plans_list_keyboard,
)
from handlers.log_channel import log_payment_started, log_payment_success, log_payment_failed

logger = logging.getLogger(__name__)

router = Router()

_IST = timezone(timedelta(hours=5, minutes=30))

# user_id -> { order_id, plan_name, plan_price, plan_validity, access_link, final_price }
_awaiting_proof: dict[int, dict] = {}

# user_id -> order_id  (set when user taps Upload Screenshot, cleared after photo received)
_waiting_proof: dict[int, str] = {}

_PRODUCT_TEXT = "Hello, {first_name} 👋\n\nChoose a plan to get started 💫"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_order_id() -> str:
    return f"ORD{int(time.time())}"


def _make_upi_qr_url(order_id: str, amount: str) -> str:
    """Build a quickchart.io QR image URL that encodes the UPI payment URI."""
    upi_uri = (
        f"upi://pay?pa=paytm.s1dw5n0@pty"
        f"&pn=VC+Payment+Gateway"
        f"&tid={order_id}"
        f"&tr={order_id}"
        f"&tn=VC+Payment"
        f"&am={amount}"
        f"&cu=INR"
    )
    encoded = urllib.parse.quote(upi_uri, safe="")
    return f"https://quickchart.io/qr?text={encoded}&size=1000&ecLevel=H&format=png&margin=2"


def _now_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


async def _verify_payment_vc(order_id: str, amount: str) -> str:
    """
    Call the VC Store payment verification API.
    Returns "success", "pending", or "failed".
    """
    logger.info("API key loaded: %s", bool(VC_API_KEY))
    if not VC_API_URL or not VC_API_KEY:
        logger.warning("VC_API_URL or VC_API_KEY is not configured")
        return "error"

    try:
        params = {"api_key": VC_API_KEY, "order_id": order_id, "amount": amount}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                VC_API_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                raw_text = await resp.text()
                final_url = str(resp.url)
                http_status = resp.status
                logger.info("Final Request URL: %s", final_url)
                logger.info("HTTP Status: %s", http_status)
                logger.info("Raw Response: %r", raw_text)
                try:
                    data = __import__("json").loads(raw_text)
                except Exception:
                    logger.error("VC API returned non-JSON for order %s", order_id)
                    return "error"
                status = str(data.get("status", "")).lower()
                if status == "success":
                    return "success"
                elif status == "pending":
                    return "pending"
                else:
                    return "failed"
    except Exception:
        logger.exception("VC API call failed for order %s", order_id)
        return "error"


# ── Shared helper: create order + send QR + send payment message ──────────────


async def _send_payment_screen(
    bot: Bot,
    chat_id: int,
    user_id: int,
    plan: dict,
    plan_id: int | None,
    final_price_str: str,
    price_section: str,
    discount_pct: int,
) -> str | None:
    """
    Create a new order, send the QR photo and payment details message.
    Saves both message IDs into _awaiting_proof so the failed-payment handler
    can delete both messages when issuing a replacement.
    Returns the new order_id, or None if order creation failed.
    """
    # Retry up to 5 times on PK collision
    order_id: str | None = None
    for _ in range(5):
        candidate = _make_order_id()
        try:
            await create_order(
                user_id=user_id,
                plan_name=plan["name"],
                plan_price=plan["price"],
                plan_validity=plan["validity"],
                order_id=candidate,
                plan_id=plan_id,
                access_link=plan["access_link"],
                final_price=final_price_str,
                referral_discount_used=discount_pct,
            )
            order_id = candidate
            break
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                logger.warning("Order ID collision on %s — retrying", candidate)
                continue
            logger.exception("Failed to create order for user %s", user_id)
            await bot.send_message(chat_id, "⚠️ Something went wrong. Please try again.")
            return None

    if order_id is None:
        await bot.send_message(chat_id, "⚠️ Could not generate order. Please try again.")
        return None

    # Build payment message text
    _default_tpl = (
        "💳 <b>Payment Details</b>\n\n"
        "📦 <b>Plan:</b> {plan_name}\n"
        "{price_section}\n"
        "⌛ <b>Validity:</b> {plan_validity}\n\n"
        "📲 Scan the QR code above using any UPI app.\n\n"
        "✅ <b>Pay ₹{final_price_str}</b> by scanning the <b>QR Code</b> above.\n"
        "✅ After paying, tap <b>Check Payment Status</b> — your plan unlocks instantly once the payment is confirmed.\n\n"
        "🆔 <b>Order:</b> #{order_id}"
    )
    payment_tpl = (await get_setting("payment_message")) or _default_tpl
    _fmt_kwargs = dict(
        plan_name=plan["name"],
        plan_price=plan["price"],
        plan_validity=plan["validity"],
        order_id=order_id,
        price_section=price_section,
        final_price_str=final_price_str,
    )
    try:
        payment_msg_text = payment_tpl.format(**_fmt_kwargs)
    except (KeyError, ValueError, IndexError):
        logger.warning("payment_message template is malformed — using default")
        payment_msg_text = _default_tpl.format(**_fmt_kwargs)

    # Send QR photo and capture its message ID
    qr_url = _make_upi_qr_url(order_id, final_price_str)
    logger.info("UPI QR URL for order %s: %s", order_id, qr_url)
    qr_msg_id: int | None = None
    try:
        qr_msg = await bot.send_photo(chat_id=chat_id, photo=qr_url)
        qr_msg_id = qr_msg.message_id
    except Exception:
        logger.exception("Failed to send QR image for order %s", order_id)

    # Send payment details message and capture its message ID
    pay_msg = await bot.send_message(
        chat_id,
        payment_msg_text,
        reply_markup=payment_details_keyboard(order_id),
    )

    # Store full context for the verification handler
    _awaiting_proof[user_id] = {
        "order_id": order_id,
        "plan_id": plan_id,
        "plan_name": plan["name"],
        "plan_price": plan["price"],
        "final_price": final_price_str,
        "plan_validity": plan["validity"],
        "access_link": plan["access_link"],
        "price_section": price_section,
        "discount_pct": discount_pct,
        "qr_msg_id": qr_msg_id,
        "payment_msg_id": pay_msg.message_id,
    }

    # Schedule abandoned-payment reminders
    _MAX_REMINDER_DELAY_MIN = 525600  # 1 year

    def _clamped_delay(raw: str, fallback: int) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return fallback
        if value <= 0:
            return fallback
        return min(value, _MAX_REMINDER_DELAY_MIN)

    first_min = _clamped_delay(await get_setting("reminder_first_delay_min", "15"), 15)
    second_min = _clamped_delay(
        await get_setting("reminder_second_delay_min", "1440"), 1440
    )
    now = datetime.now(timezone.utc)
    await set_pending_reminder(
        user_id=user_id,
        order_id=order_id,
        plan_id=plan_id,
        plan_name=plan["name"],
        plan_price=plan["price"],
        plan_validity=plan["validity"],
        first_due=now + timedelta(minutes=first_min),
        second_due=now + timedelta(minutes=second_min),
    )

    return order_id


# ── Manual payment screen (no VC QR) ─────────────────────────────────────────


async def _send_manual_payment_screen(
    bot: Bot,
    chat_id: int,
    user_id: int,
    plan: dict,
    plan_id: int | None,
    final_price_str: str,
    price_section: str,
    discount_pct: int,
) -> str | None:
    """
    Create a new order and show the admin-configured manual QR + UPI text.
    Does NOT touch any VC payment logic.
    Returns the new order_id, or None if order creation failed.
    """
    # Create order — same retry logic as automatic flow
    order_id: str | None = None
    for _ in range(5):
        candidate = _make_order_id()
        try:
            await create_order(
                user_id=user_id,
                plan_name=plan["name"],
                plan_price=plan["price"],
                plan_validity=plan["validity"],
                order_id=candidate,
                plan_id=plan_id,
                access_link=plan["access_link"],
                final_price=final_price_str,
                referral_discount_used=discount_pct,
            )
            order_id = candidate
            break
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                logger.warning("Order ID collision on %s — retrying", candidate)
                continue
            logger.exception("Failed to create order for user %s", user_id)
            await bot.send_message(chat_id, "⚠️ Something went wrong. Please try again.")
            return None

    if order_id is None:
        await bot.send_message(chat_id, "⚠️ Could not generate order. Please try again.")
        return None

    # Fetch admin-configured manual payment details
    manual_qr     = (await get_setting("manual_payment_qr", "")) or ""
    manual_upi    = (await get_setting("manual_upi_text",   "")) or ""

    # Build payment message
    upi_line = f"\n💳 <b>UPI ID:</b> <code>{manual_upi}</code>\n" if manual_upi else ""
    payment_msg_text = (
        "💳 <b>Payment Details</b>\n\n"
        f"📦 <b>Plan:</b> {plan['name']}\n"
        f"{price_section}\n"
        f"⌛ <b>Validity:</b> {plan['validity']}\n"
        f"{upi_line}\n"
        "📲 Scan the QR code above and pay the exact amount.\n\n"
        "✅ After paying, tap <b>📤 Upload Payment Screenshot</b> to submit your proof.\n\n"
        f"🆔 <b>Order:</b> #{order_id}"
    )

    # Send manual QR photo if available
    qr_msg_id: int | None = None
    if manual_qr:
        try:
            qr_msg = await bot.send_photo(chat_id=chat_id, photo=manual_qr)
            qr_msg_id = qr_msg.message_id
        except Exception:
            logger.exception("Failed to send manual QR image for order %s", order_id)

    # Send payment details message
    pay_msg = await bot.send_message(
        chat_id,
        payment_msg_text,
        reply_markup=manual_payment_keyboard(order_id),
    )

    # Store context (same shape as automatic flow for cancel/reminder compatibility)
    _awaiting_proof[user_id] = {
        "order_id":       order_id,
        "plan_id":        plan_id,
        "plan_name":      plan["name"],
        "plan_price":     plan["price"],
        "final_price":    final_price_str,
        "plan_validity":  plan["validity"],
        "access_link":    plan["access_link"],
        "price_section":  price_section,
        "discount_pct":   discount_pct,
        "qr_msg_id":      qr_msg_id,
        "payment_msg_id": pay_msg.message_id,
        "mode":           "manual",
    }

    # Schedule abandoned-payment reminders (same as automatic flow)
    _MAX_REMINDER_DELAY_MIN = 525600

    def _clamped_delay(raw: str, fallback: int) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return fallback
        return fallback if value <= 0 else min(value, _MAX_REMINDER_DELAY_MIN)

    first_min  = _clamped_delay(await get_setting("reminder_first_delay_min",  "15"),   15)
    second_min = _clamped_delay(await get_setting("reminder_second_delay_min", "1440"), 1440)
    now = datetime.now(timezone.utc)
    await set_pending_reminder(
        user_id=user_id,
        order_id=order_id,
        plan_id=plan_id,
        plan_name=plan["name"],
        plan_price=plan["price"],
        plan_validity=plan["validity"],
        first_due=now + timedelta(minutes=first_min),
        second_due=now + timedelta(minutes=second_min),
    )

    return order_id


# ── Buy Now (buy:{plan_id}) ───────────────────────────────────────────────────


@router.callback_query(lambda c: c.data and c.data.startswith("buy:"))
async def callback_buy(call: CallbackQuery, bot: Bot) -> None:
    """User tapped Buy Now — load plan from DB, generate order, show payment details."""
    logger.info("BUY CALLBACK HIT")
    print("BUY CALLBACK HIT:", call.data)
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

    user = call.from_user

    # Block repurchase of an already-active plan (approved + not yet expired).
    if await user_has_active_plan(user.id, plan_id):
        await call.message.answer(
            "✅ <b>You have already purchased this plan.</b>\n\n"
            "Thank you for your purchase! ❤️\n\n"
            "You already have access to this plan."
        )
        return

    await log_payment_started(bot, user.id, user.first_name, plan_title=plan["name"])

    # User clicked Buy Now — suppress any pending plan-interest reminder.
    try:
        await clear_plan_interest(user.id)
    except Exception:
        logger.exception("Failed to clear plan interest for user %s", user.id)

    # Referral discount
    referral_info = await get_user_referral_info(user.id)
    discount_pct = referral_info.get("referral_discount", 0) or 0
    original_price_str = plan["price"]

    if discount_pct > 0:
        try:
            final_price = round(float(original_price_str) * (1 - discount_pct / 100))
            final_price_str = str(final_price)
        except (ValueError, TypeError):
            final_price_str = original_price_str
    else:
        discount_pct = 0
        final_price_str = original_price_str

    price_section = (
        f"💰 <b>Original Price:</b> ₹{original_price_str}\n"
        f"🎁 <b>Referral Discount:</b> {discount_pct}%\n"
        f"💳 <b>Final Price:</b> ₹{final_price_str}"
    )

    # Route to manual or automatic payment screen based on current setting
    payment_mode = (await get_setting("payment_mode", "automatic")) or "automatic"

    if payment_mode == "manual":
        await _send_manual_payment_screen(
            bot=bot,
            chat_id=call.message.chat.id,
            user_id=user.id,
            plan=plan,
            plan_id=plan_id,
            final_price_str=final_price_str,
            price_section=price_section,
            discount_pct=discount_pct,
        )
    else:
        await _send_payment_screen(
            bot=bot,
            chat_id=call.message.chat.id,
            user_id=user.id,
            plan=plan,
            plan_id=plan_id,
            final_price_str=final_price_str,
            price_section=price_section,
            discount_pct=discount_pct,
        )


# ── I Have Paid → automatic VC verification ───────────────────────────────────


@router.callback_query(lambda c: c.data and c.data.startswith("paid:"))
async def callback_i_have_paid(call: CallbackQuery, bot: Bot) -> None:
    """Verify payment automatically via VC Store API and activate instantly on success."""
    await call.answer()
    order_id = call.data.split(":", 1)[1]
    user = call.from_user

    # Restore order context: prefer in-memory, fall back to DB on cold start
    info = _awaiting_proof.get(user.id, {})
    if not info or info.get("order_id") != order_id:
        # Cold start recovery — fetch what we need from the DB
        order_doc = await get_order(order_id)
        if order_doc:
            info = {
                "order_id": order_id,
                "plan_id": order_doc.get("plan_id"),
                "plan_name": order_doc.get("plan_name", ""),
                "plan_price": order_doc.get("plan_price", ""),
                "final_price": order_doc.get("final_price")
                or order_doc.get("plan_price", "0"),
                "plan_validity": order_doc.get("plan_validity", ""),
                "access_link": order_doc.get("access_link", ""),
            }
        else:
            info = {"order_id": order_id}
        _awaiting_proof[user.id] = info

    final_price = (
        info.get("final_price") or await get_order_final_price(order_id) or "0"
    )

    # Show a "verifying" message while the API call is in-flight
    verifying_msg = await call.message.answer(
        "⏳ <b>Verifying your payment...</b>\n\nPlease wait a moment."
    )

    status = await _verify_payment_vc(order_id, final_price)

    if status == "success":
        # Set to pending first (approve_order requires pending status)
        await update_order_status(order_id, "pending")
        result = await approve_order(order_id)
        _awaiting_proof.pop(user.id, None)

        if result:
            sub_end_ist = result["subscription_end"].astimezone(_IST)
            expiry_str = sub_end_ist.strftime("%d %b %Y")
            access_link = result.get("access_link", "")

            activation_text = (
                "🎉 <b>Payment Verified! Plan Activated!</b>\n\n"
                f"📦 <b>Plan:</b> {html.escape(result['plan_name'])}\n"
                f"⏳ <b>Validity:</b> {html.escape(result['plan_validity'])}\n"
                f"📅 <b>Expires:</b> {expiry_str}\n\n"
            )
            if access_link:
                activation_text += f"🔗 <b>Access Link:</b>\n{access_link}\n\n"
            activation_text += "Thank you for your purchase! ❤️"

            await log_payment_success(
                bot,
                user_id=user.id,
                first_name=user.first_name,
                plan_name=result["plan_name"],
                amount=final_price,
                order_id=order_id,
            )
            try:
                await verifying_msg.delete()
            except Exception:
                pass
            await call.message.answer(
                activation_text, reply_markup=main_menu_keyboard()
            )
        else:
            # Order may have already been approved (e.g. double-tap)
            await verifying_msg.edit_text(
                "✅ <b>Your plan is already activated.</b>\n\n"
                "Use /status to check your subscription.",
                reply_markup=main_menu_keyboard(),
            )

    elif status == "pending":
        await verifying_msg.edit_text(
            "⏳ Payment not received yet. Please wait a moment and try again.",
            reply_markup=payment_details_keyboard(order_id),
        )

    else:
        # failed or API error — show note, delete old messages, issue a fresh order
        await log_payment_failed(
            bot,
            user_id=user.id,
            first_name=user.first_name,
            plan_name=info.get("plan_name", ""),
            amount=final_price,
            order_id=order_id,
            reason=status,
        )

        chat_id = call.message.chat.id

        # 1. Show the note by editing the "verifying…" message in place
        note_text = (
            "❌ <b>Payment not detected.</b>\n\n"
            "A fresh payment QR has been generated.\n\n"
            "💡 Please pay only using the new QR below.\n"
            "Do not use the previous QR because it will no longer be verified."
        )
        try:
            await verifying_msg.edit_text(note_text)
        except Exception:
            try:
                await bot.send_message(chat_id, note_text)
            except Exception:
                pass

        # 2. Delete the old QR photo (message sent just before the payment message)
        qr_msg_id = info.get("qr_msg_id")
        if qr_msg_id:
            try:
                await bot.delete_message(chat_id, qr_msg_id)
            except Exception:
                pass

        # 3. Delete the old payment details message (call.message IS that message)
        try:
            await call.message.delete()
        except Exception:
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

        # Mark old order as failed and cancel its reminder
        try:
            await update_order_status(order_id, "failed")
        except Exception:
            logger.exception("Failed to mark old order %s as failed", order_id)
        try:
            await cancel_reminder(user.id, order_id)
        except Exception:
            logger.exception("Failed to cancel reminder for order %s", order_id)

        # 4. Re-issue the payment screen (new order ID + fresh QR, same plan/price/layout)
        plan_dict = {
            "name": info.get("plan_name", ""),
            "price": info.get("plan_price", ""),
            "validity": info.get("plan_validity", ""),
            "access_link": info.get("access_link", ""),
        }
        await _send_payment_screen(
            bot=bot,
            chat_id=chat_id,
            user_id=user.id,
            plan=plan_dict,
            plan_id=info.get("plan_id"),
            final_price_str=final_price,
            price_section=info.get("price_section", ""),
            discount_pct=info.get("discount_pct", 0),
        )


# ── Cancel Order (from payment details screen) ────────────────────────────────


@router.callback_query(lambda c: c.data and c.data.startswith("cancel_order:"))
async def callback_cancel_order(call: CallbackQuery) -> None:
    await call.answer()
    order_id = call.data.split(":", 1)[1]
    await update_order_status(order_id, "cancelled")
    _awaiting_proof.pop(call.from_user.id, None)
    await cancel_reminder(call.from_user.id, order_id)

    plans = await get_all_plans()
    await call.message.answer(
        _PRODUCT_TEXT.format(first_name=call.from_user.first_name),
        reply_markup=plans_list_keyboard(plans) if plans else main_menu_keyboard(),
    )


# ── Upload proof (manual payment — user taps the screenshot button) ───────────


@router.callback_query(lambda c: c.data and c.data.startswith("upload_proof:"))
async def callback_upload_proof(call: CallbackQuery) -> None:
    """User tapped 📤 Upload Payment Screenshot — ask them to send the photo."""
    await call.answer()
    order_id = call.data.split(":", 1)[1]
    _waiting_proof[call.from_user.id] = order_id
    await call.message.answer(
        "📤 <b>Upload Payment Screenshot</b>\n\n"
        "Please send your payment screenshot as a <b>photo</b>.\n\n"
        f"🆔 <b>Order:</b> #{order_id}"
    )


async def _user_is_waiting_proof(message: Message) -> bool:
    return message.from_user.id in _waiting_proof


@router.message(_user_is_waiting_proof, F.photo)
async def handle_proof_photo(message: Message, bot: Bot) -> None:
    """User sent their payment screenshot — forward to review channel."""
    user = message.from_user
    order_id = _waiting_proof.pop(user.id, None)
    if not order_id:
        return

    # Fetch order details from DB
    order = await get_order(order_id)
    if not order:
        await message.answer("⚠️ Order not found. Please contact support.")
        return

    # Mark order as pending (awaiting manual review)
    await update_order_status(order_id, "pending")

    # Build review caption
    uname  = f"@{html.escape(user.username)}" if user.username else "—"
    amount = order.get("final_price") or order.get("plan_price", "—")
    caption = (
        "💳 <b>New Manual Payment</b>\n\n"
        f"👤 <b>Username:</b> {uname}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"📦 <b>Plan:</b> {html.escape(order['plan_name'])}\n"
        f"💰 <b>Amount:</b> ₹{html.escape(str(amount))}\n"
        f"🆔 <b>Order ID:</b> <code>{order_id}</code>\n"
        f"🕒 <b>Time:</b> {_now_ist()}"
    )

    # Forward screenshot + details + Approve/Reject to the review channel
    try:
        await bot.send_photo(
            chat_id=LOG_CHANNEL_ID,
            photo=message.photo[-1].file_id,
            caption=caption,
            reply_markup=manual_review_keyboard(order_id, user.id),
        )
    except Exception:
        logger.exception("Failed to forward proof to review channel for order %s", order_id)

    # Confirm receipt to user
    await message.answer(
        "✅ <b>Screenshot received!</b>\n\n"
        "Your payment is under review. You will be notified once it's approved.\n\n"
        f"🆔 <b>Order:</b> #{order_id}"
    )


@router.message(_user_is_waiting_proof)
async def handle_proof_wrong_type(message: Message) -> None:
    """Reject non-photo messages while waiting for the proof screenshot."""
    if (message.text or "").startswith("/"):
        _waiting_proof.pop(message.from_user.id, None)
        return
    await message.answer("⚠️ Please send your payment screenshot as a <b>photo</b>.")


# ── Manual payment review — admin Approve / Reject from the review channel ────


@router.callback_query(lambda c: c.data and c.data.startswith("manual_approve:"))
async def callback_manual_approve(call: CallbackQuery, bot: Bot) -> None:
    """Admin clicked ✅ Approve on a manual payment review message."""
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()

    try:
        _, order_id, user_id_str = call.data.split(":", 2)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await call.answer("⚠️ Invalid callback data.", show_alert=True)
        return

    # approve_order requires pending status (already set when screenshot was received)
    result = await approve_order(order_id)
    if not result:
        try:
            await call.message.edit_caption(
                (call.message.caption or "") + "\n\n⚠️ Already processed.",
                reply_markup=None,
            )
        except Exception:
            pass
        await call.answer("⚠️ Order already processed or not found.", show_alert=True)
        return

    sub_end_ist  = result["subscription_end"].astimezone(_IST)
    expiry_str   = sub_end_ist.strftime("%d %b %Y")
    access_link  = result.get("access_link", "")
    approver_tag = f"@{call.from_user.username}" if call.from_user.username else str(call.from_user.id)

    # Notify the user
    activation_text = (
        "🎉 <b>Payment Approved! Plan Activated!</b>\n\n"
        f"📦 <b>Plan:</b> {html.escape(result['plan_name'])}\n"
        f"⏳ <b>Validity:</b> {html.escape(result['plan_validity'])}\n"
        f"📅 <b>Expires:</b> {expiry_str}\n\n"
    )
    if access_link:
        activation_text += f"🔗 <b>Access Link:</b>\n{access_link}\n\n"
    activation_text += "Thank you for your purchase! ❤️"

    try:
        await bot.send_message(user_id, activation_text, reply_markup=main_menu_keyboard())
    except Exception:
        logger.exception("Failed to notify user %s of manual approval", user_id)

    # Log the approval action
    try:
        amount = await get_order_final_price(order_id) or "—"
        try:
            chat = await bot.get_chat(user_id)
            first_name = chat.first_name or str(user_id)
        except Exception:
            first_name = str(user_id)
        await log_payment_success(
            bot,
            user_id=user_id,
            first_name=first_name,
            plan_name=result["plan_name"],
            amount=amount,
            order_id=order_id,
        )
    except Exception:
        logger.exception("Failed to log manual approval for order %s", order_id)

    # Update the review channel message to show it was handled
    try:
        await call.message.edit_caption(
            (call.message.caption or "") + f"\n\n✅ <b>Approved by {html.escape(approver_tag)}</b>",
            reply_markup=None,
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("manual_reject:"))
async def callback_manual_reject(call: CallbackQuery, bot: Bot) -> None:
    """Admin clicked ❌ Reject on a manual payment review message."""
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()

    try:
        _, order_id, user_id_str = call.data.split(":", 2)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await call.answer("⚠️ Invalid callback data.", show_alert=True)
        return

    await update_order_status(order_id, "rejected")
    await cancel_reminder(user_id, order_id)

    rejecter_tag = f"@{call.from_user.username}" if call.from_user.username else str(call.from_user.id)

    # Notify the user
    try:
        await bot.send_message(
            user_id,
            "❌ <b>Payment could not be verified.</b>\n\n"
            "Please contact support if you think this is incorrect.",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        logger.exception("Failed to notify user %s of manual rejection", user_id)

    # Update the review channel message
    try:
        await call.message.edit_caption(
            (call.message.caption or "") + f"\n\n❌ <b>Rejected by {html.escape(rejecter_tag)}</b>",
            reply_markup=None,
        )
    except Exception:
        pass


# ── Utility ───────────────────────────────────────────────────────────────────


def clear_payment_state(user_id: int) -> None:
    """Remove any in-memory payment state for a user. Does NOT touch MongoDB."""
    _awaiting_proof.pop(user_id, None)
    _waiting_proof.pop(user_id, None)
