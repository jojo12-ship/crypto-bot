"""Alert-only early-launch Solana meme-coin scanner.

This module intentionally has no Binance, wallet, order, position, or P&L
dependencies. It discovers public pool data, applies conservative risk gates,
and emits informational Telegram alerts through an injected callback.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger("meme_scanner")

GECKO_NEW_POOLS_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1"
)
GECKO_MULTI_POOLS_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/{addresses}"
)
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
USER_AGENT = "TradeForge-Early-Launch-Scanner/1.0"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _parse_time(value: str) -> datetime:
    if not value:
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@dataclass(frozen=True)
class ScannerConfig:
    scan_seconds: int = 60
    min_age_seconds: int = 90
    max_age_seconds: int = 30 * 60
    min_observations: int = 3
    min_liquidity_usd: float = 3_000
    min_buyers: int = 8
    min_latest_new_buyers: int = 3
    min_volume_m5_usd: float = 1_000
    min_latest_volume_usd: float = 250
    buyer_acceleration: float = 1.15
    volume_acceleration: float = 1.20
    min_buy_sell_ratio: float = 1.20
    max_buys_per_buyer: float = 3.5
    max_price_gain_pct: float = 35
    max_breakout_price_gain_pct: float = 250
    max_breakout_market_cap_usd: float = 1_000_000
    breakout_min_liquidity_usd: float = 8_000
    breakout_min_buyers: int = 20
    breakout_min_volume_m5_usd: float = 5_000
    breakout_min_buy_sell_ratio: float = 1.50
    max_price_drop_pct: float = 25
    max_non_pool_holder_pct: float = 20
    max_top5_non_pool_pct: float = 50
    max_insider_pct: float = 10
    min_lp_locked_pct: float = 80
    risk_cache_seconds: int = 10 * 60
    cooldown_seconds: int = 24 * 60 * 60
    max_risk_checks_per_scan: int = 3
    max_tracked_candidates: int = 60
    max_saved_candidates: int = 200
    outcome_checkpoint_minutes: tuple[int, int] = (15, 60)
    favorable_move_pct: float = 10
    max_saved_outcome_alerts: int = 500
    max_outcome_pools_per_scan: int = 30
    outcome_checkpoint_grace_seconds: int = 5 * 60
    max_outcome_attempts: int = 5
    min_outcome_sample_size: int = 20
    request_timeout_seconds: int = 20

    def __post_init__(self) -> None:
        numeric_fields = {
            "min_liquidity_usd": self.min_liquidity_usd,
            "min_volume_m5_usd": self.min_volume_m5_usd,
            "min_buy_sell_ratio": self.min_buy_sell_ratio,
            "max_price_gain_pct": self.max_price_gain_pct,
            "max_breakout_price_gain_pct": self.max_breakout_price_gain_pct,
            "max_breakout_market_cap_usd": self.max_breakout_market_cap_usd,
            "breakout_min_liquidity_usd": self.breakout_min_liquidity_usd,
            "breakout_min_volume_m5_usd": self.breakout_min_volume_m5_usd,
            "breakout_min_buy_sell_ratio": self.breakout_min_buy_sell_ratio,
        }
        invalid = [
            name
            for name, value in numeric_fields.items()
            if not math.isfinite(float(value)) or float(value) < 0
        ]
        if invalid:
            raise ValueError(
                "scanner thresholds must be finite and nonnegative: "
                + ", ".join(invalid)
            )
        stronger_breakout_checks = (
            (
                self.max_breakout_price_gain_pct > self.max_price_gain_pct,
                "maximum breakout price gain must exceed the normal price ceiling",
            ),
            (
                self.max_breakout_market_cap_usd > 0,
                "maximum breakout market cap must be positive",
            ),
            (
                self.breakout_min_liquidity_usd > self.min_liquidity_usd,
                "breakout liquidity minimum must exceed the normal minimum",
            ),
            (
                self.breakout_min_buyers > self.min_buyers,
                "breakout buyer minimum must exceed the normal minimum",
            ),
            (
                self.breakout_min_volume_m5_usd > self.min_volume_m5_usd,
                "breakout volume minimum must exceed the normal minimum",
            ),
            (
                self.breakout_min_buy_sell_ratio > self.min_buy_sell_ratio,
                "breakout flow ratio must exceed the normal minimum",
            ),
        )
        violations = [
            message for passed, message in stronger_breakout_checks if not passed
        ]
        if violations:
            raise ValueError("invalid low-cap breakout configuration: " + "; ".join(violations))

    @classmethod
    def from_env(cls) -> "ScannerConfig":
        def integer(name: str, default: int) -> int:
            return max(1, int(os.getenv(name, str(default))))

        def number(name: str, default: float) -> float:
            return max(0.0, float(os.getenv(name, str(default))))

        return cls(
            scan_seconds=integer("MEME_SCAN_SECONDS", cls.scan_seconds),
            min_age_seconds=integer("MEME_MIN_AGE_SECONDS", cls.min_age_seconds),
            max_age_seconds=integer("MEME_MAX_AGE_SECONDS", cls.max_age_seconds),
            min_observations=integer(
                "MEME_MIN_OBSERVATIONS", cls.min_observations
            ),
            min_liquidity_usd=number(
                "MEME_MIN_LIQUIDITY_USD", cls.min_liquidity_usd
            ),
            min_buyers=integer("MEME_MIN_BUYERS", cls.min_buyers),
            min_latest_new_buyers=integer(
                "MEME_MIN_NEW_BUYERS", cls.min_latest_new_buyers
            ),
            min_volume_m5_usd=number(
                "MEME_MIN_VOLUME_M5_USD", cls.min_volume_m5_usd
            ),
            min_latest_volume_usd=number(
                "MEME_MIN_NEW_VOLUME_USD", cls.min_latest_volume_usd
            ),
            max_price_gain_pct=number(
                "MEME_MAX_PRICE_GAIN_PCT", cls.max_price_gain_pct
            ),
            max_breakout_price_gain_pct=number(
                "MEME_MAX_BREAKOUT_PRICE_GAIN_PCT",
                cls.max_breakout_price_gain_pct,
            ),
            max_breakout_market_cap_usd=number(
                "MEME_MAX_BREAKOUT_MARKET_CAP_USD",
                cls.max_breakout_market_cap_usd,
            ),
            breakout_min_liquidity_usd=number(
                "MEME_BREAKOUT_MIN_LIQUIDITY_USD",
                cls.breakout_min_liquidity_usd,
            ),
            breakout_min_buyers=integer(
                "MEME_BREAKOUT_MIN_BUYERS",
                cls.breakout_min_buyers,
            ),
            breakout_min_volume_m5_usd=number(
                "MEME_BREAKOUT_MIN_VOLUME_M5_USD",
                cls.breakout_min_volume_m5_usd,
            ),
            breakout_min_buy_sell_ratio=number(
                "MEME_BREAKOUT_MIN_BUY_SELL_RATIO",
                cls.breakout_min_buy_sell_ratio,
            ),
            max_price_drop_pct=number(
                "MEME_MAX_PRICE_DROP_PCT", cls.max_price_drop_pct
            ),
            cooldown_seconds=integer(
                "MEME_ALERT_COOLDOWN_SECONDS", cls.cooldown_seconds
            ),
            favorable_move_pct=number(
                "MEME_FAVORABLE_MOVE_PCT", cls.favorable_move_pct
            ),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "network": "solana",
            "scan_seconds": self.scan_seconds,
            "minimum_age_seconds": self.min_age_seconds,
            "maximum_age_seconds": self.max_age_seconds,
            "minimum_observations": self.min_observations,
            "minimum_liquidity_usd": self.min_liquidity_usd,
            "minimum_buyers": self.min_buyers,
            "minimum_new_buyers_latest_scan": self.min_latest_new_buyers,
            "minimum_m5_volume_usd": self.min_volume_m5_usd,
            "maximum_price_gain_pct": self.max_price_gain_pct,
            "maximum_breakout_price_gain_pct": self.max_breakout_price_gain_pct,
            "maximum_breakout_market_cap_usd": self.max_breakout_market_cap_usd,
            "breakout_minimum_liquidity_usd": self.breakout_min_liquidity_usd,
            "breakout_minimum_buyers": self.breakout_min_buyers,
            "breakout_minimum_m5_volume_usd": self.breakout_min_volume_m5_usd,
            "breakout_minimum_buy_sell_ratio": self.breakout_min_buy_sell_ratio,
            "cooldown_seconds": self.cooldown_seconds,
            "maximum_tracked_candidates": self.max_tracked_candidates,
            "outcome_checkpoints_minutes": list(self.outcome_checkpoint_minutes),
            "favorable_move_pct": self.favorable_move_pct,
            "minimum_outcome_sample_size": self.min_outcome_sample_size,
        }


@dataclass(frozen=True)
class PoolSnapshot:
    pool_id: str
    pool_address: str
    token_address: str
    name: str
    symbol: str
    dex: str
    created_at: str
    observed_at: str
    age_seconds: float
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float
    volume_m5_usd: float
    price_change_m5_pct: float
    price_change_h1_pct: float
    buys_m5: int
    sells_m5: int
    buyers_m5: int
    sellers_m5: int

    def observation(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "age_seconds": round(self.age_seconds, 2),
            "price_usd": self.price_usd,
            "liquidity_usd": round(self.liquidity_usd, 2),
            "market_cap_usd": round(self.market_cap_usd, 2),
            "volume_m5_usd": round(self.volume_m5_usd, 2),
            "price_change_m5_pct": round(self.price_change_m5_pct, 3),
            "price_change_h1_pct": round(self.price_change_h1_pct, 3),
            "buys_m5": self.buys_m5,
            "sells_m5": self.sells_m5,
            "buyers_m5": self.buyers_m5,
            "sellers_m5": self.sellers_m5,
        }


@dataclass(frozen=True)
class MomentumResult:
    qualified: bool
    reason: str
    detail: str
    metrics: dict[str, Any]
    hard_rejection: bool = False


@dataclass(frozen=True)
class RiskResult:
    passed: bool
    reasons: list[str]
    checks: list[str]
    score: float | None
    max_holder_pct: float | None
    top5_holder_pct: float | None
    insider_pct: float | None
    lp_locked_pct: float | None
    market_type: str
    checked_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_gecko_pools(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[PoolSnapshot]:
    """Validate and normalize GeckoTerminal's Solana new-pools response."""
    observed = now or _utc_now()
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("GeckoTerminal response is missing a data list")

    pools: list[PoolSnapshot] = []
    for item in data:
        try:
            if not isinstance(item, dict) or item.get("type") != "pool":
                continue
            attrs = item["attributes"]
            rels = item["relationships"]
            if not isinstance(attrs, dict) or not isinstance(rels, dict):
                continue

            pool_address = str(attrs["address"]).strip()
            created_at = str(attrs["pool_created_at"]).strip()
            token_ref = rels["base_token"]["data"]["id"]
            if not isinstance(token_ref, str) or not token_ref.startswith("solana_"):
                continue
            token_address = token_ref.split("_", 1)[1]
            if not pool_address or not token_address:
                continue

            created = _parse_time(created_at)
            age = (observed - created).total_seconds()
            if age < -30:
                continue

            tx = attrs.get("transactions", {}).get("m5", {})
            changes = attrs.get("price_change_percentage", {})
            volumes = attrs.get("volume_usd", {})
            dex = str(rels.get("dex", {}).get("data", {}).get("id", "unknown"))
            pair_name = str(attrs.get("name") or token_address[:8])
            symbol = pair_name.split("/", 1)[0].strip() or token_address[:8]

            pools.append(
                PoolSnapshot(
                    pool_id=str(item.get("id") or f"solana_{pool_address}"),
                    pool_address=pool_address,
                    token_address=token_address,
                    name=symbol,
                    symbol=symbol[:32],
                    dex=dex[:64],
                    created_at=created_at,
                    observed_at=_iso(observed),
                    age_seconds=max(0.0, age),
                    price_usd=_num(attrs.get("base_token_price_usd")),
                    liquidity_usd=_num(attrs.get("reserve_in_usd")),
                    market_cap_usd=_num(
                        attrs.get("market_cap_usd"),
                        _num(attrs.get("fdv_usd")),
                    ),
                    volume_m5_usd=_num(volumes.get("m5")),
                    price_change_m5_pct=_num(changes.get("m5")),
                    price_change_h1_pct=_num(changes.get("h1")),
                    buys_m5=max(0, int(_num(tx.get("buys")))),
                    sells_m5=max(0, int(_num(tx.get("sells")))),
                    buyers_m5=max(0, int(_num(tx.get("buyers")))),
                    sellers_m5=max(0, int(_num(tx.get("sellers")))),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return pools


def evaluate_momentum(
    observations: list[dict[str, Any]],
    config: ScannerConfig,
) -> MomentumResult:
    """Require multi-scan buyer and volume acceleration before a large move."""
    if not observations:
        return MomentumResult(False, "no_observations", "No pool observations", {})

    latest = observations[-1]
    age = _num(latest.get("age_seconds"))
    if age < config.min_age_seconds:
        return MomentumResult(
            False,
            "too_new_to_evaluate",
            f"Pool age {_duration(age)}; need {_duration(config.min_age_seconds)}",
            {},
        )
    if age > config.max_age_seconds:
        return MomentumResult(
            False,
            "pool_too_old",
            f"Pool age {_duration(age)} exceeds early window",
            {},
            True,
        )
    if len(observations) < config.min_observations:
        return MomentumResult(
            False,
            "building_history",
            f"{len(observations)}/{config.min_observations} observations",
            {},
        )

    liquidity = _num(latest.get("liquidity_usd"))
    market_cap = _num(latest.get("market_cap_usd"))
    gain = _num(latest.get("price_change_m5_pct"))
    buyers = int(_num(latest.get("buyers_m5")))
    sellers = int(_num(latest.get("sellers_m5")))
    buys = int(_num(latest.get("buys_m5")))
    sells = int(_num(latest.get("sells_m5")))
    volume = _num(latest.get("volume_m5_usd"))

    breakout_window = False
    if gain > config.max_price_gain_pct:
        if not math.isfinite(market_cap) or market_cap <= 0:
            return MomentumResult(
                False,
                "market_cap_unavailable",
                "Market cap/FDV is required for a fast-breakout alert",
                {},
                True,
            )
        if market_cap > config.max_breakout_market_cap_usd:
            return MomentumResult(
                False,
                "breakout_market_cap_too_high",
                (
                    f"${market_cap:,.0f} market cap exceeds the "
                    f"${config.max_breakout_market_cap_usd:,.0f} early-alert ceiling"
                ),
                {},
                True,
            )
        if gain > config.max_breakout_price_gain_pct:
            return MomentumResult(
                False,
                "already_pumped",
                f"{gain:+.1f}% in 5m",
                {},
                True,
            )
        breakout_window = True

    required_liquidity = (
        config.breakout_min_liquidity_usd
        if breakout_window
        else config.min_liquidity_usd
    )
    required_buyers = (
        config.breakout_min_buyers
        if breakout_window
        else config.min_buyers
    )
    required_volume = (
        config.breakout_min_volume_m5_usd
        if breakout_window
        else config.min_volume_m5_usd
    )
    required_flow_ratio = (
        config.breakout_min_buy_sell_ratio
        if breakout_window
        else config.min_buy_sell_ratio
    )
    hard_checks = [
        (
            liquidity >= required_liquidity,
            "liquidity_too_low",
            f"${liquidity:,.0f} liquidity; need ${required_liquidity:,.0f}",
        ),
        (
            gain >= -config.max_price_drop_pct,
            "already_dumping",
            f"{gain:+.1f}% in 5m",
        ),
        (
            buyers >= required_buyers,
            "not_enough_unique_buyers",
            f"{buyers} buyers; need {required_buyers}",
        ),
        (
            volume >= required_volume,
            "volume_too_low",
            f"${volume:,.0f} in 5m; need ${required_volume:,.0f}",
        ),
        (
            buys / max(sells, 1) >= required_flow_ratio,
            "buy_flow_not_dominant",
            f"{buys} buys vs {sells} sells",
        ),
        (
            buyers / max(sellers, 1) >= required_flow_ratio,
            "buyer_count_not_dominant",
            f"{buyers} buyers vs {sellers} sellers",
        ),
        (
            buys / max(buyers, 1) <= config.max_buys_per_buyer,
            "transaction_concentration",
            f"{buys / max(buyers, 1):.1f} buys per buyer",
        ),
    ]
    for passed, reason, detail in hard_checks:
        if not passed:
            return MomentumResult(False, reason, detail, {}, True)

    first, previous, current = observations[-3:]
    previous_buyer_delta = max(
        0, int(_num(previous.get("buyers_m5")) - _num(first.get("buyers_m5")))
    )
    latest_buyer_delta = max(
        0, int(_num(current.get("buyers_m5")) - _num(previous.get("buyers_m5")))
    )
    previous_volume_delta = max(
        0.0,
        _num(previous.get("volume_m5_usd")) - _num(first.get("volume_m5_usd")),
    )
    latest_volume_delta = max(
        0.0,
        _num(current.get("volume_m5_usd")) - _num(previous.get("volume_m5_usd")),
    )

    needed_buyers = max(
        config.min_latest_new_buyers,
        math.ceil(previous_buyer_delta * config.buyer_acceleration),
    )
    needed_volume = max(
        config.min_latest_volume_usd,
        previous_volume_delta * config.volume_acceleration,
    )
    metrics = {
        "previous_buyer_delta": previous_buyer_delta,
        "latest_buyer_delta": latest_buyer_delta,
        "buyer_acceleration_ratio": (
            latest_buyer_delta / max(previous_buyer_delta, 1)
        ),
        "previous_volume_delta": round(previous_volume_delta, 2),
        "latest_volume_delta": round(latest_volume_delta, 2),
        "volume_acceleration_ratio": round(
            latest_volume_delta / max(previous_volume_delta, 1), 2
        ),
        "buy_sell_ratio": round(buys / max(sells, 1), 2),
        "market_cap_usd": round(market_cap, 2),
        "signal_tier": (
            "low_cap_breakout" if breakout_window else "early_acceleration"
        ),
    }
    if latest_buyer_delta < needed_buyers:
        return MomentumResult(
            False,
            "buyers_not_accelerating",
            f"+{latest_buyer_delta} new buyers; need +{needed_buyers}",
            metrics,
        )
    if latest_volume_delta < needed_volume:
        return MomentumResult(
            False,
            "volume_not_accelerating",
            f"+${latest_volume_delta:,.0f}; need +${needed_volume:,.0f}",
            metrics,
        )

    return MomentumResult(
        True,
        (
            "qualified_low_cap_breakout"
            if breakout_window
            else "qualified_momentum"
        ),
        (
            f"+{latest_buyer_delta} buyers and +${latest_volume_delta:,.0f} "
            "volume on latest scan"
            + (
                f"; ${market_cap:,.0f} market cap low-cap breakout"
                if breakout_window
                else ""
            )
        ),
        metrics,
    )


def estimate_fast_pump_chance_pct(
    latest: dict[str, Any],
    momentum: MomentumResult,
    risk: RiskResult,
) -> int:
    """Return a deliberately conservative, uncalibrated 50%+ / 6h estimate.

    This is a momentum-screening heuristic, not a model trained on realized
    outcomes. It is capped well below certainty so the alert cannot imply a
    price prediction.
    """
    if not momentum.qualified or not risk.passed:
        return 0

    buyer_acceleration = max(
        0.0, _num(momentum.metrics.get("buyer_acceleration_ratio"))
    )
    volume_acceleration = max(
        0.0, _num(momentum.metrics.get("volume_acceleration_ratio"))
    )
    buyers = max(0, int(_num(latest.get("buyers_m5"))))
    sellers = max(1, int(_num(latest.get("sellers_m5"))))
    buyer_pressure = buyers / sellers
    liquidity = max(0.0, _num(latest.get("liquidity_usd")))
    volume = max(0.0, _num(latest.get("volume_m5_usd")))
    price_gain = _num(latest.get("price_change_m5_pct"))

    score = 5.0
    score += min(12.0, max(0.0, buyer_acceleration - 1.0) * 36.0)
    score += min(10.0, max(0.0, volume_acceleration - 1.0) * 16.0)
    score += min(8.0, max(0.0, buyer_pressure - 1.0) * 4.0)
    score += min(5.0, max(0.0, liquidity - 5_000.0) / 2_000.0)
    score += min(4.0, (volume / max(liquidity, 1.0)) * 30.0)
    score += max(0.0, 5.0 - abs(price_gain - 10.0) * 0.5)
    score += min(3.0, float(len(risk.checks)))
    return max(5, min(55, round(score)))


def screen_risk_report(
    report: dict[str, Any],
    pool_address: str,
    config: ScannerConfig,
    *,
    checked_at: str | None = None,
) -> RiskResult:
    """Conservatively evaluate a RugCheck full token report."""
    reasons: list[str] = []
    checks: list[str] = []
    required = ("token", "risks", "topHolders", "markets", "knownAccounts", "rugged")
    missing = [key for key in required if key not in report]
    if missing:
        reasons.append("incomplete report: " + ", ".join(missing))

    token = report.get("token")
    risks = report.get("risks")
    holders = report.get("topHolders")
    markets = report.get("markets")
    known = report.get("knownAccounts")
    if not isinstance(token, dict):
        reasons.append("missing token authority data")
        token = {}
    if not isinstance(risks, list):
        reasons.append("missing structured risk list")
        risks = []
    if not isinstance(holders, list):
        reasons.append("missing holder concentration data")
        holders = []
    if not isinstance(markets, list):
        reasons.append("missing pool ownership data")
        markets = []
    if not isinstance(known, dict):
        reasons.append("missing known-account classifications")
        known = {}

    if report.get("rugged") is not False:
        reasons.append("token is rugged or rugged status is unavailable")

    if "mintAuthority" not in token or "freezeAuthority" not in token:
        reasons.append("mint/freeze authority status is incomplete")
    else:
        if token.get("mintAuthority") is not None:
            reasons.append("mint authority is still active")
        if token.get("freezeAuthority") is not None:
            reasons.append("freeze authority is still active")
        if token.get("mintAuthority") is None and token.get("freezeAuthority") is None:
            checks.append("Mint and freeze authorities revoked")

    extensions = report.get("token_extensions")
    required_extension_controls = (
        "nonTransferable",
        "permanentDelegate",
        "transferHook",
        "defaultAccountState",
        "pausableConfig",
        "transferFeeConfig",
        "confidentialTransferMint",
        "confidentialTransferFeeConfig",
        "mintCloseAuthority",
    )
    if "token_extensions" not in report:
        reasons.append("token extension controls are unavailable")
    elif not isinstance(extensions, dict):
        reasons.append("token extension data is malformed")
    else:
        missing_controls = [
            key for key in required_extension_controls if key not in extensions
        ]
        if missing_controls:
            reasons.append(
                "token extension controls are incomplete: "
                + ", ".join(missing_controls)
            )
        dangerous_extensions = {
            "nonTransferable": (
                extensions.get("nonTransferable") is not False
                if "nonTransferable" in extensions
                else False
            ),
            "permanentDelegate": bool(extensions.get("permanentDelegate")),
            "transferHook": bool(extensions.get("transferHook")),
            "defaultAccountState": bool(extensions.get("defaultAccountState")),
            "pausableConfig": bool(extensions.get("pausableConfig")),
            "transferFeeConfig": bool(extensions.get("transferFeeConfig")),
            "confidentialTransferMint": bool(
                extensions.get("confidentialTransferMint")
            ),
            "confidentialTransferFeeConfig": bool(
                extensions.get("confidentialTransferFeeConfig")
            ),
            "mintCloseAuthority": bool(extensions.get("mintCloseAuthority")),
        }
        active = [name for name, enabled in dangerous_extensions.items() if enabled]
        if active:
            reasons.append("dangerous token controls: " + ", ".join(active))
        elif not missing_controls:
            checks.append("No dangerous Token-2022 controls detected")

    transfer_fee = report.get("transferFee")
    if "transferFee" not in report:
        reasons.append("transfer-fee evidence is unavailable")
    elif not isinstance(transfer_fee, dict):
        reasons.append("transfer-fee evidence is malformed")
    else:
        missing_fee_fields = [
            key for key in ("pct", "maxAmount", "authority") if key not in transfer_fee
        ]
        if missing_fee_fields:
            reasons.append(
                "transfer-fee evidence is incomplete: "
                + ", ".join(missing_fee_fields)
            )
        try:
            fee_pct = float(transfer_fee["pct"])
            fee_max = float(transfer_fee["maxAmount"])
        except (KeyError, TypeError, ValueError):
            fee_pct = fee_max = -1
        if (
            not math.isfinite(fee_pct)
            or not math.isfinite(fee_max)
            or fee_pct < 0
            or fee_pct > 100
            or fee_max < 0
        ):
            reasons.append("transfer-fee values are invalid")
        elif fee_pct > 0:
            reasons.append(f"transfer fee is {fee_pct:.2f}%")
        elif not missing_fee_fields:
            checks.append("No transfer fee configured")

    serious_words = (
        "mint",
        "freeze",
        "honeypot",
        "rug",
        "insider",
        "bundled",
        "concentrat",
        "top holder",
        "low liquidity",
        "liquidity",
        "unlock",
        "mutable",
        "blacklist",
        "transfer fee",
    )
    serious_risk_found = False
    for risk in risks:
        if not isinstance(risk, dict):
            reasons.append("malformed risk entry")
            serious_risk_found = True
            continue
        name = str(risk.get("name") or "Unnamed risk")
        description = str(risk.get("description") or "")
        value = str(risk.get("value") or "")
        level = str(risk.get("level") or "").lower()
        combined = f"{name} {description} {value}".lower()
        score = _num(risk.get("score"))
        should_reject = (
            level in {"danger", "critical", "warn", "warning"}
            or any(word in combined for word in serious_words)
            or (level not in {"", "info", "notice", "good"} and score > 0)
        )
        if should_reject:
            reasons.append(f"{level or 'warning'}: {name}")
            serious_risk_found = True
    if not serious_risk_found:
        checks.append("No serious RugCheck warning")

    malformed_known_accounts = [
        address
        for address, info in known.items()
        if not isinstance(address, str)
        or not isinstance(info, dict)
        or not isinstance(info.get("type"), str)
    ]
    if malformed_known_accounts:
        reasons.append("known-account classifications are malformed")
    known_pool_accounts = {
        address
        for address, info in known.items()
        if isinstance(info, dict)
        and str(info.get("type", "")).upper() in {"AMM", "DEX", "LOCKER"}
    }
    valid_holders: list[dict[str, Any]] = []
    malformed_holders = False
    for holder in holders:
        if (
            not isinstance(holder, dict)
            or not isinstance(holder.get("address"), str)
            or not isinstance(holder.get("owner"), str)
            or not isinstance(holder.get("insider"), bool)
        ):
            malformed_holders = True
            continue
        try:
            pct = float(holder["pct"])
        except (KeyError, TypeError, ValueError):
            malformed_holders = True
            continue
        if not math.isfinite(pct) or pct < 0 or pct > 100:
            malformed_holders = True
            continue
        valid_holders.append({**holder, "pct": pct})
    if malformed_holders:
        reasons.append("holder concentration records are malformed or incomplete")
    if holders and not valid_holders:
        reasons.append("no valid holder concentration records")

    non_pool_holders = [
        holder
        for holder in valid_holders
        if holder.get("address") not in known_pool_accounts
        and holder.get("owner") not in known_pool_accounts
    ]
    holder_pcts = sorted(
        (_num(holder.get("pct")) for holder in non_pool_holders),
        reverse=True,
    )
    max_holder = holder_pcts[0] if holder_pcts else 0.0
    top5_holder = sum(holder_pcts[:5])
    if not valid_holders:
        reasons.append("no holder records available")
    elif max_holder > config.max_non_pool_holder_pct:
        reasons.append(f"largest non-pool holder owns {max_holder:.1f}%")
    elif top5_holder > config.max_top5_non_pool_pct:
        reasons.append(f"top five non-pool holders own {top5_holder:.1f}%")
    else:
        checks.append(
            f"Holder concentration within limits ({max_holder:.1f}% largest)"
        )

    try:
        supply = float(token["supply"])
    except (KeyError, TypeError, ValueError):
        supply = 0
    if not math.isfinite(supply) or supply <= 0:
        reasons.append("token supply required for insider analysis is unavailable")
        supply = 0

    graph_count = report.get("graphInsidersDetected")
    networks = report.get("insiderNetworks")
    valid_graph_count = (
        isinstance(graph_count, int)
        and not isinstance(graph_count, bool)
        and graph_count >= 0
    )
    if not valid_graph_count:
        reasons.append("insider graph result is malformed or indeterminate")
        graph_count = 0
    if not isinstance(networks, list):
        reasons.append("insider network analysis is unavailable")
        networks = []
    valid_networks: list[dict[str, Any]] = []
    malformed_networks = False
    for network in networks:
        if not isinstance(network, dict):
            malformed_networks = True
            continue
        try:
            size = int(network["size"])
            amount = float(network["tokenAmount"])
        except (KeyError, TypeError, ValueError):
            malformed_networks = True
            continue
        if size <= 0 or not math.isfinite(amount) or amount < 0:
            malformed_networks = True
            continue
        valid_networks.append({**network, "size": size, "tokenAmount": amount})
    if malformed_networks:
        reasons.append("insider network records are malformed or incomplete")
    represented_accounts = sum(network["size"] for network in valid_networks)
    if valid_graph_count and graph_count > 0 and represented_accounts < graph_count:
        reasons.append("insider graph details do not cover all detected accounts")

    insider_amount = sum(network["tokenAmount"] for network in valid_networks)
    insider_pct = insider_amount / supply * 100 if supply > 0 else 0.0
    marked_insider_pct = sum(
        _num(holder.get("pct"))
        for holder in valid_holders
        if holder.get("insider") is True
    )
    insider_pct = max(insider_pct, marked_insider_pct)
    if insider_pct > config.max_insider_pct:
        reasons.append(f"insider-linked supply is {insider_pct:.1f}%")
    elif valid_graph_count and isinstance(report.get("insiderNetworks"), list):
        checks.append(f"Insider-linked supply below limit ({insider_pct:.1f}%)")

    matching_market = next(
        (
            market
            for market in markets
            if isinstance(market, dict) and market.get("pubkey") == pool_address
        ),
        None,
    )
    market_type = (
        str(matching_market.get("marketType") or "unknown")
        if matching_market
        else "unknown"
    )
    lp_locked_pct: float | None = None
    if not matching_market:
        reasons.append("risk report does not cover this exact pool")
    else:
        lp = matching_market.get("lp")
        if isinstance(lp, dict):
            try:
                reported_pct = float(lp["lpLockedPct"])
                locked = float(lp["lpLocked"])
                unlocked = float(lp["lpUnlocked"])
                total = float(lp["lpTotalSupply"])
            except (KeyError, TypeError, ValueError):
                reported_pct = locked = unlocked = total = -1
            values = (reported_pct, locked, unlocked, total)
            if (
                any(not math.isfinite(value) for value in values)
                or reported_pct < 0
                or reported_pct > 100
                or locked < 0
                or unlocked < 0
                or total <= 0
                or locked + unlocked <= 0
            ):
                reasons.append("liquidity ownership/lock values are incomplete")
            else:
                calculated_pct = locked / (locked + unlocked) * 100
                if abs(calculated_pct - reported_pct) > 2:
                    reasons.append("liquidity lock values are internally inconsistent")
                else:
                    lp_locked_pct = reported_pct
                    if lp_locked_pct < config.min_lp_locked_pct:
                        reasons.append(
                            f"only {lp_locked_pct:.1f}% of pool liquidity is locked"
                        )
                    else:
                        checks.append(f"Pool liquidity lock {lp_locked_pct:.1f}%")
        else:
            reasons.append("liquidity lock/ownership data is unavailable")

    reasons = list(dict.fromkeys(reasons))
    return RiskResult(
        passed=not reasons,
        reasons=reasons,
        checks=checks,
        score=(
            _num(report.get("score_normalised"))
            if report.get("score_normalised") is not None
            else None
        ),
        max_holder_pct=round(max_holder, 3) if holders else None,
        top5_holder_pct=round(top5_holder, 3) if holders else None,
        insider_pct=round(insider_pct, 3),
        lp_locked_pct=(
            round(lp_locked_pct, 3) if lp_locked_pct is not None else None
        ),
        market_type=market_type,
        checked_at=checked_at or _iso(),
    )


def format_alert(
    candidate: dict[str, Any],
    momentum: MomentumResult,
    risk: RiskResult,
) -> str:
    latest = candidate["observations"][-1]
    symbol = html.escape(str(candidate.get("symbol") or "Unknown"))
    address = html.escape(str(candidate["token_address"]))
    pool_address = str(candidate["pool_address"])
    link = (
        "https://www.geckoterminal.com/solana/pools/"
        + html.escape(pool_address, quote=True)
    )
    risk_checks = risk.checks[:3] or ["No serious warning reported"]
    risk_lines = "\n".join(
        f"• {html.escape(check)}" for check in risk_checks
    )
    fast_pump_chance = estimate_fast_pump_chance_pct(latest, momentum, risk)
    breakout = momentum.metrics.get("signal_tier") == "low_cap_breakout"
    heading = (
        "🔥 <b>LOW-CAP SOLANA BREAKOUT — CATE-LIKE FLOW</b>"
        if breakout
        else "🚨 <b>EARLY SOLANA LAUNCH — WATCHLIST</b>"
    )
    breakout_note = (
        " · below the $1M breakout ceiling"
        if breakout
        else ""
    )
    return (
        f"{heading}\n"
        "⚠️ <b>Screened, still high risk</b> · <b>No trade was placed</b>\n\n"
        f"<b>{symbol}</b> · Solana · Pool age "
        f"{_duration(_num(latest.get('age_seconds')))}\n"
        f"Liquidity: <b>${_num(latest.get('liquidity_usd')):,.0f}</b> · "
        f"Market cap: <b>${_num(latest.get('market_cap_usd')):,.0f}</b>\n"
        f"5m price: {_num(latest.get('price_change_m5_pct')):+.1f}%"
        f"{breakout_note}\n\n"
        "<b>Why it made the watchlist</b>\n"
        f"• Buyers / sellers (5m): "
        f"{int(_num(latest.get('buyers_m5')))} / "
        f"{int(_num(latest.get('sellers_m5')))}\n"
        f"• Buyer acceleration: +{int(momentum.metrics['latest_buyer_delta'])} "
        f"({momentum.metrics['buyer_acceleration_ratio']:.2f}x)\n"
        f"• Volume acceleration: "
        f"+${momentum.metrics['latest_volume_delta']:,.0f} "
        f"({momentum.metrics['volume_acceleration_ratio']:.2f}x)\n\n"
        "<b>Rough 50%+ pump estimate (next 6h)</b>\n"
        f"~<b>{fast_pump_chance}%</b> based on the current flow — not a "
        "prediction.\n\n"
        "<b>Risk checks passed</b>\n"
        f"{risk_lines}\n\n"
        f"<b>Token</b>\n<code>{address}</code>\n"
        f'<a href="{link}">Open pool on GeckoTerminal</a>\n\n'
        "<b>Manual max entry</b>\n"
        "$20 maximum · alert-only, no order was placed\n\n"
        "This is a watchlist alert, not a buy call. Never risk money you "
        "cannot afford to lose."
    )


def _new_state() -> dict[str, Any]:
    return {
        "version": 1,
        "candidates": {},
        "cooldowns": {},
        "recent_alerts": [],
        # This ledger is intentionally separate from candidates and trade state.
        # It retains alerts until both read-only price checkpoints are resolved.
        "outcome_alerts": [],
        "stats": {
            "pools_observed": 0,
            "risk_checks": 0,
            "rejected_total": 0,
            "alerts_created": 0,
            "rejection_reasons": {},
        },
    }


class MemeScanner:
    """Persistent scanner coordinator with bounded public API polling."""

    def __init__(
        self,
        *,
        config: ScannerConfig | None = None,
        alert_callback: Callable[[str], Any] | None = None,
        delivery_enabled: bool = False,
        state_dir: Path | None = None,
        http_get: Callable[..., Any] = requests.get,
    ):
        self.config = config or ScannerConfig.from_env()
        self.alert_callback = alert_callback
        self.delivery_enabled = delivery_enabled
        self.http_get = http_get
        configured_dir = os.getenv("MEME_SCANNER_STATE_DIR", "").strip()
        self.state_dir = (
            state_dir
            or (Path(configured_dir).expanduser() if configured_dir else Path(__file__).parent)
        ).resolve()
        self.state_file = self.state_dir / "meme_scanner_state.json"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._persistence_error: str | None = None
        self._runtime = {
            "state": "starting",
            "reason": "Waiting for first new-pool scan",
            "last_check": None,
            "last_success": None,
            "last_error": None,
        }
        try:
            self._state = self._load_state()
            self._check_writable()
        except Exception as exc:
            self._state = _new_state()
            self._persistence_error = str(exc)[:300]
            self._runtime.update(
                state="persistence_error",
                reason="Alerts blocked because restart-safe storage is unavailable",
                last_error=self._persistence_error,
            )

    def _load_state(self) -> dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            return _new_state()
        raw = json.loads(self.state_file.read_text())
        if not isinstance(raw, dict):
            raise ValueError("scanner state is not a JSON object")
        state = _new_state()
        for key in (
            "candidates",
            "cooldowns",
            "recent_alerts",
            "outcome_alerts",
            "stats",
        ):
            if key in raw:
                state[key] = raw[key]
        if not isinstance(state["candidates"], dict):
            raise ValueError("scanner candidates state is invalid")
        if not isinstance(state["cooldowns"], dict):
            raise ValueError("scanner cooldown state is invalid")
        if not isinstance(state["recent_alerts"], list):
            raise ValueError("scanner recent-alert state is invalid")
        if not isinstance(state["outcome_alerts"], list):
            raise ValueError("scanner outcome-alert state is invalid")
        if not isinstance(state["stats"], dict):
            raise ValueError("scanner statistics state is invalid")
        state["stats"].setdefault("rejection_reasons", {})
        return state

    def _check_writable(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        probe = self.state_dir / ".meme-scanner-write-test"
        probe.write_text("ok")
        probe.unlink()

    def _save_locked(self) -> None:
        if self._persistence_error:
            raise RuntimeError(self._persistence_error)
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self._state, indent=2, sort_keys=True))
        os.replace(temp, self.state_file)

    def _request_json(self, url: str) -> dict[str, Any]:
        response = self.http_get(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("API returned a non-object JSON response")
        return payload

    def _candidate_for_locked(self, snapshot: PoolSnapshot) -> dict[str, Any]:
        candidates = self._state["candidates"]
        candidate = candidates.setdefault(
            snapshot.pool_id,
            {
                "pool_id": snapshot.pool_id,
                "pool_address": snapshot.pool_address,
                "token_address": snapshot.token_address,
                "name": snapshot.name,
                "symbol": snapshot.symbol,
                "dex": snapshot.dex,
                "created_at": snapshot.created_at,
                "first_seen": snapshot.observed_at,
                "last_seen": snapshot.observed_at,
                "last_reason": "new_pool",
                "last_detail": "First observation",
                "observations": [],
                "risk": None,
                "alerted_at": None,
            },
        )
        candidate.update(
            pool_address=snapshot.pool_address,
            token_address=snapshot.token_address,
            name=snapshot.name,
            symbol=snapshot.symbol,
            dex=snapshot.dex,
            last_seen=snapshot.observed_at,
        )
        observation = snapshot.observation()
        history = candidate["observations"]
        if not history or history[-1] != observation:
            history.append(observation)
            candidate["observations"] = history[-12:]
        return candidate

    def _tracking_addresses_locked(self, now: datetime) -> list[str]:
        """Select promising discoveries for exact-pool follow-up calls."""
        ranked: list[tuple[tuple[float, ...], str]] = []
        for candidate in self._state["candidates"].values():
            observations = candidate.get("observations")
            if (
                candidate.get("alerted_at")
                or not isinstance(observations, list)
                or not observations
            ):
                continue
            latest = observations[-1]
            age = _num(latest.get("age_seconds"))
            liquidity = _num(latest.get("liquidity_usd"))
            gain = _num(latest.get("price_change_m5_pct"))
            market_cap = _num(latest.get("market_cap_usd"))
            buyers = int(_num(latest.get("buyers_m5")))
            breakout_trackable = (
                gain > self.config.max_price_gain_pct
                and gain <= self.config.max_breakout_price_gain_pct
                and 0 < market_cap <= self.config.max_breakout_market_cap_usd
            )
            if (
                age > self.config.max_age_seconds
                or liquidity < self.config.min_liquidity_usd * 0.5
                or (
                    gain > self.config.max_price_gain_pct
                    and not breakout_trackable
                )
                or gain < -self.config.max_price_drop_pct
                or buyers < 2
            ):
                continue
            address = candidate.get("pool_address")
            if not isinstance(address, str) or not address:
                continue
            try:
                recency = _parse_time(candidate["last_seen"]).timestamp()
            except (KeyError, TypeError, ValueError):
                recency = 0
            # Prefer candidates already building history, then liquidity and
            # recency. This bounds public API use without depending on page 1.
            rank = (
                float(min(len(observations), self.config.min_observations)),
                liquidity,
                recency,
            )
            ranked.append((rank, address))
        ranked.sort(reverse=True)
        return [
            address
            for _, address in ranked[: self.config.max_tracked_candidates]
        ]

    def _fetch_followup_pools(
        self,
        addresses: list[str],
        now: datetime,
    ) -> tuple[list[PoolSnapshot], list[str]]:
        pools: list[PoolSnapshot] = []
        errors: list[str] = []
        for index in range(0, len(addresses), 30):
            chunk = addresses[index : index + 30]
            url = GECKO_MULTI_POOLS_URL.format(addresses=",".join(chunk))
            try:
                pools.extend(
                    parse_gecko_pools(self._request_json(url), now=now)
                )
            except Exception as exc:
                errors.append(f"pool follow-up batch failed: {str(exc)[:140]}")
        return pools, errors

    def _record_rejection_locked(
        self,
        candidate: dict[str, Any],
        reason: str,
        detail: str,
    ) -> None:
        candidate["last_reason"] = reason
        candidate["last_detail"] = detail
        key = f"{reason}:{detail}"
        if candidate.get("last_counted_rejection") == key:
            return
        candidate["last_counted_rejection"] = key
        stats = self._state["stats"]
        stats["rejected_total"] = int(stats.get("rejected_total", 0)) + 1
        counts = stats.setdefault("rejection_reasons", {})
        counts[reason] = int(counts.get(reason, 0)) + 1

    def _risk_is_fresh(self, candidate: dict[str, Any], now: datetime) -> bool:
        risk = candidate.get("risk")
        if not isinstance(risk, dict) or not risk.get("checked_at"):
            return False
        try:
            age = (now - _parse_time(risk["checked_at"])).total_seconds()
            return age <= self.config.risk_cache_seconds
        except (TypeError, ValueError):
            return False

    def _cooldown_active_locked(self, token: str, now: datetime) -> bool:
        value = self._state["cooldowns"].get(token)
        if not value:
            return False
        try:
            return (
                now - _parse_time(value)
            ).total_seconds() < self.config.cooldown_seconds
        except (TypeError, ValueError):
            return False

    def _build_alert_record(
        self,
        candidate: dict[str, Any],
        momentum: MomentumResult,
        risk: RiskResult,
        now: datetime,
    ) -> dict[str, Any]:
        latest = candidate["observations"][-1]
        return {
            "created_at": _iso(now),
            "network": "solana",
            "symbol": candidate["symbol"],
            "token_address": candidate["token_address"],
            "pool_address": candidate["pool_address"],
            "pool_age_seconds": latest["age_seconds"],
            "entry_price_usd": latest["price_usd"],
            "liquidity_usd": latest["liquidity_usd"],
            "market_cap_usd": latest.get("market_cap_usd", 0),
            "buyers_m5": latest["buyers_m5"],
            "sellers_m5": latest["sellers_m5"],
            "buys_m5": latest["buys_m5"],
            "sells_m5": latest["sells_m5"],
            "volume_m5_usd": latest["volume_m5_usd"],
            "price_change_m5_pct": latest["price_change_m5_pct"],
            "signal_tier": momentum.metrics.get(
                "signal_tier", "early_acceleration"
            ),
            "buyer_acceleration_ratio": momentum.metrics[
                "buyer_acceleration_ratio"
            ],
            "volume_acceleration_ratio": momentum.metrics[
                "volume_acceleration_ratio"
            ],
            "rough_fast_pump_chance_pct": estimate_fast_pump_chance_pct(
                latest, momentum, risk
            ),
            "rough_fast_pump_window": "50%+ move within 6 hours",
            "risk_score": risk.score,
            "risk_checks": risk.checks[:6],
            "market_url": (
                "https://www.geckoterminal.com/solana/pools/"
                + candidate["pool_address"]
            ),
            "label": "screened, still high risk",
            "trade_placed": False,
            "delivery": "pending" if self.delivery_enabled else "development_disabled",
            "qualification_evidence": self._qualification_evidence(momentum, risk),
            "outcomes": {},
            "outcome_attempts": {},
        }

    def _qualification_evidence(
        self, momentum: MomentumResult, risk: RiskResult
    ) -> list[str]:
        """Human-readable evidence groups used only for outcome analysis."""
        buyer_ratio = momentum.metrics["buyer_acceleration_ratio"]
        volume_ratio = momentum.metrics["volume_acceleration_ratio"]
        return [
            (
                "low-cap breakout tier"
                if momentum.metrics.get("signal_tier") == "low_cap_breakout"
                else "early acceleration tier"
            ),
            (
                "buyer acceleration ≥1.5x"
                if buyer_ratio >= 1.5
                else "buyer acceleration 1.15–1.49x"
            ),
            (
                "volume acceleration ≥1.5x"
                if volume_ratio >= 1.5
                else "volume acceleration 1.20–1.49x"
            ),
            "risk screen passed" if risk.passed else "risk screen unavailable",
        ]

    def _due_outcome_alerts_locked(self, now: datetime) -> list[dict[str, Any]]:
        due: list[tuple[datetime, dict[str, Any]]] = []
        for alert in self._state["outcome_alerts"]:
            try:
                created = _parse_time(str(alert.get("created_at", "")))
            except (TypeError, ValueError):
                continue
            for minutes in self.config.outcome_checkpoint_minutes:
                checkpoint = f"{minutes}m"
                due_at = created + timedelta(minutes=minutes)
                attempts = int(
                    alert.get("outcome_attempts", {}).get(checkpoint, 0)
                )
                if (
                    checkpoint not in alert.get("outcomes", {})
                    and attempts < self.config.max_outcome_attempts
                    and now >= due_at
                ):
                    due.append((due_at, alert))
                    break
        due.sort(key=lambda item: item[0])
        return [alert for _, alert in due]

    @staticmethod
    def _alert_key(alert: dict[str, Any]) -> tuple[str, str]:
        return (
            str(alert.get("pool_address", "")),
            str(alert.get("created_at", "")),
        )

    def _sync_recent_outcomes_locked(self) -> None:
        outcomes_by_alert = {
            self._alert_key(alert): alert.get("outcomes", {})
            for alert in self._state["outcome_alerts"]
        }
        for alert in self._state["recent_alerts"]:
            outcomes = outcomes_by_alert.get(self._alert_key(alert))
            if isinstance(outcomes, dict):
                alert["outcomes"] = json.loads(json.dumps(outcomes))

    def _expire_outcome_checkpoints_locked(self, now: datetime) -> bool:
        changed = False
        for alert in self._state["outcome_alerts"]:
            try:
                created = _parse_time(str(alert.get("created_at", "")))
            except (TypeError, ValueError):
                continue
            outcomes = alert.setdefault("outcomes", {})
            attempts = alert.setdefault("outcome_attempts", {})
            for minutes in self.config.outcome_checkpoint_minutes:
                checkpoint = f"{minutes}m"
                if checkpoint in outcomes:
                    continue
                due_at = created + timedelta(minutes=minutes)
                expired = (
                    now - due_at
                ).total_seconds() > self.config.outcome_checkpoint_grace_seconds
                exhausted = int(attempts.get(checkpoint, 0)) >= (
                    self.config.max_outcome_attempts
                )
                if expired or exhausted:
                    outcomes[checkpoint] = {
                        "status": "unavailable",
                        "due_at": _iso(due_at),
                        "closed_at": _iso(now),
                        "attempts": int(attempts.get(checkpoint, 0)),
                        "reason": (
                            "checkpoint window expired"
                            if expired
                            else "price lookup retry limit reached"
                        ),
                    }
                    changed = True
        if changed:
            self._sync_recent_outcomes_locked()
        return changed

    def _resolve_due_outcomes(
        self,
        now: datetime,
        outcome_payload: dict[str, Any] | None = None,
    ) -> None:
        """Resolve due alert checkpoints via GeckoTerminal only; never trade."""
        with self._lock:
            expired = self._expire_outcome_checkpoints_locked(now)
            due = self._due_outcome_alerts_locked(now)
            selected: list[dict[str, Any]] = []
            selected_addresses: set[str] = set()
            for alert in due:
                address = alert.get("pool_address")
                if not isinstance(address, str) or not address:
                    continue
                if (
                    address not in selected_addresses
                    and len(selected_addresses)
                    >= self.config.max_outcome_pools_per_scan
                ):
                    continue
                selected.append(alert)
                selected_addresses.add(address)
            for alert in selected:
                created = _parse_time(str(alert["created_at"]))
                attempts = alert.setdefault("outcome_attempts", {})
                for minutes in self.config.outcome_checkpoint_minutes:
                    checkpoint = f"{minutes}m"
                    if (
                        checkpoint not in alert.get("outcomes", {})
                        and now >= created + timedelta(minutes=minutes)
                    ):
                        attempts[checkpoint] = int(attempts.get(checkpoint, 0)) + 1
            if selected or expired:
                self._save_locked()
            due = selected
        if not due:
            return

        addresses = list(selected_addresses)
        if not addresses:
            return
        try:
            if outcome_payload is not None:
                pools = parse_gecko_pools(outcome_payload, now=now)
            else:
                pools, errors = self._fetch_followup_pools(addresses, now)
                if errors:
                    logger.warning(
                        "Early-alert outcome price fetch incomplete: %s",
                        "; ".join(errors),
                    )
            prices = {pool.pool_address: pool.price_usd for pool in pools}
        except Exception as exc:
            logger.warning("Early-alert outcome price fetch failed: %s", exc)
            return

        changed = False
        with self._lock:
            for alert in self._due_outcome_alerts_locked(now):
                price = prices.get(alert.get("pool_address"))
                entry_price = _num(alert.get("entry_price_usd"))
                if price is None or price <= 0 or entry_price <= 0:
                    continue
                created = _parse_time(str(alert["created_at"]))
                outcomes = alert.setdefault("outcomes", {})
                for minutes in self.config.outcome_checkpoint_minutes:
                    checkpoint = f"{minutes}m"
                    if (
                        checkpoint in outcomes
                        or now < created + timedelta(minutes=minutes)
                    ):
                        continue
                    move_pct = round((price / entry_price - 1) * 100, 3)
                    outcomes[checkpoint] = {
                        "status": "recorded",
                        "due_at": _iso(created + timedelta(minutes=minutes)),
                        "sampled_at": _iso(now),
                        "delay_seconds": round(
                            (
                                now
                                - (created + timedelta(minutes=minutes))
                            ).total_seconds(),
                            1,
                        ),
                        "price_usd": price,
                        "move_pct": move_pct,
                        "favorable": move_pct >= self.config.favorable_move_pct,
                    }
                    changed = True
            if changed:
                self._sync_recent_outcomes_locked()
                self._save_locked()

    def _outcome_summary_locked(self) -> dict[str, Any]:
        alerts = self._state["outcome_alerts"]
        checkpoints: dict[str, dict[str, Any]] = {}
        evidence: dict[str, dict[str, dict[str, int]]] = {}
        for minutes in self.config.outcome_checkpoint_minutes:
            key = f"{minutes}m"
            resolved = [
                alert["outcomes"][key]
                for alert in alerts
                if isinstance(alert.get("outcomes"), dict)
                and isinstance(alert["outcomes"].get(key), dict)
                and alert["outcomes"][key].get("status") == "recorded"
            ]
            unavailable = sum(
                1
                for alert in alerts
                if alert.get("outcomes", {}).get(key, {}).get("status")
                == "unavailable"
            )
            moves = [_num(outcome.get("move_pct")) for outcome in resolved]
            drawdowns = [min(0.0, move) for move in moves]
            checkpoints[key] = {
                "resolved": len(resolved),
                "unavailable": unavailable,
                "pending": sum(
                    1 for alert in alerts if key not in alert.get("outcomes", {})
                ),
                "favorable": sum(
                    1 for outcome in resolved if outcome.get("favorable") is True
                ),
                "favorable_rate_pct": round(
                    100
                    * sum(1 for outcome in resolved if outcome.get("favorable") is True)
                    / len(resolved),
                    1,
                )
                if resolved
                else None,
                "average_move_pct": round(sum(moves) / len(moves), 2)
                if moves
                else None,
                "average_drawdown_pct": round(
                    sum(drawdowns) / len(drawdowns), 2
                )
                if drawdowns
                else None,
                "worst_move_pct": round(min(moves), 2) if moves else None,
            }
            for alert in alerts:
                outcome = alert.get("outcomes", {}).get(key)
                if (
                    not isinstance(outcome, dict)
                    or outcome.get("status") != "recorded"
                ):
                    continue
                for label in alert.get("qualification_evidence", []):
                    bucket = evidence.setdefault(label, {}).setdefault(
                        key, {"alerts": 0, "favorable": 0}
                    )
                    bucket["alerts"] += 1
                    bucket["favorable"] += int(outcome.get("favorable") is True)
        evidence_summary = {
            label: {
                key: {
                    **values,
                    "favorable_rate_pct": round(
                        100 * values["favorable"] / values["alerts"], 1
                    )
                    if values["alerts"]
                    else None,
                }
                for key, values in by_checkpoint.items()
            }
            for label, by_checkpoint in evidence.items()
        }
        alert_drawdowns = []
        for alert in alerts:
            moves = [
                _num(outcome.get("move_pct"))
                for outcome in alert.get("outcomes", {}).values()
                if isinstance(outcome, dict)
                and outcome.get("status") == "recorded"
            ]
            if moves:
                alert_drawdowns.append(min(0.0, min(moves)))
        resolved_hour = checkpoints.get("60m", {}).get("resolved", 0)
        return {
            "alerts_tracked": len(alerts),
            "favorable_move_pct": self.config.favorable_move_pct,
            "checkpoints": checkpoints,
            "by_qualification_evidence": evidence_summary,
            "drawdown": {
                "alerts_measured": len(alert_drawdowns),
                "average_max_drawdown_pct": round(
                    sum(alert_drawdowns) / len(alert_drawdowns), 2
                )
                if alert_drawdowns
                else None,
                "worst_drawdown_pct": round(min(alert_drawdowns), 2)
                if alert_drawdowns
                else None,
            },
            "decision_readiness": {
                "minimum_sample_size": self.config.min_outcome_sample_size,
                "resolved_60m": resolved_hour,
                "ready": resolved_hour >= self.config.min_outcome_sample_size,
                "guidance": (
                    "Enough 60-minute outcomes to review threshold changes"
                    if resolved_hour >= self.config.min_outcome_sample_size
                    else (
                        "Keep thresholds unchanged until "
                        f"{self.config.min_outcome_sample_size} reliable "
                        "60-minute outcomes resolve"
                    )
                ),
            },
        }

    def _prune_locked(self, now: datetime) -> None:
        candidate_cutoff = max(self.config.max_age_seconds * 4, 2 * 60 * 60)
        for key, candidate in list(self._state["candidates"].items()):
            try:
                age = (now - _parse_time(candidate["last_seen"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                age = candidate_cutoff + 1
            if age > candidate_cutoff:
                del self._state["candidates"][key]
        if len(self._state["candidates"]) > self.config.max_saved_candidates:
            newest = sorted(
                self._state["candidates"].items(),
                key=lambda item: str(item[1].get("last_seen", "")),
                reverse=True,
            )[: self.config.max_saved_candidates]
            self._state["candidates"] = dict(newest)
        for token, sent_at in list(self._state["cooldowns"].items()):
            try:
                age = (now - _parse_time(sent_at)).total_seconds()
            except (TypeError, ValueError):
                age = self.config.cooldown_seconds + 1
            if age > self.config.cooldown_seconds * 2:
                del self._state["cooldowns"][token]
        self._state["recent_alerts"] = self._state["recent_alerts"][:25]
        # Retain resolved outcomes for analysis but never discard a pending alert.
        completed = [
            alert
            for alert in self._state["outcome_alerts"]
            if all(
                f"{minutes}m" in alert.get("outcomes", {})
                for minutes in self.config.outcome_checkpoint_minutes
            )
        ]
        pending = [
            alert
            for alert in self._state["outcome_alerts"]
            if alert not in completed
        ]
        self._state["outcome_alerts"] = (
            pending
            + completed[: self.config.max_saved_outcome_alerts]
        )

    def scan_once(
        self,
        *,
        pools_payload: dict[str, Any] | None = None,
        followup_payload: dict[str, Any] | None = None,
        risk_reports: dict[str, dict[str, Any]] | None = None,
        outcome_payload: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed_at = now or _utc_now()
        self._runtime["last_check"] = _iso(observed_at)
        # Outcome sampling is independent from new-pool discovery. A discovery
        # outage must not prevent an already-alerted pool's scheduled price read.
        self._resolve_due_outcomes(observed_at, outcome_payload)
        try:
            with self._lock:
                tracked_addresses = self._tracking_addresses_locked(observed_at)

            live_source = pools_payload is None
            discovery_payload = (
                self._request_json(GECKO_NEW_POOLS_URL)
                if pools_payload is None
                else pools_payload
            )
            discovered = parse_gecko_pools(
                discovery_payload, now=observed_at
            )
            source_warnings: list[str] = []
            if followup_payload is not None:
                followed = parse_gecko_pools(
                    followup_payload, now=observed_at
                )
            elif live_source and tracked_addresses:
                followed, source_warnings = self._fetch_followup_pools(
                    tracked_addresses, observed_at
                )
            else:
                followed = []

            # Exact-pool follow-ups replace page-one copies of the same pool.
            combined = {pool.pool_id: pool for pool in discovered}
            combined.update({pool.pool_id: pool for pool in followed})
            pools = list(combined.values())
            if not pools:
                raise ValueError(
                    "discovery and exact-pool feeds contained no valid Solana pools"
                )
            qualified: list[tuple[str, MomentumResult]] = []
            with self._lock:
                self._runtime.update(state="scanning", reason="Evaluating new pools")
                self._state["stats"]["pools_observed"] = int(
                    self._state["stats"].get("pools_observed", 0)
                ) + len(pools)
                for pool in pools:
                    candidate = self._candidate_for_locked(pool)
                    momentum = evaluate_momentum(
                        candidate["observations"], self.config
                    )
                    if self._cooldown_active_locked(pool.token_address, observed_at):
                        self._record_rejection_locked(
                            candidate,
                            "duplicate_cooldown",
                            "Token already alerted inside the cooldown window",
                        )
                    elif momentum.qualified:
                        candidate["last_reason"] = momentum.reason
                        candidate["last_detail"] = momentum.detail
                        qualified.append((pool.pool_id, momentum))
                    else:
                        candidate["last_reason"] = momentum.reason
                        candidate["last_detail"] = momentum.detail
                        if momentum.hard_rejection:
                            self._record_rejection_locked(
                                candidate, momentum.reason, momentum.detail
                            )

            risk_budget = self.config.max_risk_checks_per_scan
            alerts_to_send: list[tuple[str, dict[str, Any]]] = []
            for pool_id, momentum in qualified:
                with self._lock:
                    candidate = self._state["candidates"].get(pool_id)
                    if not candidate or candidate.get("alerted_at"):
                        continue
                    if self._risk_is_fresh(candidate, observed_at):
                        risk = RiskResult(**candidate["risk"])
                    elif risk_budget <= 0:
                        candidate["last_reason"] = "risk_check_queued"
                        candidate["last_detail"] = "Waiting for bounded risk-check capacity"
                        continue
                    else:
                        risk = None
                        token = candidate["token_address"]
                        pool_address = candidate["pool_address"]

                if risk is None:
                    risk_budget -= 1
                    try:
                        report = (
                            risk_reports[token]
                            if risk_reports is not None and token in risk_reports
                            else self._request_json(
                                RUGCHECK_REPORT_URL.format(mint=token)
                            )
                        )
                        risk = screen_risk_report(
                            report,
                            pool_address,
                            self.config,
                            checked_at=_iso(observed_at),
                        )
                    except Exception as exc:
                        risk = RiskResult(
                            passed=False,
                            reasons=[f"risk data unavailable: {str(exc)[:180]}"],
                            checks=[],
                            score=None,
                            max_holder_pct=None,
                            top5_holder_pct=None,
                            insider_pct=None,
                            lp_locked_pct=None,
                            market_type="unknown",
                            checked_at=_iso(observed_at),
                        )
                    with self._lock:
                        self._state["stats"]["risk_checks"] = int(
                            self._state["stats"].get("risk_checks", 0)
                        ) + 1
                        candidate = self._state["candidates"].get(pool_id)
                        if candidate:
                            candidate["risk"] = risk.as_dict()

                with self._lock:
                    candidate = self._state["candidates"].get(pool_id)
                    if not candidate or candidate.get("alerted_at"):
                        continue
                    if not risk.passed:
                        self._record_rejection_locked(
                            candidate,
                            "risk_screen_failed",
                            "; ".join(risk.reasons[:4]),
                        )
                        continue

                    alert_text = format_alert(candidate, momentum, risk)
                    record = self._build_alert_record(
                        candidate, momentum, risk, observed_at
                    )
                    # Persist the cooldown before delivery. A timeout after
                    # Telegram accepted a message is ambiguous; retrying risks
                    # a duplicate, so this scanner uses at-most-once delivery.
                    candidate["alerted_at"] = _iso(observed_at)
                    candidate["last_reason"] = "alert_created"
                    candidate["last_detail"] = "Passed momentum and risk screens"
                    self._state["cooldowns"][candidate["token_address"]] = _iso(
                        observed_at
                    )
                    self._state["recent_alerts"].insert(0, record)
                    self._state["outcome_alerts"].insert(0, record)
                    self._state["stats"]["alerts_created"] = int(
                        self._state["stats"].get("alerts_created", 0)
                    ) + 1
                    self._save_locked()
                    alerts_to_send.append((alert_text, record))

            for text, record in alerts_to_send:
                if not self.delivery_enabled or not self.alert_callback:
                    continue
                try:
                    delivered = self.alert_callback(text)
                    record["delivery"] = (
                        f"sent_to_{delivered}"
                        if isinstance(delivered, int)
                        else "sent"
                    )
                except Exception as exc:
                    record["delivery"] = "unconfirmed"
                    record["delivery_error"] = str(exc)[:180]

            with self._lock:
                self._prune_locked(observed_at)
                self._runtime.update(
                    state="degraded" if source_warnings else "watching",
                    reason=(
                        f"Observed {len(pools)} new Solana pools; "
                        f"followed {len(followed)} exact pools; "
                        f"{len(alerts_to_send)} alert(s) created"
                    ),
                    last_success=_iso(observed_at),
                    last_error=(
                        "; ".join(source_warnings) if source_warnings else None
                    ),
                )
                self._save_locked()
            return self.snapshot()
        except Exception as exc:
            with self._lock:
                self._runtime.update(
                    state="source_error",
                    reason="Public new-pool scan failed; prior observations preserved",
                    last_error=str(exc)[:300],
                )
            logger.warning("Early-launch scan failed: %s", exc)
            return self.snapshot()

    def run_forever(self) -> None:
        logger.info(
            "Early-launch scanner starting: network=solana scan=%ss delivery=%s",
            self.config.scan_seconds,
            "enabled" if self.delivery_enabled else "health-only",
        )
        while not self._stop.is_set():
            if self._persistence_error:
                self._runtime.update(
                    state="persistence_error",
                    reason="Alerts blocked because restart-safe storage is unavailable",
                )
            else:
                self.scan_once()
            self._stop.wait(self.config.scan_seconds)

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run_forever,
            daemon=True,
            name="solana-early-launch-scanner",
        )
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            live_reasons = Counter(
                str(candidate.get("last_reason") or "unknown")
                for candidate in self._state["candidates"].values()
            )
            stats = json.loads(json.dumps(self._state["stats"]))
            return {
                **self._runtime,
                "enabled": self._persistence_error is None,
                "delivery_enabled": self.delivery_enabled,
                "network": "solana",
                "persistence_error": self._persistence_error,
                "observed_candidates": len(self._state["candidates"]),
                "candidates_screened": int(stats.get("pools_observed", 0)),
                "risk_checks": int(stats.get("risk_checks", 0)),
                "rejected_total": int(stats.get("rejected_total", 0)),
                "alerts_created": int(stats.get("alerts_created", 0)),
                "rejection_reasons": stats.get("rejection_reasons", {}),
                "live_rejection_reasons": dict(live_reasons),
                "thresholds": self.config.public_dict(),
                "outcomes": self._outcome_summary_locked(),
                "recent_alerts": json.loads(
                    json.dumps(self._state["recent_alerts"][:10])
                ),
            }

    def telegram_status(self) -> str:
        snap = self.snapshot()
        return (
            "\n\n🚨 <b>Early Solana Launch Scanner</b>\n"
            f"  State: {html.escape(str(snap['state']))}\n"
            f"  {html.escape(str(snap['reason']))}\n"
            f"  Observed: {snap['observed_candidates']} | "
            f"Risk checks: {snap['risk_checks']} | "
            f"Alerts: {snap['alerts_created']}\n"
            "  Alerts are screened, still high risk; no meme-coin trades are placed."
        )