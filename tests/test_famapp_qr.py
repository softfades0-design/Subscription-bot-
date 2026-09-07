import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import zxingcpp
from PIL import Image


os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("FAMAPP_UPI_ID", "payments@example")
os.environ.setdefault("FAMAPP_PAYEE_NAME", "Test Payee")

import handlers.payment as payment  # noqa: E402
from handlers.payment import (  # noqa: E402
    _build_famapp_upi_uri,
    _generate_famapp_qr_bytes,
)


class FakeFamAppImap:
    raw_email = b"""From: no-reply@famapp.in
Subject: You received \xe2\x82\xb91.0 in your FamX account
Date: Sat, 29 Aug 2026 12:00:00 +0000
Content-Type: text/plain; charset=utf-8

You have successfully received \xe2\x82\xb91.0
Purpose :
FAP20260901GAH9Y8
"""

    def __init__(self, *_args):
        self.selected_mailbox = None

    def login(self, *_args):
        return "OK", [b"logged in"]

    def select(self, mailbox):
        self.selected_mailbox = mailbox
        return "OK", [b""]

    def search(self, *_args):
        return "OK", [b"1"]

    def fetch(self, *_args):
        return "OK", [(b"1 (RFC822 {0})", self.raw_email), b")"]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


class FamAppQrTests(unittest.TestCase):
    def test_qr_decodes_to_the_exact_order_specific_upi_uri(self):
        amount = "129.50"
        purpose = "FAP20260901ABC123"
        expected_uri = _build_famapp_upi_uri(amount, purpose)

        qr_bytes = _generate_famapp_qr_bytes(amount, purpose)
        with Image.open(BytesIO(qr_bytes)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.width, image.height)
            self.assertEqual(image.mode, "1")
            decoded = zxingcpp.read_barcode(image, is_pure=True)

        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.text, expected_uri)

    def test_verifier_accepts_real_famapp_received_email(self):
        order_id = "ORD-TEST-20260829"
        amount = "1.00"
        purpose = "FAP20260901GAH9Y8"
        order = {
            "final_price": amount,
            "plan_price": amount,
            "payment_purpose": purpose,
            "upi_uri": _build_famapp_upi_uri(amount, purpose),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }

        with (
            patch.object(payment, "get_order", new=AsyncMock(return_value=order)) as get_order,
            patch.object(payment, "imaplib") as imaplib,
            patch.object(payment, "IMAP_USERNAME", "test@example.com"),
            patch.object(payment, "IMAP_APP_PASSWORD", "test-password"),
            self.assertLogs(payment.logger, level="INFO") as logs,
        ):
            imaplib.IMAP4_SSL.side_effect = FakeFamAppImap
            result = asyncio.run(payment._verify_payment_famapp(order_id, amount))

        self.assertEqual(result, "success")
        get_order.assert_awaited_once_with(order_id)
        messages = "\n".join(logs.output)
        self.assertIn("candidate_email_count=1", messages)
        self.assertIn(f"payment_qr_upi_purpose={purpose}", messages)
        self.assertIn("masked_sender=n***@famapp.in", messages)
        self.assertNotIn("no-reply@famapp.in", messages)
        self.assertIn("extracted_amount=1.00", messages)
        self.assertIn(f"extracted_purpose={purpose}", messages)
        self.assertIn("amount_match=True", messages)
        self.assertIn("purpose_match=True", messages)
        self.assertIn("final_match=True", messages)

    def test_manual_screenshot_submission_reaches_review(self):
        user = type("User", (), {"id": 7, "username": "buyer"})()
        message = type("Message", (), {})()
        message.from_user = user
        message.photo = [type("Photo", (), {"file_id": "proof-file"})()]
        message.answer = AsyncMock()
        bot = AsyncMock()
        order = {
            "order_id": "ORD-MANUAL-1",
            "user_id": user.id,
            "plan_name": "Gold",
            "plan_price": "199",
            "final_price": "199",
            "payment_status": "created",
        }

        async def submit_proof():
            payment._waiting_proof[user.id] = order["order_id"]
            with (
                patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
                patch.object(payment, "update_order_status", new=AsyncMock(return_value=True)) as update_status,
                patch.object(payment, "LOG_CHANNEL_ID", 999),
            ):
                bot.send_photo = AsyncMock()
                await payment.handle_proof_photo(message, bot)
            return update_status

        update_status = asyncio.run(submit_proof())
        update_status.assert_awaited_once_with(order["order_id"], "pending")
        bot.send_photo.assert_awaited_once()
        message.answer.assert_awaited_once()

    def test_payment_status_message_is_reused_on_repeated_update(self):
        call = SimpleNamespace(
            message=SimpleNamespace(
                chat=SimpleNamespace(id=7),
                answer=AsyncMock(return_value=SimpleNamespace(message_id=101)),
            )
        )
        bot = AsyncMock()
        info = {}

        async def update_status_message():
            with patch.object(payment, "update_order_status_message", new=AsyncMock()) as save_status:
                first_id = await payment._edit_or_create_status_message(
                    call,
                    bot,
                    7,
                    "ORD-STATUS-1",
                    info,
                    "first",
                )
                second_id = await payment._edit_or_create_status_message(
                    call,
                    bot,
                    7,
                    "ORD-STATUS-1",
                    info,
                    "second",
                )
            return first_id, second_id, save_status

        first_id, second_id, save_status = asyncio.run(update_status_message())
        self.assertEqual(first_id, 101)
        self.assertEqual(second_id, 101)
        call.message.answer.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once_with(
            chat_id=7,
            message_id=101,
            text="second",
            reply_markup=None,
        )
        save_status.assert_awaited_once_with("ORD-STATUS-1", 7, 101)

    def test_generated_purpose_flows_through_qr_and_verification(self):
        amount = "1.00"
        purpose = payment._generate_famapp_purpose()
        self.assertRegex(purpose, r"^FAP\d{8}[A-Z0-9]{6}$")
        self.assertNotIn("-", purpose)

        upi_uri = _build_famapp_upi_uri(amount, purpose)
        qr_bytes = _generate_famapp_qr_bytes(amount, purpose)
        with Image.open(BytesIO(qr_bytes)) as image:
            decoded = zxingcpp.read_barcode(image, is_pure=True)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.text, upi_uri)
        self.assertEqual(
            payment.urllib.parse.parse_qs(
                payment.urllib.parse.urlsplit(decoded.text).query
            )["tn"][0],
            purpose,
        )

        email_message = EmailMessage()
        email_message["From"] = "no-reply@famapp.in"
        email_message["Subject"] = "You received ₹1.0 in your FamX account"
        email_message.set_content(
            "You have successfully received\n"
            "₹1.0\n"
            "from ASHISH\n\n"
            "Purpose :\n"
            f"{purpose}\n"
        )
        order = {
            "final_price": amount,
            "plan_price": amount,
            "payment_purpose": purpose,
            "upi_uri": upi_uri,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }

        with (
            patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
            patch.object(payment, "imaplib") as imaplib,
            patch.object(payment, "IMAP_USERNAME", "test@example.com"),
            patch.object(payment, "IMAP_APP_PASSWORD", "test-password"),
            patch.object(FakeFamAppImap, "raw_email", bytes(email_message)),
            self.assertLogs(payment.logger, level="INFO") as logs,
        ):
            imaplib.IMAP4_SSL.side_effect = FakeFamAppImap
            result = asyncio.run(payment._verify_payment_famapp("ORD-GENERATED", amount))

        self.assertEqual(result, "success")
        messages = "\n".join(logs.output)
        self.assertIn(f"expected_payment_purpose={purpose}", messages)
        self.assertIn(f"extracted_purpose={purpose}", messages)
        self.assertIn("amount_match=True", messages)
        self.assertIn("purpose_match=True", messages)
        self.assertIn("final_match=True", messages)

    def test_parser_extracts_purpose_from_html_multipart_famapp_email(self):
        message = EmailMessage()
        message["From"] = "no-reply@famapp.in"
        message["Subject"] = "You received ₹1.0 in your FamX account"
        message["Date"] = "Sat, 29 Aug 2026 12:00:00 +0000"
        message.set_content(
            "You have successfully received\n"
            "₹1.0\n"
            "from ASHISH\n\n"
            "Purpose :\n"
            "FAP20260901GAH9Y8\n"
        )
        message.add_alternative(
            """
            <html><body>
              <p>Example reference: FAP20260829UNRELATED</p>
              <div><span>Purpose</span> :<br>
                <span>FAP20260901GAH9Y8</span>
              </div>
            </body></html>
            """,
            subtype="html",
        )

        parsed = payment._parse_famapp_email(
            bytes(message),
            "html-multipart-1",
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["amount"], payment.Decimal("1.00"))
        self.assertEqual(parsed["purpose"], "FAP20260901GAH9Y8")


if __name__ == "__main__":
    unittest.main()