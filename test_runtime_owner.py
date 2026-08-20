from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import notify
import runtime_owner
from singleton_lease import SingletonLease
from state_store import StateStorageError, configured_state_dir
import trader


class RuntimeOwnerTests(unittest.TestCase):
    def test_only_expected_railway_service_is_designated(self) -> None:
        ownership = runtime_owner.determine_runtime_ownership(
            {
                "RAILWAY_SERVICE_ID": runtime_owner.EXPECTED_RAILWAY_SERVICE_ID,
                "RAILWAY_PROJECT_ID": runtime_owner.EXPECTED_RAILWAY_PROJECT_ID,
                "RAILWAY_ENVIRONMENT_ID":
                    runtime_owner.EXPECTED_RAILWAY_ENVIRONMENT_ID,
            }
        )
        other_service = runtime_owner.determine_runtime_ownership(
            {
                "RAILWAY_SERVICE_ID": "another-service",
                "RAILWAY_PROJECT_ID": runtime_owner.EXPECTED_RAILWAY_PROJECT_ID,
                "RAILWAY_ENVIRONMENT_ID":
                    runtime_owner.EXPECTED_RAILWAY_ENVIRONMENT_ID,
            }
        )
        spoofed_replit = runtime_owner.determine_runtime_ownership(
            {"RAILWAY_SERVICE_ID": runtime_owner.EXPECTED_RAILWAY_SERVICE_ID}
        )
        self.assertTrue(ownership.is_designated_service)
        self.assertEqual("railway", ownership.owner)
        self.assertFalse(other_service.is_designated_service)
        self.assertFalse(spoofed_replit.is_designated_service)

    def test_replit_and_development_are_health_only(self) -> None:
        for environment in (
            {},
            {"REPLIT_DEPLOYMENT": "1"},
            {"REPLIT_DEV_DOMAIN": "example.replit.dev"},
        ):
            with self.subTest(environment=environment):
                ownership = runtime_owner.determine_runtime_ownership(environment)
                self.assertFalse(ownership.is_designated_service)
                self.assertEqual("health-only", ownership.owner)

    def test_notify_refuses_delivery_and_polling_outside_railway(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(notify, "TOKEN", "configured"),
            patch.object(notify, "_post") as post,
        ):
            notify.set_delivery_enabled(True)
            self.assertEqual(0, notify.broadcast("test"))
            notify.poll_commands(lambda: "", lambda: "", lambda: "")
            notify.set_delivery_enabled(False)
        post.assert_not_called()

    def test_designated_service_requires_volume_and_uses_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_env = {
                "RAILWAY_SERVICE_ID": runtime_owner.EXPECTED_RAILWAY_SERVICE_ID,
                "RAILWAY_PROJECT_ID": runtime_owner.EXPECTED_RAILWAY_PROJECT_ID,
                "RAILWAY_ENVIRONMENT_ID":
                    runtime_owner.EXPECTED_RAILWAY_ENVIRONMENT_ID,
            }
            with self.assertRaises(StateStorageError):
                configured_state_dir(base_env)
            selected, durable = configured_state_dir(
                {
                    **base_env,
                    "RAILWAY_VOLUME_MOUNT_PATH": directory,
                }
            )
            self.assertEqual(Path(directory).resolve(), selected)
            self.assertTrue(durable)
            with self.assertRaises(StateStorageError):
                configured_state_dir(
                    {
                        **base_env,
                        "RAILWAY_VOLUME_MOUNT_PATH": directory,
                        "CRYPTO_STATE_DIR": str(Path(directory).parent),
                    }
                )

    def test_singleton_lease_allows_only_one_holder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner.lock"
            first = SingletonLease(path)
            second = SingletonLease(path)
            self.assertTrue(first.try_acquire())
            self.assertFalse(second.try_acquire())
            first.close()
            self.assertTrue(second.try_acquire())
            second.close()

    def test_active_startup_requires_all_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            for key in (
                "BINANCE_API_KEY",
                "BINANCE_API_SECRET",
                "CRYPTO_BOT_TOKEN",
                "CRYPTO_STATE_DIR",
            ):
                env.pop(key, None)
            env.update(
                {
                    "RAILWAY_SERVICE_ID":
                        runtime_owner.EXPECTED_RAILWAY_SERVICE_ID,
                    "RAILWAY_PROJECT_ID":
                        runtime_owner.EXPECTED_RAILWAY_PROJECT_ID,
                    "RAILWAY_ENVIRONMENT_ID":
                        runtime_owner.EXPECTED_RAILWAY_ENVIRONMENT_ID,
                    "RAILWAY_VOLUME_MOUNT_PATH": directory,
                    "PORT": "8999",
                }
            )
            result = subprocess.run(
                [sys.executable, "main.py"],
                cwd=Path(__file__).parent,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("requires BINANCE_API_KEY", result.stderr)

    def test_trader_writes_state_atomically_in_configured_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = trader._DATA_DIR
            try:
                trader._DATA_DIR = Path(directory)
                instance = trader.CryptoTrader.__new__(trader.CryptoTrader)
                instance.symbol = "BTCUSDT"
                instance._pos_file = trader._DATA_DIR / "crypto_position_BTCUSDT.json"
                instance._trades_file = trader._DATA_DIR / "crypto_trades_BTCUSDT.json"
                instance._record_trade("sell", 10.0, 1.0, 10.0, pnl=-2.0)
                self.assertTrue(instance._trades_file.exists())
                self.assertFalse(
                    instance._trades_file.with_suffix(".json.tmp").exists()
                )
                self.assertIn('"pnl": -2.0', instance._trades_file.read_text())
            finally:
                trader._DATA_DIR = original

    def test_invalid_existing_position_state_aborts_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            position_file = Path(directory) / "crypto_position_SOLUSDT.json"
            position_file.write_text('{"symbol":"SOLUSDT","qty":"broken"}')
            instance = trader.CryptoTrader.__new__(trader.CryptoTrader)
            instance.symbol = "SOLUSDT"
            instance._pos_file = position_file
            with self.assertRaises(trader.StateValidationError):
                instance._load_position()

    def test_invalid_trade_history_aborts_before_binance_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = trader._DATA_DIR
            try:
                trader._DATA_DIR = Path(directory)
                (
                    trader._DATA_DIR / "crypto_trades_SOLUSDT.json"
                ).write_text('[{"action":"buy"}]')
                with (
                    patch("binance.client.Client") as client,
                    self.assertRaises(trader.StateValidationError),
                ):
                    trader.CryptoTrader(
                        "api-key",
                        "api-secret",
                        symbol="SOLUSDT",
                    )
                client.assert_not_called()
            finally:
                trader._DATA_DIR = original

    def test_invalid_legacy_position_state_aborts_migration(self) -> None:
        with tempfile.TemporaryDirectory() as legacy_directory:
            with tempfile.TemporaryDirectory() as state_directory:
                legacy = Path(legacy_directory) / "crypto_position.json"
                legacy.write_text("{not-json")
                instance = trader.CryptoTrader.__new__(trader.CryptoTrader)
                instance.symbol = "SOLUSDT"
                instance._pos_file = (
                    Path(state_directory) / "crypto_position_SOLUSDT.json"
                )
                instance._trades_file = (
                    Path(state_directory) / "crypto_trades_SOLUSDT.json"
                )
                with (
                    patch.object(
                        trader.Path,
                        "cwd",
                        return_value=Path(legacy_directory),
                    ),
                    self.assertRaises(trader.StateValidationError),
                ):
                    instance._migrate_legacy()
                self.assertFalse(instance._pos_file.exists())


if __name__ == "__main__":
    unittest.main()