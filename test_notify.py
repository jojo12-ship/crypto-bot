import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import notify


class TelegramPollingTests(unittest.TestCase):
    def setUp(self) -> None:
        notify._delivery_enabled = True
        notify._last_operational_error_at = 0.0
        notify._last_poll_success_monotonic = None
        notify._last_poll_error = None
        notify._consecutive_poll_failures = 0
        notify._status_render_inflight = False

    @patch("notify.current_runtime_ownership")
    @patch("notify._save_state_locked")
    @patch("notify.broadcast")
    def test_operational_errors_are_limited_globally_to_one_per_hour(
        self, broadcast, save_state, ownership
    ) -> None:
        ownership.return_value = SimpleNamespace(is_designated_service=True)
        with patch.object(notify, "TOKEN", "configured"):
            self.assertEqual(
                broadcast.return_value,
                notify.broadcast_operational_error("SOL failed", now=10_000),
            )
            self.assertEqual(
                0,
                notify.broadcast_operational_error("BTC failed", now=10_060),
            )
            self.assertEqual(
                broadcast.return_value,
                notify.broadcast_operational_error("ETH failed", now=13_600),
            )

        self.assertEqual(
            [unittest.mock.call("SOL failed"), unittest.mock.call("ETH failed")],
            broadcast.call_args_list,
        )
        self.assertEqual(2, save_state.call_count)

    @patch("notify.current_runtime_ownership")
    @patch("notify._save_state_locked")
    @patch("notify.broadcast")
    def test_trade_alerts_bypass_operational_error_limit(
        self, broadcast, save_state, ownership
    ) -> None:
        ownership.return_value = SimpleNamespace(is_designated_service=True)
        notify._last_operational_error_at = 10_000
        with patch.object(notify, "TOKEN", "configured"):
            notify.broadcast("BUY SOLUSDT")
            notify.broadcast("STOP LOSS SOLUSDT")

        self.assertEqual(
            [unittest.mock.call("BUY SOLUSDT"), unittest.mock.call("STOP LOSS SOLUSDT")],
            broadcast.call_args_list,
        )
        save_state.assert_not_called()

    @patch("notify.current_runtime_ownership")
    @patch("notify._post")
    def test_successful_poll_is_reported_healthy(self, post, ownership) -> None:
        ownership.return_value = SimpleNamespace(is_designated_service=True)
        post.return_value = {"ok": True, "result": []}

        self.assertTrue(
            notify.poll_commands(
                lambda: "status",
                lambda: "pause",
                lambda: "resume",
                timeout_seconds=0,
            )
        )

        status = notify.telegram_health_snapshot()
        self.assertTrue(status["polling_healthy"])
        self.assertEqual(status["consecutive_poll_failures"], 0)
        self.assertIsNone(status["last_error"])

    @patch("notify.current_runtime_ownership")
    @patch("notify._post")
    def test_api_error_keeps_polling_unhealthy(self, post, ownership) -> None:
        ownership.return_value = SimpleNamespace(is_designated_service=True)
        post.return_value = {
            "ok": False,
            "error_code": 409,
            "description": "Conflict: terminated by another getUpdates request",
        }

        self.assertFalse(
            notify.poll_commands(
                lambda: "status",
                lambda: "pause",
                lambda: "resume",
                timeout_seconds=0,
            )
        )

        status = notify.telegram_health_snapshot()
        self.assertFalse(status["polling_healthy"])
        self.assertEqual(status["consecutive_poll_failures"], 1)
        self.assertIn("Conflict", status["last_error"])

    @patch("notify._post")
    def test_invalid_token_fails_configuration_validation(self, post) -> None:
        post.return_value = {
            "ok": False,
            "error_code": 401,
            "description": "Unauthorized",
        }

        with self.assertRaisesRegex(RuntimeError, "Unauthorized"):
            notify.validate_configuration()

    @patch("notify.requests.post")
    def test_http_read_timeout_exceeds_telegram_long_poll(self, post) -> None:
        response = Mock()
        response.json.return_value = {"ok": True, "result": []}
        post.return_value = response

        notify._post("getUpdates", offset=0, timeout=20)

        self.assertEqual((5, 30), post.call_args.kwargs["timeout"])

    @patch("notify.current_runtime_ownership")
    @patch("notify._post")
    @patch("notify._save_state_locked")
    def test_status_button_returns_status_and_preserves_keyboard(
        self, save_state, post, ownership
    ) -> None:
        ownership.return_value = SimpleNamespace(is_designated_service=True)
        notify._subscribers = {123}
        notify._offset = 0
        post.side_effect = [
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 123},
                            "text": "📊 Status",
                        },
                    }
                ],
            },
            {"ok": True},
        ]

        self.assertTrue(
            notify.poll_commands(
                lambda: "current status",
                lambda: "pause",
                lambda: "resume",
                timeout_seconds=0,
            )
        )

        self.assertEqual(post.call_count, 2)
        self.assertEqual("sendMessage", post.call_args.args[0])
        self.assertEqual("current status", post.call_args.kwargs["text"])
        self.assertEqual(
            [[{"text": "📊 Status"}]],
            post.call_args.kwargs["reply_markup"]["keyboard"],
        )
        save_state.assert_called_once()

    @patch("notify.current_runtime_ownership")
    @patch("notify._post")
    @patch("notify._save_state_locked")
    def test_status_formatter_failure_returns_visible_fallback(
        self, save_state, post, ownership
    ) -> None:
        ownership.return_value = SimpleNamespace(is_designated_service=True)
        notify._subscribers = {123}
        notify._offset = 0
        post.side_effect = [
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 123},
                            "text": "/status",
                        },
                    }
                ],
            },
            {"ok": True},
        ]

        def broken_status() -> str:
            raise RuntimeError("bad production state")

        self.assertTrue(
            notify.poll_commands(
                broken_status,
                lambda: "pause",
                lambda: "resume",
                timeout_seconds=0,
            )
        )
        self.assertIn(
            "status is temporarily unavailable",
            post.call_args.kwargs["text"],
        )
        save_state.assert_called_once()

    @patch("notify.current_runtime_ownership")
    @patch("notify._post")
    @patch("notify._save_state_locked")
    def test_html_reply_failure_retries_as_plain_text(
        self, save_state, post, ownership
    ) -> None:
        ownership.return_value = SimpleNamespace(is_designated_service=True)
        notify._subscribers = {123}
        notify._offset = 0
        post.side_effect = [
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 123},
                            "text": "/status",
                        },
                    }
                ],
            },
            {"ok": False, "description": "can't parse entities"},
            {"ok": True},
        ]

        self.assertTrue(
            notify.poll_commands(
                lambda: "<b>Current</b> P&amp;L",
                lambda: "pause",
                lambda: "resume",
                timeout_seconds=0,
            )
        )
        self.assertEqual(3, post.call_count)
        self.assertEqual("Current P&L", post.call_args.kwargs["text"])
        self.assertNotIn("parse_mode", post.call_args.kwargs)
        save_state.assert_called_once()

    @patch("notify.current_runtime_ownership")
    @patch("notify._post")
    @patch("notify._save_state_locked")
    def test_command_reply_failure_marks_polling_unhealthy(
        self, save_state, post, ownership
    ) -> None:
        ownership.return_value = SimpleNamespace(is_designated_service=True)
        notify._subscribers = {123}
        notify._offset = 0
        post.side_effect = [
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 123},
                            "text": "/status",
                        },
                    }
                ],
            },
            {"ok": False, "description": "send failed"},
            {"ok": False, "description": "fallback failed"},
        ]

        self.assertFalse(
            notify.poll_commands(
                lambda: "<b>Current status</b>",
                lambda: "pause",
                lambda: "resume",
                timeout_seconds=0,
            )
        )
        health = notify.telegram_health_snapshot()
        self.assertFalse(health["polling_healthy"])
        self.assertIn("could not send", health["last_error"])
        save_state.assert_called_once()

    def test_repeated_status_timeouts_keep_only_one_renderer_inflight(self) -> None:
        release = threading.Event()
        started = threading.Event()
        calls = 0

        def blocked_status() -> str:
            nonlocal calls
            calls += 1
            started.set()
            release.wait()
            return "late status"

        first = notify._render_status(blocked_status, timeout_seconds=0.01)
        self.assertTrue(started.is_set())
        self.assertIn("status is temporarily unavailable", first)
        self.assertTrue(notify._status_render_inflight)

        second = notify._render_status(blocked_status, timeout_seconds=0.01)
        self.assertIn("status is temporarily unavailable", second)
        self.assertEqual(1, calls)

        release.set()
        deadline = time.monotonic() + 1
        while notify._status_render_inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(notify._status_render_inflight)


if __name__ == "__main__":
    unittest.main()