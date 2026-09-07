import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

from handlers import log_channel  # noqa: E402


class LogContactTests(unittest.TestCase):
    def _logged_text(self, function, *args, **kwargs):
        bot = AsyncMock()

        async def run():
            with patch.object(log_channel, "LOG_CHANNEL_ID", 999):
                await function(bot, *args, **kwargs)

        asyncio.run(run())
        return bot.send_message.await_args.kwargs["text"]

    def test_new_user_log_contains_username_contact_link(self):
        text = self._logged_text(log_channel.log_new_user, 123, "Bobby", "bobby")
        self.assertIn('<a href="https://t.me/bobby">👤 Contact User</a>', text)
        self.assertEqual(text.count("🆕 <b>New User</b>"), 1)

    def test_plan_selected_log_contains_id_fallback_contact_link(self):
        text = self._logged_text(
            log_channel.log_plan_selected,
            123,
            "Bobby",
            plan_title="Gold",
            price="₹199",
        )
        self.assertIn('<a href="tg://user?id=123">👤 Contact User</a>', text)
        self.assertIn("💎 <b>Plan Selected</b>", text)

    def test_payment_success_and_failed_logs_keep_single_events(self):
        success = self._logged_text(
            log_channel.log_payment_success,
            123,
            "Bobby",
            plan_name="Gold",
            amount="199",
            order_id="ORD-1",
            username="bobby",
        )
        failed = self._logged_text(
            log_channel.log_payment_failed,
            123,
            "Bobby",
            plan_name="Gold",
            amount="199",
            order_id="ORD-1",
            reason="not found",
        )
        self.assertIn('<a href="https://t.me/bobby">👤 Contact User</a>', success)
        self.assertIn('<a href="tg://user?id=123">👤 Contact User</a>', failed)
        self.assertEqual(success.count("🎉 <b>Payment Successful</b>"), 1)
        self.assertEqual(failed.count("❌ <b>Payment Failed</b>"), 1)

    def test_payment_expired_log_contains_exact_user_contact_link(self):
        text = self._logged_text(
            log_channel.log_payment_expired,
            123,
            "Bobby",
            order_id="ORD-EXPIRED",
            username="bobby",
        )
        self.assertIn("⏰ <b>Payment Expired</b>", text)
        self.assertIn('<a href="https://t.me/bobby">👤 Contact User</a>', text)
        self.assertEqual(text.count("⏰ <b>Payment Expired</b>"), 1)


if __name__ == "__main__":
    unittest.main()
