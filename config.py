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

# FamApp payment verification configuration (environment-based only; no secrets are hard-coded)
# These values match the upstream FamApp repo's actual .env contract, but the runtime keeps
# the implementation minimal by requiring only the keys needed for the automatic FamApp flow.
def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default

FAMAPP_UPI_ID: str = os.getenv("FAMAPP_UPI_ID", "").strip() or os.getenv("DEFAULT_UPI_ID", "").strip()
FAMAPP_PAYEE_NAME: str = os.getenv("FAMAPP_PAYEE_NAME", "").strip() or os.getenv("DEFAULT_PAYEE_NAME", "Project Stack").strip() or "Project Stack"
DEFAULT_UPI_ID: str = FAMAPP_UPI_ID
DEFAULT_PAYEE_NAME: str = FAMAPP_PAYEE_NAME
PURPOSE_PREFIX: str = os.getenv("FAMAPP_PURPOSE_PREFIX", "FAP").strip().upper() or "FAP"
FAMAPP_PURPOSE_PREFIX: str = PURPOSE_PREFIX
BRAND_NAME: str = os.getenv("BRAND_NAME", "Project Stack").strip() or "Project Stack"
ORDER_EXPIRY_MINUTES: int = _safe_int(os.getenv("ORDER_EXPIRY_MINUTES"), 10)
GMAIL_LOOKBACK_HOURS: int = _safe_int(os.getenv("GMAIL_LOOK_BACK_HOURS") or os.getenv("GMAIL_LOOKBACK_HOURS"), 24)
IMAP_HOST: str = os.getenv("IMAP_HOST", "imap.gmail.com").strip() or "imap.gmail.com"
IMAP_PORT: int = _safe_int(os.getenv("IMAP_PORT"), 993)
IMAP_USERNAME: str = os.getenv("IMAP_USERNAME", "").strip()
IMAP_APP_PASSWORD: str = os.getenv("IMAP_APP_PASSWORD", "").strip()
IMAP_MAILBOX: str = os.getenv("IMAP_MAILBOX", "INBOX").strip() or "INBOX"
IMAP_SENDER_FILTER: str = (os.getenv("IMAP_SENDER_FILTER") or "no-reply@famapp.in").strip() or "no-reply@famapp.in"


def get_famapp_runtime_config() -> dict[str, str | int]:
    """Return the configured FamApp runtime settings.

    The automatic FamApp verification path is lazy-validated so the bot can keep starting
    while the provider is still being transitioned. Only the required credentials are enforced.
    """
    config = {
        "famapp_upi_id": FAMAPP_UPI_ID,
        "famapp_payee_name": FAMAPP_PAYEE_NAME,
        "purpose_prefix": PURPOSE_PREFIX,
        "brand_name": BRAND_NAME,
        "order_expiry_minutes": ORDER_EXPIRY_MINUTES,
        "gmail_look_back_hours": GMAIL_LOOKBACK_HOURS,
        "imap_host": IMAP_HOST,
        "imap_port": IMAP_PORT,
        "imap_username": IMAP_USERNAME,
        "imap_app_password": IMAP_APP_PASSWORD,
        "imap_mailbox": IMAP_MAILBOX,
        "imap_sender_filter": IMAP_SENDER_FILTER,
    }

    missing = [
        key
        for key, value in {
            "famapp_upi_id": FAMAPP_UPI_ID,
            "famapp_payee_name": FAMAPP_PAYEE_NAME,
            "imap_username": IMAP_USERNAME,
            "imap_app_password": IMAP_APP_PASSWORD,
        }.items()
        if not str(value).strip()
    ]
    if missing:
        raise ValueError(
            "Missing required FamApp environment configuration: " + ", ".join(missing)
        )
    return config
