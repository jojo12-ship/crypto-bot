import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import notify


class TelegramPollingTests(unittest.TestCase):
    def setUp(self) -> None:
        notify._delivery_enabled = True
        notify._last_poll_success_monotonic = None
        notify._last_poll_error = None
        notify._consecutive_poll_failures = 0

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


if __name__ == "__main__":
    unittest.main()