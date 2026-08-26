from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ── Admin panel ───────────────────────────────────────────────────────────────

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Add Plan",    callback_data="admin_add"),
                InlineKeyboardButton(text="✏️ Edit Plan",   callback_data="admin_edit"),
                InlineKeyboardButton(text="🗑 Delete Plan", callback_data="admin_delete"),
            ],
            [
                InlineKeyboardButton(text="🎥 Change Demo Videos", callback_data="admin_demo"),
                InlineKeyboardButton(text="🔗 Change Access Link", callback_data="admin_link"),
            ],
            [
                InlineKeyboardButton(text="📝 Edit Plan Buy Message", callback_data="admin_buymsg"),
                InlineKeyboardButton(text="💳 Edit Plan QR",          callback_data="admin_planqr"),
            ],
            [
                InlineKeyboardButton(text="📹 Start Demo Settings", callback_data="admin_startdemo"),
            ],
            [
                InlineKeyboardButton(text="🔔 Payment Reminder Settings", callback_data="admin_reminders"),
            ],
            [
                InlineKeyboardButton(text="👥 Referral Settings", callback_data="admin_referral"),
            ],
            [
                InlineKeyboardButton(text="💳 Payment Settings", callback_data="admin_payment_settings"),
            ],
            [
                InlineKeyboardButton(text="📊 Statistics", callback_data="admin_stats"),
                InlineKeyboardButton(text="📢 Broadcast",  callback_data="admin_broadcast"),
            ],
            [
                InlineKeyboardButton(text="📢 Premium Subscribers", callback_data="admin_premium_subscribers"),
            ],
            [
                InlineKeyboardButton(text="📋 Pending Payments", callback_data="admin_pending"),
                InlineKeyboardButton(text="📦 Plans",            callback_data="admin_plans"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Settings", callback_data="admin_settings"),
            ],
        ]
    )


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    """Settings sub-panel."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Welcome Message", callback_data="settings_welcome")],
            [InlineKeyboardButton(text="💳 Payment Message",  callback_data="settings_payment")],
            [InlineKeyboardButton(text="🖼 QR Image",         callback_data="settings_qr")],
            [InlineKeyboardButton(text="👥 Support Group",    callback_data="settings_support")],
            [InlineKeyboardButton(text="⬅️ Back",             callback_data="settings_back")],
        ]
    )


def settings_cancel_keyboard() -> InlineKeyboardMarkup:
    """Shown while awaiting new setting value — lets admin bail out."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="settings_cancel")],
        ]
    )


def admin_plan_list_keyboard(plans: list[dict], cb_prefix: str) -> InlineKeyboardMarkup:
    """Render a list of plans as inline buttons. cb_prefix:plan_id is the callback_data."""
    rows = [
        [InlineKeyboardButton(
            text=f"📦 {p['name']} — ₹{p['price']}",
            callback_data=f"{cb_prefix}:{p['id']}",
        )]
        for p in plans
    ]
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def premium_subscribers_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Send", callback_data="admin_ps_send"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="admin_ps_cancel"),
            ],
        ]
    )


def admin_edit_fields_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Name",         callback_data=f"admin_ef:{plan_id}:name")],
            [InlineKeyboardButton(text="💰 Price",        callback_data=f"admin_ef:{plan_id}:price")],
            [InlineKeyboardButton(text="⏳ Validity",     callback_data=f"admin_ef:{plan_id}:validity")],
            [InlineKeyboardButton(text="🔗 Access Link",  callback_data=f"admin_ef:{plan_id}:access_link")],
            [
                InlineKeyboardButton(text="⬆ Move Up",   callback_data=f"admin_mv:up:{plan_id}"),
                InlineKeyboardButton(text="⬇ Move Down", callback_data=f"admin_mv:down:{plan_id}"),
            ],
            [
                InlineKeyboardButton(text="⏫ Move to Top",    callback_data=f"admin_mv:top:{plan_id}"),
                InlineKeyboardButton(text="⏬ Move to Bottom", callback_data=f"admin_mv:bottom:{plan_id}"),
            ],
            [InlineKeyboardButton(text="❌ Cancel",       callback_data="admin_cancel")],
        ]
    )


def admin_delete_confirm_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, Delete", callback_data=f"admin_dc:{plan_id}"),
                InlineKeyboardButton(text="❌ No, Cancel",  callback_data="admin_cancel"),
            ]
        ]
    )


def admin_demo_done_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ Done ({count} video{'s' if count != 1 else ''} collected)",
                callback_data="admin_demo_done",
            )],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel")],
        ]
    )


def start_demo_settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Sub-menu for Start Demo Settings."""
    status = "✅ Enabled" if enabled else "🚫 Disabled"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Status: {status}", callback_data="admin_sd_noop")],
            [InlineKeyboardButton(text="✅ Enable",  callback_data="admin_sd_enable")],
            [InlineKeyboardButton(text="🚫 Disable", callback_data="admin_sd_disable")],
            [InlineKeyboardButton(text="🔄 Change Start Demo Videos", callback_data="admin_sd_change")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_cancel")],
        ]
    )


def reminder_settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Sub-menu for Payment Reminder Settings."""
    status = "✅ Enabled" if enabled else "🚫 Disabled"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Status: {status}", callback_data="admin_rm_noop")],
            [InlineKeyboardButton(text="✅ Enable",  callback_data="admin_rm_enable")],
            [InlineKeyboardButton(text="🚫 Disable", callback_data="admin_rm_disable")],
            [InlineKeyboardButton(text="⏱ Edit First Reminder Delay",   callback_data="admin_rm_first")],
            [InlineKeyboardButton(text="🕒 Edit Second Reminder Delay", callback_data="admin_rm_second")],
            [InlineKeyboardButton(text="✏ Edit 15 Minute Reminder Message", callback_data="admin_rm_msg15")],
            [InlineKeyboardButton(text="✏ Edit 24 Hour Reminder Message",   callback_data="admin_rm_msg24")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_cancel")],
        ]
    )


def reminder_buy_now_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    """
    Fixed, non-editable button attached to every reminder message. Reuses the
    same buy:{plan_id} callback as the normal Buy Now flow, so tapping it
    resumes payment for the exact plan the reminder was scheduled for.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Buy Now", callback_data=f"buy:{plan_id}")],
        ]
    )


def start_demo_done_keyboard(count: int) -> InlineKeyboardMarkup:
    """Shown while admin is forwarding start demo videos."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ Done ({count} video{'s' if count != 1 else ''} collected)",
                callback_data="admin_sd_done",
            )],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel")],
        ]
    )


def admin_confirm_save_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Save",   callback_data="admin_save"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel"),
            ]
        ]
    )


# ── User-facing plan list (dynamic) ───────────────────────────────────────────

_SUPPORT_URL = "https://t.me/+a0bn6M0FzXUxNmY9"

def plans_list_keyboard(plans: list[dict]) -> InlineKeyboardMarkup:
    """Show all plans as selectable buttons, with Support & Get Discount once at the bottom."""
    rows = [
        [InlineKeyboardButton(
            text=f"📦 {p['name']} — ₹{p['price']} / {p['validity']}",
            callback_data=f"plan:{p['id']}",
        )]
        for p in plans
    ]
    rows.append([
        InlineKeyboardButton(text="📞 Support",      url=_SUPPORT_URL),
        InlineKeyboardButton(text="🏷️ Get Discount", callback_data="open_refer"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_interest_reminder_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    """Keyboard sent with the plan-interest reminder.

    'Buy Now' opens the exact plan the user last viewed.
    'Get Discount' opens the referral page.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Buy Now",        callback_data=f"buy:{plan_id}")],
            [InlineKeyboardButton(text="🏷️ Get Discount",   callback_data="open_refer")],
        ]
    )


def plan_detail_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    """Plan detail screen — Buy Now + Back."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Buy Now",   callback_data=f"buy:{plan_id}")],
            [InlineKeyboardButton(text="⬅️ Back",       callback_data="back")],
        ]
    )


# ── Payment flow ──────────────────────────────────────────────────────────────

def payment_details_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Check Payment Status", callback_data=f"paid:{order_id}")],
            [InlineKeyboardButton(text="❌ Cancel",       callback_data=f"cancel_order:{order_id}")],
        ]
    )


def manual_payment_keyboard(order_id: str) -> InlineKeyboardMarkup:
    """Keyboard shown on the manual payment screen."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Upload Payment Screenshot", callback_data=f"upload_proof:{order_id}")],
            [InlineKeyboardButton(text="❌ Cancel",                    callback_data=f"cancel_order:{order_id}")],
        ]
    )


def manual_review_keyboard(order_id: str, user_id: int) -> InlineKeyboardMarkup:
    """Approve / Reject buttons posted to the review channel alongside the screenshot."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"manual_approve:{order_id}:{user_id}"),
                InlineKeyboardButton(text="❌ Reject",  callback_data=f"manual_reject:{order_id}:{user_id}"),
            ]
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
        ]
    )


# ── Referral admin keyboards ──────────────────────────────────────────────────

def referral_settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Sub-panel for Referral Settings."""
    status = "✅ Enabled" if enabled else "❌ Disabled"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Status: {status}",                    callback_data="admin_ref_noop")],
            [InlineKeyboardButton(text="✅ Enable Referral System",             callback_data="admin_ref_enable")],
            [InlineKeyboardButton(text="❌ Disable Referral System",            callback_data="admin_ref_disable")],
            [InlineKeyboardButton(text="✏️ Edit Referral Reward (%)",           callback_data="admin_ref_reward")],
            [InlineKeyboardButton(text="✏️ Edit Maximum Referral Discount (%)", callback_data="admin_ref_maxdiscount")],
            [InlineKeyboardButton(text="👥 Edit Maximum Referrals",             callback_data="admin_ref_maxreferrals")],
            [InlineKeyboardButton(text="➕ Add Referral",                       callback_data="admin_ref_add")],
            [InlineKeyboardButton(text="📊 Referral Statistics",                callback_data="admin_ref_stats")],
            [InlineKeyboardButton(text="🔄 Reset Referral Data",                callback_data="admin_ref_reset")],
            [InlineKeyboardButton(text="⬅️ Back",                               callback_data="admin_cancel")],
        ]
    )


def payment_settings_keyboard(mode: str) -> InlineKeyboardMarkup:
    """Sub-panel for Payment Settings. Marks the active mode with ✅."""
    auto_mark   = "✅ " if mode == "automatic" else ""
    manual_mark = "✅ " if mode == "manual"    else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{auto_mark}🟢 Automatic Payment",      callback_data="admin_pm_automatic")],
            [InlineKeyboardButton(text=f"{manual_mark}🟠 Manual Payment",        callback_data="admin_pm_manual")],
            [InlineKeyboardButton(text="🖼 Change Manual Payment QR",            callback_data="admin_pm_qr")],
            [InlineKeyboardButton(text="📝 Change Manual UPI Text",              callback_data="admin_pm_upi")],
            [InlineKeyboardButton(text="⬅️ Back",                                callback_data="admin_cancel")],
        ]
    )


def referral_reset_confirm_keyboard() -> InlineKeyboardMarkup:
    """Confirmation keyboard before wiping all referral data."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, Reset Everything", callback_data="admin_ref_reset_confirm"),
                InlineKeyboardButton(text="❌ Cancel",                callback_data="admin_referral"),
            ]
        ]
    )


# ── Referral reminder keyboard ────────────────────────────────────────────────

def referral_reminder_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard sent with the one-time referral reminder."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏷️ Get Discount", callback_data="open_refer")],
        ]
    )


# ── Referral user-facing ──────────────────────────────────────────────────────

def refer_share_keyboard(referral_link: str) -> InlineKeyboardMarkup:
    """Share button that opens Telegram's native share dialog with a pre-filled message."""
    from urllib.parse import quote
    share_text = quote("🎉 Join this premium bot and unlock exclusive content!\n\n" + referral_link)
    share_url  = f"https://t.me/share/url?url={quote(referral_link)}&text={share_text}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Share Link", url=share_url)],
        ]
    )


# ── Legacy aliases (kept so older imports don't break) ────────────────────────

def product_keyboard() -> InlineKeyboardMarkup:
    """Fallback shown when no plans exist yet."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 View Plans", callback_data="show_plans")]
        ]
    )
