"""
database.py — MongoDB (Motor / async) data layer.

Migrated from SQLite (aiosqlite) to MongoDB Atlas. All function names and
return shapes are kept identical to the previous SQLite implementation so
every handler module continues to work unmodified.

Collections (auto-created on first write — no manual setup needed):
  users            — Telegram users (​_id = telegram user id)
  plans            — subscription plans (_id = auto-increment int, exposed as "id")
  orders           — payment orders (_id = order_id string)
  settings         — admin-editable key/value config (_id = key)
  counters         — internal auto-increment sequence tracker

Connection:
  URI is read exclusively from the MONGODB_URI environment variable.
  Database name is fixed: "premium_bot".
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

logger = logging.getLogger(__name__)

MONGODB_URI = os.getenv("MONGODB_URI", "")
if not MONGODB_URI:
    raise ValueError(
        "MONGODB_URI is not set. Add it as an environment secret "
        "(MongoDB Atlas connection string)."
    )

DB_NAME = "premium_bot"

# ── Motor client (created once, reused for the whole process) ─────────────────

_client = AsyncIOMotorClient(MONGODB_URI)
_db = _client[DB_NAME]

_users = _db["users"]
_plans = _db["plans"]
_orders = _db["orders"]
_settings = _db["settings"]
_counters = _db["counters"]
_reminders = _db["reminders"]


# ── Settings defaults (seeded once; admin can change via /admin → Settings) ───

_DEFAULT_WELCOME = (
    "🎉 <b>Welcome to Premium Bot!</b>\n\n"
    "✨ Get exclusive access to premium content\n"
    "💰 Affordable plans available\n"
    "✨ Daily New Uploads\n"
    "✨ High Quality Content\n\n"
    "✨ <b>SELECT A PLAN TO GET STARTED</b> ✨"
)

_DEFAULT_PAYMENT = (
    "💳 <b>Payment Details</b>\n\n"
    "📦 <b>Plan:</b> {plan_name}\n"
    "{price_section}\n"
    "⌛ <b>Validity:</b> {plan_validity}\n\n"
    "📲 Scan the QR code above using any UPI app.\n\n"
    "✅ <b>Pay ₹{final_price_str}</b> by scanning the <b>QR Code</b> above.\n"
    "✅ After paying, tap <b>Check Payment Status</b> — your plan unlocks instantly once the payment is confirmed.\n\n"
    "🆔 <b>Order:</b> #{order_id}"
)

# Old default kept only for the one-time migration below.
_OLD_DEFAULT_PAYMENT = (
    "💳 <b>Payment Details</b>\n\n"
    "📦 <b>Plan:</b> {plan_name}\n"
    "💰 <b>Amount:</b> ₹{plan_price}\n"
    "⌛ <b>Validity:</b> {plan_validity}\n\n"
    "📲 Scan the QR code above using any UPI app.\n\n"
    "✓ Pay ₹{plan_price} to the UPI ID shown.\n"
    "✓ After payment, click <b>✅ I Have Paid</b>\n\n"
    "🆔 <b>Order:</b> #{order_id}"
)

_DEFAULT_REMINDER_FIRST_MESSAGE = (
    "🔔 <b>Reminder</b>\n\n"
    "You still haven't completed payment for <b>{plan_name}</b>.\n\n"
    "💰 <b>Price:</b> ₹{plan_price}\n"
    "⏳ <b>Validity:</b> {plan_validity}\n\n"
    "Tap <b>Buy Now</b> below and complete your payment to activate your plan!"
)

_DEFAULT_REMINDER_SECOND_MESSAGE = (
    "🔔 <b>Last Chance!</b>\n\n"
    "Your payment for <b>{plan_name}</b> is still incomplete.\n\n"
    "💰 <b>Price:</b> ₹{plan_price}\n"
    "⏳ <b>Validity:</b> {plan_validity}\n\n"
    "Tap <b>Buy Now</b> below before this offer slips away!"
)


# ── Schema / index initialisation ─────────────────────────────────────────────

async def init_db() -> None:
    """
    Ensure indexes exist and seed default settings. Collections themselves
    are auto-created by MongoDB on first insert, so nothing else is required.
    This is safe to call on every startup — it never resets existing data.
    """
    # Helpful indexes (no-ops if they already exist)
    await _orders.create_index("payment_status")
    await _orders.create_index("user_id")
    await _orders.create_index("subscription_end")
    await _reminders.create_index("first_due")
    await _reminders.create_index("second_due")

    # Backfill sort_order for legacy plans (created before plan ordering
    # existed) so every plan has one, without disturbing admin-set orders.
    await _ensure_plan_sort_order()

    # Make sure the plan sort-order counter starts at least past the highest
    # sort_order already in use, so newly-created plans can never collide
    # with an existing position. $max is idempotent/safe if init_db runs
    # concurrently (e.g. multiple workers starting up at once).
    top_plan = await _plans.find_one({}, sort=[("sort_order", -1)])
    seed = (top_plan["sort_order"] + 1) if top_plan else 0
    await _counters.update_one(
        {"_id": "plan_sort_order"},
        {"$max": {"seq": seed}},
        upsert=True,
    )

    # Seed default settings only if they don't already exist —
    # existing admin-edited values are never overwritten.
    _defaults = [
        ("welcome_message",          _DEFAULT_WELCOME),
        ("payment_message",          _DEFAULT_PAYMENT),
        ("qr_image",                 ""),   # falls back to QR_IMAGE_URL env var when empty
        ("support_group_url",        ""),   # falls back to SUPPORT_GROUP_URL env var when empty
        ("start_demo_enabled",       "0"),  # disabled until admin explicitly enables
        ("start_demo_ids",           "[]"), # JSON-encoded list of message IDs
        ("start_demo_source",        ""),   # source channel for start demo videos
        ("reminder_enabled",         "1"),  # abandoned-payment reminders on by default
        ("reminder_first_delay_min", "15"),
        ("reminder_second_delay_min", "1440"),  # 24 hours
        ("reminder_first_message",   _DEFAULT_REMINDER_FIRST_MESSAGE),   # 15-minute reminder
        ("reminder_second_message",  _DEFAULT_REMINDER_SECOND_MESSAGE),  # 24-hour reminder
        ("referral_enabled",         "1"),   # referral system on by default
        ("referral_reward_pct",      "5"),   # discount % awarded per referral
        ("max_referral_discount",    "100"), # maximum total discount a user can earn
        ("max_referrals",            "0"),   # max referrals per referrer (0 = unlimited)
        ("payment_mode",             "automatic"),  # "automatic" or "manual"
        ("manual_payment_qr",        ""),    # QR image URL for manual payment mode
        ("manual_upi_text",          ""),    # UPI ID / text shown in manual payment mode
    ]
    for key, value in _defaults:
        await _settings.update_one(
            {"_id": key},
            {"$setOnInsert": {"value": value}},
            upsert=True,
        )

    # Ensure the plans auto-increment counter exists without resetting it.
    await _counters.update_one(
        {"_id": "plans"},
        {"$setOnInsert": {"seq": 0}},
        upsert=True,
    )

    # One-time migration: replace the old payment_message template (which had
    # "Amount:" and plain-text pay line) with the new one that uses {price_section}
    # and {final_price_str}. Only runs if the stored value still matches the old
    # default exactly, so admin-customised templates are never overwritten.
    old_doc = await _settings.find_one({"_id": "payment_message"})
    if old_doc and old_doc.get("value") == _OLD_DEFAULT_PAYMENT:
        await _settings.update_one(
            {"_id": "payment_message"},
            {"$set": {"value": _DEFAULT_PAYMENT}},
        )
        logger.info("Migrated payment_message to new price_section template")

    # One-time migration: replace "✓ After payment, click ✅ I Have Paid" line
    # with the new "Check Payment Status" wording. Only patches the stored value
    # if it still contains the old line, so admin-customised templates are safe.
    _OLD_PAID_LINE = "✓ After payment, click <b>✅ I Have Paid</b>"
    _NEW_PAID_LINE = "✅ After paying, tap <b>Check Payment Status</b> — your plan unlocks instantly once the payment is confirmed."
    pm_doc = await _settings.find_one({"_id": "payment_message"})
    if pm_doc and _OLD_PAID_LINE in (pm_doc.get("value") or ""):
        updated_value = (pm_doc["value"]).replace(_OLD_PAID_LINE, _NEW_PAID_LINE)
        await _settings.update_one(
            {"_id": "payment_message"},
            {"$set": {"value": updated_value}},
        )
        logger.info("Migrated payment_message: updated 'I Have Paid' line to 'Check Payment Status'")

    logger.info("MongoDB connected — database %r ready", DB_NAME)


async def _next_plan_id() -> int:
    """Atomically increment and return the next integer plan id."""
    doc = await _counters.find_one_and_update(
        {"_id": "plans"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return doc["seq"]


async def _next_plan_sort_order() -> int:
    """
    Atomically increment and return the next sort_order value, used to append
    newly-created plans at the end of the display order without racing other
    concurrent plan creations.
    """
    doc = await _counters.find_one_and_update(
        {"_id": "plan_sort_order"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return doc["seq"]


# ── Users ─────────────────────────────────────────────────────────────────────

async def save_user(user_id: int, username: str | None, first_name: str) -> bool:
    """Upsert the user record. Returns True if the user is brand-new."""
    existing = await _users.find_one({"_id": user_id}, {"_id": 1})
    is_new = existing is None
    await _users.update_one(
        {"_id": user_id},
        {
            "$set": {"username": username, "first_name": first_name},
            "$setOnInsert": {
                "joined_at":                 datetime.now(timezone.utc),
                "referral_code":             user_id,   # referral_code == user_id
                "referred_by":               None,
                "total_referrals":           0,
                "referral_discount":         0,
                "referral_reminder_sent":    False,
                "referral_reminder_due_at":  None,
                "plan_interest_plan_id":     None,
                "plan_interest_due_at":      None,
                "plan_interest_sent":        False,
            },
        },
        upsert=True,
    )
    return is_new


async def schedule_referral_reminder(user_id: int, due_at: datetime) -> None:
    """Schedule a one-time referral reminder for a new user.
    No-op if the reminder has already been sent or scheduled."""
    await _users.update_one(
        {"_id": user_id, "referral_reminder_sent": False},
        {"$set": {"referral_reminder_due_at": due_at}},
    )


async def get_due_referral_reminders(now: datetime) -> list[int]:
    """Return user_ids whose referral reminder is due and not yet sent."""
    cursor = _users.find(
        {
            "referral_reminder_sent":   False,
            "referral_reminder_due_at": {"$ne": None, "$lte": now},
        },
        {"_id": 1},
    )
    return [doc["_id"] async for doc in cursor]


async def mark_referral_reminder_sent(user_id: int) -> None:
    """Mark the referral reminder as sent so it is never sent again."""
    await _users.update_one(
        {"_id": user_id},
        {"$set": {"referral_reminder_sent": True}},
    )


async def save_plan_interest(user_id: int, plan_id: int, due_at: datetime) -> None:
    """Record the last plan the user viewed without buying.

    Overwrites any previous interest so that viewing a different plan always
    replaces the old one — the reminder always fires for the most recent plan.
    Resets plan_interest_sent to False so a fresh reminder can be scheduled.
    """
    await _users.update_one(
        {"_id": user_id},
        {"$set": {
            "plan_interest_plan_id": plan_id,
            "plan_interest_due_at":  due_at,
            "plan_interest_sent":    False,
        }},
    )


async def clear_plan_interest(user_id: int) -> None:
    """Suppress the plan-interest reminder when the user clicks Buy Now.

    Sets plan_interest_sent to True so the scheduler skips this user even if
    the due timestamp has not yet elapsed.  Does not clear plan_interest_plan_id
    so the stored plan is still available for auditing.
    """
    await _users.update_one(
        {"_id": user_id},
        {"$set": {"plan_interest_sent": True}},
    )


async def get_due_plan_interest_reminders(now: datetime) -> list[dict]:
    """Return users whose plan-interest reminder is due and not yet sent.

    Each item is {"user_id": int, "plan_id": int}.
    """
    cursor = _users.find(
        {
            "plan_interest_sent":    False,
            "plan_interest_plan_id": {"$ne": None},
            "plan_interest_due_at":  {"$ne": None, "$lte": now},
        },
        {"_id": 1, "plan_interest_plan_id": 1},
    )
    return [
        {"user_id": doc["_id"], "plan_id": doc["plan_interest_plan_id"]}
        async for doc in cursor
    ]


async def mark_plan_interest_sent(user_id: int) -> None:
    """Mark the plan-interest reminder as sent so it never fires again."""
    await _users.update_one(
        {"_id": user_id},
        {"$set": {"plan_interest_sent": True}},
    )


async def has_any_approved_order(user_id: int) -> bool:
    """Return True if the user has at least one approved (purchased) order."""
    doc = await _orders.find_one(
        {"user_id": user_id, "payment_status": "approved"},
        {"_id": 1},
    )
    return doc is not None


async def save_referral(user_id: int, referrer_id: int) -> bool:
    """
    Record that user_id was referred by referrer_id.

    Guards:
    - user must be new (referred_by is None — set only on first call)
    - not self-referral (user_id != referrer_id)
    - referral not already counted (atomic $setOnInsert equivalent via filter)

    Returns True if the referral was successfully recorded.
    """
    if user_id == referrer_id:
        return False

    # Honour the admin toggle — do not count new referrals when disabled.
    if (await get_setting("referral_enabled", "1")) != "1":
        return False

    # Atomically set referred_by only if it is still None (first referral wins).
    result = await _users.update_one(
        {"_id": user_id, "referred_by": None},
        {"$set": {"referred_by": referrer_id}},
    )
    if result.modified_count == 0:
        return False  # already had a referrer, or user not found

    # Read config values once.
    reward_pct   = max(1, int((await get_setting("referral_reward_pct",   "5"))   or 5))
    max_discount = max(1, int((await get_setting("max_referral_discount", "100")) or 100))
    max_refs     =         int((await get_setting("max_referrals",        "0"))   or 0)

    # Fetch the referrer's current counters.
    referrer_doc = await _users.find_one(
        {"_id": referrer_id},
        {"total_referrals": 1, "referral_discount": 1},
    )
    if not referrer_doc:
        logger.info("Referral recorded (referrer %s not found in DB)", referrer_id)
        return True  # new-user link still saved even if referrer is gone

    current_referrals = int(referrer_doc.get("total_referrals",   0))
    current_discount  = int(referrer_doc.get("referral_discount", 0))

    # Honour max_referrals limit (0 = unlimited).
    if max_refs > 0 and current_referrals >= max_refs:
        logger.info(
            "Referral limit reached for referrer %s (%d/%d) — no credit added",
            referrer_id, current_referrals, max_refs,
        )
        return True  # referred_by already recorded for the new user; referrer just gets no more credit

    new_referrals = current_referrals + 1
    new_discount  = min(current_discount + reward_pct, max_discount)

    await _users.update_one(
        {"_id": referrer_id},
        {"$set": {"total_referrals": new_referrals, "referral_discount": new_discount}},
    )
    logger.info(
        "Referral recorded: user %s referred by %s → referrals=%d discount=%d%%",
        user_id, referrer_id, new_referrals, new_discount,
    )
    return True


async def admin_add_referrals(user_id: int, count: int) -> dict | None:
    """
    Admin helper: manually credit `count` referrals to a user.
    Uses the same referral_reward_pct, max_referral_discount, and max_referrals
    settings as the live referral system.
    Does NOT trigger notifications or affect global referral statistics.
    Returns updated {total_referrals, referral_discount}, or None if user not found.
    """
    doc = await _users.find_one(
        {"_id": user_id},
        {"total_referrals": 1, "referral_discount": 1},
    )
    if doc is None:
        return None

    reward_pct   = max(1, int((await get_setting("referral_reward_pct",   "5"))   or 5))
    max_discount = max(1, int((await get_setting("max_referral_discount", "100")) or 100))
    max_refs     =         int((await get_setting("max_referrals",        "0"))   or 0)

    current_referrals = int(doc.get("total_referrals",   0))
    current_discount  = int(doc.get("referral_discount", 0))

    # Respect max_referrals — admin cannot exceed the configured limit either.
    if max_refs > 0:
        allowed = max(0, max_refs - current_referrals)
        count = min(count, allowed)

    new_referrals = current_referrals + count
    new_discount  = min(current_discount + count * reward_pct, max_discount)

    await _users.update_one(
        {"_id": user_id},
        {"$set": {
            "total_referrals":   new_referrals,
            "referral_discount": new_discount,
        }},
    )
    logger.info(
        "Admin manually added %d referrals to user %s → referrals=%d discount=%d%%",
        count, user_id, new_referrals, new_discount,
    )
    return {"total_referrals": new_referrals, "referral_discount": new_discount}


async def get_referral_stats() -> dict:
    """Return referral statistics and config for the admin referral settings panel."""
    enabled = (await get_setting("referral_enabled", "1")) == "1"
    total_referrers = await _users.count_documents({"total_referrals": {"$gt": 0}})
    agg_cursor = _users.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$total_referrals"}}},
    ])
    agg_docs = await agg_cursor.to_list(length=1)
    total_referrals = int(agg_docs[0]["total"]) if agg_docs else 0
    reward_pct   = int((await get_setting("referral_reward_pct",   "5"))   or 5)
    max_discount = int((await get_setting("max_referral_discount", "100")) or 100)
    max_refs     = int((await get_setting("max_referrals",         "0"))   or 0)
    return {
        "enabled":          enabled,
        "total_referrers":  total_referrers,
        "total_referrals":  total_referrals,
        "reward_pct":       reward_pct,
        "max_discount":     max_discount,
        "max_referrals":    max_refs,
    }


async def reset_referral_data() -> None:
    """Reset all referral counters and discounts for every user. Keeps referral_code."""
    await _users.update_many(
        {},
        {"$set": {
            "referred_by":       None,
            "total_referrals":   0,
            "referral_discount": 0,
        }},
    )
    logger.info("All referral data reset by admin")


async def get_all_user_ids() -> list[int]:
    """Return every registered user ID (for broadcast)."""
    cursor = _users.find({}, {"_id": 1})
    return [doc["_id"] async for doc in cursor]


async def get_user_referral_info(user_id: int) -> dict:
    """Return referral stats for a user: total_referrals and referral_discount."""
    doc = await _users.find_one(
        {"_id": user_id},
        {"total_referrals": 1, "referral_discount": 1},
    )
    if not doc:
        return {"total_referrals": 0, "referral_discount": 0}
    return {
        "total_referrals":  doc.get("total_referrals", 0),
        "referral_discount": doc.get("referral_discount", 0),
    }


# ── Plans ─────────────────────────────────────────────────────────────────────

def _plan_doc_to_dict(doc: dict) -> dict:
    """Convert a Mongo plan document to the legacy dict shape (id, not _id)."""
    return {
        "id":                 doc["_id"],
        "name":               doc.get("name", ""),
        "price":              doc.get("price", ""),
        "validity":           doc.get("validity", ""),
        "demo_message_ids":   doc.get("demo_message_ids", []),
        "source_channel_id":  doc.get("source_channel_id", ""),
        "access_link":        doc.get("access_link", ""),
        "buy_message":        doc.get("buy_message", ""),
        "qr_image":           doc.get("qr_image", ""),
        "sort_order":         doc.get("sort_order", 0),
        "created_at":         doc.get("created_at"),
    }


async def _ensure_plan_sort_order() -> None:
    """
    Backfill the `sort_order` field for legacy plans that don't have it yet
    (ordered by their original creation order, i.e. _id). Idempotent and
    never touches plans that already have a sort_order.
    """
    cursor = _plans.find({"sort_order": {"$exists": False}}).sort("_id", 1)
    missing_ids = [doc["_id"] async for doc in cursor]
    if not missing_ids:
        return
    top = await _plans.find_one({"sort_order": {"$exists": True}}, sort=[("sort_order", -1)])
    next_order = (top["sort_order"] + 1) if top else 0
    for pid in missing_ids:
        await _plans.update_one({"_id": pid}, {"$set": {"sort_order": next_order}})
        next_order += 1


async def create_plan(
    name: str,
    price: str,
    validity: str,
    demo_message_ids: list[int],
    source_channel_id: str,
    access_link: str,
) -> int:
    """Insert a new plan, appended to the end of the display order. Returns the new plan's id."""
    plan_id = await _next_plan_id()
    sort_order = await _next_plan_sort_order()
    await _plans.insert_one({
        "_id":               plan_id,
        "name":              name,
        "price":             price,
        "validity":          validity,
        "demo_message_ids":  list(demo_message_ids or []),
        "source_channel_id": source_channel_id,
        "access_link":       access_link,
        "sort_order":        sort_order,
        "created_at":        datetime.now(timezone.utc),
    })
    logger.info("Created plan id=%s name=%r", plan_id, name)
    return plan_id


async def get_all_plans() -> list[dict]:
    """Return all plans as a list of dicts, ordered by their display order."""
    cursor = _plans.find({}).sort([("sort_order", 1), ("_id", 1)])
    return [_plan_doc_to_dict(doc) async for doc in cursor]


async def get_plan(plan_id: int) -> dict | None:
    """Return a single plan dict or None."""
    doc = await _plans.find_one({"_id": plan_id})
    return _plan_doc_to_dict(doc) if doc else None


async def update_plan(plan_id: int, **fields) -> None:
    """Update any subset of plan fields."""
    if "demo_message_ids" in fields:
        fields["demo_message_ids"] = list(fields["demo_message_ids"] or [])
    if fields:
        await _plans.update_one({"_id": plan_id}, {"$set": fields})
    logger.info("Updated plan id=%s fields=%s", plan_id, list(fields.keys()))


# ── Plan ordering ─────────────────────────────────────────────────────────────

async def _ordered_plan_ids() -> list[int]:
    """Return all plan ids in current display order."""
    cursor = _plans.find({}, {"_id": 1}).sort([("sort_order", 1), ("_id", 1)])
    return [doc["_id"] async for doc in cursor]


async def _persist_plan_order(ordered_ids: list[int]) -> None:
    """
    Persist sort_order = position for every plan in ordered_ids in a single
    bulk write, so the whole reorder lands as one round trip instead of many
    sequential update_one calls that could interleave with a concurrent
    reorder from another admin.
    """
    if not ordered_ids:
        return
    ops = [
        UpdateOne({"_id": pid}, {"$set": {"sort_order": index}})
        for index, pid in enumerate(ordered_ids)
    ]
    await _plans.bulk_write(ops, ordered=True)


async def move_plan_up(plan_id: int) -> bool:
    """Swap the plan with the one immediately above it. Returns True if moved."""
    ids = await _ordered_plan_ids()
    if plan_id not in ids:
        return False
    idx = ids.index(plan_id)
    if idx == 0:
        return False
    ids[idx - 1], ids[idx] = ids[idx], ids[idx - 1]
    await _persist_plan_order(ids)
    logger.info("Moved plan id=%s up", plan_id)
    return True


async def move_plan_down(plan_id: int) -> bool:
    """Swap the plan with the one immediately below it. Returns True if moved."""
    ids = await _ordered_plan_ids()
    if plan_id not in ids:
        return False
    idx = ids.index(plan_id)
    if idx >= len(ids) - 1:
        return False
    ids[idx + 1], ids[idx] = ids[idx], ids[idx + 1]
    await _persist_plan_order(ids)
    logger.info("Moved plan id=%s down", plan_id)
    return True


async def move_plan_to_top(plan_id: int) -> bool:
    """Move the plan to the first position. Returns True if it actually moved."""
    ids = await _ordered_plan_ids()
    if plan_id not in ids:
        return False
    if ids.index(plan_id) == 0:
        return False  # already at the top — nothing to persist
    ids.remove(plan_id)
    ids.insert(0, plan_id)
    await _persist_plan_order(ids)
    logger.info("Moved plan id=%s to top", plan_id)
    return True


async def move_plan_to_bottom(plan_id: int) -> bool:
    """Move the plan to the last position. Returns True if it actually moved."""
    ids = await _ordered_plan_ids()
    if plan_id not in ids:
        return False
    if ids.index(plan_id) == len(ids) - 1:
        return False  # already at the bottom — nothing to persist
    ids.remove(plan_id)
    ids.append(plan_id)
    await _persist_plan_order(ids)
    logger.info("Moved plan id=%s to bottom", plan_id)
    return True


# ── Start Demo Videos ─────────────────────────────────────────────────────────

async def get_start_demo() -> dict:
    """Return start demo config: {enabled: bool, ids: list[int], source: str}."""
    import json
    enabled = (await get_setting("start_demo_enabled")) == "1"
    raw_ids = (await get_setting("start_demo_ids")) or "[]"
    source  = (await get_setting("start_demo_source")) or ""
    try:
        ids = [int(x) for x in json.loads(raw_ids)]
    except Exception:
        ids = []
    return {"enabled": enabled, "ids": ids, "source": source}


async def set_start_demo_ids(ids: list[int], source_channel_id: str) -> None:
    """Persist the start demo message IDs and their source channel."""
    import json
    await set_setting("start_demo_ids", json.dumps(ids))
    await set_setting("start_demo_source", source_channel_id)


async def delete_plan(plan_id: int) -> None:
    await _plans.delete_one({"_id": plan_id})
    logger.info("Deleted plan id=%s", plan_id)


# ── Orders ────────────────────────────────────────────────────────────────────

async def consume_referral_discount(user_id: int) -> None:
    """Reset a user's referral discount and referral count to 0 after a
    discounted purchase is approved.  The user keeps their referral_code and
    referred_by so future referrals still work and new ones build a fresh
    discount from 0%."""
    await _users.update_one(
        {"_id": user_id},
        {"$set": {"referral_discount": 0, "total_referrals": 0}},
    )
    logger.info("Referral discount consumed for user %s — reset to 0%%", user_id)


async def create_order(
    user_id: int,
    plan_name: str,
    plan_price: str,
    plan_validity: str,
    order_id: str,
    plan_id: int | None = None,
    access_link: str = "",
    final_price: str = "",
    referral_discount_used: int = 0,
) -> None:
    """Insert a new order row with status 'created'.

    ``referral_discount_used`` records the discount percentage (0–100) that
    was applied to this order.  A non-zero value causes the referral discount
    to be consumed when the order is approved.
    """
    try:
        await _orders.insert_one({
            "_id":                    order_id,
            "user_id":                user_id,
            "plan_name":              plan_name,
            "plan_price":             plan_price,
            "final_price":            final_price or plan_price,
            "plan_validity":          plan_validity,
            "payment_status":         "created",
            "created_at":             datetime.now(timezone.utc),
            "subscription_start":     None,
            "subscription_end":       None,
            "plan_id":                plan_id,
            "access_link":            access_link,
            "referral_discount_used": referral_discount_used,
        })
    except Exception as exc:
        # Preserve the SQLite-era "UNIQUE" signal so callers retrying on
        # order_id collisions (see handlers/payment.py) keep working.
        if "duplicate key" in str(exc).lower() or "E11000" in str(exc):
            raise Exception(f"UNIQUE constraint failed: orders.order_id ({exc})")
        raise
    logger.debug("Created order %s for user %s", order_id, user_id)


async def update_order_status(order_id: str, status: str) -> None:
    await _orders.update_one({"_id": order_id}, {"$set": {"payment_status": status}})
    logger.debug("Order %s → %s", order_id, status)


async def save_order_transaction_id(order_id: str, transaction_id: str) -> None:
    """Persist the VC-assigned transaction_id on the order document."""
    await _orders.update_one(
        {"_id": order_id},
        {"$set": {"transaction_id": transaction_id}},
    )
    logger.debug("Order %s — transaction_id saved: %s", order_id, transaction_id)


async def approve_order(order_id: str) -> dict | None:
    """
    Approve an order:
      - payment_status   → 'approved'
      - subscription_start → now (UTC)
      - subscription_end   → now + plan validity days (UTC)

    Returns a dict with user_id, plan_name, plan_validity, subscription_end, access_link.
    Returns None if the order does not exist or is not in 'pending' state.
    """
    # Peek at plan_validity first so we know the subscription length; the actual
    # state transition below is atomic on (_id, payment_status='pending') so two
    # concurrent approvals of the same order can't both succeed.
    peek = await _orders.find_one({"_id": order_id, "payment_status": "pending"})
    if not peek:
        return None

    try:
        days = int(str(peek["plan_validity"]).strip().split()[0])
    except (ValueError, IndexError):
        days = 30

    now = datetime.now(timezone.utc)
    sub_end = now + timedelta(days=days)

    # Atomic compare-and-set: only succeeds if the order is still 'pending' at
    # the moment of the update, preventing a double-approve race.
    doc = await _orders.find_one_and_update(
        {"_id": order_id, "payment_status": "pending"},
        {"$set": {
            "payment_status":     "approved",
            "subscription_start": now.isoformat(),
            "subscription_end":   sub_end.isoformat(),
        }},
    )
    if not doc:
        # Another concurrent call already approved/changed this order.
        return None

    logger.info("Order %s approved; sub ends %s", order_id, sub_end.date())

    # Payment is confirmed — cancel the reminder scheduled for THIS order only
    # (scoped by order_id so a newer schedule from a repeat Buy Now isn't
    # accidentally wiped if an older order gets approved late).
    await cancel_reminder(doc["user_id"], order_id)

    # Consume the referral discount if one was applied to this order.
    # Only an approved payment triggers consumption; pending/rejected/cancelled
    # orders never reach this branch.
    if int(doc.get("referral_discount_used", 0) or 0) > 0:
        await consume_referral_discount(doc["user_id"])

    return {
        "user_id":          doc["user_id"],
        "plan_name":        doc["plan_name"],
        "plan_validity":    doc["plan_validity"],
        "subscription_end": sub_end,
        "access_link":      doc.get("access_link") or "",
    }


async def get_pending_orders() -> list[dict]:
    """Return all orders with payment_status='pending'."""
    cursor = _orders.find(
        {"payment_status": "pending"},
        {"_id": 1, "user_id": 1, "plan_name": 1, "plan_price": 1, "final_price": 1, "created_at": 1},
    ).sort("created_at", -1)
    result = []
    async for doc in cursor:
        plan_price = doc.get("plan_price", "—")
        result.append({
            "order_id":    doc["_id"],
            "user_id":     doc.get("user_id"),
            "plan_name":   doc.get("plan_name"),
            "plan_price":  plan_price,
            "final_price": doc.get("final_price") or plan_price,
            "created_at":  doc.get("created_at"),
        })
    return result


async def get_order(order_id: str) -> dict | None:
    """Return a full order document as a plain dict, or None if not found."""
    doc = await _orders.find_one({"_id": order_id})
    if doc is None:
        return None
    return {
        "order_id":      doc["_id"],
        "user_id":       doc.get("user_id"),
        "plan_name":     doc.get("plan_name", ""),
        "plan_price":    doc.get("plan_price", ""),
        "final_price":   doc.get("final_price") or doc.get("plan_price", ""),
        "plan_validity": doc.get("plan_validity", ""),
        "plan_id":       doc.get("plan_id"),
        "access_link":   doc.get("access_link", ""),
        "payment_status": doc.get("payment_status", ""),
    }


async def get_order_final_price(order_id: str) -> str | None:
    """Return the stored final_price for an order, falling back to plan_price.
    Returns None if the order does not exist."""
    doc = await _orders.find_one(
        {"_id": order_id},
        {"plan_price": 1, "final_price": 1},
    )
    if doc is None:
        return None
    return doc.get("final_price") or doc.get("plan_price", "—")


async def user_has_active_plan(user_id: int, plan_id: int) -> bool:
    """Return True if the user has an approved, still-valid subscription for this exact plan."""
    now_iso = datetime.now(timezone.utc).isoformat()
    doc = await _orders.find_one(
        {
            "user_id":        user_id,
            "plan_id":        plan_id,
            "payment_status": "approved",
            "subscription_end": {"$gte": now_iso},
        },
        {"_id": 1},
    )
    return doc is not None


async def get_user_active_subscription(user_id: int) -> dict | None:
    """Return the most recent approved/active order for a user, or None."""
    now_iso = datetime.now(timezone.utc).isoformat()
    doc = await _orders.find_one(
        {
            "user_id": user_id,
            "payment_status": "approved",
            "subscription_end": {"$gte": now_iso},
        },
        sort=[("subscription_end", -1)],
    )
    if not doc:
        return None
    return {
        "order_id":           doc["_id"],
        "plan_name":          doc.get("plan_name"),
        "plan_price":         doc.get("plan_price"),
        "plan_validity":      doc.get("plan_validity"),
        "subscription_start": doc.get("subscription_start"),
        "subscription_end":   doc.get("subscription_end"),
    }


# ── Settings ─────────────────────────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    """Return the value for a settings key, or default if not found."""
    doc = await _settings.find_one({"_id": key})
    return doc["value"] if doc else default


async def set_setting(key: str, value: str) -> None:
    """Upsert a settings key-value pair."""
    await _settings.update_one(
        {"_id": key},
        {"$set": {"value": value}},
        upsert=True,
    )
    logger.info("Setting %r updated", key)


async def get_all_settings() -> dict[str, str]:
    """Return all settings as a plain dict."""
    cursor = _settings.find({})
    return {doc["_id"]: doc.get("value", "") async for doc in cursor}


# ── Abandoned Payment Reminders ─────────────────────────────────────────────

async def set_pending_reminder(
    user_id: int,
    order_id: str,
    plan_id: int,
    plan_name: str,
    plan_price: str,
    plan_validity: str,
    first_due: datetime,
    second_due: datetime,
) -> None:
    """
    Create or replace the reminder schedule for a user (one active schedule
    per user — a repeat Buy Now replaces the previous schedule instead of
    creating a duplicate). plan_id is kept so the reminder's Buy Now button
    can resume the flow for the exact plan it was scheduled for.
    """
    await _reminders.replace_one(
        {"_id": user_id},
        {
            "_id":           user_id,
            "order_id":      order_id,
            "plan_id":       plan_id,
            "plan_name":     plan_name,
            "plan_price":    plan_price,
            "plan_validity": plan_validity,
            "first_due":     first_due,
            "second_due":    second_due,
            "first_sent":    False,
            "second_sent":   False,
            "created_at":    datetime.now(timezone.utc),
        },
        upsert=True,
    )
    logger.debug("Reminder schedule set for user %s (order %s)", user_id, order_id)


async def cancel_reminder(user_id: int, order_id: str | None = None) -> None:
    """
    Cancel (delete) the scheduled reminder for a user.

    If order_id is given, only cancel when the stored schedule still belongs
    to that order — otherwise a newer schedule created by a repeat Buy Now
    (which replaces by user_id) could be deleted by a stale caller acting on
    an older order (e.g. approving an old order after the user re-bought).
    """
    query: dict = {"_id": user_id}
    if order_id is not None:
        query["order_id"] = order_id
    await _reminders.delete_one(query)


async def get_due_first_reminders(now: datetime) -> list[dict]:
    """Return reminder docs whose first reminder is due and not yet sent."""
    cursor = _reminders.find({"first_sent": False, "first_due": {"$lte": now}})
    return [doc async for doc in cursor]


async def mark_first_reminder_sent(user_id: int, order_id: str) -> None:
    """
    Flag the first reminder as sent, scoped to the order it was scheduled
    for — if the user replaced the schedule (repeat Buy Now) in the meantime,
    this is a no-op and the new schedule is left untouched.
    """
    await _reminders.update_one(
        {"_id": user_id, "order_id": order_id},
        {"$set": {"first_sent": True}},
    )


async def get_due_second_reminders(now: datetime) -> list[dict]:
    """Return reminder docs whose final reminder is due and not yet sent."""
    cursor = _reminders.find({"second_sent": False, "second_due": {"$lte": now}})
    return [doc async for doc in cursor]


async def mark_second_reminder_sent(user_id: int, order_id: str) -> None:
    """
    The schedule is complete after the final reminder — remove it, scoped to
    the order it was scheduled for (see mark_first_reminder_sent).
    """
    await _reminders.delete_one({"_id": user_id, "order_id": order_id})


# ── Statistics ────────────────────────────────────────────────────────────────

async def get_stats() -> dict:
    """Return a stats dict for the admin statistics panel."""
    now_iso = datetime.now(timezone.utc).isoformat()

    total_users = await _users.count_documents({})
    total_plans = await _plans.count_documents({})

    active_subs = await _orders.count_documents({
        "payment_status": "approved",
        "subscription_end": {"$gte": now_iso},
    })
    expired_subs = await _orders.count_documents({
        "payment_status": "approved",
        "subscription_end": {"$lt": now_iso},
    })
    pending = await _orders.count_documents({"payment_status": "pending"})
    approved_orders = await _orders.count_documents({"payment_status": "approved"})
    rejected_orders = await _orders.count_documents({"payment_status": "rejected"})

    # Revenue: sum plan_price (stored as string) across every approved order.
    # $toDouble handles numeric strings like "99", "199.50"; non-numeric
    # values are coerced to 0 via $convert's onError so the pipeline never raises.
    revenue_cursor = _orders.aggregate([
        {"$match": {"payment_status": "approved"}},
        {"$group": {
            "_id": None,
            "total": {
                "$sum": {
                    "$convert": {
                        "input": "$plan_price",
                        "to": "double",
                        "onError": 0,
                        "onNull": 0,
                    }
                }
            },
        }},
    ])
    revenue_docs = await revenue_cursor.to_list(length=1)
    total_revenue = float(revenue_docs[0]["total"]) if revenue_docs else 0.0

    return {
        "total_users":      total_users,
        "total_plans":      total_plans,
        "active_subs":      active_subs,
        "expired_subs":     expired_subs,
        "pending":          pending,
        "approved_orders":  approved_orders,
        "rejected_orders":  rejected_orders,
        "total_revenue":    total_revenue,
    }
