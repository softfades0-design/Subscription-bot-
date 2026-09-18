import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("VC_GATEWAY_UPI_ID", "merchant@example")

import handlers.payment as payment  # noqa: E402


class VcGatewayTests(unittest.TestCase):
    def test_qr_contains_stored_amount_and_vc_order_id(self):
        with patch.object(payment, "VC_GATEWAY_UPI_ID", "merchant@example"):
            provider_order_id = "VC2609171412446FEE8B01"
            uri = payment._build_vc_upi_uri("129.50", provider_order_id)
        query = parse_qs(urlsplit(uri).query)
        self.assertEqual(query["pa"], ["merchant@example"])
        self.assertEqual(query["am"], ["129.5"])
        self.assertEqual(query["tid"], [provider_order_id])
        self.assertEqual(query["tr"], [provider_order_id])
        self.assertEqual(query["tn"], [provider_order_id])
        self.assertTrue(payment._generate_vc_qr_bytes("129.50", provider_order_id))

    def test_provider_order_id_uses_gateway_format(self):
        order_id = payment._make_vc_order_id()
        self.assertRegex(order_id, r"^VC\d{12}[0-9A-F]{8}$")
        self.assertFalse(order_id.startswith("ORD"))
        self.assertTrue(payment._is_valid_vc_order_id(order_id))
        self.assertTrue(payment._is_valid_vc_order_id("VC2609171412446FEE8B01"))
        self.assertFalse(payment._is_valid_vc_order_id("ORD260917ABC123"))

    def test_vc_provider_id_starts_with_uppercase_vc(self):
        provider_order_id = payment._make_vc_order_id()
        self.assertTrue(provider_order_id.startswith("VC"))
        self.assertFalse(provider_order_id.startswith(("ve", "vo", "ORD", "vc")))

    def test_response_parser_handles_json_and_plain_statuses(self):
        parsed = payment._parse_vc_gateway_response(
            '{"status":"SUCCESS","data":{"order_id":"VC1","amount":"10.00"}}'
        )
        self.assertEqual(parsed["status"], "SUCCESS")
        self.assertEqual(parsed["order_id"], "VC1")
        self.assertEqual(parsed["amount"], "10.00")
        self.assertIsNone(parsed["gateway_message"])
        self.assertEqual(payment._parse_vc_gateway_response("PENDING")["status"], "PENDING")
        self.assertIsNone(payment._parse_vc_gateway_response("not a provider response"))

    def test_mocked_gateway_statuses_and_success_validation(self):
        class FakeResponse:
            status = 200

            def __init__(self, body):
                self.body = body
                self.headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return self.body

        class FakeSession:
            def __init__(self, body):
                self.body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                self.request_args = _args
                self.request_kwargs = _kwargs
                return FakeResponse(self.body)

        order = {
            "order_id": "ORD1",
            "user_id": 7,
            "payment_provider": "vc_gateway",
            "vc_order_id": "VC2609171412446FEE8B01",
            "expected_amount": "10.00",
        }

        async def verify(body):
            with (
                patch.object(payment, "VC_GATEWAY_API_KEY", "secret"),
                patch.object(payment.aiohttp, "ClientSession", lambda **_kwargs: FakeSession(body)),
            ):
                return await payment.verify_vc_gateway_payment(order)

        for status in ("PENDING", "FAILED", "INVALID", "NOT_FOUND"):
            result, _summary = asyncio.run(verify('{"status":"%s"}' % status))
            self.assertEqual(result, status)

        success, _summary = asyncio.run(verify('{"status":"SUCCESS","order_id":"VC2609171412446FEE8B01","amount":"10.00"}'))
        self.assertEqual(success, "SUCCESS")
        mismatch, _summary = asyncio.run(verify('{"status":"SUCCESS","order_id":"VC1","amount":"11.00"}'))
        self.assertEqual(mismatch, "ERROR")

        missing_optional, _summary = asyncio.run(verify('{"status":"success"}'))
        self.assertEqual(missing_optional, "ERROR")

    def test_response_parser_normalizes_nested_and_human_status_values(self):
        parsed = payment._parse_vc_gateway_response(
            '{"DATA":{"PAYMENT_STATUS":"success","ORDER_ID":"VC1","AMOUNT":39.0}}'
        )
        self.assertEqual(parsed, {
            "status": "SUCCESS",
            "order_id": "VC1",
            "amount": 39.0,
            "message": None,
            "gateway_message": None,
        })
        self.assertEqual(
            payment._parse_vc_gateway_response('{"status":"not found"}')['status'],
            "NOT_FOUND",
        )

    def test_success_requires_provider_order_id_and_amount(self):
        order = {
            "order_id": "ORD1",
            "vc_order_id": "VC2609171412446FEE8B01",
            "expected_amount": "39.00",
        }

        async def verify(body):
            class FakeResponse:
                status = 200
                headers = {"Content-Type": "application/json"}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                async def text(self):
                    return body

            class FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                def get(self, *_args, **_kwargs):
                    return FakeResponse()

            with (
                patch.object(payment, "VC_GATEWAY_API_KEY", "secret"),
                patch.object(payment.aiohttp, "ClientSession", return_value=FakeSession()),
            ):
                return await payment.verify_vc_gateway_payment(order)

        self.assertEqual(asyncio.run(verify('{"status":"SUCCESS","amount":"39"}'))[0], "ERROR")
        self.assertEqual(asyncio.run(verify('{"status":"SUCCESS","order_id":"VC1"}'))[0], "ERROR")

    def test_invalid_order_id_gateway_message_is_not_payment_failed(self):
        order = {"order_id": "ORD1", "vc_order_id": "VC2609171412446FEE8B01", "expected_amount": "1"}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return '{"status":"failed","message":"Payment received","order_id":"VC2609171412446FEE8B01","gateway_message":"Invalid Order Id."}'

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        async def verify():
            with (
                patch.object(payment, "VC_GATEWAY_API_KEY", "secret"),
                patch.object(payment.aiohttp, "ClientSession", return_value=FakeSession()),
            ):
                return await payment.verify_vc_gateway_payment(order)

        result, summary = asyncio.run(verify())
        self.assertEqual(result, "ERROR")
        self.assertEqual(summary["status"], "FAILED")
        self.assertEqual(summary["gateway_message"], "Invalid Order Id.")

    def test_provider_order_id_is_not_restricted_to_local_format(self):
        order = {
            "order_id": "ORD-INTERNAL",
            "vc_order_id": "VC2609171412446FEE8B01",
            "expected_amount": "1",
        }
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return '{"status":"PENDING"}'

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        with (
            patch.object(payment, "VC_GATEWAY_API_KEY", "secret"),
            patch.object(payment.aiohttp, "ClientSession", return_value=FakeSession()),
        ):
            result = asyncio.run(payment.verify_vc_gateway_payment(order))

        self.assertEqual(result[0], "PENDING")

    def test_invalid_order_id_does_not_activate_vc_order(self):
        order = {
            "order_id": "ORD1",
            "user_id": 7,
            "plan_name": "Gold",
            "expected_amount": "1",
            "payment_provider": "vc_gateway",
            "vc_order_id": "VC2609171412446FEE8B01",
            "payment_status": "created",
            "expires_at": None,
            "status_message_id": 55,
        }

        class DummyCall:
            data = "vc_check:ORD1"
            from_user = SimpleNamespace(id=7, first_name="Demo")
            message = SimpleNamespace()
            answer = AsyncMock()

        async def run():
            with (
                patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
                patch.object(
                    payment,
                    "verify_vc_gateway_payment",
                    new=AsyncMock(return_value=("ERROR", {
                        "status": "FAILED",
                        "message": "Payment received",
                        "gateway_message": "Invalid Order Id.",
                    })),
                ),
                patch.object(payment, "save_provider_response_summary", new=AsyncMock()),
                patch.object(payment, "approve_vc_gateway_order", new=AsyncMock()) as approve,
                patch.object(payment, "_edit_or_create_status_message", new=AsyncMock()) as edit,
                patch.object(payment, "_replace_invalid_vc_payment", new=AsyncMock()) as replace,
            ):
                await payment.callback_vc_check(DummyCall(), AsyncMock())
            return approve, edit, replace

        approve, edit, replace = asyncio.run(run())
        approve.assert_not_awaited()
        edit.assert_awaited_once()
        replace.assert_awaited_once()

    def test_vc_check_pending_and_transport_error_keep_payment_open(self):
        order = {
            "order_id": "ORD1",
            "user_id": 7,
            "plan_id": 3,
            "plan_name": "Gold",
            "plan_price": "39",
            "expected_amount": "39.00",
            "plan_validity": "30 days",
            "access_link": "https://example.com/access",
            "payment_provider": "vc_gateway",
            "vc_order_id": "VC2609171412446FEE8B01",
            "payment_status": "created",
            "expires_at": None,
            "status_message_id": 55,
        }

        class DummyCall:
            data = "vc_check:ORD1"
            from_user = SimpleNamespace(id=7, first_name="Demo")
            message = SimpleNamespace()
            answer = AsyncMock()

        async def run(provider_status):
            with (
                patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
                patch.object(payment, "verify_vc_gateway_payment", new=AsyncMock(return_value=(provider_status, {"status": provider_status}))),
                patch.object(payment, "save_provider_response_summary", new=AsyncMock()),
                patch.object(payment, "_edit_or_create_status_message", new=AsyncMock()) as edit,
                patch.object(payment, "approve_vc_gateway_order", new=AsyncMock()) as approve,
            ):
                await payment.callback_vc_check(DummyCall(), AsyncMock())
            return edit, approve

        for provider_status, expected_text in (
            ("PENDING", "Payment not detected yet. Please wait a moment and try again."),
            ("ERROR", "Payment verification is temporarily unavailable."),
        ):
            edit, approve = asyncio.run(run(provider_status))
            self.assertFalse(approve.await_args_list)
            self.assertTrue(
                any(expected_text in repr(call.args) for call in edit.await_args_list),
                f"{provider_status}: {edit.await_args_list}",
            )
            self.assertIsNotNone(edit.await_args_list[-1].args[6])

    def test_vc_definitive_failure_replaces_once(self):
        order = {
            "order_id": "ORD1",
            "user_id": 7,
            "plan_id": 3,
            "expected_amount": "39.00",
            "payment_provider": "vc_gateway",
            "vc_order_id": "VC-OLD",
            "payment_status": "created",
            "expires_at": None,
        }

        class DummyCall:
            data = "vc_check:ORD1"
            from_user = SimpleNamespace(id=7, first_name="Demo")
            message = SimpleNamespace()
            answer = AsyncMock()

        async def run(provider_status):
            with (
                patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
                patch.object(payment, "verify_vc_gateway_payment", new=AsyncMock(return_value=(provider_status, {"status": provider_status}))),
                patch.object(payment, "save_provider_response_summary", new=AsyncMock()),
                patch.object(payment, "_edit_or_create_status_message", new=AsyncMock()),
                patch.object(payment, "_replace_invalid_vc_payment", new=AsyncMock()) as replace,
            ):
                await payment.callback_vc_check(DummyCall(), AsyncMock())
            return replace

        for provider_status in ("FAILED", "INVALID", "NOT_FOUND"):
            replace = asyncio.run(run(provider_status))
            replace.assert_awaited_once()

    def test_vc_success_activates_once_and_delivers_access_link(self):
        order = {
            "order_id": "ORD1",
            "user_id": 7,
            "plan_name": "Gold",
            "expected_amount": "39.00",
            "payment_provider": "vc_gateway",
            "vc_order_id": "VC2609171412446FEE8B01",
            "payment_status": "created",
            "expires_at": None,
            "status_message_id": 55,
        }

        class DummyCall:
            data = "vc_check:ORD1"
            from_user = SimpleNamespace(id=7, first_name="Demo")
            message = SimpleNamespace()
            answer = AsyncMock()

        approved = {
            "plan_name": "Gold",
            "plan_validity": "30 days",
            "subscription_end": datetime.now(timezone.utc),
            "access_link": "https://example.com/access",
        }

        async def run(approval):
            with (
                patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
                patch.object(payment, "verify_vc_gateway_payment", new=AsyncMock(return_value=("SUCCESS", {"status": "SUCCESS", "order_id": "VC1", "amount": "39.00"}))),
                patch.object(payment, "save_provider_response_summary", new=AsyncMock()),
                patch.object(payment, "approve_vc_gateway_order", new=AsyncMock(return_value=approval)) as approve,
                patch.object(payment, "log_payment_success", new=AsyncMock()),
                patch.object(payment, "_edit_or_create_status_message", new=AsyncMock()) as edit,
            ):
                await payment.callback_vc_check(DummyCall(), AsyncMock())
            return approve, edit

        approve, edit = asyncio.run(run(approved))
        approve.assert_awaited_once()
        self.assertIn("https://example.com/access", edit.await_args_list[-1].args[5])

        approve, edit = asyncio.run(run(None))
        approve.assert_awaited_once()
        self.assertIn("already activated", edit.await_args_list[-1].args[5])

    def test_repeated_vc_status_checks_reuse_current_status_message(self):
        order = {
            "order_id": "ORD1",
            "user_id": 7,
            "plan_name": "Gold",
            "expected_amount": "39.00",
            "payment_provider": "vc_gateway",
            "vc_order_id": "VC2609171412446FEE8B01",
            "payment_status": "created",
            "expires_at": None,
            "status_message_id": 55,
        }

        class DummyCall:
            data = "vc_check:ORD1"
            from_user = SimpleNamespace(id=7, first_name="Demo")
            message = SimpleNamespace()
            answer = AsyncMock()

        async def run():
            with (
                patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
                patch.object(payment, "verify_vc_gateway_payment", new=AsyncMock(return_value=("PENDING", {"status": "PENDING"}))),
                patch.object(payment, "save_provider_response_summary", new=AsyncMock()),
                patch.object(payment, "_edit_or_create_status_message", new=AsyncMock()) as edit,
            ):
                await payment.callback_vc_check(DummyCall(), AsyncMock())
                await payment.callback_vc_check(DummyCall(), AsyncMock())
            return edit

        edit = asyncio.run(run())
        final_infos = [call.args[4] for call in edit.await_args_list if len(call.args) > 5]
        self.assertEqual(len(final_infos), 4)
        self.assertTrue(all(info["status_message_id"] == 55 for info in final_infos))

    def test_vc_check_rejects_expired_and_superseded_orders_before_api_call(self):
        class DummyCall:
            data = "vc_check:ORD1"
            from_user = SimpleNamespace(id=7, first_name="Demo")
            message = SimpleNamespace()
            answer = AsyncMock()

        for payment_status, expires_at in (
            ("superseded", None),
            ("created", datetime.now(timezone.utc) - timedelta(minutes=1)),
        ):
            order = {
                "order_id": "ORD1",
                "user_id": 7,
                "payment_provider": "vc_gateway",
                "payment_status": payment_status,
                "expires_at": expires_at,
                "status_message_id": 55,
            }
            async def run():
                with (
                    patch.object(payment, "get_order", new=AsyncMock(return_value=order)),
                    patch.object(payment, "verify_vc_gateway_payment", new=AsyncMock()) as verify,
                    patch.object(payment, "update_order_status", new=AsyncMock()),
                    patch.object(payment, "_edit_or_create_status_message", new=AsyncMock()),
                ):
                    await payment.callback_vc_check(DummyCall(), AsyncMock())
                return verify

            verify = asyncio.run(run())
            verify.assert_not_awaited()

    def test_gateway_request_uses_vc_order_id_and_expected_amount(self):
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return '{"status":"PENDING"}'

        class FakeSession:
            def __init__(self, **_kwargs):
                self.params = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, url, **kwargs):
                self.url = url
                self.params = kwargs["params"]
                return FakeResponse()

        session = FakeSession()
        order = {"order_id": "ORD-INTERNAL", "vc_order_id": "VC2609171412446FEE8B01", "expected_amount": "39.00"}
        async def verify():
            with (
                patch.object(payment, "VC_GATEWAY_API_KEY", "secret"),
                patch.object(payment.aiohttp, "ClientSession", return_value=session),
                patch.object(payment.logger, "info") as info,
            ):
                result = await payment.verify_vc_gateway_payment(order)
                return result, info

        (result, _summary), info = asyncio.run(verify())
        self.assertEqual(result, "PENDING")
        self.assertEqual(session.url, payment.VC_GATEWAY_API_URL)
        self.assertEqual(session.params["api_key"], "secret")
        self.assertEqual(session.params["order_id"], order["vc_order_id"])
        self.assertEqual(session.params["amount"], "39")
        qr_query = parse_qs(urlsplit(payment._build_vc_upi_uri("39.00", order["vc_order_id"])).query)
        self.assertEqual(qr_query["tn"], [session.params["order_id"]])
        self.assertEqual(qr_query["am"], [session.params["amount"]])
        self.assertNotEqual(session.params["order_id"], order["order_id"])
        self.assertNotIn("secret", repr(info.call_args_list))

    def test_missing_gateway_api_key_is_configuration_error_and_is_not_logged(self):
        order = {"order_id": "ORD1", "vc_order_id": "VC2609171412446FEE8B01", "expected_amount": "39.00"}
        with patch.object(payment, "VC_GATEWAY_API_KEY", ""), patch.object(
            payment.logger, "warning"
        ) as warning:
            result, summary = asyncio.run(payment.verify_vc_gateway_payment(order))

        self.assertEqual((result, summary), ("ERROR", None))
        warning.assert_called_once_with("VC Gateway API key is not configured")
        self.assertNotIn("secret", repr(warning.call_args))

    def test_provider_settings_default_to_famapp_and_manual(self):
        async def setting(_key, default=""):
            return default

        with patch.object(payment, "get_setting", new=setting):
            providers = asyncio.run(payment._enabled_payment_providers())
        self.assertEqual(providers, ["famapp", "manual"])

    def test_active_provider_defaults_to_famapp(self):
        async def setting(_key, default=""):
            return default

        with patch.object(payment, "get_setting", new=setting):
            self.assertEqual(asyncio.run(payment._active_payment_provider()), "famapp")

    def test_invalid_active_provider_falls_back_to_famapp(self):
        with patch.object(payment, "get_setting", new=AsyncMock(return_value="unknown")):
            self.assertEqual(asyncio.run(payment._active_payment_provider()), "famapp")

    def test_active_provider_is_normalized_from_the_single_setting(self):
        with patch.object(payment, "get_setting", new=AsyncMock(return_value="  VC_GATEWAY ")):
            self.assertEqual(asyncio.run(payment.get_active_payment_provider()), "vc_gateway")

    def test_buy_routes_only_to_the_selected_provider(self):
        class DummyMessage:
            chat = SimpleNamespace(id=123)
            answer = AsyncMock(return_value=SimpleNamespace(message_id=99, delete=AsyncMock()))

        class DummyCall:
            data = "buy:3"
            message = DummyMessage()
            from_user = SimpleNamespace(id=7, first_name="Demo")
            answer = AsyncMock()

        async def run(provider):
            with (
                patch.object(payment, "get_active_payment_provider", new=AsyncMock(return_value=provider)),
                patch.object(payment, "get_plan", new=AsyncMock(return_value={
                    "name": "Gold",
                    "price": "199",
                    "validity": "30 days",
                    "access_link": "https://example.com/access",
                })),
                patch.object(payment, "cancel_start_reminders", new=AsyncMock()),
                patch.object(payment, "user_has_active_plan", new=AsyncMock(return_value=False)),
                patch.object(payment, "log_payment_started", new=AsyncMock()),
                patch.object(payment, "clear_plan_interest", new=AsyncMock()),
                patch.object(payment, "get_user_referral_info", new=AsyncMock(return_value={"referral_discount": 0})),
                patch.object(payment, "_send_payment_screen", new=AsyncMock(return_value="FAM-1")) as famapp,
                patch.object(payment, "_send_manual_payment_screen", new=AsyncMock(return_value="MAN-1")) as manual,
                patch.object(payment, "create_vc_gateway_payment", new=AsyncMock(return_value="VC-1")) as vc,
            ):
                await payment.callback_buy(DummyCall(), AsyncMock())
            return famapp, manual, vc

        for provider, expected in (
            ("famapp", "famapp"),
            ("manual", "manual"),
            ("vc_gateway", "vc_gateway"),
        ):
            famapp, manual, vc = asyncio.run(run(provider))
            calls = {"famapp": famapp, "manual": manual, "vc_gateway": vc}
            calls[expected].assert_awaited_once()
            for name, mock in calls.items():
                if name != expected:
                    mock.assert_not_awaited()

    def test_legacy_payment_method_callback_does_not_override_active_provider(self):
        call = SimpleNamespace(
            data="payment_method:3:famapp",
            message=SimpleNamespace(),
            answer=AsyncMock(),
        )
        bot = AsyncMock()
        with patch.object(payment, "callback_buy", new=AsyncMock()) as callback_buy:
            asyncio.run(payment.callback_payment_method(call, bot))
        callback_buy.assert_awaited_once_with(call, bot, plan_id=3)

    def test_legacy_provider_selection_callback_routes_directly(self):
        call = SimpleNamespace(
            data="choose_payment:3",
            message=SimpleNamespace(),
            answer=AsyncMock(),
        )
        bot = AsyncMock()
        with patch.object(payment, "callback_buy", new=AsyncMock()) as callback_buy:
            asyncio.run(payment.callback_choose_payment(call, bot))
        callback_buy.assert_awaited_once_with(call, bot, plan_id=3)

    def test_all_disabled_has_no_enabled_provider(self):
        async def setting(_key, _default=""):
            return "0"

        with patch.object(payment, "get_setting", new=setting):
            providers = asyncio.run(payment._enabled_payment_providers())
        self.assertEqual(providers, [])

    def test_vc_buy_creates_fresh_provider_orders(self):
        bot = AsyncMock()
        bot.send_photo.side_effect = [SimpleNamespace(message_id=10), SimpleNamespace(message_id=12)]
        bot.send_message.side_effect = [SimpleNamespace(message_id=11), SimpleNamespace(message_id=13)]
        plan = {
            "name": "Gold",
            "price": "199",
            "validity": "30 days",
            "access_link": "https://example.com/access",
        }
        created = []

        async def record_order(**kwargs):
            created.append(kwargs)

        async def run():
            with (
                patch.object(payment, "VC_GATEWAY_UPI_ID", "merchant@example"),
                patch.object(payment, "create_order", new=record_order),
                patch.object(payment, "supersede_active_orders", new=AsyncMock()),
                patch.object(payment, "update_order_messages", new=AsyncMock()),
                patch.object(payment, "set_pending_reminder", new=AsyncMock()),
            ):
                await payment.create_vc_gateway_payment(bot, 7, 7, plan, 3, "199", "Price", 0)
                await payment.create_vc_gateway_payment(bot, 7, 7, plan, 3, "199", "Price", 0)

        asyncio.run(run())
        self.assertEqual(len(created), 2)
        self.assertNotEqual(created[0]["order_id"], created[1]["order_id"])
        self.assertNotEqual(created[0]["vc_order_id"], created[1]["vc_order_id"])
        self.assertTrue(created[0]["order_id"].startswith("ORD"))
        self.assertEqual(created[0]["payment_provider"], "vc_gateway")
        self.assertEqual(created[0]["final_price"], "199")
        self.assertRegex(created[0]["vc_order_id"], r"^VC\d{12}[0-9A-F]{8}$")
        first_qr_uri = payment._build_vc_upi_uri("199", created[0]["vc_order_id"])
        self.assertIn(created[0]["vc_order_id"], first_qr_uri)
        payment_texts = [call.args[1] for call in bot.send_message.call_args_list]
        self.assertIn(created[0]["vc_order_id"], payment_texts[0])
        self.assertIn(created[1]["vc_order_id"], payment_texts[1])

    def test_vc_payment_message_never_contains_internal_ord_id(self):
        bot = AsyncMock()
        bot.send_photo.return_value = SimpleNamespace(message_id=10)
        bot.send_message.return_value = SimpleNamespace(message_id=11)
        plan = {
            "name": "Gold",
            "price": "199",
            "validity": "30 days",
            "access_link": "https://example.com/access",
        }

        async def record_order(**kwargs):
            return None

        async def run():
            with (
                patch.object(payment, "VC_GATEWAY_UPI_ID", "merchant@example"),
                patch.object(payment, "create_order", new=record_order),
                patch.object(payment, "supersede_active_orders", new=AsyncMock()),
                patch.object(payment, "update_order_messages", new=AsyncMock()),
                patch.object(payment, "set_pending_reminder", new=AsyncMock()),
            ):
                await payment.create_vc_gateway_payment(bot, 7, 7, plan, 3, "199", "Price", 0)

        asyncio.run(run())
        payment_text = bot.send_message.call_args.args[1]
        self.assertRegex(payment_text, r"VC Order ID:</b> <code>VC\d{12}[0-9A-F]{8}</code>")
        self.assertNotRegex(payment_text, r"ORD\d")

    def test_order_provider_rejects_cross_provider_callbacks(self):
        self.assertEqual(payment._order_provider({"payment_provider": "vc_gateway"}), "vc_gateway")
        self.assertEqual(payment._order_provider({"payment_purpose": "FAP123"}), "famapp")
        self.assertEqual(payment._order_provider({}), "manual")


if __name__ == "__main__":
    unittest.main()
