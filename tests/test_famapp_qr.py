import os
import unittest
from io import BytesIO

import zxingcpp
from PIL import Image


os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("FAMAPP_UPI_ID", "payments@example")
os.environ.setdefault("FAMAPP_PAYEE_NAME", "Test Payee")

from handlers.payment import (  # noqa: E402
    _build_famapp_upi_uri,
    _generate_famapp_qr_bytes,
)


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


if __name__ == "__main__":
    unittest.main()