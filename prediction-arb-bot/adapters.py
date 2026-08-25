"""Public, read-only market-data adapters for the prediction arbitrage scanner."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
import math
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives.asymmetric import ed25519

from scanner import BinaryMarket, Quote, canonical_text


UTC = timezone.utc


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _is_exact_https_url(url: str, host: str, path: str | None = None) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    return parsed.path == (path or "")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def _raw_cents(value: Any, *, dollars: bool = False) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if dollars:
        numeric *= Decimal(100)
    elif Decimal(0) < numeric < Decimal(1):
        numeric *= Decimal(100)
    return numeric if numeric.is_finite() and Decimal(0) < numeric < Decimal(100) else None


def _ask_cents(value: Any, *, dollars: bool = False) -> int | None:
    numeric = _raw_cents(value, dollars=dollars)
    if numeric is None:
        return None
    cents = int(numeric.to_integral_value(rounding=ROUND_CEILING))
    return cents if 0 < cents < 100 else None


def _complement_ask_cents(value: Any, *, dollars: bool = False) -> int | None:
    numeric = _raw_cents(value, dollars=dollars)
    if numeric is None:
        return None
    bid_cents = int(numeric.to_integral_value(rounding=ROUND_FLOOR))
    ask_cents = 100 - bid_cents
    return ask_cents if 0 < ask_cents < 100 else None


def _depth(value: Any) -> int | None:
    try:
        depth = int(float(value))
    except (TypeError, ValueError):
        return None
    return depth if depth >= 0 else None


class PublicJsonClient:
    def __init__(self) -> None:
        self._opener = build_opener(_RejectRedirects())

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        target = f"{url}?{urlencode(params)}" if params else url
        request_headers = {"Accept": "application/json", "User-Agent": "TradeForge-read-only-scanner/1.0"}
        if headers:
            request_headers.update(headers)
        request = Request(target, headers=request_headers)
        try:
            with self._opener.open(request, timeout=12) as response:  # noqa: S310 -- allowlisted HTTPS origins
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"public market-data request failed: {exc}") from exc

    def get_status(self, url: str, headers: dict[str, str]) -> int:
        """Return only the response status; deliberately never read the body."""
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "TradeForge-read-only-scanner/1.0",
            **headers,
        }
        request = Request(url, headers=request_headers)
        try:
            with self._opener.open(request, timeout=12) as response:  # noqa: S310 -- exact official HTTPS URL
                return int(response.status)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"authenticated access probe failed: {exc}") from exc


class _Source:
    def __init__(self) -> None:
        self._health: dict[str, Any] = {
            "ready": False,
            "status": "starting",
            "last_attempt_at": None,
            "last_success_at": None,
            "reason": "Waiting for first source check",
            "market_count": 0,
        }

    def health(self) -> dict[str, Any]:
        return dict(self._health)

    def _success(self, count: int, usable_count: int) -> None:
        timestamp = _now()
        self._health = {
            "ready": True,
            "status": "ready",
            "last_attempt_at": timestamp,
            "last_success_at": timestamp,
            "reason": None,
            "market_count": count,
            "usable_market_count": usable_count,
        }

    def _failed(self, reason: str, status: str = "unavailable") -> None:
        self._health = {
            "ready": False,
            "status": status,
            "last_attempt_at": _now(),
            "last_success_at": self._health.get("last_success_at"),
            "reason": reason,
            "market_count": 0,
            "usable_market_count": 0,
        }


class KalshiPublicSource(_Source):
    """Uses only Kalshi's public catalog and event endpoints; it never authenticates."""

    def __init__(self, client: PublicJsonClient | None = None, base_url: str | None = None):
        super().__init__()
        self.client = client or PublicJsonClient()
        self.base_url = (base_url or os.getenv("KALSHI_PUBLIC_API_BASE_URL") or "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

    def fetch_markets(self, market_ids: set[str] | None = None) -> list[BinaryMarket]:
        try:
            payload = self.client.get(
                f"{self.base_url}/markets",
                {"status": "open", "limit": "1000", "mve_filter": "exclude"},
            )
            raw_markets = payload.get("markets", []) if isinstance(payload, dict) else []
            if not isinstance(raw_markets, list):
                raise RuntimeError("Kalshi returned an unexpected market list")
            if not raw_markets:
                raise RuntimeError("Kalshi returned no open markets")
            requested = market_ids if market_ids is not None else {
                str(_first(raw, "ticker", "id"))
                for raw in raw_markets
                if isinstance(raw, dict) and _first(raw, "ticker", "id")
            }
            selected = [
                raw
                for raw in raw_markets
                if isinstance(raw, dict)
                and str(_first(raw, "ticker", "id")) in requested
            ]
            selected_ids = {
                str(_first(raw, "ticker", "id"))
                for raw in selected
                if _first(raw, "ticker", "id")
            }
            for missing_id in sorted(requested - selected_ids):
                detail = self.client.get(
                    f"{self.base_url}/markets/{urlquote(missing_id, safe='')}"
                )
                raw = detail.get("market") if isinstance(detail, dict) else None
                if isinstance(raw, dict):
                    selected.append(raw)
            events: dict[str, dict[str, Any]] = {}
            for event_ticker in {
                str(raw.get("event_ticker"))
                for raw in selected
                if isinstance(raw.get("event_ticker"), str) and raw["event_ticker"].strip()
            }:
                event_payload = self.client.get(
                    f"{self.base_url}/events/{urlquote(event_ticker, safe='')}"
                )
                if not isinstance(event_payload, dict) or not isinstance(
                    event_payload.get("event"), dict
                ):
                    raise RuntimeError(f"Kalshi event metadata is unavailable for {event_ticker}")
                events[event_ticker] = event_payload
            markets = [
                self._to_market(raw, events.get(str(raw.get("event_ticker"))))
                for raw in selected
            ]
            markets = [market for market in markets if market is not None]
            if market_ids and not markets:
                raise RuntimeError(
                    "Kalshi returned no markets with complete settlement authority, cutoff, "
                    "provider timestamp, executable quotes, and depth"
                )
            self._success(len(raw_markets), len(markets))
            return markets
        except Exception as exc:
            self._failed(str(exc))
            return []

    @staticmethod
    def _to_market(
        raw: dict[str, Any],
        event_payload: dict[str, Any] | None = None,
    ) -> BinaryMarket | None:
        market_id = _first(raw, "ticker", "id")
        event = event_payload.get("event") if isinstance(event_payload, dict) else None
        event_markets = event_payload.get("markets") if isinstance(event_payload, dict) else None
        event_market = None
        if isinstance(event_markets, list):
            event_market = next(
                (
                    candidate
                    for candidate in event_markets
                    if isinstance(candidate, dict)
                    and str(candidate.get("ticker")) == str(market_id)
                ),
                None,
            )
        rules = _first(event_market or {}, "rules_primary", "rules")
        if rules is None:
            rules = _first(raw, "rules_primary", "rules")
        settlement_sources = event.get("settlement_sources") if isinstance(event, dict) else None
        if isinstance(settlement_sources, list):
            source_names = [
                str(source.get("name")).strip()
                for source in settlement_sources
                if isinstance(source, dict)
                and isinstance(source.get("name"), str)
                and source["name"].strip()
            ]
            resolution = "; ".join(source_names) if source_names else None
        else:
            resolution = None
        cutoff = _first(raw, "close_time", "expiration_time", "expected_expiration_time")
        event_start = _first(
            raw,
            "occurrence_datetime",
        )
        quote_updated_at = _first(raw, "quote_updated_at", "last_updated_at", "updated_time", "updatedAt")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (market_id, rules, resolution, cutoff, event_start)
        ):
            return None
        yes_ask = (
            _ask_cents(raw.get("yes_ask_dollars"), dollars=True)
            if raw.get("yes_ask_dollars") not in (None, "")
            else _ask_cents(_first(raw, "yes_ask", "yes_ask_cents"))
        )
        no_ask = (
            _ask_cents(raw.get("no_ask_dollars"), dollars=True)
            if raw.get("no_ask_dollars") not in (None, "")
            else _ask_cents(_first(raw, "no_ask", "no_ask_cents"))
        )
        yes_depth = _depth(
            _first(
                raw,
                "yes_ask_size",
                "yes_ask_size_fp",
            )
        )
        no_depth = _depth(
            _first(
                raw,
                "no_ask_size",
                "no_ask_size_fp",
                # A NO ask is the complement of the best YES bid.
                "yes_bid_size_fp",
            )
        )
        if None in (yes_ask, no_ask, yes_depth, no_depth):
            return None
        return BinaryMarket(
            venue="kalshi",
            market_id=str(market_id),
            title=str(_first(raw, "title", "subtitle") or market_id),
            rules_text=str(rules),
            resolution_source=str(resolution),
            cutoff_at=str(cutoff),
            event_start_at=str(event_start),
            yes=Quote(
                "yes",
                yes_ask,
                yes_depth,
            ),
            no=Quote(
                "no",
                no_ask,
                no_depth,
            ),
            # Never relabel an upstream quote as fresh using local receipt time.
            # Missing provider timestamps remain empty and fail the core freshness gate.
            fetched_at=str(quote_updated_at or ""),
            url=(
                "https://api.elections.kalshi.com/trade-api/v2/markets/"
                + urlquote(str(market_id), safe="")
            ),
        )


class PolymarketUSPublicSource(_Source):
    """Public-data adapter gated by one authenticated retail probe per process.

    Retail API keys do not support the institutional ``/v1/whoami`` and
    ``/v1/accounts`` routes. Runtime readiness performs a signed GET against the
    supported retail balance route, checks only HTTP 200 without reading the
    response body, and then uses the unauthenticated public gateway.
    """

    PUBLIC_HOST = "gateway.polymarket.us"
    RETAIL_HOST = "api.polymarket.us"
    ACCESS_DOCUMENTATION_URL = "https://docs.polymarket.us/api-reference/authentication"
    ACCESS_VERIFICATION_URL = "https://api.polymarket.us/v1/account/balances"

    def __init__(
        self,
        client: PublicJsonClient | None = None,
        base_url: str | None = None,
        key_id: str | None = None,
        secret_key: str | None = None,
    ):
        super().__init__()
        self.client = client or PublicJsonClient()
        self.base_url = (
            base_url
            if base_url is not None
            else os.getenv("POLYMARKET_US_PUBLIC_API_BASE_URL", "https://gateway.polymarket.us")
        ).rstrip("/")
        self.key_id = (key_id if key_id is not None else os.getenv("POLYMARKET_US_KEY_ID", "")).strip()
        self.secret_key = (
            secret_key if secret_key is not None else os.getenv("POLYMARKET_US_SECRET_KEY", "")
        ).strip()
        self._access_verified = False

    def health(self) -> dict[str, Any]:
        health = super().health()
        health["authorization"] = {
            "ready": self._access_verified,
            "status": "verified" if self._access_verified else "unverified",
        }
        return health

    def _credential_private_key(self) -> ed25519.Ed25519PrivateKey:
        if not self.key_id or not self.secret_key:
            raise RuntimeError("Polymarket US retail API credentials are required")
        try:
            raw_key = base64.b64decode(self.secret_key, validate=True)
            if len(raw_key) not in {32, 64}:
                raise ValueError("expected a 32-byte Ed25519 seed or 64-byte private key")
            return ed25519.Ed25519PrivateKey.from_private_bytes(raw_key[:32])
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Polymarket US retail secret key is malformed") from exc

    def _auth_headers(self) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        path = "/v1/account/balances"
        message = f"{timestamp}GET{path}".encode("utf-8")
        signature = self._credential_private_key().sign(message)
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": base64.b64encode(signature).decode("ascii"),
        }

    def _verify_retail_access(self) -> None:
        if self._access_verified:
            return
        if not _is_exact_https_url(
            self.ACCESS_VERIFICATION_URL,
            self.RETAIL_HOST,
            "/v1/account/balances",
        ):
            raise RuntimeError("Polymarket US retail access probe must use the exact official endpoint")
        status = self.client.get_status(
            self.ACCESS_VERIFICATION_URL,
            self._auth_headers(),
        )
        if status != 200:
            raise RuntimeError(
                f"Polymarket US retail access probe returned HTTP {status}, expected 200"
            )
        self._access_verified = True

    def fetch_markets(self, market_ids: set[str] | None = None) -> list[BinaryMarket]:
        if not _is_exact_https_url(self.base_url, self.PUBLIC_HOST):
            self._failed(
                "POLYMARKET_US_PUBLIC_API_BASE_URL must be the official HTTPS public market-data host",
                "stand_down",
            )
            return []
        try:
            self._verify_retail_access()
            payload = self.client.get(
                f"{self.base_url}/v1/markets",
                {"active": "true", "closed": "false", "limit": "500"},
            )
            raw_markets = payload.get("markets", []) if isinstance(payload, dict) else []
            if not isinstance(raw_markets, list):
                raise RuntimeError("Polymarket US endpoint returned an unexpected market list")
            if not raw_markets:
                raise RuntimeError("Polymarket US returned no open markets")
            requested = market_ids if market_ids is not None else {
                str(_first(raw, "id", "condition_id", "conditionId"))
                for raw in raw_markets
                if isinstance(raw, dict) and _first(raw, "id", "condition_id", "conditionId")
            }
            selected = [
                raw
                for raw in raw_markets
                if isinstance(raw, dict)
                and str(_first(raw, "id", "condition_id", "conditionId")) in requested
            ]
            selected_ids = {
                str(_first(raw, "id", "condition_id", "conditionId"))
                for raw in selected
                if _first(raw, "id", "condition_id", "conditionId")
            }
            for missing_id in sorted(requested - selected_ids):
                detail = self.client.get(
                    f"{self.base_url}/v1/market/id/{urlquote(missing_id, safe='')}"
                )
                raw = detail.get("market") if isinstance(detail, dict) else None
                if isinstance(raw, dict):
                    selected.append(raw)
            markets: list[BinaryMarket | None] = []
            for raw in selected:
                slug = raw.get("slug")
                if not isinstance(slug, str) or not slug.strip():
                    markets.append(None)
                    continue
                book = self.client.get(
                    f"{self.base_url}/v1/markets/{urlquote(slug, safe='')}/book"
                )
                markets.append(self._to_market(raw, book))
            markets = [market for market in markets if market is not None]
            if market_ids and not markets:
                raise RuntimeError(
                    "Polymarket US returned no requested markets with complete rules, fees, "
                    "provider timestamps, executable quotes, and depth"
                )
            self._success(len(raw_markets), len(markets))
            return markets
        except Exception as exc:
            self._failed(str(exc))
            return []

    @staticmethod
    def _to_market(
        raw: dict[str, Any],
        book_payload: dict[str, Any] | None = None,
    ) -> BinaryMarket | None:
        sides = raw.get("marketSides")
        if (
            raw.get("active") is not True
            or raw.get("closed") is not False
            or raw.get("archived") is not False
            or raw.get("hidden") is not False
            or raw.get("status") not in {"MARKET_STATUS_OPEN", "open", "active"}
            or not isinstance(sides, list)
            or len(sides) != 2
            or {side.get("long") for side in sides if isinstance(side, dict)} != {True, False}
            or not all(isinstance(side, dict) and side.get("tradable") is True for side in sides)
        ):
            return None
        market_id = _first(raw, "condition_id", "conditionId", "id")
        rules = _first(raw, "description", "rules", "question")
        resolution = _first(raw, "resolution_source", "resolutionSource", "resolution", "resolutionSourceName")
        cutoff = _first(raw, "end_date_iso", "endDateIso", "end_date", "endDate")
        event_start = _first(raw, "gameStartTime", "eventStartTime")
        fee_coefficient = raw.get("feeCoefficient")
        try:
            fee_coefficient = float(fee_coefficient)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(fee_coefficient) or fee_coefficient <= 0:
            return None
        if not resolution and isinstance(rules, str):
            source_match = re.search(r"Outcome sourced from ([^.]+)\.?\s*$", rules)
            if source_match:
                resolution = source_match.group(1).strip()
        if not all(
            isinstance(value, str) and value.strip()
            for value in (market_id, rules, resolution, cutoff, event_start)
        ):
            return None
        side_by_long = {
            side.get("long"): side
            for side in sides
            if isinstance(side, dict)
        }
        for long, expected_description in ((True, "yes"), (False, "no")):
            side = side_by_long.get(long)
            quote = side.get("quote") if isinstance(side, dict) else None
            if (
                not isinstance(side, dict)
                or canonical_text(str(side.get("description", ""))) != expected_description
                or not isinstance(quote, dict)
                or quote.get("currency") != "USD"
                or _ask_cents(quote.get("value"), dollars=True) is None
            ):
                return None
        market_data = (
            book_payload.get("marketData") if isinstance(book_payload, dict) else None
        )
        if (
            not isinstance(market_data, dict)
            or market_data.get("marketSlug") != raw.get("slug")
            or market_data.get("state") != "MARKET_STATE_OPEN"
        ):
            return None
        bids = market_data.get("bids")
        offers = market_data.get("offers")
        if not isinstance(bids, list) or not isinstance(offers, list):
            return None

        def level(entry: Any) -> tuple[Decimal, int] | None:
            if not isinstance(entry, dict) or not isinstance(entry.get("px"), dict):
                return None
            if entry["px"].get("currency") != "USD":
                return None
            price = _raw_cents(entry["px"].get("value"), dollars=True)
            depth = _depth(entry.get("qty"))
            if price is None or depth is None or depth <= 0:
                return None
            return price, depth

        bid_levels = [parsed for entry in bids if (parsed := level(entry)) is not None]
        offer_levels = [parsed for entry in offers if (parsed := level(entry)) is not None]
        if not bid_levels or not offer_levels:
            return None
        best_bid, no_depth = max(bid_levels, key=lambda item: item[0])
        best_offer, yes_depth = min(offer_levels, key=lambda item: item[0])
        yes_ask = _ask_cents(best_offer)
        no_ask = _complement_ask_cents(best_bid)
        quote_updated_at = market_data.get("transactTime")
        if None in (yes_ask, no_ask, yes_depth, no_depth):
            return None
        return BinaryMarket(
            venue="polymarket_us",
            market_id=str(market_id),
            title=str(_first(raw, "question", "title") or market_id),
            rules_text=str(rules),
            resolution_source=str(resolution),
            cutoff_at=str(cutoff),
            event_start_at=str(event_start),
            yes=Quote(
                "yes",
                yes_ask,
                yes_depth,
            ),
            no=Quote(
                "no",
                no_ask,
                no_depth,
            ),
            fetched_at=str(quote_updated_at or ""),
            url=(
                f"https://gateway.polymarket.us/v1/markets?"
                + urlencode(
                    {
                        "slug": str(raw.get("slug")),
                        "active": "true",
                        "closed": "false",
                    }
                )
                if isinstance(raw.get("slug"), str) and raw["slug"].strip()
                else ""
            ),
            fee_model="taker_fee_coefficient",
            fee_coefficient=fee_coefficient,
        )