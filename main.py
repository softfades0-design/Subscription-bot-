import asyncio
import logging
import os
import threading

from flask import Flask
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import BOT_TOKEN
from database import init_db
from handlers import commands
from handlers import start
from handlers import payment
from handlers import admin
from handlers import settings as settings_handler
import reminder_scheduler

logging.basicConfig(level=logging.INFO)

_startup_logger = logging.getLogger(__name__)
_startup_logger.info("RUNNING NEW BUILD")

# ── Flask health-check server ─────────────────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return "Bot is running"


def run_flask() -> None:
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


# ── Telegram bot ──────────────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand(command="start",   description="Start the bot"),
    BotCommand(command="plans",   description="View available plans"),
    BotCommand(command="status",  description="Check your subscription status"),
    BotCommand(command="refer",   description="👥 Refer & Earn"),
    BotCommand(command="help",    description="Help & usage guide"),
    BotCommand(command="contact", description="Contact support"),
    BotCommand(command="admin",   description="Admin panel (admins only)"),
]


async def main() -> None:
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    await bot.delete_webhook(drop_pending_updates=True)

    dp = Dispatcher()

    # Registration order matters for filter priority:
    # admin first (catches /admin and all admin_ callbacks),
    # then commands (/plans, /status, /help, /contact),
    # then payment (buy:, paid:, approve:, reject:, cancel_*),
    # then start (catch-all plan:, back, show_plans, main_menu).
    dp.include_router(admin.router)
    dp.include_router(settings_handler.router)
    dp.include_router(commands.router)
    dp.include_router(payment.router)
    dp.include_router(start.router)

    await bot.set_my_commands(BOT_COMMANDS)

    asyncio.create_task(reminder_scheduler.run(bot))

    await dp.start_polling(bot)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    asyncio.run(main())
