import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

import database  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()