from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import notify
from order_gate import OrderPermissionError, SynchronizedOrderGate
import runtime_owner
from singleton_lease import SingletonLease
from state_store import (
    StateStorageError,
    configured_state_dir,
    position_recovery_marker_matches,
)
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

    def test_recovery_marker_requires_valid_current_symbol_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "exchange-position-recovery-v1.json"
            marker.write_text("{not-json")
            self.assertFalse(
                position_recovery_marker_matches(
                    marker,
                    ["SOLUSDT", "ETHUSDT", "BTCUSDT"],
                )
            )
            marker.write_text(
                '{"version":1,"completed_at":"2026-08-20T00:00:00Z",'
                '"symbols":["SOLUSDT","ETHUSDT"]}'
            )
            self.assertFalse(
                position_recovery_marker_matches(
                    marker,
                    ["SOLUSDT", "ETHUSDT", "BTCUSDT"],
                )
            )
            marker.write_text(
                '{"version":1,"completed_at":"2026-08-20T00:00:00Z",'
                '"symbols":["BTCUSDT","SOLUSDT","ETHUSDT"]}'
            )
            self.assertTrue(
                position_recovery_marker_matches(
                    marker,
                    ["SOLUSDT", "ETHUSDT", "BTCUSDT"],
                )
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

    def test_unhealthy_telegram_gate_blocks_all_binance_orders(self) -> None:
        gate = SynchronizedOrderGate(lambda: False)
        gate.set_enabled(True)
        guarded_trader = object.__new__(trader.CryptoTrader)
        guarded_trader._order_allowed = gate.is_allowed
        guarded_trader._order_submitter = gate.submit
        guarded_trader.client = Mock()

        with self.assertRaises(OrderPermissionError):
            guarded_trader.buy(usdt_amount=20)
        with self.assertRaises(OrderPermissionError):
            guarded_trader.sell()

        guarded_trader.client.order_market_buy.assert_not_called()
        guarded_trader.client.order_market_sell.assert_not_called()

    def test_order_submission_and_permission_revocation_are_serialized(self) -> None:
        import threading

        healthy = True
        gate = SynchronizedOrderGate(lambda: healthy)
        gate.set_enabled(True)
        order_started = threading.Event()
        release_order = threading.Event()
        revocation_finished = threading.Event()

        def order_call() -> dict:
            order_started.set()
            self.assertTrue(release_order.wait(timeout=2))
            return {"executedQty": "1", "cummulativeQuoteQty": "10"}

        order_thread = threading.Thread(target=lambda: gate.submit(order_call))
        order_thread.start()
        self.assertTrue(order_started.wait(timeout=2))

        def revoke() -> None:
            nonlocal healthy
            healthy = False
            gate.set_enabled(False)
            revocation_finished.set()

        revoke_thread = threading.Thread(target=revoke)
        revoke_thread.start()
        self.assertFalse(revocation_finished.wait(timeout=0.1))

        release_order.set()
        order_thread.join(timeout=2)
        revoke_thread.join(timeout=2)
        self.assertTrue(revocation_finished.is_set())
        with self.assertRaises(OrderPermissionError):
            gate.submit(lambda: {})

    def test_invalid_existing_position_state_aborts_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            position_file = Path(directory) / "crypto_position_SOLUSDT.json"
            position_file.write_text('{"symbol":"SOLUSDT","qty":"broken"}')
            instance = trader.CryptoTrader.__new__(trader.CryptoTrader)
            instance.symbol = "SOLUSDT"
            instance._pos_file = position_file
            with self.assertRaises(trader.StateValidationError):
                instance._load_position()

    def test_unmatched_buys_are_recovered_before_worker_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = trader.CryptoTrader.__new__(trader.CryptoTrader)
            instance.symbol = "SOLUSDT"
            instance.base = "SOL"
            instance._pos_file = (
                Path(directory) / "crypto_position_SOLUSDT.json"
            )
            instance._trades_file = (
                Path(directory) / "crypto_trades_SOLUSDT.json"
            )
            instance.position = None
            instance.client = Mock()
            instance.client.get_my_trades.return_value = [
                {
                    "time": 1000,
                    "isBuyer": False,
                    "qty": "1",
                    "quoteQty": "10",
                },
                {
                    "time": 2000,
                    "isBuyer": True,
                    "qty": "1",
                    "quoteQty": "10",
                    "commission": "0.01",
                    "commissionAsset": "SOL",
                },
                {
                    "time": 3000,
                    "isBuyer": True,
                    "qty": "2",
                    "quoteQty": "22",
                },
            ]
            instance.client.get_account.return_value = {
                "balances": [
                    {"asset": "USDT", "free": "50", "locked": "0"},
                    {"asset": "SOL", "free": "2.9", "locked": "0"},
                ]
            }
            instance.client.get_symbol_ticker.return_value = {"price": "12"}

            recovered = instance._recover_unmatched_position()

            self.assertIsNotNone(recovered)
            self.assertAlmostEqual(2.9, recovered.qty)
            self.assertAlmostEqual(32 / 2.99, recovered.entry_price)
            self.assertAlmostEqual((32 / 2.99) * 2.9, recovered.entry_value)
            self.assertEqual(12, recovered.high_watermark)
            saved = instance._load_position()
            self.assertAlmostEqual(recovered.qty, saved.qty)
            trades = instance._load_trades()
            self.assertEqual(1, len(trades))
            self.assertIn("Recovered unmatched Binance fills", trades[0]["reason"])

    def test_ambiguous_truncated_history_blocks_position_recovery(self) -> None:
        instance = trader.CryptoTrader.__new__(trader.CryptoTrader)
        instance.symbol = "BTCUSDT"
        instance.base = "BTC"
        instance.client = Mock()
        instance.client.get_my_trades.return_value = [
            {
                "time": index + 1,
                "isBuyer": True,
                "qty": "0.001",
                "quoteQty": "50",
            }
            for index in range(1000)
        ]
        with self.assertRaisesRegex(
            trader.StateValidationError,
            "recovery is ambiguous",
        ):
            instance._recover_unmatched_position()

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