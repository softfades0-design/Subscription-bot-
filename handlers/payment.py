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
import random
import re
import secrets
import string
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from io import BytesIO

import qrcode
from qrcode.constants import ERROR_CORRECT_H

from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, Message, BufferedInputFile

from config import (
    LOG_CHANNEL_ID,
    ADMIN_IDS,
    DEFAULT_UPI_ID,
    DEFAULT_PAYEE_NAME,
    PURPOSE_PREFIX,
    ORDER_EXPIRY_MINUTES,
    GMAIL_LOOKBACK_HOURS,
    IMAP_HOST,
    IMAP_PORT,
    IMAP_USERNAME,
    IMAP_APP_PASSWORD,
    IMAP_MAILBOX,
    IMAP_SENDER_FILTER,
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


def _generate_famapp_qr_bytes(amount: str | Decimal, purpose: str) -> bytes:
    """Return a QR-only PNG encoding the order-specific FamApp UPI URI.

    QR generation stays entirely local.  Keeping the URI construction here and
    passing it directly to qrcode ensures the amount and payment purpose used
    by IMAP verification are exactly the values encoded in the image.
    """
    upi_uri = _build_famapp_upi_uri(amount, purpose)
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)
    output = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(
        output,
        format="PNG",
        optimize=True,
    )
    return output.getvalue()


def _now_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _parse_amount_from_text(text: str) -> Decimal | None:
    match = re.search(r"₹\s*([0-9][0-9,]*(?:\.\d+)?)", text, re.IGNORECASE)
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


class _FamAppHTMLTextParser(HTMLParser):
    """Convert an HTML email body to text while preserving useful spacing."""

    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, _attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth == 0 and tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth == 0 and tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def _html_to_readable_text(value: str) -> str:
    parser = _FamAppHTMLTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        # Keep any text already parsed; malformed email HTML must not prevent
        # amount/purpose extraction from the other MIME parts.
        pass
    return parser.get_text()


def _get_message_part_text(part) -> str:
    try:
        payload = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return _decode_imap_part(payload)
    return str(payload) if payload is not None else ""


def _extract_text_from_message(msg) -> str:
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type().lower()
            if content_type == "text/plain":
                text = _get_message_part_text(part)
            elif content_type == "text/html":
                text = _html_to_readable_text(_get_message_part_text(part))
            else:
                continue
            if text:
                parts.append(text)
        return "\n".join(parts)
    content_type = msg.get_content_type().lower()
    text = _get_message_part_text(msg)
    return _html_to_readable_text(text) if content_type == "text/html" else text


def _mask_sender(sender: str | None) -> str:
    """Keep verification diagnostics from exposing a complete sender address."""
    _, address = parseaddr(sender or "")
    value = address or (sender or "").strip()
    if "@" in value:
        local, domain = value.rsplit("@", 1)
        return f"{local[:1]}***@{domain}"
    return f"{value[:1]}***" if value else "<unknown>"


def _parse_famapp_email(raw_message: bytes, message_id: str) -> dict | None:
    try:
        message = email.message_from_bytes(raw_message, policy=policy.default)
    except Exception:
        return None
    subject = str(message.get("Subject", ""))
    sender = str(message.get("From", ""))
    body = _extract_text_from_message(message)
    combined_text = f"{subject}\n{body}"
    prefix = (PURPOSE_PREFIX or "FAP").upper()
    purpose_pattern = re.compile(
        rf"(?i)\bPurpose\s*:\s*"
        rf"({re.escape(prefix)}-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b"
    )
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
        "sender": sender,
        "subject": subject,
        "body": body,
        "timestamp": timestamp,
        "purpose": purpose.group(1) if purpose else None,
        "amount": amount,
    }


async def _verify_payment_famapp(order_id: str, amount: str) -> str:
    """Verify a FamApp order via Gmail IMAP using the same purpose+amount matching pattern as the upstream repo."""
    if not IMAP_USERNAME or not IMAP_APP_PASSWORD:
        logger.info(
            "FamApp verification order_id=%s expected_amount=%s "
            "expected_payment_purpose=%s imap_connection=failure "
            "selected_mailbox=%s candidate_email_count=%d "
            "final_match=%s expiry_status=%s",
            order_id,
            "<unknown>",
            "<unknown>",
            "<not_selected>",
            0,
            False,
            "not_checked",
        )
        return "error"

    order = await get_order(order_id)
    if not order:
        logger.info(
            "FamApp verification order_id=%s expected_amount=%s "
            "expected_payment_purpose=%s imap_connection=%s "
            "selected_mailbox=%s candidate_email_count=%d "
            "final_match=%s expiry_status=%s",
            order_id,
            "<unknown>",
            "<unknown>",
            "not_attempted",
            "<not_selected>",
            0,
            False,
            "not_checked",
        )
        return "failed"

    expected_amount = Decimal(str(amount or order.get("final_price") or order.get("plan_price") or "0")).quantize(Decimal("0.01"))
    expected_purpose = order.get("payment_purpose")
    payment_qr_upi_purpose = "<none>"
    stored_upi_uri = order.get("upi_uri") or ""
    if stored_upi_uri:
        try:
            payment_qr_upi_purpose = (
                urllib.parse.parse_qs(
                    urllib.parse.urlsplit(str(stored_upi_uri)).query
                ).get("tn", [None])[0]
                or "<none>"
            )
        except (TypeError, ValueError):
            payment_qr_upi_purpose = "<invalid>"
    expiry = None
    expiry_status = "missing"
    if order.get("expires_at"):
        try:
            expiry = datetime.fromisoformat(str(order["expires_at"]))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            expiry = expiry.astimezone(timezone.utc)
            expiry_status = "expired" if datetime.now(timezone.utc) >= expiry else "active"
        except Exception:
            expiry = None
            expiry_status = "invalid"
    if expiry and datetime.now(timezone.utc) >= expiry:
        logger.info(
            "FamApp verification order_id=%s expected_amount=%s "
            "expected_payment_purpose=%s imap_connection=not_attempted "
            "selected_mailbox=%s candidate_email_count=%d "
            "final_match=%s expiry_status=%s",
            order_id,
            expected_amount,
            expected_purpose,
            "<not_selected>",
            0,
            False,
            expiry_status,
        )
        return "expired"

    selected_mailbox = "<not_selected>"
    candidate_email_count = 0
    try:
        imap_client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        status, _ = imap_client.login(IMAP_USERNAME, IMAP_APP_PASSWORD)
        if status != "OK":
            logger.info(
                "FamApp verification order_id=%s expected_amount=%s "
                "expected_payment_purpose=%s imap_connection=failure "
                "selected_mailbox=%s candidate_email_count=%d "
                "final_match=%s expiry_status=%s",
                order_id,
                expected_amount,
                expected_purpose,
                selected_mailbox,
                candidate_email_count,
                False,
                expiry_status,
            )
            try:
                imap_client.logout()
            except Exception:
                pass
            return "error"

        mailbox = IMAP_MAILBOX or "INBOX"
        selected_mailbox = mailbox
        imap_client.select(mailbox)
        logger.info(
            "FamApp verification order_id=%s expected_amount=%s "
            "expected_payment_purpose=%s imap_connection=success "
            "selected_mailbox=%s candidate_email_count=%d "
            "final_match=%s expiry_status=%s",
            order_id,
            expected_amount,
            expected_purpose,
            selected_mailbox,
            candidate_email_count,
            False,
            expiry_status,
        )
        sender_candidates: list[str] = []
        configured_sender = (IMAP_SENDER_FILTER or "").strip()
        if configured_sender:
            sender_candidates.append(configured_sender)
            if "@" in configured_sender:
                sender_candidates.append(configured_sender.rsplit("@", 1)[1])
        if not sender_candidates:
            sender_candidates = ["famapp", "famx"]

        seen_message_ids: set[bytes] = set()
        for sender_filter in sender_candidates:
            if not sender_filter:
                continue
            search_status, data = imap_client.search(
                None,
                "FROM",
                sender_filter,
                "SINCE",
                (datetime.now(timezone.utc) - timedelta(hours=GMAIL_LOOKBACK_HOURS)).strftime("%d-%b-%Y"),
            )
            if search_status == "OK" and data and data[0]:
                for message_id in data[0].split():
                    seen_message_ids.add(message_id)

        message_ids = sorted(seen_message_ids)
        candidate_email_count = len(message_ids)
        logger.info(
            "FamApp verification order_id=%s expected_amount=%s "
            "expected_payment_purpose=%s imap_connection=success "
            "selected_mailbox=%s candidate_email_count=%d "
            "final_match=%s expiry_status=%s",
            order_id,
            expected_amount,
            expected_purpose,
            selected_mailbox,
            candidate_email_count,
            False,
            expiry_status,
        )
        if not message_ids:
            imap_client.close()
            imap_client.logout()
            return "pending"

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
                        logger.info(
                            "FamApp verification order_id=%s expected_amount=%s "
                            "expected_payment_purpose=%s imap_connection=success "
                            "selected_mailbox=%s candidate_email_count=%d "
                            "masked_sender=%s subject=%s extracted_amount=%s "
                            "extracted_purpose=%s amount_match=%s "
                            "purpose_match=%s final_match=%s expiry_status=%s",
                            order_id,
                            expected_amount,
                            expected_purpose,
                            selected_mailbox,
                            candidate_email_count,
                            "<unknown>",
                            "<unparsed>",
                            "<unknown>",
                            "<unknown>",
                            False,
                            False,
                            False,
                            expiry_status,
                        )
                        continue
                    subject = (parsed.get("subject") or "").strip()
                    body = (parsed.get("body") or "").strip()

                    is_outgoing_payment = bool(
                        re.search(r"your payment of ₹.* is successful", subject, re.IGNORECASE)
                        or re.search(r"you have successfully paid", body, re.IGNORECASE)
                    )
                    is_received_subject = bool(
                        re.search(
                            r"you\s+received\s+₹\s*\d[0-9,]*(?:\.\d+)?\s+in\s+your\s+famx\s+account",
                            subject,
                            re.IGNORECASE,
                        )
                    )
                    is_received_body = bool(
                        re.search(r"you\s+have\s+successfully\s+received", body, re.IGNORECASE)
                    )
                    amount_match = parsed.get("amount") == expected_amount
                    purpose_match = (
                        expected_purpose is not None
                        and parsed.get("purpose") == expected_purpose
                    )
                    final_match = (
                        not is_outgoing_payment
                        and is_received_subject
                        and is_received_body
                        and purpose_match
                        and amount_match
                    )
                    logger.info(
                        "FamApp verification order_id=%s expected_amount=%s "
                        "expected_payment_purpose=%s payment_qr_upi_purpose=%s "
                        "imap_connection=success "
                        "selected_mailbox=%s candidate_email_count=%d "
                        "masked_sender=%s subject=%r extracted_amount=%s "
                        "extracted_purpose=%s amount_match=%s "
                        "purpose_match=%s final_match=%s expiry_status=%s",
                        order_id,
                        expected_amount,
                        expected_purpose,
                        payment_qr_upi_purpose,
                        selected_mailbox,
                        candidate_email_count,
                        _mask_sender(parsed.get("sender")),
                        subject,
                        parsed.get("amount") if parsed.get("amount") is not None else "<none>",
                        parsed.get("purpose") or "<none>",
                        amount_match,
                        purpose_match,
                        final_match,
                        expiry_status,
                    )
                    if is_outgoing_payment:
                        continue
                    if not is_received_subject:
                        continue
                    if not is_received_body:
                        continue
                    if not purpose_match:
                        continue
                    if not amount_match:
                        continue
                    imap_client.close()
                    imap_client.logout()
                    return "success"
        imap_client.close()
        imap_client.logout()
        return "pending"
    except Exception:
        logger.info(
            "FamApp verification order_id=%s expected_amount=%s "
            "expected_payment_purpose=%s imap_connection=failure "
            "selected_mailbox=%s candidate_email_count=%d "
            "final_match=%s expiry_status=%s",
            order_id,
            expected_amount,
            expected_purpose,
            selected_mailbox,
            candidate_email_count,
            False,
            expiry_status,
        )
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
    logger.info(
        "FamApp payment order_id=%s payment_qr_upi_purpose=%s",
        order_id,
        famapp_purpose,
    )
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
