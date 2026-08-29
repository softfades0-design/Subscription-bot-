"""
handlers/payment.py — Buy Now flow with automatic FamApp payment verification.

Sequence:
  1. callback_buy         → load plan from DB, generate order, show payment details
  2. callback_i_have_paid → verify the FamApp payment automatically via IMAP
       success  → approve the order and send access link
       pending  → tell the user the payment is still being processed
       failed   → ask the user to complete payment and try again
  3. cancel callbacks     → cancel the order and return to the main menu
"""

import email
import html
import imaplib
import logging
import os
import random
import re
import secrets
import string
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from email import policy
from email.utils import parsedate_to_datetime
from io import BytesIO

import qrcode
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from qrcode.constants import ERROR_CORRECT_H

from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, Message, BufferedInputFile

from config import (
    LOG_CHANNEL_ID,
    ADMIN_IDS,
    DEFAULT_UPI_ID,
    DEFAULT_PAYEE_NAME,
    PURPOSE_PREFIX,
    BRAND_NAME,
    ORDER_EXPIRY_MINUTES,
)
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
    cancel_start_reminders,
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


def _format_amount(amount: str | Decimal) -> str:
    value = Decimal(str(amount))
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _generate_famapp_purpose() -> str:
    prefix = "".join(ch for ch in str(PURPOSE_PREFIX or "FAP").upper() if ch.isalnum()) or "FAP"
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"{prefix}-{date_part}-{suffix}"


def _build_famapp_upi_uri(amount: str | Decimal, purpose: str) -> str:
    params = {
        "pa": DEFAULT_UPI_ID,
        "pn": DEFAULT_PAYEE_NAME,
        "am": _format_amount(amount),
        "cu": "INR",
        "tn": purpose,
    }
    return "upi://pay?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in font_candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _draw_centered_text(draw: ImageDraw.ImageDraw, x_center: int, y: int, text: str, font: ImageFont.ImageFont, fill: str = "#111111") -> int:
    text_width, text_height = _text_size(draw, text, font)
    x = x_center - text_width // 2
    draw.text((x, y), text, font=font, fill=fill)
    return text_height


def _draw_badge(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int, text: str, font: ImageFont.ImageFont) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill="#EAF2FF", outline="#C9DAFF", width=2)
    text_width, text_height = _text_size(draw, text, font)
    draw.text((x + (width - text_width) // 2, y + (height - text_height) // 2 - 2), text, font=font, fill="#1849A9")


def _generate_famapp_qr_bytes(amount: str | Decimal, purpose: str) -> bytes:
    upi_uri = _build_famapp_upi_uri(amount, purpose)
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=14, border=4)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    canvas = Image.new("RGB", (1400, 1700), "#F6F8FC")
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((110, 80, 1290, 1620), radius=54, fill=(0, 0, 0, 74))
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    canvas.paste(shadow.convert("RGB"), (0, 0), shadow)

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((90, 60, 1310, 1600), radius=54, fill="white", outline="#E3EAF6", width=3)
    draw.rounded_rectangle((90, 60, 1310, 220), radius=54, fill="#0F4C81")
    draw.rectangle((90, 150, 1310, 220), fill="#0F4C81")

    brand_font = _load_font(56, bold=True)
    subtitle_font = _load_font(24, bold=False)
    title_font = _load_font(34, bold=True)
    body_font = _load_font(30, bold=False)
    label_font = _load_font(24, bold=True)
    small_font = _load_font(22, bold=False)

    initials = "".join(part[0] for part in BRAND_NAME.split() if part)[:3].upper() or "FAP"
    _draw_badge(draw, 120, 92, 92, 56, initials, _load_font(24, bold=True))
    _draw_centered_text(draw, 700, 88, BRAND_NAME, brand_font, fill="white")
    _draw_centered_text(draw, 700, 156, "Secure UPI Payment", subtitle_font, fill="#D9E8F7")

    qr_box = (290, 290, 1110, 1110)
    draw.rounded_rectangle(qr_box, radius=40, fill="white", outline="#DCE6F4", width=4)
    qr_size = 720
    qr_image = qr_image.resize((qr_size, qr_size), Image.Resampling.LANCZOS)
    qr_x = qr_box[0] + (qr_box[2] - qr_box[0] - qr_size) // 2
    qr_y = qr_box[1] + (qr_box[3] - qr_box[1] - qr_size) // 2
    canvas.paste(qr_image, (qr_x, qr_y))

    _draw_centered_text(draw, 700, 1160, "Scan to Pay", title_font, fill="#102A43")
    _draw_centered_text(draw, 700, 1208, f"₹{_format_amount(amount)}", _load_font(44, bold=True), fill="#0F4C81")
    _draw_centered_text(draw, 700, 1260, f"Payee: {DEFAULT_PAYEE_NAME}", body_font, fill="#243B53")

    details_top = 1330
    details_left = 180
    details_right = 1220
    draw.rounded_rectangle((details_left, details_top, details_right, 1515), radius=32, fill="#F8FBFF", outline="#DDE7F3", width=2)

    draw.text((220, details_top + 26), "Purpose", font=label_font, fill="#5B7083")
    draw.text((390, details_top + 26), purpose, font=body_font, fill="#102A43")
    draw.text((220, details_top + 98), "UPI ID", font=label_font, fill="#5B7083")
    draw.text((390, details_top + 98), DEFAULT_UPI_ID, font=body_font, fill="#102A43")

    footer = f"{BRAND_NAME} • {purpose}"
    footer_width, _ = _text_size(draw, footer, small_font)
    draw.text(((canvas.width - footer_width) // 2, 1568), footer, font=small_font, fill="#66788A")

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _now_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _parse_amount_from_text(text: str) -> Decimal | None:
    match = re.search(r"₹\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, re.IGNORECASE)
    if not match:
        return None
    normalized = match.group(1).replace(",", "")
    try:
        return Decimal(normalized).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _decode_imap_part(value: bytes | None) -> str:
    if not value:
        return ""
    for encoding in ("utf-8", "latin-1"):
        try:
            return value.decode(encoding, errors="replace")
        except Exception:
            continue
    return value.decode("utf-8", errors="replace")


def _extract_text_from_message(msg) -> str:
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type().startswith("text/"):
                try:
                    payload = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                if payload:
                    parts.append(str(payload))
        return "\n".join(parts)
    try:
        payload = msg.get_content()
    except Exception:
        payload = msg.get_payload(decode=True)
    return str(payload) if payload is not None else ""


def _parse_famapp_email(raw_message: bytes, message_id: str) -> dict | None:
    try:
        message = email.message_from_bytes(raw_message, policy=policy.default)
    except Exception:
        return None
    subject = str(message.get("Subject", ""))
    body = _extract_text_from_message(message)
    combined_text = f"{subject}\n{body}"
    purpose_pattern = re.compile(rf"\b{re.escape((PURPOSE_PREFIX or 'FAP').upper())}-[A-Z0-9]{{8}}-[A-Z0-9]{{6}}\b")
    purpose = purpose_pattern.search(combined_text)
    amount = _parse_amount_from_text(combined_text)
    date_value = message.get("Date")
    timestamp = datetime.now(timezone.utc)
    if date_value:
        try:
            parsed = parsedate_to_datetime(date_value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = parsed.astimezone(timezone.utc)
        except Exception:
            pass
    return {
        "message_id": message_id,
        "subject": subject,
        "body": body,
        "timestamp": timestamp,
        "purpose": purpose.group(0) if purpose else None,
        "amount": amount,
    }


async def _verify_payment_famapp(order_id: str, amount: str) -> str:
    """Verify a FamApp order via Gmail IMAP using the same purpose+amount matching pattern as the upstream repo."""
    if not IMAP_USERNAME or not IMAP_APP_PASSWORD:
        logger.warning("FamApp IMAP credentials are missing for order %s", order_id)
        return "error"

    order = await get_order(order_id)
    if not order:
        return "failed"

    expected_amount = Decimal(str(amount or order.get("final_price") or order.get("plan_price") or "0")).quantize(Decimal("0.01"))
    expected_purpose = order.get("payment_purpose") or _generate_famapp_purpose()
    expiry = None
    if order.get("expires_at"):
        try:
            expiry = datetime.fromisoformat(str(order["expires_at"]))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            expiry = expiry.astimezone(timezone.utc)
        except Exception:
            expiry = None
    if expiry and datetime.now(timezone.utc) >= expiry:
        return "expired"

    try:
        imap_client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        status, _ = imap_client.login(IMAP_USERNAME, IMAP_APP_PASSWORD)
        if status != "OK":
            imap_client.logout()
            return "error"
        search_from = IMAP_SENDER_FILTER or "no-reply@famapp.in"
        imap_client.select(IMAP_MAILBOX or "INBOX")
        search_status, data = imap_client.search(None, "FROM", search_from, "SINCE", (datetime.now(timezone.utc) - timedelta(hours=GMAIL_LOOKBACK_HOURS)).strftime("%d-%b-%Y"))
        if search_status != "OK":
            imap_client.close()
            imap_client.logout()
            return "pending"
        message_ids = data[0].split() if data and data[0] else []
        for message_id in message_ids:
            try:
                fetch_status, fetched = imap_client.fetch(message_id, "(RFC822)")
            except imaplib.IMAP4.error:
                continue
            if fetch_status != "OK":
                continue
            for part in fetched:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw_message = part[1]
                    parsed = _parse_famapp_email(raw_message, message_id.decode("ascii", errors="ignore"))
                    if not parsed:
                        continue
                    subject = (parsed.get("subject") or "").strip()
                    body = (parsed.get("body") or "").strip()
                    if re.search(r"your payment of ₹.* is successful", subject, re.IGNORECASE) or re.search(r"you have successfully paid", body, re.IGNORECASE):
                        continue
                    if not re.search(r"you received ₹.* in your famx account", subject, re.IGNORECASE) or not re.search(r"you have successfully received", body, re.IGNORECASE):
                        continue
                    if parsed.get("purpose") != expected_purpose:
                        continue
                    if parsed.get("amount") != expected_amount:
                        continue
                    imap_client.close()
                    imap_client.logout()
                    return "success"
        imap_client.close()
        imap_client.logout()
        return "pending"
    except Exception:
        logger.exception("FamApp Gmail IMAP verification failed for order %s", order_id)
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
    famapp_purpose = _generate_famapp_purpose()
    famapp_upi_uri = _build_famapp_upi_uri(final_price_str, famapp_purpose)
    famapp_qr_bytes = _generate_famapp_qr_bytes(final_price_str, famapp_purpose)
    famapp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=int(ORDER_EXPIRY_MINUTES or 15))

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
                payment_purpose=famapp_purpose,
                upi_uri=famapp_upi_uri,
                payee_name=DEFAULT_PAYEE_NAME,
                qr_image=famapp_qr_bytes,
                expires_at=famapp_expires_at,
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

    # Send FamApp QR image and capture its message ID.
    qr_data = BytesIO(famapp_qr_bytes)
    qr_data.name = f"{order_id}.png"
    logger.info("FamApp UPI URI for order %s: %s", order_id, famapp_upi_uri)
    qr_msg_id: int | None = None
    try:
        qr_msg = await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(qr_data.getvalue(), filename=qr_data.name))
        qr_msg_id = qr_msg.message_id
    except Exception:
        logger.exception("Failed to send FamApp QR image for order %s", order_id)

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

    await cancel_start_reminders(user.id)

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


# ── I Have Paid → automatic FamApp verification ──────────────────────────────


@router.callback_query(lambda c: c.data and c.data.startswith("paid:"))
async def callback_i_have_paid(call: CallbackQuery, bot: Bot) -> None:
    """Verify payment automatically via FamApp IMAP and activate it on success."""
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

    status = await _verify_payment_famapp(order_id, final_price)

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
