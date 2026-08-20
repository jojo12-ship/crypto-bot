"""Deterministic tests for the alert-only early Solana launch scanner."""
from __future__ import annotations

import ast
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from meme_scanner import (
    MemeScanner,
    ScannerConfig,
    estimate_fast_pump_chance_pct,
    evaluate_momentum,
    format_alert,
    parse_gecko_pools,
    screen_risk_report,
)
import notify

BASE_TIME = datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)
POOL = "Pool111111111111111111111111111111111111111"
TOKEN = "Token11111111111111111111111111111111111111"


def pool_payload(
    *,
    buyers: int,
    sellers: int,
    buys: int,
    sells: int,
    volume: float,
    liquidity: float = 8_000,
    price_change: float = 8,
    price_usd: float = 0.0000123,
) -> dict:
    return {
        "data": [
            {
                "id": f"solana_{POOL}",
                "type": "pool",
                "attributes": {
                    "address": POOL,
                    "name": "EARLY / SOL",
                    "pool_created_at": BASE_TIME.isoformat().replace("+00:00", "Z"),
                    "base_token_price_usd": str(price_usd),
                    "reserve_in_usd": str(liquidity),
                    "price_change_percentage": {
                        "m5": str(price_change),
                        "h1": str(price_change),
                    },
                    "transactions": {
                        "m5": {
                            "buys": buys,
                            "sells": sells,
                            "buyers": buyers,
                            "sellers": sellers,
                        }
                    },
                    "volume_usd": {"m5": str(volume)},
                },
                "relationships": {
                    "base_token": {
                        "data": {"id": f"solana_{TOKEN}", "type": "token"}
                    },
                    "quote_token": {
                        "data": {
                            "id": "solana_So11111111111111111111111111111111111111112",
                            "type": "token",
                        }
                    },
                    "dex": {"data": {"id": "raydium", "type": "dex"}},
                },
            }
        ]
    }


def safe_risk_report() -> dict:
    return {
        "mint": TOKEN,
        "rugged": False,
        "score": 1,
        "score_normalised": 99,
        "token": {
            "mintAuthority": None,
            "freezeAuthority": None,
            "supply": 1_000_000_000,
            "isInitialized": True,
        },
        "token_extensions": {
            "nonTransferable": False,
            "permanentDelegate": None,
            "transferHook": None,
            "defaultAccountState": None,
            "pausableConfig": None,
            "transferFeeConfig": None,
            "confidentialTransferMint": None,
            "confidentialTransferFeeConfig": None,
            "mintCloseAuthority": None,
        },
        "transferFee": {"pct": 0, "maxAmount": 0, "authority": None},
        "risks": [],
        "knownAccounts": {
            "PoolTokenAccount": {"name": "Test AMM pool", "type": "AMM"}
        },
        "topHolders": [
            {
                "address": "PoolTokenAccount",
                "owner": "PoolOwner",
                "pct": 60,
                "insider": False,
            },
            {"address": "Holder1", "owner": "Owner1", "pct": 12, "insider": False},
            {"address": "Holder2", "owner": "Owner2", "pct": 10, "insider": False},
            {"address": "Holder3", "owner": "Owner3", "pct": 8, "insider": False},
        ],
        "graphInsidersDetected": 0,
        "insiderNetworks": [],
        "markets": [
            {
                "pubkey": POOL,
                "marketType": "raydium_amm",
                "lp": {
                    "lpLockedPct": 100,
                    "lpLocked": 1_000_000,
                    "lpUnlocked": 0,
                    "lpTotalSupply": 1_000_000,
                },
            }
        ],
    }


def qualifying_observations(config: ScannerConfig) -> list[dict]:
    snapshots = [
        (100, 8, 3, 10, 4, 1_000),
        (160, 11, 4, 14, 5, 1_300),
        (220, 15, 5, 19, 6, 1_800),
    ]
    observations = []
    for age, buyers, sellers, buys, sells, volume in snapshots:
        pool = parse_gecko_pools(
            pool_payload(
                buyers=buyers,
                sellers=sellers,
                buys=buys,
                sells=sells,
                volume=volume,
            ),
            now=BASE_TIME + timedelta(seconds=age),
        )[0]
        observations.append(pool.observation())
    return observations


class MemeScannerLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ScannerConfig()

    def test_accepts_multi_scan_acceleration_before_large_move(self) -> None:
        result = evaluate_momentum(
            qualifying_observations(self.config), self.config
        )
        self.assertTrue(result.qualified)
        self.assertEqual("qualified_momentum", result.reason)
        self.assertEqual(4, result.metrics["latest_buyer_delta"])
        self.assertEqual(500, result.metrics["latest_volume_delta"])

    def test_rejects_pool_that_already_pumped(self) -> None:
        observations = qualifying_observations(self.config)
        observations[-1]["price_change_m5_pct"] = 48
        result = evaluate_momentum(observations, self.config)
        self.assertFalse(result.qualified)
        self.assertTrue(result.hard_rejection)
        self.assertEqual("already_pumped", result.reason)

    def test_rejects_activity_one_wallet_can_manufacture(self) -> None:
        observations = qualifying_observations(self.config)
        observations[-1]["buys_m5"] = 60
        result = evaluate_momentum(observations, self.config)
        self.assertFalse(result.qualified)
        self.assertEqual("transaction_concentration", result.reason)

    def test_incomplete_risk_data_fails_closed(self) -> None:
        risk = screen_risk_report({"rugged": False}, POOL, self.config)
        self.assertFalse(risk.passed)
        self.assertTrue(
            any("incomplete report" in reason for reason in risk.reasons)
        )
        self.assertTrue(
            any("authority" in reason for reason in risk.reasons)
        )

    def test_authority_concentration_and_liquidity_checks_are_required(self) -> None:
        report = safe_risk_report()
        report["token"]["mintAuthority"] = "StillActive"
        report["topHolders"][1]["pct"] = 25
        report["markets"][0]["lp"]["lpLockedPct"] = 40
        report["markets"][0]["lp"]["lpLocked"] = 400_000
        report["markets"][0]["lp"]["lpUnlocked"] = 600_000
        risk = screen_risk_report(report, POOL, self.config)
        self.assertFalse(risk.passed)
        joined = " ".join(risk.reasons)
        self.assertIn("mint authority", joined)
        self.assertIn("largest non-pool holder", joined)
        self.assertIn("liquidity is locked", joined)

    def test_safe_fixture_passes_risk_screen(self) -> None:
        risk = screen_risk_report(safe_risk_report(), POOL, self.config)
        self.assertTrue(risk.passed, risk.reasons)
        self.assertEqual(100, risk.lp_locked_pct)
        self.assertEqual(12, risk.max_holder_pct)

    def test_malformed_holder_and_insider_data_fail_closed(self) -> None:
        report = safe_risk_report()
        report["topHolders"] = [
            {"address": "Holder1", "owner": "Owner1", "pct": "unknown"}
        ]
        report["graphInsidersDetected"] = "indeterminate"
        report["insiderNetworks"] = {"unexpected": True}
        risk = screen_risk_report(report, POOL, self.config)
        self.assertFalse(risk.passed)
        joined = " ".join(risk.reasons)
        self.assertIn("holder concentration records", joined)
        self.assertIn("insider graph result", joined)
        self.assertIn("insider network analysis", joined)

    def test_concentrated_liquidity_without_ownership_proof_fails(self) -> None:
        report = safe_risk_report()
        report["markets"][0] = {
            "pubkey": POOL,
            "marketType": "raydium_clmm",
        }
        risk = screen_risk_report(report, POOL, self.config)
        self.assertFalse(risk.passed)
        self.assertTrue(
            any("liquidity lock/ownership" in reason for reason in risk.reasons)
        )

    def test_serious_warning_description_cannot_hide_behind_benign_name(self) -> None:
        report = safe_risk_report()
        report["risks"] = [
            {
                "name": "Ownership note",
                "level": "info",
                "description": "Creator can drain unlocked pool liquidity",
                "score": 0,
            }
        ]
        risk = screen_risk_report(report, POOL, self.config)
        self.assertFalse(risk.passed)
        self.assertTrue(any("Ownership note" in reason for reason in risk.reasons))

    def test_missing_or_malformed_token_extension_data_fails_closed(self) -> None:
        variants = []
        missing = safe_risk_report()
        del missing["token_extensions"]
        variants.append(missing)
        malformed = safe_risk_report()
        malformed["token_extensions"] = ["unexpected"]
        variants.append(malformed)
        incomplete = safe_risk_report()
        del incomplete["token_extensions"]["transferFeeConfig"]
        variants.append(incomplete)

        for report in variants:
            with self.subTest(report=report.get("token_extensions", "missing")):
                risk = screen_risk_report(report, POOL, self.config)
                self.assertFalse(risk.passed)
                self.assertTrue(
                    any("token extension" in reason for reason in risk.reasons)
                )

    def test_missing_or_malformed_transfer_fee_data_fails_closed(self) -> None:
        variants = []
        missing = safe_risk_report()
        del missing["transferFee"]
        variants.append(missing)
        malformed = safe_risk_report()
        malformed["transferFee"] = "unknown"
        variants.append(malformed)
        incomplete = safe_risk_report()
        incomplete["transferFee"] = {"pct": 0}
        variants.append(incomplete)
        invalid = safe_risk_report()
        invalid["transferFee"]["pct"] = "unknown"
        variants.append(invalid)

        for report in variants:
            with self.subTest(report=report.get("transferFee", "missing")):
                risk = screen_risk_report(report, POOL, self.config)
                self.assertFalse(risk.passed)
                self.assertTrue(
                    any("transfer-fee" in reason for reason in risk.reasons)
                )

    def test_alert_is_transparent_and_html_safe(self) -> None:
        candidate = {
            "symbol": "M<EME",
            "token_address": TOKEN,
            "pool_address": POOL,
            "observations": qualifying_observations(self.config),
        }
        momentum = evaluate_momentum(candidate["observations"], self.config)
        risk = screen_risk_report(safe_risk_report(), POOL, self.config)
        alert = format_alert(candidate, momentum, risk)
        self.assertIn("EARLY SOLANA LAUNCH — WATCHLIST", alert)
        self.assertIn("Screened, still high risk", alert)
        self.assertIn("No trade was placed", alert)
        self.assertIn("Why it made the watchlist", alert)
        self.assertIn("Rough 50%+ pump estimate (next 6h)", alert)
        self.assertIn("not a prediction", alert)
        self.assertIn("Risk checks passed", alert)
        self.assertIn("Manual max entry", alert)
        self.assertIn("$20 maximum", alert)
        self.assertIn("alert-only, no order was placed", alert)
        self.assertIn("watchlist alert, not a buy call", alert)
        self.assertIn(TOKEN, alert)
        self.assertIn("GeckoTerminal", alert)
        self.assertIn("M&lt;EME", alert)
        self.assertNotIn("M<EME", alert)

    def test_fast_pump_estimate_is_conservative_and_requires_screen_pass(self) -> None:
        observations = qualifying_observations(self.config)
        momentum = evaluate_momentum(observations, self.config)
        risk = screen_risk_report(safe_risk_report(), POOL, self.config)
        chance = estimate_fast_pump_chance_pct(
            observations[-1], momentum, risk
        )
        self.assertGreaterEqual(chance, 5)
        self.assertLessEqual(chance, 55)
        self.assertGreaterEqual(chance, 35)

        self.assertEqual(
            0,
            estimate_fast_pump_chance_pct(
                observations[-1],
                evaluate_momentum([], self.config),
                risk,
            ),
        )

    def test_scanner_module_has_no_exchange_or_order_imports(self) -> None:
        source = Path(__file__).with_name("meme_scanner.py").read_text()
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(
            imports.isdisjoint({"binance", "trader", "strategy", "ccxt"})
        )
        self.assertNotIn("BINANCE_API_KEY", source)
        self.assertNotIn(".buy(", source)
        self.assertNotIn(".sell(", source)


class MemeScannerPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.sent: list[str] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def scanner(self) -> MemeScanner:
        return MemeScanner(
            config=ScannerConfig(),
            alert_callback=lambda text: self.sent.append(text) or 1,
            delivery_enabled=True,
            state_dir=self.state_dir,
        )

    def test_observations_cooldown_and_alert_history_survive_restart(self) -> None:
        scanner = self.scanner()
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=8, sellers=3, buys=10, sells=4, volume=1_000
            ),
            now=BASE_TIME + timedelta(seconds=100),
        )
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=11, sellers=4, buys=14, sells=5, volume=1_300
            ),
            now=BASE_TIME + timedelta(seconds=160),
        )

        restarted = self.scanner()
        before = restarted.snapshot()
        self.assertEqual(1, before["observed_candidates"])
        restarted.scan_once(
            pools_payload=pool_payload(
                buyers=15, sellers=5, buys=19, sells=6, volume=1_800
            ),
            risk_reports={TOKEN: safe_risk_report()},
            now=BASE_TIME + timedelta(seconds=220),
        )
        self.assertEqual(1, len(self.sent))
        self.assertEqual(1, restarted.snapshot()["alerts_created"])

        restarted_again = self.scanner()
        restarted_again.scan_once(
            pools_payload=pool_payload(
                buyers=20, sellers=6, buys=25, sells=7, volume=2_500
            ),
            risk_reports={TOKEN: safe_risk_report()},
            now=BASE_TIME + timedelta(seconds=280),
        )
        snapshot = restarted_again.snapshot()
        self.assertEqual(1, len(self.sent))
        self.assertEqual(1, snapshot["alerts_created"])
        self.assertEqual(1, len(snapshot["recent_alerts"]))
        self.assertFalse(snapshot["recent_alerts"][0]["trade_placed"])

    def test_source_failure_preserves_state_and_next_scan_recovers(self) -> None:
        scanner = self.scanner()
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=8, sellers=3, buys=10, sells=4, volume=1_000
            ),
            now=BASE_TIME + timedelta(seconds=100),
        )
        failed = scanner.scan_once(
            pools_payload={"unexpected": []},
            now=BASE_TIME + timedelta(seconds=130),
        )
        self.assertEqual("source_error", failed["state"])
        self.assertEqual(1, failed["observed_candidates"])

        recovered = scanner.scan_once(
            pools_payload=pool_payload(
                buyers=10, sellers=3, buys=12, sells=4, volume=1_200
            ),
            now=BASE_TIME + timedelta(seconds=160),
        )
        self.assertEqual("watching", recovered["state"])
        self.assertIsNone(recovered["last_error"])
        self.assertEqual(1, recovered["observed_candidates"])

    def test_exact_pool_followup_survives_discovery_page_churn(self) -> None:
        scanner = self.scanner()
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=8, sellers=3, buys=10, sells=4, volume=1_000
            ),
            now=BASE_TIME + timedelta(seconds=100),
        )
        scanner.scan_once(
            pools_payload={"data": []},
            followup_payload=pool_payload(
                buyers=11, sellers=4, buys=14, sells=5, volume=1_300
            ),
            now=BASE_TIME + timedelta(seconds=160),
        )
        result = scanner.scan_once(
            pools_payload={"data": []},
            followup_payload=pool_payload(
                buyers=15, sellers=5, buys=19, sells=6, volume=1_800
            ),
            risk_reports={TOKEN: safe_risk_report()},
            now=BASE_TIME + timedelta(seconds=220),
        )
        self.assertEqual("watching", result["state"])
        self.assertEqual(1, result["alerts_created"])
        self.assertEqual(1, len(self.sent))

    def test_alert_outcomes_record_read_only_15_and_60_minute_prices(self) -> None:
        scanner = self.scanner()
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=8, sellers=3, buys=10, sells=4, volume=1_000
            ),
            now=BASE_TIME + timedelta(seconds=100),
        )
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=11, sellers=4, buys=14, sells=5, volume=1_300
            ),
            now=BASE_TIME + timedelta(seconds=160),
        )
        alerted_at = BASE_TIME + timedelta(seconds=220)
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=15, sellers=5, buys=19, sells=6, volume=1_800
            ),
            risk_reports={TOKEN: safe_risk_report()},
            now=alerted_at,
        )

        at_fifteen = scanner.scan_once(
            pools_payload=pool_payload(
                buyers=16, sellers=5, buys=20, sells=6, volume=1_900
            ),
            outcome_payload=pool_payload(
                buyers=16,
                sellers=5,
                buys=20,
                sells=6,
                volume=1_900,
                price_usd=0.000014,
            ),
            now=alerted_at + timedelta(minutes=15, seconds=5),
        )
        outcome_15 = at_fifteen["recent_alerts"][0]["outcomes"]["15m"]
        self.assertAlmostEqual(13.821, outcome_15["move_pct"], places=3)
        self.assertTrue(outcome_15["favorable"])
        self.assertEqual(1, at_fifteen["outcomes"]["checkpoints"]["15m"]["resolved"])
        self.assertEqual(
            100.0,
            at_fifteen["outcomes"]["checkpoints"]["15m"]["favorable_rate_pct"],
        )

        restarted = self.scanner()
        at_sixty = restarted.scan_once(
            pools_payload=pool_payload(
                buyers=16, sellers=5, buys=20, sells=6, volume=1_900
            ),
            outcome_payload=pool_payload(
                buyers=16,
                sellers=5,
                buys=20,
                sells=6,
                volume=1_900,
                price_usd=0.000010,
            ),
            now=alerted_at + timedelta(minutes=60, seconds=5),
        )
        outcome_60 = at_sixty["recent_alerts"][0]["outcomes"]["60m"]
        self.assertAlmostEqual(-18.699, outcome_60["move_pct"], places=3)
        self.assertFalse(outcome_60["favorable"])
        summary = at_sixty["outcomes"]["checkpoints"]["60m"]
        self.assertEqual(0.0, summary["favorable_rate_pct"])
        self.assertAlmostEqual(-18.7, summary["average_drawdown_pct"])
        self.assertAlmostEqual(-18.7, summary["worst_move_pct"])
        self.assertIn(
            "risk screen passed",
            at_sixty["outcomes"]["by_qualification_evidence"],
        )
        self.assertFalse(at_sixty["outcomes"]["decision_readiness"]["ready"])
        self.assertFalse(at_sixty["recent_alerts"][0]["trade_placed"])

    def test_outcome_sampling_survives_discovery_failure(self) -> None:
        scanner = self.scanner()
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=8, sellers=3, buys=10, sells=4, volume=1_000
            ),
            now=BASE_TIME + timedelta(seconds=100),
        )
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=11, sellers=4, buys=14, sells=5, volume=1_300
            ),
            now=BASE_TIME + timedelta(seconds=160),
        )
        alerted_at = BASE_TIME + timedelta(seconds=220)
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=15, sellers=5, buys=19, sells=6, volume=1_800
            ),
            risk_reports={TOKEN: safe_risk_report()},
            now=alerted_at,
        )

        result = scanner.scan_once(
            pools_payload={"unexpected": []},
            outcome_payload=pool_payload(
                buyers=16,
                sellers=5,
                buys=20,
                sells=6,
                volume=1_900,
                price_usd=0.000014,
            ),
            now=alerted_at + timedelta(minutes=15, seconds=5),
        )
        self.assertEqual("source_error", result["state"])
        self.assertEqual(
            "recorded",
            result["recent_alerts"][0]["outcomes"]["15m"]["status"],
        )

    def test_late_checkpoint_is_unavailable_not_mislabeled(self) -> None:
        scanner = self.scanner()
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=8, sellers=3, buys=10, sells=4, volume=1_000
            ),
            now=BASE_TIME + timedelta(seconds=100),
        )
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=11, sellers=4, buys=14, sells=5, volume=1_300
            ),
            now=BASE_TIME + timedelta(seconds=160),
        )
        alerted_at = BASE_TIME + timedelta(seconds=220)
        scanner.scan_once(
            pools_payload=pool_payload(
                buyers=15, sellers=5, buys=19, sells=6, volume=1_800
            ),
            risk_reports={TOKEN: safe_risk_report()},
            now=alerted_at,
        )

        result = scanner.scan_once(
            pools_payload=pool_payload(
                buyers=16, sellers=5, buys=20, sells=6, volume=1_900
            ),
            outcome_payload=pool_payload(
                buyers=16,
                sellers=5,
                buys=20,
                sells=6,
                volume=1_900,
                price_usd=0.000020,
            ),
            now=alerted_at + timedelta(minutes=60, seconds=5),
        )
        alert = result["recent_alerts"][0]
        self.assertEqual("unavailable", alert["outcomes"]["15m"]["status"])
        self.assertNotIn("move_pct", alert["outcomes"]["15m"])
        self.assertEqual("recorded", alert["outcomes"]["60m"]["status"])
        self.assertEqual(1, result["outcomes"]["checkpoints"]["15m"]["unavailable"])
        self.assertEqual(0, result["outcomes"]["checkpoints"]["15m"]["resolved"])


class NotifyPersistenceTests(unittest.TestCase):
    def test_subscribers_and_update_offset_survive_reload(self) -> None:
        original_dir = notify._state_dir
        original_file = notify._state_file
        original_subscribers = set(notify._subscribers)
        original_offset = notify._offset
        with tempfile.TemporaryDirectory() as directory:
            try:
                notify._state_dir = Path(directory)
                notify._state_file = notify._state_dir / "crypto_notify_state.json"
                notify._subscribers = {101, 202}
                notify._offset = 303
                with notify._lock:
                    notify._save_state_locked()
                subscribers, offset = notify._load_state()
                self.assertEqual({101, 202}, subscribers)
                self.assertEqual(303, offset)
            finally:
                notify._state_dir = original_dir
                notify._state_file = original_file
                notify._subscribers = original_subscribers
                notify._offset = original_offset


if __name__ == "__main__":
    unittest.main()