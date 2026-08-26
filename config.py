import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set. Check your .env file.")

SOURCE_CHANNEL_ID: str = os.getenv("SOURCE_CHANNEL_ID", "-1004483132545")

SUPPORT_GROUP_URL: str = os.getenv("SUPPORT_GROUP_URL", "https://t.me/+a0bn6M0FzXUxNmY9")

# Channel where new-user and activity events are logged.
LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "-1003923230922"))

# Comma-separated list of Telegram user IDs that can access /admin.
# Example: ADMIN_IDS=123456789,987654321
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {
    int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().isdigit()
}

if not ADMIN_IDS:
    raise ValueError(
        "ADMIN_IDS is not set or contains no valid user IDs.\n"
        "Set it to a comma-separated list of Telegram user IDs that should have admin access.\n"
        "Example: ADMIN_IDS=123456789,987654321"
    )

# VC Store payment verification API
VC_API_KEY: str = os.getenv("VC_API_KEY", "")
VC_API_URL: str = os.getenv("VC_API_URL", "https://vcapi.vcstore.site/payment_api.php")
