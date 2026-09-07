import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

import database  # noqa: E402
from handlers.admin import _user_contact_link  # noqa: E402


class AdminUsersTests(unittest.TestCase):
    def test_username_contact_link_uses_display_name(self):
        link = _user_contact_link({"user_id": 123, "first_name": "John Doe", "username": "john"})
        self.assertEqual(link, '<a href="https://t.me/john">John Doe</a>')

    def test_contact_link_falls_back_to_telegram_id(self):
        link = _user_contact_link({"user_id": 123, "first_name": "John", "username": None})
        self.assertEqual(link, '<a href="tg://user?id=123">John</a>')

    def test_contact_link_escapes_display_name(self):
        link = _user_contact_link({"user_id": 123, "first_name": '<John & "Jane">', "username": "john"})
        self.assertEqual(
            link,
            '<a href="https://t.me/john">&lt;John &amp; &quot;Jane&quot;&gt;</a>',
        )

    def test_user_info_reads_current_profile_fields(self):
        users = AsyncMock()
        users.find_one = AsyncMock(return_value={
            "_id": 123,
            "first_name": "John",
            "username": "john",
            "joined_at": "joined",
            "referral_discount": 99,
        })

        async def lookup():
            with patch.object(database, "_users", users):
                return await database.get_user_info(123)

        result = asyncio.run(lookup())
        self.assertEqual(result["user_id"], 123)
        self.assertEqual(result["first_name"], "John")
        self.assertEqual(result["username"], "john")
        self.assertEqual(result["joined_at"], "joined")
        projection = users.find_one.await_args.args[1]
        self.assertNotIn("referral_discount", projection)


if __name__ == "__main__":
    unittest.main()