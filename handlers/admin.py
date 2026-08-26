"""
handlers/admin.py — /admin command and full admin panel wizard.

State machine is kept in module-level dicts (no FSM storage needed in main.py).

Steps
─────
add:name         → add:price → add:validity → add:demo → add:access → add:confirm
edit:select      → edit:field → edit:value
delete:select    → delete:confirm
demo:select      → demo:videos  (admin sends media, clicks Done)
link:select      → link:value
buymsg:select    → buymsg:value
startdemo:videos (admin forwards from demo channel, clicks Done)
broadcast:msg
"""

import asyncio
import html
import logging

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from config import ADMIN_IDS, SOURCE_CHANNEL_ID
import handlers.settings as _settings_module  # for cross-module state clearing
from database import (
    create_plan,
    get_all_plans,
    get_plan,
    update_plan,
    delete_plan,
    get_stats,
    get_pending_orders,
    get_all_user_ids,
    get_setting,
    set_setting,
    get_start_demo,
    set_start_demo_ids,
    move_plan_up,
    move_plan_down,
    move_plan_to_top,
    move_plan_to_bottom,
    get_referral_stats,
    reset_referral_data,
    admin_add_referrals,
    _orders,
)
from keyboards.menu import (
    admin_panel_keyboard,
    admin_plan_list_keyboard,
    premium_subscribers_preview_keyboard,
    admin_edit_fields_keyboard,
    admin_delete_confirm_keyboard,
    admin_demo_done_keyboard,
    admin_confirm_save_keyboard,
    main_menu_keyboard,
    start_demo_settings_keyboard,
    start_demo_done_keyboard,
    reminder_settings_keyboard,
    referral_settings_keyboard,
    referral_reset_confirm_keyboard,
    payment_settings_keyboard,
)

logger = logging.getLogger(__name__)
router = Router()

# ── State storage ─────────────────────────────────────────────────────────────
# { user_id: { "step": str, "data": dict } }
_state: dict[int, dict] = {}

_STEPS_LABEL = {
    "add:name":     "📝 Enter the plan name:",
    "add:price":    "💰 Enter the price (e.g. 49):",
    "add:validity": "⏳ Enter validity (e.g. 30 Days):",
    "add:demo":     (
        "🎥 <b>Forward</b> demo messages from the source channel.\n\n"
        "Go to your source channel → select messages → forward them here.\n"
        "When you're done, click <b>✅ Done</b> below."
    ),
    "add:access":   "🔗 Enter the access link (channel/group invite URL):",
}

EDIT_FIELD_LABELS = {
    "name":        "📝 Enter the new plan name:",
    "price":       "💰 Enter the new price (e.g. 49):",
    "validity":    "⏳ Enter new validity (e.g. 30 Days):",
    "access_link": "🔗 Enter the new access link:",
}


# ── Guards ────────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _in_state(step: str):
    """Filter: user must be in the given state step."""
    async def _check(message: Message) -> bool:
        st = _state.get(message.from_user.id)
        return st is not None and st["step"] == step
    return _check


def _in_any_admin_state(message: Message) -> bool:
    return message.from_user.id in _state


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _go_panel(target, bot: Bot | None = None) -> None:
    """Send or edit-to the main admin panel."""
    text = "🛠 <b>ADMIN PANEL</b>\n\nSelect an option:"
    kb = admin_panel_keyboard()
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except Exception:
            await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def _cancel(call: CallbackQuery) -> None:
    _state.pop(call.from_user.id, None)
    await call.answer("Cancelled.")
    await _go_panel(call)


# ── /admin command ────────────────────────────────────────────────────────────

@router.message(Command("admin", ignore_case=True))
async def cmd_admin(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ You are not authorised to use this command.")
        return
    _state.pop(message.from_user.id, None)
    await _go_panel(message)


# ── /cancel — global escape from any admin wizard state ──────────────────────

@router.message(Command("cancel", ignore_case=True))
async def cmd_cancel(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return  # not an admin — let other handlers deal with it
    uid = message.from_user.id
    # Clear state from both admin wizard AND settings panel
    in_admin    = uid in _state
    in_settings = uid in _settings_module._state
    _state.pop(uid, None)
    _settings_module._state.pop(uid, None)
    if in_admin or in_settings:
        await message.answer(
            "✅ Cancelled.",
            reply_markup=admin_panel_keyboard(),
        )
    else:
        await message.answer("No active wizard to cancel.")


# ── Cancel (global) ───────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_cancel")
async def cb_cancel(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await _cancel(call)


# ── Admin panel entry buttons ─────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_stats")
async def cb_stats(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    s = await get_stats()

    # Format revenue: show as integer when it's a whole number, else 2 d.p.
    rev = s["total_revenue"]
    rev_str = f"₹{int(rev):,}" if rev == int(rev) else f"₹{rev:,.2f}"

    await call.message.edit_text(
        "📊 <b>Statistics</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥  <b>Total Users</b>              {s['total_users']}\n"
        f"📦  <b>Total Plans</b>              {s['total_plans']}\n\n"
        f"💰  <b>Total Revenue</b>            {rev_str}\n\n"
        f"✅  <b>Active Subscriptions</b>     {s['active_subs']}\n"
        f"❌  <b>Expired Subscriptions</b>    {s['expired_subs']}\n\n"
        f"⏳  <b>Pending Payments</b>         {s['pending']}\n"
        f"✔️  <b>Approved Orders</b>          {s['approved_orders']}\n"
        f"✖️  <b>Rejected Orders</b>          {s['rejected_orders']}\n"
        "━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(lambda c: c.data == "admin_plans")
async def cb_plans_list(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.edit_text(
            "📦 <b>Plans</b>\n\nNo plans found. Use ➕ Add Plan to create one.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    lines = []
    for p in plans:
        lines.append(
            f"📦 <b>{p['name']}</b>\n"
            f"   💰 ₹{p['price']} / {p['validity']}\n"
            f"   🔗 {p['access_link'] or '—'}"
        )
    text = "📦 <b>All Plans</b>\n\n" + "\n\n".join(lines)
    await call.message.edit_text(text, reply_markup=admin_panel_keyboard())


@router.callback_query(lambda c: c.data == "admin_premium_subscribers")
async def cb_premium_subscribers(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.edit_text(
            "📢 <b>Premium Subscribers</b>\n\nNo plans found.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    await call.message.edit_text(
        "📢 <b>Premium Subscribers</b>\n\nSelect a plan:",
        reply_markup=admin_plan_list_keyboard(plans, "admin_ps"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_ps:"))
async def cb_premium_subscribers_plan(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    try:
        plan_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.message.edit_text(
            "⚠️ Invalid plan.",
            reply_markup=admin_panel_keyboard(),
        )
        return

    plan = await get_plan(plan_id)
    if not plan:
        await call.message.edit_text(
            "⚠️ Plan not found.",
            reply_markup=admin_panel_keyboard(),
        )
        return

    user_ids = await _orders.distinct(
        "user_id",
        {
            "payment_status": "approved",
            "plan_id": plan_id,
        },
    )

    await call.message.edit_text(
        "📢 <b>Premium Subscribers</b>\n\n"
        f"Plan: {html.escape(plan['name'])}\n"
        f"Premium Subscribers: {len(user_ids)}\n\n"
        "Send the complete custom broadcast message.\n"
        "You can use Telegram formatting, emojis, and line breaks.\n\n"
        "Type /cancel to exit.",
        reply_markup=None,
    )
    _state[call.from_user.id] = {
        "step": "premium_subscribers:message",
        "data": {
            "plan_id": plan_id,
            "plan_name": plan["name"],
            "subscriber_count": len(user_ids),
        },
    }


@router.message(_in_state("premium_subscribers:message"), F.text)
async def handle_premium_subscribers_message(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        return

    st = _state.get(message.from_user.id)
    if not st:
        return

    data = st["data"]
    data["message_html"] = message.html_text
    await message.answer(
        "📢 <b>Broadcast Preview</b>\n\n"
        f"Plan: {html.escape(data['plan_name'])}\n"
        f"Premium Subscribers: {data['subscriber_count']}\n\n"
        f"{data['message_html']}",
        reply_markup=premium_subscribers_preview_keyboard(),
    )
    st["step"] = "premium_subscribers:preview"


@router.callback_query(lambda c: c.data == "admin_ps_send")
async def cb_premium_subscribers_send(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return

    st = _state.get(call.from_user.id)
    if not st or st.get("step") != "premium_subscribers:preview":
        await call.answer("⛔ This preview is no longer active.", show_alert=True)
        return

    data = st["data"]
    plan_id = data["plan_id"]
    message_html = data["message_html"]
    st["step"] = "premium_subscribers:sending"
    await call.answer()

    user_ids = await _orders.distinct(
        "user_id",
        {
            "payment_status": "approved",
            "plan_id": plan_id,
        },
    )
    total = len(user_ids)
    sent = 0
    failed = 0

    def _progress_text() -> str:
        return (
            "📤 <b>Sending...</b>\n\n"
            f"Total: {total}\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}\n"
            f"⏳ Remaining: {total - sent - failed}"
        )

    try:
        await call.message.edit_text(_progress_text())
    except Exception:
        logger.exception("Failed to show Premium Subscribers broadcast progress")

    for index, user_id in enumerate(user_ids, start=1):
        delivered = False
        try:
            for attempt in range(2):
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=message_html,
                    )
                    delivered = True
                    break
                except TelegramRetryAfter as exc:
                    if attempt == 1:
                        raise
                    await asyncio.sleep(exc.retry_after)
        except (TelegramForbiddenError, TelegramBadRequest):
            logger.info(
                "Premium Subscribers delivery rejected for user %s",
                user_id,
            )
        except Exception:
            logger.exception(
                "Premium Subscribers delivery failed for user %s",
                user_id,
            )

        if delivered:
            sent += 1
        else:
            failed += 1

        # Avoid excessive edit requests while still showing live progress.
        if index == total or index % 10 == 0:
            try:
                await call.message.edit_text(_progress_text())
            except Exception:
                logger.exception("Failed to update Premium Subscribers progress")

        await asyncio.sleep(0.05)

    _state.pop(call.from_user.id, None)
    await call.message.edit_text(
        "📊 <b>Broadcast Complete</b>\n\n"
        f"Premium Subscribers: {total}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}",
    )


@router.callback_query(lambda c: c.data == "admin_ps_cancel")
async def cb_premium_subscribers_cancel(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await _cancel(call)


@router.callback_query(lambda c: c.data == "admin_pending")
async def cb_pending(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    orders = await get_pending_orders()
    if not orders:
        await call.message.edit_text(
            "📋 <b>Pending Payments</b>\n\nNo pending payments right now.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    lines = [
        f"🆔 #{o['order_id']}\n"
        f"👤 User: <code>{o['user_id']}</code>\n"
        f"📦 {o['plan_name']} — ₹{o['plan_price']}\n"
        f"🕒 {o['created_at']}"
        for o in orders
    ]
    text = "📋 <b>Pending Payments</b>\n\n" + "\n\n".join(lines)
    await call.message.edit_text(text, reply_markup=admin_panel_keyboard())


# ── Broadcast ─────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_broadcast")
async def cb_broadcast_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "broadcast:msg", "data": {}}
    await call.message.edit_text(
        "📢 <b>Broadcast</b>\n\n"
        "Send the message you want to broadcast to all users.\n"
        "Supports text, photos, and videos.\n\n"
        "Type /cancel to abort.",
        reply_markup=None,
    )


@router.message(_in_state("broadcast:msg"), F.text | F.photo | F.video)
async def handle_broadcast_msg(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        return
    _state.pop(message.from_user.id, None)

    user_ids = await get_all_user_ids()
    sent = failed = blocked = 0

    status_msg = await message.answer(
        f"📢 Broadcasting to {len(user_ids)} users… please wait."
    )

    for uid in user_ids:
        try:
            if message.photo:
                await bot.send_photo(
                    chat_id=uid,
                    photo=message.photo[-1].file_id,
                    caption=message.caption or "",
                )
            elif message.video:
                await bot.send_video(
                    chat_id=uid,
                    video=message.video.file_id,
                    caption=message.caption or "",
                )
            else:
                await bot.send_message(chat_id=uid, text=message.text or "")
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 msg/s to stay under rate limits

    await status_msg.edit_text(
        "📢 <b>Broadcast Completed</b>\n\n"
        f"✅ <b>Successfully Sent:</b>  {sent}\n"
        f"❌ <b>Failed:</b>            {failed}\n"
        f"🚫 <b>Blocked Users:</b>     {blocked}"
    )
    await message.answer("🛠 <b>ADMIN PANEL</b>\n\nSelect an option:", reply_markup=admin_panel_keyboard())


# ── Add Plan wizard ───────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_add")
async def cb_add_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "add:name", "data": {}}
    await call.message.edit_text(
        "➕ <b>Add Plan — Step 1/5</b>\n\n" + _STEPS_LABEL["add:name"],
        reply_markup=None,
    )


@router.message(_in_state("add:name"), F.text)
async def add_step_name(message: Message) -> None:
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    _state[message.from_user.id]["data"]["name"] = message.text.strip()
    _state[message.from_user.id]["step"] = "add:price"
    await message.answer("➕ <b>Add Plan — Step 2/5</b>\n\n" + _STEPS_LABEL["add:price"])


@router.message(_in_state("add:price"), F.text)
async def add_step_price(message: Message) -> None:
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    _state[message.from_user.id]["data"]["price"] = message.text.strip()
    _state[message.from_user.id]["step"] = "add:validity"
    await message.answer("➕ <b>Add Plan — Step 3/5</b>\n\n" + _STEPS_LABEL["add:validity"])


@router.message(_in_state("add:validity"), F.text)
async def add_step_validity(message: Message) -> None:
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    _state[message.from_user.id]["data"]["validity"] = message.text.strip()
    _state[message.from_user.id]["data"]["demo_ids"] = []
    _state[message.from_user.id]["step"] = "add:demo"
    await message.answer(
        "➕ <b>Add Plan — Step 4/5</b>\n\n" + _STEPS_LABEL["add:demo"],
        reply_markup=admin_demo_done_keyboard(0),
    )


@router.message(_in_state("add:demo"))
async def add_step_demo_media(message: Message) -> None:
    """
    Accept forwarded messages from the source channel during the demo step.
    We store the original message ID from the source channel (forward_from_message_id),
    not the admin-chat message ID, so copy_messages() works correctly later.
    """
    if not _is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        return

    st = _state[message.from_user.id]

    # Must be a forward from a channel so we can resolve the source message ID.
    fwd_id = getattr(message, "forward_from_message_id", None)
    fwd_chat = getattr(message, "forward_from_chat", None)

    if fwd_id is None or fwd_chat is None:
        await message.answer(
            "⚠️ Please <b>forward</b> the demo videos directly from the source channel.\n"
            "Do not upload new files — forward existing messages from the channel."
        )
        return

    st["data"]["demo_ids"].append(fwd_id)
    # Store (or confirm) the source channel ID from the first forwarded message.
    if not st["data"].get("source_channel_id"):
        st["data"]["source_channel_id"] = str(fwd_chat.id)

    count = len(st["data"]["demo_ids"])
    try:
        await message.answer(
            f"✅ Got it! {count} message{'s' if count != 1 else ''} forwarded so far.\n"
            "Forward more, or click <b>✅ Done</b> when finished.",
            reply_markup=admin_demo_done_keyboard(count),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin_demo_done")
async def cb_demo_done(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    st = _state.get(call.from_user.id)
    if not st:
        await call.answer("No active wizard.", show_alert=True)
        return
    await call.answer()
    step = st["step"]

    if step == "add:demo":
        if not st["data"].get("demo_ids"):
            await call.message.answer(
                "⚠️ No demo videos captured yet. Please forward at least one message from the source channel."
            )
            return
        # Carry forward the source channel resolved from forwarded messages.
        if st["data"].get("source_channel_id"):
            st["data"]["resolved_source_channel"] = st["data"]["source_channel_id"]
        st["step"] = "add:access"
        await call.message.answer(
            "➕ <b>Add Plan — Step 5/5</b>\n\n" + _STEPS_LABEL["add:access"]
        )

    elif step == "demo:videos":
        # Changing demo videos for existing plan
        plan_id = st["data"]["plan_id"]
        new_ids = st["data"]["demo_ids"]
        if not new_ids:
            await call.message.answer(
                "⚠️ No demo videos captured yet. Please forward at least one message from the source channel."
            )
            return
        update_kwargs: dict = {"demo_message_ids": new_ids}
        # If forwarded messages reveal a source channel, update it too.
        if st["data"].get("source_channel_id"):
            update_kwargs["source_channel_id"] = st["data"]["source_channel_id"]
        await update_plan(plan_id, **update_kwargs)
        _state.pop(call.from_user.id, None)
        count = len(new_ids)
        await call.message.answer(
            f"✅ Demo videos updated ({count} message ID{'s' if count != 1 else ''} saved).",
            reply_markup=admin_panel_keyboard(),
        )


@router.message(_in_state("add:access"), F.text)
async def add_step_access(message: Message) -> None:
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    st = _state[message.from_user.id]
    st["data"]["access_link"] = message.text.strip()
    st["step"] = "add:confirm"

    d = st["data"]
    count = len(d.get("demo_ids", []))
    await message.answer(
        "➕ <b>Add Plan — Confirmation</b>\n\n"
        f"📝 <b>Name:</b> {d['name']}\n"
        f"💰 <b>Price:</b> ₹{d['price']}\n"
        f"⏳ <b>Validity:</b> {d['validity']}\n"
        f"🎥 <b>Demo IDs:</b> {count} message{'s' if count != 1 else ''}\n"
        f"🔗 <b>Access Link:</b> {d['access_link']}\n\n"
        "Save this plan?",
        reply_markup=admin_confirm_save_keyboard(),
    )


@router.callback_query(lambda c: c.data == "admin_save")
async def cb_save_plan(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    st = _state.pop(call.from_user.id, None)
    if not st or st["step"] != "add:confirm":
        await call.answer("No plan to save.", show_alert=True)
        return
    await call.answer()
    d = st["data"]
    # Use the source channel resolved from forwarded demo messages;
    # fall back to the global SOURCE_CHANNEL_ID from config.
    source = d.get("resolved_source_channel") or SOURCE_CHANNEL_ID
    await create_plan(
        name=d["name"],
        price=d["price"],
        validity=d["validity"],
        demo_message_ids=d.get("demo_ids", []),
        source_channel_id=source,
        access_link=d["access_link"],
    )
    await call.message.edit_text(
        f"✅ Plan <b>{d['name']}</b> saved successfully!",
        reply_markup=admin_panel_keyboard(),
    )


# ── Edit Plan wizard ──────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_edit")
async def cb_edit_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.edit_text(
            "✏️ <b>Edit Plan</b>\n\nNo plans found.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    _state[call.from_user.id] = {"step": "edit:select", "data": {}}
    await call.message.edit_text(
        "✏️ <b>Edit Plan</b>\n\nSelect a plan to edit:",
        reply_markup=admin_plan_list_keyboard(plans, "admin_ep"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_ep:"))
async def cb_edit_plan_selected(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    plan_id = int(call.data.split(":", 1)[1])
    await call.answer()
    plan = await get_plan(plan_id)
    if not plan:
        await call.answer("Plan not found.", show_alert=True)
        return
    _state[call.from_user.id] = {"step": "edit:field", "data": {"plan_id": plan_id}}
    await call.message.edit_text(
        f"✏️ <b>Edit Plan:</b> {plan['name']}\n\nSelect a field to edit:",
        reply_markup=admin_edit_fields_keyboard(plan_id),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_mv:"))
async def cb_move_plan(call: CallbackQuery) -> None:
    """Reorder a plan: admin_mv:{up|down|top|bottom}:{plan_id}."""
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    try:
        _, action, plan_id_str = call.data.split(":", 2)
        plan_id = int(plan_id_str)
    except (ValueError, IndexError):
        await call.answer("⚠️ Invalid request.", show_alert=True)
        return

    mover = {
        "up":     move_plan_up,
        "down":   move_plan_down,
        "top":    move_plan_to_top,
        "bottom": move_plan_to_bottom,
    }.get(action)
    if mover is None:
        await call.answer("⚠️ Invalid request.", show_alert=True)
        return

    moved = await mover(plan_id)

    plan = await get_plan(plan_id)
    if not plan:
        await call.answer("Plan not found.", show_alert=True)
        return

    if moved:
        await call.answer("✅ Order updated.")
    else:
        await call.answer("Already at that position.")

    try:
        await call.message.edit_text(
            f"✏️ <b>Edit Plan:</b> {plan['name']}\n\nSelect a field to edit:",
            reply_markup=admin_edit_fields_keyboard(plan_id),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("admin_ef:"))
async def cb_edit_field_selected(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    _, plan_id_str, field = call.data.split(":", 2)
    plan_id = int(plan_id_str)
    await call.answer()
    _state[call.from_user.id] = {
        "step": "edit:value",
        "data": {"plan_id": plan_id, "field": field},
    }
    await call.message.edit_text(
        f"✏️ <b>Edit Field</b>\n\n{EDIT_FIELD_LABELS.get(field, 'Enter new value:')}",
        reply_markup=None,
    )


@router.message(_in_state("edit:value"), F.text)
async def handle_edit_value(message: Message) -> None:
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    st = _state.pop(message.from_user.id, None)
    if not st:
        return
    plan_id = st["data"]["plan_id"]
    field = st["data"]["field"]
    await update_plan(plan_id, **{field: message.text.strip()})
    await message.answer(
        f"✅ <b>{field.replace('_', ' ').title()}</b> updated successfully!",
        reply_markup=admin_panel_keyboard(),
    )


# ── Delete Plan wizard ────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_delete")
async def cb_delete_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.edit_text(
            "🗑 <b>Delete Plan</b>\n\nNo plans found.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    _state[call.from_user.id] = {"step": "delete:select", "data": {}}
    await call.message.edit_text(
        "🗑 <b>Delete Plan</b>\n\nSelect a plan to delete:",
        reply_markup=admin_plan_list_keyboard(plans, "admin_dp"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_dp:"))
async def cb_delete_plan_selected(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    plan_id = int(call.data.split(":", 1)[1])
    await call.answer()
    plan = await get_plan(plan_id)
    if not plan:
        await call.answer("Plan not found.", show_alert=True)
        return
    _state[call.from_user.id] = {"step": "delete:confirm", "data": {"plan_id": plan_id}}
    await call.message.edit_text(
        f"🗑 <b>Delete Plan</b>\n\n"
        f"Are you sure you want to delete <b>{plan['name']}</b>?\n"
        f"This cannot be undone.",
        reply_markup=admin_delete_confirm_keyboard(plan_id),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_dc:"))
async def cb_delete_confirm(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    plan_id = int(call.data.split(":", 1)[1])
    await call.answer()
    plan = await get_plan(plan_id)
    name = plan["name"] if plan else f"#{plan_id}"
    await delete_plan(plan_id)
    _state.pop(call.from_user.id, None)
    await call.message.edit_text(
        f"✅ Plan <b>{name}</b> has been deleted.",
        reply_markup=admin_panel_keyboard(),
    )


# ── Change Demo Videos ────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_demo")
async def cb_demo_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.edit_text(
            "🎥 <b>Change Demo Videos</b>\n\nNo plans found.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    _state[call.from_user.id] = {"step": "demo:select", "data": {}}
    await call.message.edit_text(
        "🎥 <b>Change Demo Videos</b>\n\nSelect a plan:",
        reply_markup=admin_plan_list_keyboard(plans, "admin_dmp"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_dmp:"))
async def cb_demo_plan_selected(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    plan_id = int(call.data.split(":", 1)[1])
    await call.answer()
    plan = await get_plan(plan_id)
    if not plan:
        await call.answer("Plan not found.", show_alert=True)
        return
    _state[call.from_user.id] = {
        "step": "demo:videos",
        "data": {"plan_id": plan_id, "demo_ids": []},
    }
    await call.message.edit_text(
        f"🎥 <b>Change Demo Videos: {plan['name']}</b>\n\n"
        "Send the new demo videos (photos/videos) one by one or as an album.\n"
        "Click <b>✅ Done</b> when finished. This will replace the old demo videos.",
        reply_markup=admin_demo_done_keyboard(0),
    )


@router.message(_in_state("demo:videos"))
async def handle_demo_videos_media(message: Message) -> None:
    """Accept forwarded messages from the source channel when changing demo videos."""
    if not _is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        return

    st = _state[message.from_user.id]

    fwd_id = getattr(message, "forward_from_message_id", None)
    fwd_chat = getattr(message, "forward_from_chat", None)

    if fwd_id is None or fwd_chat is None:
        await message.answer(
            "⚠️ Please <b>forward</b> the demo videos directly from the source channel.\n"
            "Do not upload new files — forward existing messages from the channel."
        )
        return

    st["data"]["demo_ids"].append(fwd_id)
    if not st["data"].get("source_channel_id"):
        st["data"]["source_channel_id"] = str(fwd_chat.id)

    count = len(st["data"]["demo_ids"])
    try:
        await message.answer(
            f"✅ {count} message{'s' if count != 1 else ''} forwarded. Forward more or click Done.",
            reply_markup=admin_demo_done_keyboard(count),
        )
    except Exception:
        pass


# ── Start Demo Settings ───────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_startdemo")
async def cb_startdemo_panel(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state.pop(call.from_user.id, None)
    cfg = await get_start_demo()
    ids = cfg["ids"]
    status_line = (
        f"📹 <b>Start Demo Settings</b>\n\n"
        f"Status: {'✅ Enabled' if cfg['enabled'] else '🚫 Disabled'}\n"
        f"Videos saved: {len(ids)}\n\n"
        "Select an option:"
    )
    try:
        await call.message.edit_text(status_line, reply_markup=start_demo_settings_keyboard(cfg["enabled"]))
    except Exception:
        await call.message.answer(status_line, reply_markup=start_demo_settings_keyboard(cfg["enabled"]))


@router.callback_query(lambda c: c.data == "admin_sd_noop")
async def cb_sd_noop(call: CallbackQuery) -> None:
    await call.answer()  # status badge tap — do nothing


@router.callback_query(lambda c: c.data == "admin_sd_enable")
async def cb_sd_enable(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await set_setting("start_demo_enabled", "1")
    await call.answer("✅ Start demo videos enabled.", show_alert=True)
    cfg = await get_start_demo()
    try:
        await call.message.edit_text(
            f"📹 <b>Start Demo Settings</b>\n\nStatus: ✅ Enabled\nVideos saved: {len(cfg['ids'])}\n\nSelect an option:",
            reply_markup=start_demo_settings_keyboard(True),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin_sd_disable")
async def cb_sd_disable(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await set_setting("start_demo_enabled", "0")
    await call.answer("🚫 Start demo videos disabled.", show_alert=True)
    cfg = await get_start_demo()
    try:
        await call.message.edit_text(
            f"📹 <b>Start Demo Settings</b>\n\nStatus: 🚫 Disabled\nVideos saved: {len(cfg['ids'])}\n\nSelect an option:",
            reply_markup=start_demo_settings_keyboard(False),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin_sd_change")
async def cb_sd_change(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "startdemo:videos", "data": {"demo_ids": []}}
    try:
        await call.message.edit_text(
            "📹 <b>Change Start Demo Videos</b>\n\n"
            f"Forward the start demo videos from the configured Demo Videos Channel "
            f"(<code>{SOURCE_CHANNEL_ID}</code>).\n\n"
            "When finished, press <b>✅ Done</b>.",
            reply_markup=start_demo_done_keyboard(0),
        )
    except Exception:
        await call.message.answer(
            "📹 <b>Change Start Demo Videos</b>\n\n"
            f"Forward the start demo videos from the configured Demo Videos Channel "
            f"(<code>{SOURCE_CHANNEL_ID}</code>).\n\n"
            "When finished, press <b>✅ Done</b>.",
            reply_markup=start_demo_done_keyboard(0),
        )


@router.message(_in_state("startdemo:videos"))
async def handle_startdemo_media(message: Message) -> None:
    """Accept forwarded messages from the demo videos channel."""
    if not _is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        return

    st = _state[message.from_user.id]

    fwd_id   = getattr(message, "forward_from_message_id", None)
    fwd_chat = getattr(message, "forward_from_chat", None)

    if fwd_id is None or fwd_chat is None:
        await message.answer(
            "⚠️ Please <b>forward</b> messages directly from the Demo Videos Channel.\n"
            "Do not upload new files — forward existing messages from the channel."
        )
        return

    # Verify the message originates from the configured demo videos channel.
    # Fail closed: if SOURCE_CHANNEL_ID is not a valid integer, reject all forwards.
    try:
        expected_id = int(SOURCE_CHANNEL_ID)
    except (ValueError, TypeError):
        await message.answer(
            "⚠️ The Demo Videos Channel is not configured correctly on this bot.\n"
            "Please contact the bot owner to set <code>SOURCE_CHANNEL_ID</code>."
        )
        return

    if fwd_chat.id != expected_id:
        await message.answer(
            f"⚠️ That message is not from the configured Demo Videos Channel "
            f"(<code>{SOURCE_CHANNEL_ID}</code>).\n\n"
            "Please forward messages only from that channel."
        )
        return

    st["data"]["demo_ids"].append(fwd_id)
    if not st["data"].get("source_channel_id"):
        st["data"]["source_channel_id"] = str(fwd_chat.id)

    count = len(st["data"]["demo_ids"])
    try:
        await message.answer(
            f"✅ Got it! {count} video{'s' if count != 1 else ''} collected so far.\n"
            "Forward more, or press <b>✅ Done</b> when finished.",
            reply_markup=start_demo_done_keyboard(count),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin_sd_done")
async def cb_sd_done(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    st = _state.get(call.from_user.id)
    if not st or st.get("step") != "startdemo:videos":
        await call.answer("No active wizard.", show_alert=True)
        return

    ids = st["data"].get("demo_ids", [])
    if not ids:
        await call.answer(
            "⚠️ No videos collected yet. Forward at least one message from the channel.",
            show_alert=True,
        )
        return

    source = st["data"].get("source_channel_id") or SOURCE_CHANNEL_ID
    _state.pop(call.from_user.id, None)
    await call.answer()

    await set_start_demo_ids(ids, source)
    count = len(ids)
    await call.message.edit_text(
        f"✅ <b>Start demo videos updated!</b>\n\n"
        f"{count} video{'s' if count != 1 else ''} saved.\n\n"
        "They will be sent to users on /start when the feature is enabled.",
        reply_markup=admin_panel_keyboard(),
    )


# ── Payment Reminder Settings ────────────────────────────────────────────────

def _reminder_panel_text(enabled: bool, first_min: str, second_min: str) -> str:
    return (
        "🔔 <b>Payment Reminder Settings</b>\n\n"
        f"Status: {'✅ Enabled' if enabled else '🚫 Disabled'}\n"
        f"⏱ First reminder: {first_min} minutes after Buy Now\n"
        f"🕒 Final reminder: {second_min} minutes after Buy Now\n\n"
        "Select an option:"
    )


@router.callback_query(lambda c: c.data == "admin_reminders")
async def cb_reminders_panel(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state.pop(call.from_user.id, None)
    enabled = (await get_setting("reminder_enabled", "1")) == "1"
    first_min = await get_setting("reminder_first_delay_min", "15")
    second_min = await get_setting("reminder_second_delay_min", "1440")
    text = _reminder_panel_text(enabled, first_min, second_min)
    try:
        await call.message.edit_text(text, reply_markup=reminder_settings_keyboard(enabled))
    except Exception:
        await call.message.answer(text, reply_markup=reminder_settings_keyboard(enabled))


@router.callback_query(lambda c: c.data == "admin_rm_noop")
async def cb_rm_noop(call: CallbackQuery) -> None:
    await call.answer()  # status badge tap — do nothing


@router.callback_query(lambda c: c.data == "admin_rm_enable")
async def cb_rm_enable(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await set_setting("reminder_enabled", "1")
    await call.answer("✅ Payment reminders enabled.", show_alert=True)
    first_min = await get_setting("reminder_first_delay_min", "15")
    second_min = await get_setting("reminder_second_delay_min", "1440")
    try:
        await call.message.edit_text(
            _reminder_panel_text(True, first_min, second_min),
            reply_markup=reminder_settings_keyboard(True),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin_rm_disable")
async def cb_rm_disable(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await set_setting("reminder_enabled", "0")
    await call.answer("🚫 Payment reminders disabled.", show_alert=True)
    first_min = await get_setting("reminder_first_delay_min", "15")
    second_min = await get_setting("reminder_second_delay_min", "1440")
    try:
        await call.message.edit_text(
            _reminder_panel_text(False, first_min, second_min),
            reply_markup=reminder_settings_keyboard(False),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin_rm_first")
async def cb_rm_first_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "reminder:first_delay", "data": {}}
    current = await get_setting("reminder_first_delay_min", "15")
    await call.message.edit_text(
        "⏱ <b>First Reminder Delay</b>\n\n"
        f"Current: {current} minutes\n\n"
        "Send the new delay, in whole minutes, after a user clicks Buy Now before "
        "the first reminder is sent (e.g. <code>15</code>).\n\n"
        "Type /cancel to abort.",
        reply_markup=None,
    )


_MAX_REMINDER_DELAY_MIN = 525600  # 1 year — generous upper bound, prevents overflow/nonsense values


@router.message(_in_state("reminder:first_delay"), F.text)
async def handle_rm_first_delay(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        return
    if not raw.isdigit() or not (0 < int(raw) <= _MAX_REMINDER_DELAY_MIN):
        await message.answer(
            f"⚠️ Please send a whole number of minutes between 1 and {_MAX_REMINDER_DELAY_MIN} (e.g. 15)."
        )
        return
    _state.pop(message.from_user.id, None)
    await set_setting("reminder_first_delay_min", raw)
    await message.answer(
        f"✅ First reminder delay updated to {raw} minutes.",
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(lambda c: c.data == "admin_rm_second")
async def cb_rm_second_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "reminder:second_delay", "data": {}}
    current = await get_setting("reminder_second_delay_min", "1440")
    await call.message.edit_text(
        "🕒 <b>Final Reminder Delay</b>\n\n"
        f"Current: {current} minutes\n\n"
        "Send the new delay, in whole minutes, after a user clicks Buy Now before "
        "the final reminder is sent (e.g. <code>1440</code> for 24 hours).\n\n"
        "Type /cancel to abort.",
        reply_markup=None,
    )


@router.message(_in_state("reminder:second_delay"), F.text)
async def handle_rm_second_delay(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        return
    if not raw.isdigit() or not (0 < int(raw) <= _MAX_REMINDER_DELAY_MIN):
        await message.answer(
            f"⚠️ Please send a whole number of minutes between 1 and {_MAX_REMINDER_DELAY_MIN} (e.g. 1440)."
        )
        return
    _state.pop(message.from_user.id, None)
    await set_setting("reminder_second_delay_min", raw)
    await message.answer(
        f"✅ Final reminder delay updated to {raw} minutes.",
        reply_markup=admin_panel_keyboard(),
    )


def _reminder_message_prompt(title: str, current: str) -> str:
    preview = html.escape(current) if current else "(not set — using default)"
    return (
        f"✏️ <b>{title}</b>\n\n"
        "<b>Current message</b> (shown escaped/raw below):\n"
        f"<pre>{preview}</pre>\n\n"
        "Send the new message. You can use these placeholders — they'll be "
        "filled in automatically:\n"
        "<code>{plan_name}</code>  <code>{plan_price}</code>  <code>{plan_validity}</code>\n\n"
        "A 💳 Buy Now button is always attached automatically for the exact plan "
        "the reminder was sent for — it cannot be edited or removed.\n\n"
        "HTML formatting is supported. Type /cancel to abort."
    )


@router.callback_query(lambda c: c.data == "admin_rm_msg15")
async def cb_rm_msg15_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "reminder:message_15", "data": {}}
    current = await get_setting("reminder_first_message", "")
    await call.message.edit_text(
        _reminder_message_prompt("15 Minute Reminder Message", current),
        reply_markup=None,
    )


@router.message(_in_state("reminder:message_15"), F.text)
async def handle_rm_msg15(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    _state.pop(message.from_user.id, None)
    await set_setting("reminder_first_message", text)
    await message.answer(
        "✅ 15 minute reminder message updated.",
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(lambda c: c.data == "admin_rm_msg24")
async def cb_rm_msg24_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "reminder:message_24", "data": {}}
    current = await get_setting("reminder_second_message", "")
    await call.message.edit_text(
        _reminder_message_prompt("24 Hour Reminder Message", current),
        reply_markup=None,
    )


@router.message(_in_state("reminder:message_24"), F.text)
async def handle_rm_msg24(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    _state.pop(message.from_user.id, None)
    await set_setting("reminder_second_message", text)
    await message.answer(
        "✅ 24 hour reminder message updated.",
        reply_markup=admin_panel_keyboard(),
    )


# ── Referral Settings ────────────────────────────────────────────────────────

async def _referral_panel(target) -> None:
    """Show (or re-show) the Referral Settings sub-panel."""
    enabled = (await get_setting("referral_enabled", "1")) == "1"
    status  = "✅ Enabled" if enabled else "❌ Disabled"
    text = (
        "👥 <b>Referral Settings</b>\n\n"
        f"Status: {status}\n\n"
        "Select an option:"
    )
    kb = referral_settings_keyboard(enabled)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except Exception:
            await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data == "admin_referral")
async def cb_referral_panel(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state.pop(call.from_user.id, None)
    await _referral_panel(call)


@router.callback_query(lambda c: c.data == "admin_ref_noop")
async def cb_ref_noop(call: CallbackQuery) -> None:
    await call.answer()  # status badge tap — do nothing


@router.callback_query(lambda c: c.data == "admin_ref_enable")
async def cb_ref_enable(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await set_setting("referral_enabled", "1")
    await call.answer("✅ Referral system enabled.", show_alert=True)
    await _referral_panel(call)


@router.callback_query(lambda c: c.data == "admin_ref_disable")
async def cb_ref_disable(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await set_setting("referral_enabled", "0")
    await call.answer("❌ Referral system disabled.", show_alert=True)
    await _referral_panel(call)


@router.callback_query(lambda c: c.data == "admin_ref_stats")
async def cb_ref_stats(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    s = await get_referral_stats()
    status = "✅ ON" if s["enabled"] else "❌ OFF"
    max_refs_display = str(s["max_referrals"]) if s["max_referrals"] > 0 else "Unlimited"
    try:
        await call.message.edit_text(
            "📊 <b>Referral Statistics</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔘 <b>Referral System:</b>          {status}\n"
            f"🎁 <b>Referral Reward:</b>           {s['reward_pct']}%\n"
            f"💰 <b>Maximum Discount:</b>          {s['max_discount']}%\n"
            f"👥 <b>Maximum Referrals:</b>         {max_refs_display}\n\n"
            f"👥 <b>Total Referrers:</b>           {s['total_referrers']}\n"
            f"✅ <b>Total Successful Referrals:</b> {s['total_referrals']}\n"
            "━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=referral_settings_keyboard(s["enabled"]),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin_ref_reset")
async def cb_ref_reset(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    try:
        await call.message.edit_text(
            "🔄 <b>Reset Referral Data</b>\n\n"
            "⚠️ This will reset <b>all</b> referral counts, discounts, and referrer links "
            "for every user.\n\n"
            "This action cannot be undone. Are you sure?",
            reply_markup=referral_reset_confirm_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "admin_ref_reset_confirm")
async def cb_ref_reset_confirm(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer("🔄 Resetting…")
    await reset_referral_data()
    try:
        await call.message.edit_text(
            "✅ <b>Referral data has been reset.</b>\n\n"
            "All referral counts, discounts, and referrer links have been cleared.",
            reply_markup=referral_settings_keyboard(
                (await get_setting("referral_enabled", "1")) == "1"
            ),
        )
    except Exception:
        pass


# ── Referral: Edit configurable settings ──────────────────────────────────────

def _referral_kb_back() -> InlineKeyboardMarkup:
    """Quick back-to-referral-panel keyboard used in edit prompts."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Cancel", callback_data="admin_referral")]]
    )


# ── Edit Referral Reward % ────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_ref_reward")
async def cb_ref_reward(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    current = (await get_setting("referral_reward_pct", "5")) or "5"
    _state[call.from_user.id] = {"step": "ref_reward:value", "data": {}}
    try:
        await call.message.edit_text(
            f"✏️ <b>Edit Referral Reward (%)</b>\n\n"
            f"Current value: <b>{current}%</b>\n\n"
            "Send the new reward percentage (1–100).\n"
            "Example: <code>5</code> → every referral gives 5% discount.",
            reply_markup=_referral_kb_back(),
        )
    except Exception:
        await call.message.answer(
            f"✏️ <b>Edit Referral Reward (%)</b>\n\nCurrent: <b>{current}%</b>\n\n"
            "Send the new value (1–100):",
            reply_markup=_referral_kb_back(),
        )


@router.message(_in_state("ref_reward:value"), F.text)
async def handle_ref_reward_value(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 100):
        await message.answer("⚠️ Please send a number between 1 and 100:")
        return
    _state.pop(message.from_user.id, None)
    await set_setting("referral_reward_pct", raw)
    await message.answer(
        f"✅ Referral reward set to <b>{raw}%</b> per referral.",
        reply_markup=referral_settings_keyboard(
            (await get_setting("referral_enabled", "1")) == "1"
        ),
    )


# ── Edit Maximum Referral Discount % ──────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_ref_maxdiscount")
async def cb_ref_maxdiscount(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    current = (await get_setting("max_referral_discount", "100")) or "100"
    _state[call.from_user.id] = {"step": "ref_maxdiscount:value", "data": {}}
    try:
        await call.message.edit_text(
            f"✏️ <b>Edit Maximum Referral Discount (%)</b>\n\n"
            f"Current value: <b>{current}%</b>\n\n"
            "Send the new maximum discount percentage (1–100).\n"
            "No user's referral discount will ever exceed this value.",
            reply_markup=_referral_kb_back(),
        )
    except Exception:
        await call.message.answer(
            f"✏️ <b>Edit Maximum Referral Discount (%)</b>\n\nCurrent: <b>{current}%</b>\n\n"
            "Send the new value (1–100):",
            reply_markup=_referral_kb_back(),
        )


@router.message(_in_state("ref_maxdiscount:value"), F.text)
async def handle_ref_maxdiscount_value(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 100):
        await message.answer("⚠️ Please send a number between 1 and 100:")
        return
    _state.pop(message.from_user.id, None)
    await set_setting("max_referral_discount", raw)
    await message.answer(
        f"✅ Maximum referral discount set to <b>{raw}%</b>.",
        reply_markup=referral_settings_keyboard(
            (await get_setting("referral_enabled", "1")) == "1"
        ),
    )


# ── Edit Maximum Referrals per user ───────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_ref_maxreferrals")
async def cb_ref_maxreferrals(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    current = (await get_setting("max_referrals", "0")) or "0"
    display = current if current != "0" else "Unlimited"
    _state[call.from_user.id] = {"step": "ref_maxreferrals:value", "data": {}}
    try:
        await call.message.edit_text(
            f"👥 <b>Edit Maximum Referrals</b>\n\n"
            f"Current value: <b>{display}</b>\n\n"
            "Send the maximum number of referrals counted per user.\n"
            "Send <code>0</code> for unlimited.",
            reply_markup=_referral_kb_back(),
        )
    except Exception:
        await call.message.answer(
            f"👥 <b>Edit Maximum Referrals</b>\n\nCurrent: <b>{display}</b>\n\n"
            "Send the new value (0 = unlimited):",
            reply_markup=_referral_kb_back(),
        )


@router.message(_in_state("ref_maxreferrals:value"), F.text)
async def handle_ref_maxreferrals_value(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("⚠️ Please send a non-negative whole number (0 = unlimited):")
        return
    _state.pop(message.from_user.id, None)
    await set_setting("max_referrals", raw)
    display = raw if raw != "0" else "Unlimited"
    await message.answer(
        f"✅ Maximum referrals per user set to <b>{display}</b>.",
        reply_markup=referral_settings_keyboard(
            (await get_setting("referral_enabled", "1")) == "1"
        ),
    )


# ── Referral: Add Referral (admin manual credit) ──────────────────────────────

@router.callback_query(lambda c: c.data == "admin_ref_add")
async def cb_ref_add(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "ref_add:user_id", "data": {}}
    try:
        await call.message.edit_text(
            "➕ <b>Add Referral</b>\n\n"
            "Send the <b>User ID</b> you want to credit referrals to:",
            reply_markup=None,
        )
    except Exception:
        await call.message.answer(
            "➕ <b>Add Referral</b>\n\n"
            "Send the <b>User ID</b> you want to credit referrals to:",
        )


@router.message(_in_state("ref_add:user_id"), F.text)
async def handle_ref_add_user_id(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("⚠️ Invalid User ID. Please send a numeric Telegram User ID:")
        return
    _state[message.from_user.id]["data"]["target_user_id"] = int(raw)
    _state[message.from_user.id]["step"] = "ref_add:count"
    await message.answer(
        "➕ <b>Add Referral</b>\n\n"
        f"User ID: <code>{raw}</code>\n\n"
        "How many referrals do you want to add?",
    )


@router.message(_in_state("ref_add:count"), F.text)
async def handle_ref_add_count(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("⚠️ Please send a positive number:")
        return
    count = int(raw)
    st = _state.pop(message.from_user.id, None)
    if not st:
        return
    target_user_id = st["data"]["target_user_id"]
    result = await admin_add_referrals(target_user_id, count)
    if result is None:
        await message.answer(
            "❌ User not found.",
            reply_markup=referral_settings_keyboard(
                (await get_setting("referral_enabled", "1")) == "1"
            ),
        )
        return
    await message.answer(
        "✅ <b>Referral Added Successfully</b>\n\n"
        f"👤 <b>User ID:</b> <code>{target_user_id}</code>\n"
        f"👥 <b>Total Referrals:</b> {result['total_referrals']}\n"
        f"🎁 <b>Current Discount:</b> {result['referral_discount']}%",
        reply_markup=referral_settings_keyboard(
            (await get_setting("referral_enabled", "1")) == "1"
        ),
    )


# ── Edit Plan QR ─────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_planqr")
async def cb_planqr_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.edit_text(
            "💳 <b>Edit Plan QR</b>\n\nNo plans found.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    _state[call.from_user.id] = {"step": "planqr:select", "data": {}}
    await call.message.edit_text(
        "💳 <b>Edit Plan QR</b>\n\nSelect a plan:",
        reply_markup=admin_plan_list_keyboard(plans, "admin_pq"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_pq:"))
async def cb_planqr_plan_selected(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    try:
        plan_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("⚠️ Invalid plan.", show_alert=True)
        return
    await call.answer()
    plan = await get_plan(plan_id)
    if not plan:
        await call.answer("Plan not found.", show_alert=True)
        return
    _state[call.from_user.id] = {"step": "planqr:photo", "data": {"plan_id": plan_id}}
    has_qr = bool(plan.get("qr_image"))
    await call.message.edit_text(
        f"💳 <b>Edit Plan QR: {plan['name']}</b>\n\n"
        f"Current QR: {'✅ Set' if has_qr else '⬜ Not set'}\n\n"
        "Send the new QR image for this plan.",
        reply_markup=None,
    )


@router.message(_in_state("planqr:photo"), F.photo)
async def handle_planqr_photo(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    st = _state.pop(message.from_user.id, None)
    if not st:
        return
    plan_id = st["data"]["plan_id"]
    file_id = message.photo[-1].file_id
    await update_plan(plan_id, qr_image=file_id)
    await message.answer(
        "✅ QR updated successfully.",
        reply_markup=admin_panel_keyboard(),
    )


@router.message(_in_state("planqr:photo"))
async def handle_planqr_wrong_type(message: Message) -> None:
    """Catch non-photo messages when planqr:photo step is active."""
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    await message.answer("⚠️ Please send a <b>photo</b> of the QR code.")


# ── Edit Plan Buy Message ─────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_buymsg")
async def cb_buymsg_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.edit_text(
            "📝 <b>Edit Plan Buy Message</b>\n\nNo plans found.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    _state[call.from_user.id] = {"step": "buymsg:select", "data": {}}
    await call.message.edit_text(
        "📝 <b>Edit Plan Buy Message</b>\n\nSelect a plan:",
        reply_markup=admin_plan_list_keyboard(plans, "admin_bm"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_bm:"))
async def cb_buymsg_plan_selected(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    plan_id = int(call.data.split(":", 1)[1])
    await call.answer()
    plan = await get_plan(plan_id)
    if not plan:
        await call.answer("Plan not found.", show_alert=True)
        return
    _state[call.from_user.id] = {
        "step": "buymsg:value",
        "data": {"plan_id": plan_id},
    }
    current = plan.get("buy_message", "")
    await call.message.edit_text(
        f"📝 <b>Edit Buy Message: {plan['name']}</b>\n\n"
        "<b>Current message:</b>\n"
        f"{current if current else '(not set — using default)'}\n\n"
        "Send the new complete Buy Message for this plan (title, description, "
        "price, validity, emojis, formatting, line breaks, etc. — all in one message).\n\n"
        "You can use these placeholders and they will be filled in automatically:\n"
        "<code>{plan_name}</code>  <code>{plan_price}</code>  <code>{plan_validity}</code>\n\n"
        "HTML formatting is supported.",
        reply_markup=None,
    )


@router.message(_in_state("buymsg:value"), F.text)
async def handle_buymsg_value(message: Message) -> None:
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    st = _state.pop(message.from_user.id, None)
    if not st:
        return
    plan_id = st["data"]["plan_id"]
    await update_plan(plan_id, buy_message=message.text.strip())
    await message.answer(
        "✅ <b>Buy message updated for this plan!</b>\n\n"
        "Users will see it the next time they select this plan.",
        reply_markup=admin_panel_keyboard(),
    )


# ── Change Access Link ────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data == "admin_link")
async def cb_link_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    plans = await get_all_plans()
    if not plans:
        await call.message.edit_text(
            "🔗 <b>Change Access Link</b>\n\nNo plans found.",
            reply_markup=admin_panel_keyboard(),
        )
        return
    _state[call.from_user.id] = {"step": "link:select", "data": {}}
    await call.message.edit_text(
        "🔗 <b>Change Access Link</b>\n\nSelect a plan:",
        reply_markup=admin_plan_list_keyboard(plans, "admin_lp"),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_lp:"))
async def cb_link_plan_selected(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    plan_id = int(call.data.split(":", 1)[1])
    await call.answer()
    plan = await get_plan(plan_id)
    if not plan:
        await call.answer("Plan not found.", show_alert=True)
        return
    _state[call.from_user.id] = {
        "step": "link:value",
        "data": {"plan_id": plan_id},
    }
    await call.message.edit_text(
        f"🔗 <b>Change Access Link: {plan['name']}</b>\n\n"
        f"Current link: {plan['access_link'] or '—'}\n\n"
        "Send the new access link:",
        reply_markup=None,
    )


@router.message(_in_state("link:value"), F.text)
async def handle_link_value(message: Message) -> None:
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    st = _state.pop(message.from_user.id, None)
    if not st:
        return
    plan_id = st["data"]["plan_id"]
    await update_plan(plan_id, access_link=message.text.strip())
    await message.answer(
        "✅ Access link updated successfully!",
        reply_markup=admin_panel_keyboard(),
    )


# ── Payment Settings ──────────────────────────────────────────────────────────

async def _payment_settings_panel(target) -> None:
    """Show (or re-show) the Payment Settings sub-panel."""
    mode = (await get_setting("payment_mode", "automatic")) or "automatic"
    mode_label = "🟢 Automatic" if mode == "automatic" else "🟠 Manual"
    qr_val  = (await get_setting("manual_payment_qr",  "")) or ""
    upi_val = (await get_setting("manual_upi_text",    "")) or ""
    text = (
        "💳 <b>Payment Settings</b>\n\n"
        f"Active mode: <b>{mode_label}</b>\n"
        f"Manual QR: {'✅ set' if qr_val else '⬜ not set'}\n"
        f"Manual UPI text: {'✅ set' if upi_val else '⬜ not set'}\n\n"
        "Select an option:"
    )
    kb = payment_settings_keyboard(mode)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except Exception:
            await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data == "admin_payment_settings")
async def cb_payment_settings_panel(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state.pop(call.from_user.id, None)
    await _payment_settings_panel(call)


@router.callback_query(lambda c: c.data == "admin_pm_automatic")
async def cb_pm_automatic(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await set_setting("payment_mode", "automatic")
    await call.answer("🟢 Switched to Automatic Payment.", show_alert=True)
    await _payment_settings_panel(call)


@router.callback_query(lambda c: c.data == "admin_pm_manual")
async def cb_pm_manual(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await set_setting("payment_mode", "manual")
    await call.answer("🟠 Switched to Manual Payment.", show_alert=True)
    await _payment_settings_panel(call)


@router.callback_query(lambda c: c.data == "admin_pm_qr")
async def cb_pm_qr_start(call: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "payment:qr", "data": {}}
    current_file_id = (await get_setting("manual_payment_qr", "")) or ""
    if current_file_id:
        try:
            await call.message.answer_photo(
                photo=current_file_id,
                caption=(
                    "🖼 <b>Manual Payment QR</b>\n\n"
                    "<b>Current QR image shown above.</b>\n\n"
                    "Send a new photo to replace it.\n"
                    "Type /cancel to abort."
                ),
            )
            return
        except Exception:
            pass  # stale file_id — fall through to plain text prompt
    await call.message.answer(
        "🖼 <b>Manual Payment QR</b>\n\n"
        "No QR image is currently saved.\n\n"
        "Send a photo of the QR code to save it.\n"
        "Type /cancel to abort.",
    )


@router.message(_in_state("payment:qr"), F.photo)
async def handle_pm_qr_photo(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    _state.pop(message.from_user.id, None)
    file_id = message.photo[-1].file_id
    await set_setting("manual_payment_qr", file_id)
    await message.answer(
        "✅ <b>Manual Payment QR updated!</b>\n\n"
        "The new QR code will be shown to users during manual payment.",
        reply_markup=admin_panel_keyboard(),
    )


@router.message(_in_state("payment:qr"))
async def handle_pm_qr_wrong_type(message: Message) -> None:
    """Reject non-photo messages while waiting for the manual QR image."""
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    await message.answer("⚠️ Please send a <b>photo</b> of the QR code.")


@router.callback_query(lambda c: c.data == "admin_pm_upi")
async def cb_pm_upi_start(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Unauthorised.", show_alert=True)
        return
    await call.answer()
    _state[call.from_user.id] = {"step": "payment:upi", "data": {}}
    current = (await get_setting("manual_upi_text", "")) or ""
    await call.message.edit_text(
        "📝 <b>Manual UPI Text</b>\n\n"
        f"Current value: {current if current else '(not set)'}\n\n"
        "Send the new UPI ID or payment text to display during manual payment.\n"
        "Type /cancel to abort.",
        reply_markup=None,
    )


@router.message(_in_state("payment:upi"), F.text)
async def handle_pm_upi_input(message: Message) -> None:
    if not _is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    _state.pop(message.from_user.id, None)
    await set_setting("manual_upi_text", message.text.strip())
    await message.answer(
        "✅ <b>Manual UPI text updated!</b>",
        reply_markup=admin_panel_keyboard(),
    )
