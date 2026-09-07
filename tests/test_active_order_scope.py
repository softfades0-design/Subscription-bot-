import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

import database  # noqa: E402
import handlers.payment as payment  # noqa: E402


class ActiveOrderScopeTests(unittest.TestCase):
    def test_active_order_lookup_includes_user_and_plan(self):
        orders = AsyncMock()
        orders.find_one = AsyncMock(return_value=None)

        async def lookup():
            with patch.object(database, "_orders", orders):
                return await database.get_active_order_for_user_plan(7, 27)

        asyncio.run(lookup())
        query = orders.find_one.await_args.args[0]
        self.assertEqual(query["user_id"], 7)
        self.assertEqual(query["plan_id"], 27)
        self.assertEqual(query["payment_status"], {"$in": ["created", "pending"]})

    def test_buy_now_does_not_reuse_existing_order(self):
        class DummyMessage:
            def __init__(self):
                self.chat = SimpleNamespace(id=123)
                self.answer = AsyncMock(return_value=SimpleNamespace(message_id=99, delete=AsyncMock()))
                self.delete = AsyncMock()
                self.edit_text = AsyncMock()
                self.edit_reply_markup = AsyncMock()

        class DummyCall:
            def __init__(self):
                self.data = "buy:27"
                self.message = DummyMessage()
                self.from_user = SimpleNamespace(id=7, first_name="Demo")

            async def answer(self, *args, **kwargs):
                return None

        async def run_test():
            with (
                patch.object(payment, "cancel_start_reminders", AsyncMock()),
                patch.object(payment, "user_has_active_plan", AsyncMock(return_value=False)),
                patch.object(payment, "log_payment_started", AsyncMock()),
                patch.object(payment, "clear_plan_interest", AsyncMock()),
                patch.object(payment, "get_user_referral_info", AsyncMock(return_value={"referral_discount": 0})),
                patch.object(payment, "get_plan", AsyncMock(return_value={
                    "name": "Gold",
                    "price": "199",
                    "validity": "30 days",
                    "access_link": "https://example.com/access",
                })),
                patch.object(payment, "get_setting", AsyncMock(return_value="automatic")),
                patch.object(database, "get_active_order_for_user_plan", AsyncMock(side_effect=AssertionError("reused order should not be checked"))),
                patch.object(payment, "_send_payment_screen", AsyncMock(return_value="ORD-NEW-1")) as send_screen,
            ):
                await payment.callback_buy(DummyCall(), bot=AsyncMock())
            send_screen.assert_awaited_once()

        asyncio.run(run_test())

    def test_pending_transition_accepts_naive_mongo_expiry_as_utc(self):
        orders = AsyncMock()
        orders.find_one = AsyncMock(return_value={
            "payment_status": "created",
            "expires_at": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
        })
        orders.update_one = AsyncMock(return_value=SimpleNamespace(modified_count=1))

        async def transition():
            with patch.object(database, "_orders", orders):
                return await database.update_order_status("ORD-NAIVE-EXPIRY", "pending")

        self.assertTrue(asyncio.run(transition()))
        self.assertEqual(
            orders.update_one.await_args.args[0],
            {"_id": "ORD-NAIVE-EXPIRY", "payment_status": "created"},
        )

    def test_approval_accepts_pending_order_with_naive_mongo_expiry(self):
        order = {
            "_id": "ORD-APPROVE-NAIVE",
            "user_id": 7,
            "plan_name": "Gold",
            "plan_validity": "30 days",
            "access_link": "https://example.com/access",
            "payment_status": "pending",
            "expires_at": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            "referral_discount_used": 0,
        }
        orders = AsyncMock()
        orders.find_one = AsyncMock(return_value=order)
        orders.find_one_and_update = AsyncMock(return_value=order)

        async def approve():
            with (
                patch.object(database, "_orders", orders),
                patch.object(database, "cancel_reminder", AsyncMock()),
                patch.object(database, "consume_referral_discount", AsyncMock()),
            ):
                return await database.approve_order(order["_id"], expected_user_id=7)

        result = asyncio.run(approve())
        self.assertIsNotNone(result)
        self.assertEqual(result["user_id"], 7)
        self.assertEqual(orders.find_one_and_update.await_args.args[0]["user_id"], 7)


if __name__ == "__main__":
    unittest.main()