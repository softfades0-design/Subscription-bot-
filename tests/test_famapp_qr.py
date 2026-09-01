import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
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
Payment purpose: FAP-20260829-XI61XD
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
        purpose = "FAP-20260901-ABC123"
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
        purpose = "FAP-20260829-XI61XD"
        order = {
            "final_price": amount,
            "plan_price": amount,
            "payment_purpose": purpose,
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
        self.assertIn("masked_sender=n***@famapp.in", messages)
        self.assertNotIn("no-reply@famapp.in", messages)
        self.assertIn("extracted_amount=1.00", messages)
        self.assertIn(f"extracted_purpose={purpose}", messages)
        self.assertIn("amount_match=True", messages)
        self.assertIn("purpose_match=True", messages)
        self.assertIn("final_match=True", messages)


if __name__ == "__main__":
    unittest.main()