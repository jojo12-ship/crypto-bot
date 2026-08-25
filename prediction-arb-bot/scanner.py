"""Fail-closed, read-only prediction-market arbitrage scanner.

This module intentionally contains no order, wallet, balance, position, or
fund-moving code. It only evaluates quotes supplied by read-only adapters
against an explicit human-reviewed equivalence registry.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urlparse


UTC = timezone.utc
STATE_VERSION = 1
MAX_DEDUP_KEYS = 2_000
MAX_FEE_REVIEW_AGE_DAYS = 30


class ValidationError(ValueError):
    """Raised when a market, registry record, or quote is unsafe to evaluate."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def canonical_text(value: str) -> str:
    return " ".join(str(value).casefold().split())


def rules_fingerprint(value: str) -> str:
    normalized = canonical_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_official_review_url(field: str, value: str, raw: dict[str, Any]) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        return False
    if field == "kalshi_market_url":
        expected_path = (
            "/trade-api/v2/markets/"
            + quote(str(raw.get("kalshi_market_id", "")), safe="")
        )
        return (
            parsed.hostname == "api.elections.kalshi.com"
            and parsed.path == expected_path
            and not parsed.query
        )
    if field == "polymarket_market_url":
        query = parse_qs(parsed.query, keep_blank_values=True)
        return bool(
            parsed.hostname == "gateway.polymarket.us"
            and parsed.path == "/v1/markets"
            and set(query) == {"slug", "active", "closed"}
            and len(query["slug"]) == 1
            and bool(query["slug"][0])
            and query["active"] == ["true"]
            and query["closed"] == ["false"]
        )
    if field == "kalshi_fee_evidence_url":
        return value in {
            "https://kalshi.com/fee-schedule",
            "https://help.kalshi.com/en/articles/13823805-fees",
        }
    if field == "polymarket_fee_evidence_url":
        return value == "https://docs.polymarket.us/fees"
    return False


@dataclass(frozen=True)
class Quote:
    outcome: str
    ask_cents: int | None
    available_contracts: int | None

    def is_executable(self, minimum_contracts: int) -> bool:
        return bool(
            self.ask_cents is not None
            and 1 <= self.ask_cents <= 99
            and self.available_contracts is not None
            and self.available_contracts >= minimum_contracts
        )


@dataclass(frozen=True)
class BinaryMarket:
    venue: str
    market_id: str
    title: str
    rules_text: str
    resolution_source: str
    cutoff_at: str
    event_start_at: str
    yes: Quote
    no: Quote
    fetched_at: str
    url: str = ""
    fee_model: str = ""
    fee_coefficient: float | None = None

    @property
    def rules_hash(self) -> str:
        return rules_fingerprint(self.rules_text)

    def is_fresh(self, maximum_age_seconds: int, now: datetime | None = None) -> bool:
        fetched = parse_timestamp(self.fetched_at)
        if fetched is None:
            return False
        current = now or utc_now()
        age = current - fetched
        # Provider timestamps in the future are invalid, not "extra fresh".
        # Strict rejection avoids clock-skewed or malformed data bypassing the
        # stale-quote gate.
        return timedelta(0) <= age <= timedelta(seconds=maximum_age_seconds)


@dataclass(frozen=True)
class ReviewedEquivalence:
    """A human-reviewed, immutable pairing; title similarity is never used."""

    record_id: str
    kalshi_market_id: str
    polymarket_market_id: str
    kalshi_market_url: str
    polymarket_market_url: str
    kalshi_rules_hash: str
    polymarket_rules_hash: str
    kalshi_resolution_source: str
    polymarket_resolution_source: str
    kalshi_fee_evidence_url: str
    polymarket_fee_evidence_url: str
    kalshi_max_taker_fee_cents: int
    polymarket_max_taker_fee_cents: int
    fees_reviewed_at: str
    event_cutoff_at: str
    event_start_at: str
    kalshi_yes_means: str
    polymarket_yes_means: str
    settlement_summary: str
    reviewed_at: str
    reviewer_note: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReviewedEquivalence":
        fields = (
            "record_id",
            "kalshi_market_id",
            "polymarket_market_id",
            "kalshi_market_url",
            "polymarket_market_url",
            "kalshi_rules_hash",
            "polymarket_rules_hash",
            "kalshi_resolution_source",
            "polymarket_resolution_source",
            "kalshi_fee_evidence_url",
            "polymarket_fee_evidence_url",
            "fees_reviewed_at",
            "event_cutoff_at",
            "event_start_at",
            "kalshi_yes_means",
            "polymarket_yes_means",
            "settlement_summary",
            "reviewed_at",
            "reviewer_note",
        )
        missing = [name for name in fields if not isinstance(raw.get(name), str) or not raw[name].strip()]
        if missing:
            raise ValidationError(f"reviewed equivalence missing: {', '.join(missing)}")
        fee_fields = ("kalshi_max_taker_fee_cents", "polymarket_max_taker_fee_cents")
        invalid_fees = [
            name
            for name in fee_fields
            if not isinstance(raw.get(name), int)
            or isinstance(raw.get(name), bool)
            or raw[name] <= 0
        ]
        if invalid_fees:
            raise ValidationError(
                "reviewed equivalence requires positive per-contract fee caps: "
                + ", ".join(invalid_fees)
            )
        if parse_timestamp(raw["event_cutoff_at"]) is None:
            raise ValidationError("event_cutoff_at must be an ISO-8601 timestamp")
        if parse_timestamp(raw["event_start_at"]) is None:
            raise ValidationError("event_start_at must be an ISO-8601 timestamp")
        if parse_timestamp(raw["reviewed_at"]) is None:
            raise ValidationError("reviewed_at must be an ISO-8601 timestamp")
        if parse_timestamp(raw["fees_reviewed_at"]) is None:
            raise ValidationError("fees_reviewed_at must be an ISO-8601 timestamp")
        for field in (
            "kalshi_market_url",
            "polymarket_market_url",
            "kalshi_fee_evidence_url",
            "polymarket_fee_evidence_url",
        ):
            if not _is_official_review_url(field, raw[field], raw):
                raise ValidationError(f"{field} must use the canonical official venue URL")
        if canonical_text(raw["kalshi_yes_means"]) != canonical_text(raw["polymarket_yes_means"]):
            raise ValidationError("reviewed YES outcome meanings must be exactly equivalent")
        values: dict[str, Any] = {name: raw[name].strip() for name in fields}
        values.update({name: raw[name] for name in fee_fields})
        return cls(**values)

    def fees_are_current(self, now: datetime) -> bool:
        reviewed = parse_timestamp(self.fees_reviewed_at)
        if reviewed is None:
            return False
        age = now - reviewed
        return timedelta(0) <= age <= timedelta(days=MAX_FEE_REVIEW_AGE_DAYS)

    def validates(self, kalshi: BinaryMarket, polymarket: BinaryMarket) -> tuple[bool, str]:
        if kalshi.venue != "kalshi" or polymarket.venue != "polymarket_us":
            return False, "wrong market venues"
        if kalshi.market_id != self.kalshi_market_id or polymarket.market_id != self.polymarket_market_id:
            return False, "market identifiers do not match reviewed record"
        if kalshi.url != self.kalshi_market_url or polymarket.url != self.polymarket_market_url:
            return False, "market URLs do not match reviewed record"
        if kalshi.rules_hash != self.kalshi_rules_hash:
            return False, "Kalshi settlement wording changed or is unavailable"
        if polymarket.rules_hash != self.polymarket_rules_hash:
            return False, "Polymarket US settlement wording changed or is unavailable"
        if canonical_text(kalshi.resolution_source) != canonical_text(self.kalshi_resolution_source):
            return False, "Kalshi resolution source changed or is unavailable"
        if canonical_text(polymarket.resolution_source) != canonical_text(self.polymarket_resolution_source):
            return False, "Polymarket US resolution source changed or is unavailable"
        if (
            polymarket.fee_model != "taker_fee_coefficient"
            or not isinstance(polymarket.fee_coefficient, (int, float))
            or isinstance(polymarket.fee_coefficient, bool)
            or not math.isfinite(polymarket.fee_coefficient)
            or polymarket.fee_coefficient <= 0
        ):
            return False, "Polymarket US per-market fee information changed or is unavailable"
        live_max_fee_cents = math.ceil(polymarket.fee_coefficient * 25)
        if live_max_fee_cents > self.polymarket_max_taker_fee_cents:
            return False, "Polymarket US live fee coefficient exceeds the reviewed fee cap"
        if kalshi.cutoff_at != self.event_cutoff_at or polymarket.cutoff_at != self.event_cutoff_at:
            return False, "event cutoff does not exactly match reviewed timing"
        if (
            kalshi.event_start_at != self.event_start_at
            or polymarket.event_start_at != self.event_start_at
        ):
            return False, "event start does not exactly match reviewed timing"
        if canonical_text(self.kalshi_yes_means) != canonical_text(self.polymarket_yes_means):
            return False, "reviewed YES outcome mapping is not complementary across platforms"
        return True, "reviewed settlement wording, resolution source, cutoff, and YES mapping verified"


@dataclass(frozen=True)
class ScannerConfig:
    max_source_age_seconds: int = 30
    minimum_contracts: int = 1
    kalshi_max_taker_fee_cents: int | None = None
    polymarket_max_taker_fee_cents: int | None = None
    kalshi_slippage_cents: int = 1
    polymarket_slippage_cents: int = 1
    dedup_minutes: int = 60
    fees_verified: bool = False
    minimum_event_horizon_hours: int = 1
    maximum_event_horizon_hours: int = 72

    def cost_model_ready(self) -> bool:
        return bool(
            self.fees_verified
            and isinstance(self.kalshi_max_taker_fee_cents, int)
            and self.kalshi_max_taker_fee_cents > 0
            and isinstance(self.polymarket_max_taker_fee_cents, int)
            and self.polymarket_max_taker_fee_cents > 0
            and self.kalshi_slippage_cents >= 0
            and self.polymarket_slippage_cents >= 0
        )

    def event_horizon_ready(self) -> bool:
        return bool(
            isinstance(self.minimum_event_horizon_hours, int)
            and isinstance(self.maximum_event_horizon_hours, int)
            and 1 <= self.minimum_event_horizon_hours
            and self.maximum_event_horizon_hours > self.minimum_event_horizon_hours
            and self.maximum_event_horizon_hours <= 72
        )


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    equivalence_id: str
    market: str
    settlement_summary: str
    validation_reason: str
    leg_a: dict[str, Any]
    leg_b: dict[str, Any]
    available_contracts: int
    gross_gap_cents: int
    estimated_fees_cents: int
    estimated_slippage_cents: int
    net_edge_cents: int
    net_edge_pct: float
    observed_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DurableOpportunityState:
    """Atomic JSON state for cross-restart alert deduplication."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = self._load()

    def _default(self) -> dict[str, Any]:
        return {"version": STATE_VERSION, "seen": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
            if parsed.get("version") != STATE_VERSION or not isinstance(parsed.get("seen"), dict):
                raise ValidationError("unsupported opportunity state")
            return parsed
        except (OSError, json.JSONDecodeError, AttributeError, ValidationError) as exc:
            raise RuntimeError(f"Cannot safely load opportunity deduplication state: {exc}") from exc

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self._state, indent=2, sort_keys=True) + "\n"
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            # Reserve the deduplication key before publishing an alert. A crash
            # after a visible notification must prefer a suppressed retry over a
            # duplicate alert.
            import os
            os.fsync(handle.fileno())
        temp.replace(self.path)

    def reserve_alert(self, opportunity_id: str, now: datetime, ttl_minutes: int) -> bool:
        """Return true once per alert key/TTL, including after a process restart."""
        with self._lock:
            seen = self._state["seen"]
            previous = parse_timestamp(seen.get(opportunity_id))
            if previous and now - previous < timedelta(minutes=ttl_minutes):
                return False
            seen[opportunity_id] = now.isoformat()
            while len(seen) > MAX_DEDUP_KEYS:
                oldest = min(seen, key=lambda key: seen[key])
                seen.pop(oldest, None)
            self._save()
            return True


def load_reviewed_equivalences(path: Path) -> list[ReviewedEquivalence]:
    """Read reviewed pair records; a missing registry intentionally yields no pairs."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid reviewed equivalence registry: {exc}") from exc
    if raw.get("version") != 1 or not isinstance(raw.get("matches"), list):
        raise ValidationError("registry must contain version 1 and a matches array")
    return [ReviewedEquivalence.from_dict(record) for record in raw["matches"]]


def _fee_cents(max_taker_fee_cents: int | None) -> int:
    if max_taker_fee_cents is None:
        raise ValidationError("fee configuration is unknown")
    if max_taker_fee_cents <= 0:
        raise ValidationError("fee configuration must be a positive conservative per-contract cap")
    return max_taker_fee_cents


def _opportunity_id(equivalence_id: str, first: Quote, second: Quote) -> str:
    raw = f"{equivalence_id}|{first.outcome}|{first.ask_cents}|{second.outcome}|{second.ask_cents}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def calculate_opportunities(
    reviewed: ReviewedEquivalence,
    kalshi: BinaryMarket,
    polymarket: BinaryMarket,
    config: ScannerConfig,
    now: datetime | None = None,
) -> list[Opportunity]:
    """Evaluate both complementary YES/NO combinations after all conservative costs."""
    if not config.cost_model_ready():
        return []
    valid, reason = reviewed.validates(kalshi, polymarket)
    current = now or utc_now()
    if not valid:
        return []
    if not config.event_horizon_ready():
        return []
    if not reviewed.fees_are_current(current):
        return []
    minimum_start = current + timedelta(hours=config.minimum_event_horizon_hours)
    maximum_start = current + timedelta(hours=config.maximum_event_horizon_hours)
    for candidate in (kalshi, polymarket):
        event_start = parse_timestamp(candidate.event_start_at)
        if event_start is None or not minimum_start <= event_start <= maximum_start:
            return []
    if not kalshi.is_fresh(config.max_source_age_seconds, current):
        return []
    if not polymarket.is_fresh(config.max_source_age_seconds, current):
        return []

    combinations = ((kalshi.yes, polymarket.no), (kalshi.no, polymarket.yes))
    results: list[Opportunity] = []
    for kalshi_leg, polymarket_leg in combinations:
        if not kalshi_leg.is_executable(config.minimum_contracts):
            continue
        if not polymarket_leg.is_executable(config.minimum_contracts):
            continue
        assert kalshi_leg.ask_cents is not None and polymarket_leg.ask_cents is not None
        assert kalshi_leg.available_contracts is not None and polymarket_leg.available_contracts is not None
        gross_gap = 100 - kalshi_leg.ask_cents - polymarket_leg.ask_cents
        fees = max(
            _fee_cents(config.kalshi_max_taker_fee_cents),
            _fee_cents(reviewed.kalshi_max_taker_fee_cents),
        ) + max(
            _fee_cents(config.polymarket_max_taker_fee_cents),
            _fee_cents(reviewed.polymarket_max_taker_fee_cents),
        )
        slippage = config.kalshi_slippage_cents + config.polymarket_slippage_cents
        net_edge = gross_gap - fees - slippage
        depth = min(kalshi_leg.available_contracts, polymarket_leg.available_contracts)
        if depth < config.minimum_contracts or net_edge <= 0:
            continue
        results.append(
            Opportunity(
                opportunity_id=_opportunity_id(reviewed.record_id, kalshi_leg, polymarket_leg),
                equivalence_id=reviewed.record_id,
                market=kalshi.title,
                settlement_summary=reviewed.settlement_summary,
                validation_reason=reason,
                leg_a={
                    "venue": "Kalshi",
                    "market_id": kalshi.market_id,
                    "outcome": kalshi_leg.outcome.upper(),
                    "ask_cents": kalshi_leg.ask_cents,
                    "available_contracts": kalshi_leg.available_contracts,
                    "url": kalshi.url,
                },
                leg_b={
                    "venue": "Polymarket US",
                    "market_id": polymarket.market_id,
                    "outcome": polymarket_leg.outcome.upper(),
                    "ask_cents": polymarket_leg.ask_cents,
                    "available_contracts": polymarket_leg.available_contracts,
                    "url": polymarket.url,
                },
                available_contracts=depth,
                gross_gap_cents=gross_gap,
                estimated_fees_cents=fees,
                estimated_slippage_cents=slippage,
                net_edge_cents=net_edge,
                net_edge_pct=round(float(net_edge), 2),
                observed_at=current.isoformat(),
            )
        )
    return results


def pair_safety_status(
    reviewed: ReviewedEquivalence,
    kalshi: BinaryMarket,
    polymarket: BinaryMarket,
    config: ScannerConfig,
    now: datetime,
) -> tuple[bool, str]:
    valid, reason = reviewed.validates(kalshi, polymarket)
    if not valid:
        return False, reason
    if not reviewed.fees_are_current(now):
        return False, "pair-specific fee review is stale or future-dated"
    minimum_start = now + timedelta(hours=config.minimum_event_horizon_hours)
    maximum_start = now + timedelta(hours=config.maximum_event_horizon_hours)
    for candidate in (kalshi, polymarket):
        event_start = parse_timestamp(candidate.event_start_at)
        if event_start is None or not minimum_start <= event_start <= maximum_start:
            return False, "event start is outside the configured short-duration horizon"
        if not candidate.is_fresh(config.max_source_age_seconds, now):
            return False, f"{candidate.venue} quote timestamp is missing, stale, or future-dated"
        if not all(
            quote.is_executable(config.minimum_contracts)
            for quote in (candidate.yes, candidate.no)
        ):
            return False, f"{candidate.venue} has a missing price or insufficient quoted depth"
    return True, reason


class ReadOnlyArbitrageScanner:
    """Coordinates adapters and exposes dashboard-safe, non-executable data."""

    def __init__(
        self,
        *,
        kalshi_source: Any,
        polymarket_source: Any,
        registry_path: Path,
        state: DurableOpportunityState,
        config: ScannerConfig,
    ):
        self.kalshi_source = kalshi_source
        self.polymarket_source = polymarket_source
        self.registry_path = Path(registry_path)
        self.state = state
        self.config = config
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] = {
            "mode": "alert_only_read_only",
            "status": "starting",
            "stand_down_reason": "Waiting for first scan",
            "last_scan_at": None,
            "opportunities": [],
            "new_alerts": [],
            "sources": {},
            "reviewed_matches": 0,
        }

    def scan_once(self) -> dict[str, Any]:
        """Scan only when both sources are available; uncertainty always stands down."""
        now = utc_now()
        registry_error: ValidationError | None = None
        try:
            reviewed = load_reviewed_equivalences(self.registry_path)
        except ValidationError as exc:
            registry_error = exc
            reviewed = []
        kalshi_ids = {record.kalshi_market_id for record in reviewed}
        polymarket_ids = {record.polymarket_market_id for record in reviewed}
        kalshi = self.kalshi_source.fetch_markets(kalshi_ids)
        polymarket = self.polymarket_source.fetch_markets(polymarket_ids)
        sources = {
            "kalshi": self.kalshi_source.health(),
            "polymarket_us": self.polymarket_source.health(),
        }
        base = {
            "mode": "alert_only_read_only",
            "last_scan_at": now.isoformat(),
            "sources": sources,
            "opportunities": [],
            "new_alerts": [],
            "reviewed_matches": 0,
        }
        if not sources["kalshi"].get("ready"):
            base.update(status="stand_down", stand_down_reason="Kalshi source is unavailable or stale")
            return self._publish(base)
        if not sources["polymarket_us"].get("ready"):
            base.update(
                status="stand_down",
                stand_down_reason=(
                    sources["polymarket_us"].get("reason")
                    or "Polymarket US API access or eligibility has not been confirmed"
                ),
            )
            return self._publish(base)

        if registry_error is not None:
            base.update(
                status="stand_down",
                stand_down_reason=f"Reviewed equivalence registry rejected: {registry_error}",
            )
            return self._publish(base)
        base["reviewed_matches"] = len(reviewed)
        if not reviewed:
            base.update(
                status="stand_down",
                stand_down_reason="No human-reviewed cross-platform equivalence records are configured",
            )
            return self._publish(base)
        if not self.config.cost_model_ready():
            base.update(
                status="stand_down",
                stand_down_reason=(
                    "Venue fee schedules are not explicitly configured and verified; "
                    "unknown fees are never treated as zero"
                ),
            )
            return self._publish(base)
        if not self.config.event_horizon_ready():
            base.update(
                status="stand_down",
                stand_down_reason="Short-duration event horizon configuration is invalid",
            )
            return self._publish(base)

        kalshi_by_id = {market.market_id: market for market in kalshi}
        polymarket_by_id = {market.market_id: market for market in polymarket}
        opportunities: list[Opportunity] = []
        missing_reviewed_markets: list[str] = []
        rejected_reviewed_markets: list[str] = []
        for record in reviewed:
            left = kalshi_by_id.get(record.kalshi_market_id)
            right = polymarket_by_id.get(record.polymarket_market_id)
            if left is None or right is None:
                missing_reviewed_markets.append(record.record_id)
                continue
            safe, safety_reason = pair_safety_status(record, left, right, self.config, now)
            if not safe:
                rejected_reviewed_markets.append(f"{record.record_id}: {safety_reason}")
                continue
            opportunities.extend(calculate_opportunities(record, left, right, self.config, now))
        if missing_reviewed_markets:
            base.update(
                status="stand_down",
                stand_down_reason=(
                    "One or more reviewed pairs lack complete current public quotes, "
                    "depth, settlement metadata, or provider timestamps"
                ),
            )
            return self._publish(base)
        if rejected_reviewed_markets:
            base.update(
                status="stand_down",
                stand_down_reason=(
                    "One or more reviewed pairs failed a current safety gate: "
                    + "; ".join(rejected_reviewed_markets)
                ),
            )
            return self._publish(base)

        opportunities.sort(key=lambda opportunity: opportunity.net_edge_cents, reverse=True)
        current_alerts = [
            opportunity.as_dict()
            for opportunity in opportunities
            if self.state.reserve_alert(opportunity.opportunity_id, now, self.config.dedup_minutes)
        ]
        base.update(
            status="ready",
            stand_down_reason=None,
            opportunities=[opportunity.as_dict() for opportunity in opportunities],
            new_alerts=current_alerts,
        )
        return self._publish(base)

    def _publish(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._snapshot = snapshot
            return json.loads(json.dumps(snapshot))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._snapshot))

    def fail_closed(self, reason: str) -> dict[str, Any]:
        """Immediately withdraw every opportunity after an unexpected scan failure."""
        current = self.snapshot()
        return self._publish(
            {
                "mode": "alert_only_read_only",
                "status": "stand_down",
                "stand_down_reason": reason,
                "last_scan_at": utc_now().isoformat(),
                "opportunities": [],
                "new_alerts": [],
                "sources": current.get("sources", {}),
                "reviewed_matches": current.get("reviewed_matches", 0),
            }
        )