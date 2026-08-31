from __future__ import annotations

import socket
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
import strategy
import trader


class StrategySafetyTests(unittest.TestCase):
    class _StopWorker(BaseException):
        pass

    def test_binance_dns_failure_is_classified_as_transient_connectivity(self) -> None:
        message = (
            "HTTPSConnectionPool(host='api.binance.us', port=443): "
            "Max retries exceeded with url: /api/v3/ping "
            "(Caused by NameResolutionError: Failed to resolve "
            "'api.binance.us' ([Errno -2] Name or service not known))"
        )
        self.assertTrue(
            main._is_transient_binance_connectivity_error(RuntimeError(message))
        )
        self.assertTrue(
            main._is_transient_binance_connectivity_error(
                socket.gaierror(-2, "Name or service not known")
            )
        )
        self.assertFalse(
            main._is_transient_binance_connectivity_error(
                RuntimeError("APIError(code=-2010): insufficient balance")
            )
        )
        for ambiguous_message in (
            "HTTPSConnectionPool: Read timed out",
            "Connection reset by peer",
            "Connection aborted after order submission",
        ):
            with self.subTest(message=ambiguous_message):
                self.assertFalse(
                    main._is_transient_binance_connectivity_error(
                        RuntimeError(ambiguous_message)
                    )
                )

    def test_binance_connectivity_backoff_is_bounded(self) -> None:
        self.assertEqual(15, main._connectivity_retry_seconds(1))
        self.assertEqual(30, main._connectivity_retry_seconds(2))
        self.assertEqual(240, main._connectivity_retry_seconds(5))
        self.assertEqual(300, main._connectivity_retry_seconds(50))

    def test_worker_deduplicates_dns_outage_and_announces_recovery(self) -> None:
        symbol = "TESTUSDT"
        dns_error = RuntimeError(
            "Max retries exceeded: Failed to resolve 'api.binance.us' "
            "(Name or service not known)"
        )
        worker_trader = Mock()
        worker_trader.position = None
        worker_trader.get_completed_klines.side_effect = [
            dns_error,
            dns_error,
            ([100.0] * 50, [10.0] * 50, 1234),
        ]
        snapshot = strategy.Snapshot(
            price=100,
            rsi=50,
            ema_fast=100,
            ema_slow=100,
            ema_200=100,
            macd_hist=0,
            macd_hist_prev=0,
            vol_ratio=1,
        )
        gate = main.SynchronizedOrderGate(
            main._binance_connectivity_available
        )
        gate.set_enabled(True)
        broadcasts: list[str] = []
        with (
            patch.dict(main._pair_states, {symbol: main.PairState(symbol=symbol)}),
            patch.object(main, "_traders", {}),
            patch.object(main, "_binance_unavailable_sources", set()),
            patch.object(main, "CryptoTrader", return_value=worker_trader),
            patch.object(main, "_order_gate", gate),
            patch.object(main, "_control_plane_ready", return_value=True),
            patch.object(main.strategy, "analyze", return_value=snapshot),
            patch.object(main.strategy, "get_signal", return_value=("hold", "test")),
            patch.object(main.strategy, "entry_requirements"),
            patch.object(main.notify, "broadcast", side_effect=broadcasts.append),
            patch.object(
                main.notify,
                "broadcast_operational_error",
                side_effect=broadcasts.append,
            ),
            patch.object(
                main.time,
                "sleep",
                side_effect=[None, None, self._StopWorker()],
            ),
            self.assertRaises(self._StopWorker),
        ):
            main._trade_pair(symbol)

        outage_messages = [
            message for message in broadcasts
            if "temporarily unavailable" in message
        ]
        recovery_messages = [
            message for message in broadcasts
            if "connectivity restored" in message
        ]
        crash_messages = [
            message for message in broadcasts if "bot crashed" in message
        ]
        self.assertEqual(1, len(outage_messages))
        self.assertEqual(1, len(recovery_messages))
        self.assertEqual([], crash_messages)
        self.assertTrue(gate.is_allowed())

    def test_dns_failure_during_buy_enters_fail_closed_outage(self) -> None:
        symbol = "TESTUSDT"
        dns_error = RuntimeError(
            "HTTPSConnectionPool: NameResolutionError: Failed to resolve "
            "'api.binance.us'"
        )
        worker_trader = Mock()
        worker_trader.position = None
        worker_trader.get_completed_klines.return_value = (
            [100.0] * 50,
            [10.0] * 50,
            1234,
        )
        worker_trader.buy.side_effect = dns_error
        snapshot = strategy.Snapshot(
            price=100,
            rsi=30,
            ema_fast=101,
            ema_slow=100,
            ema_200=90,
            macd_hist=1,
            macd_hist_prev=-1,
            vol_ratio=2,
            confidence=0.9,
        )
        gate = main.SynchronizedOrderGate(
            main._binance_connectivity_available
        )
        gate.set_enabled(True)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(main._pair_states, {symbol: main.PairState(symbol=symbol)}),
            patch.object(main, "_traders", {}),
            patch.object(main, "_binance_unavailable_sources", set()),
            patch.object(main, "_ENTRY_GUARD_FILE", Path(directory) / "guards.json"),
            patch.object(main, "CryptoTrader", return_value=worker_trader),
            patch.object(main, "_order_gate", gate),
            patch.object(main, "_control_plane_ready", return_value=True),
            patch.object(main.strategy, "analyze", return_value=snapshot),
            patch.object(main.strategy, "get_signal", return_value=("buy", "test")),
            patch.object(main.strategy, "entry_requirements"),
            patch.object(main.notify, "broadcast"),
            patch.object(
                main.time,
                "sleep",
                side_effect=self._StopWorker(),
            ),
            self.assertRaises(self._StopWorker),
        ):
            main._trade_pair(symbol)

        worker_trader.buy.assert_called_once()
        self.assertFalse(gate.is_allowed())

    def test_startup_preflight_requires_successful_binance_read(self) -> None:
        candidate = Mock()
        candidate.get_price.side_effect = RuntimeError(
            "NameResolutionError: Failed to resolve 'api.binance.us'"
        )
        with (
            patch.object(main, "SYMBOLS", ["SOLUSDT"]),
            patch.object(main, "_traders", {}),
            patch.object(main, "CryptoTrader", return_value=candidate),
            patch.object(
                main,
                "position_recovery_marker_matches",
                return_value=True,
            ),
            self.assertRaises(RuntimeError),
        ):
            main._prepare_active_traders()

        candidate.get_price.assert_called_once()

    def test_weak_volume_and_confidence_are_rejected(self) -> None:
        snap = strategy.Snapshot(
            price=101, rsi=40, ema_fast=101, ema_slow=100, ema_200=90,
            macd_hist=1, macd_hist_prev=-1, vol_ratio=0.2, confidence=0.9,
        )
        signal, reason = strategy.get_signal(
            snap, False, None, requirements=strategy.EntryRequirements()
        )
        self.assertEqual("hold", signal)
        self.assertIn("Volume gate", reason)
        snap.vol_ratio = 1
        snap.confidence = 0.2
        signal, reason = strategy.get_signal(
            snap, False, None, requirements=strategy.EntryRequirements()
        )
        self.assertEqual("hold", signal)
        self.assertIn("Confidence gate", reason)

    def test_btc_defaults_are_stricter(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            btc = strategy.entry_requirements("BTCUSDT")
            sol = strategy.entry_requirements("SOLUSDT")
        self.assertGreater(btc.min_confidence, sol.min_confidence)
        self.assertGreater(btc.min_volume_ratio, sol.min_volume_ratio)

    def test_only_completed_candles_are_returned(self) -> None:
        instance = trader.CryptoTrader.__new__(trader.CryptoTrader)
        instance.symbol = "SOLUSDT"
        now = int(time.time() * 1000)
        instance.client = Mock()
        instance.client.get_klines.return_value = [
            [1000, "1", "1", "1", "10", "5", now - 1],
            [2000, "1", "1", "1", "99", "50", now + 60_000],
        ]
        closes, volumes, candle = instance.get_completed_klines("5m", 2)
        self.assertEqual([10.0], closes)
        self.assertEqual([5.0], volumes)
        self.assertEqual(1000, candle)

    def test_completed_candle_volume_is_used(self) -> None:
        closes = [100 + i / 10 for i in range(50)]
        volumes = [10.0] * 49 + [25.0]
        snap = strategy.analyze(closes, volumes)
        self.assertEqual(2.5, snap.vol_ratio)

    def test_entry_attempt_and_loss_cooldown_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guard_file = Path(directory) / "guards.json"
            with (
                patch.object(main, "_ENTRY_GUARD_FILE", guard_file),
                patch.object(main, "LOSS_COOLDOWN_MINUTES", 60),
            ):
                self.assertEqual("", main._entry_block_reason("SOLUSDT", 100, now=1000))
                main._record_entry_attempt("SOLUSDT", 100)
                self.assertIn(
                    "already evaluated",
                    main._entry_block_reason("SOLUSDT", 100, now=1000),
                )
                main._record_exit("SOLUSDT", lost=True, now=1000)
                self.assertIn(
                    "cooldown active",
                    main._entry_block_reason("SOLUSDT", 200, now=1001),
                )
                self.assertEqual(
                    "",
                    main._entry_block_reason("SOLUSDT", 200, now=4601),
                )


if __name__ == "__main__":
    unittest.main()