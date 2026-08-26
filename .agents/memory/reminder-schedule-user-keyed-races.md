---
name: User-keyed schedule docs need order-scoping to avoid cross-order races
description: Lesson from building an abandoned-payment reminder scheduler keyed by user_id in a single-process bot with MongoDB
---

When a background scheduler stores per-user state (e.g. `_id: user_id`) that
gets replaced on a repeat user action (e.g. clicking "Buy Now" again creates
a new order and replaces the old reminder schedule), any later code that
cancels or mutates that state by `user_id` alone can race with the replacement
and corrupt/delete the *new* schedule instead of the one it meant to touch.

**Why:** A code review on the PremiumBot reminder feature caught two such
races: (1) the scheduler tick marking a reminder "sent"/deleting it by
`user_id` right after the user re-bought and got a fresh schedule, and (2)
`approve_order` cancelling "the reminder for this user" when an *older* order
got approved late, wiping a schedule that actually belonged to a newer order.

**How to apply:** Whenever a per-user record can be replaced by a repeat
action, always scope reads/writes/deletes that reference it by the compound
key `(user_id, order_id)` (or equivalent transaction/version id), never by
`user_id` alone — even though the primary key is just `user_id` for the
"replace, don't duplicate" behavior.
