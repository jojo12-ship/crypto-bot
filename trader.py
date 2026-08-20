"""
Binance.US trading client — market buy/sell with position persistence.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trader")

_DATA_DIR = Path(
    os.getenv("CRYPTO_STATE_DIR", str(Path(__file__).parent))
).expanduser().resolve()


class StateValidationError(RuntimeError):
    pass


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _validated_position_payload(payload: object, symbol: str) -> dict:
    if payload == {}:
        return {}
    if not isinstance(payload, dict):
        raise StateValidationError("Position state must be a JSON object.")
    required = {"symbol", "qty", "entry_price", "entry_value"}
    if not required.issubset(payload):
        raise StateValidationError("Position state is missing required fields.")
    if str(payload["symbol"]).upper() != symbol:
        raise StateValidationError("Position state belongs to a different symbol.")
    for field_name in ("qty", "entry_price", "entry_value"):
        try:
            value = float(payload[field_name])
        except (TypeError, ValueError) as exc:
            raise StateValidationError(
                f"Position field {field_name} is not numeric."
            ) from exc
        if value <= 0:
            raise StateValidationError(
                f"Position field {field_name} must be positive."
            )
    return payload


def _validated_trade_payload(payload: object) -> list[dict]:
    if not isinstance(payload, list) or any(
        not isinstance(item, dict) for item in payload
    ):
        raise StateValidationError("Trade history must be a list of objects.")
    required = {"ts", "action", "symbol", "price", "qty", "value", "pnl"}
    for index, item in enumerate(payload):
        if not required.issubset(item):
            raise StateValidationError(
                f"Trade record {index} is missing required fields."
            )
        if not isinstance(item["ts"], str) or not item["ts"].strip():
            raise StateValidationError(
                f"Trade record {index} has an invalid timestamp."
            )
        if item["action"] not in {"buy", "sell"}:
            raise StateValidationError(
                f"Trade record {index} has an invalid action."
            )
        if not isinstance(item["symbol"], str) or not item["symbol"].strip():
            raise StateValidationError(
                f"Trade record {index} has an invalid symbol."
            )
        for field_name in ("price", "qty", "value", "pnl"):
            try:
                value = float(item[field_name])
            except (TypeError, ValueError) as exc:
                raise StateValidationError(
                    f"Trade record {index} field {field_name} is not numeric."
                ) from exc
            if field_name != "pnl" and value <= 0:
                raise StateValidationError(
                    f"Trade record {index} field {field_name} must be positive."
                )
    return payload


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    entry_value: float
    high_watermark: float = 0.0   # for trailing stop — updated each scan


class CryptoTrader:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbol: str = "SOLUSDT",
        budget_usdt: float = 100.0,
        recover_unmatched_position: bool = False,
    ):
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.symbol = symbol.upper()
        self.base = self.symbol.replace("USDT", "")
        self.budget_usdt = budget_usdt

        # Per-symbol storage files
        self._pos_file = _DATA_DIR / f"crypto_position_{self.symbol}.json"
        self._trades_file = _DATA_DIR / f"crypto_trades_{self.symbol}.json"
        self._migrate_legacy()

        self.position: Optional[Position] = self._load_position()
        self._load_trades()
        from binance.client import Client
        self.client = Client(api_key, api_secret, tld="us")
        self._filters = self._fetch_filters()
        self.recovered_on_startup = False
        if self.position is None and recover_unmatched_position:
            self.position = self._recover_unmatched_position()
            self.recovered_on_startup = self.position is not None
        logger.info(f"Trader ready: {self.symbol} | budget=${budget_usdt}")
        if self.position:
            logger.info(
                f"Restored position: {self.position.qty} {self.base} "
                f"@ ${self.position.entry_price:.4f} | HWM=${self.position.high_watermark:.4f}"
            )

    # ── Migration ─────────────────────────────────────────────────────────────

    def _migrate_legacy(self) -> None:
        """Copy validated legacy state into the configured state directory."""
        if self.symbol == "SOLUSDT":
            migrations = (
                ("crypto_position.json", self._pos_file),
                ("crypto_trades.json", self._trades_file),
            )
            for filename, new in migrations:
                if new.exists():
                    continue
                candidates = (Path.cwd() / filename, Path(__file__).parent / filename)
                for legacy in candidates:
                    if legacy.resolve() == new.resolve() or not legacy.exists():
                        continue
                    try:
                        payload = json.loads(legacy.read_text())
                    except Exception as exc:
                        raise StateValidationError(
                            f"Could not read legacy state {legacy}."
                        ) from exc
                    if filename == "crypto_position.json":
                        payload = _validated_position_payload(payload, self.symbol)
                    else:
                        payload = _validated_trade_payload(payload)
                    _atomic_write_json(new, payload)
                    logger.info("Copied legacy state %s → %s", legacy, new)
                    break

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_position(self) -> Optional[Position]:
        if not self._pos_file.exists():
            return None
        try:
            payload = json.loads(self._pos_file.read_text())
        except Exception as exc:
            raise StateValidationError(
                f"Could not read position state {self._pos_file}."
            ) from exc
        data = _validated_position_payload(payload, self.symbol)
        if not data:
            return None
        return Position(
            **{
                key: data[key]
                for key in Position.__dataclass_fields__
                if key in data
            }
        )

    def _save_position(self) -> None:
        _atomic_write_json(
            self._pos_file,
            asdict(self.position) if self.position else {},
        )

    def _recover_unmatched_position(self) -> Optional[Position]:
        """Recover buys after the latest sell before active workers may start."""
        try:
            raw_trades = self.client.get_my_trades(
                symbol=self.symbol,
                limit=1000,
            )
        except Exception as exc:
            raise StateValidationError(
                f"Could not read Binance trade history for {self.symbol}."
            ) from exc
        if not isinstance(raw_trades, list):
            raise StateValidationError(
                f"Binance trade history for {self.symbol} is malformed."
            )

        parsed: list[tuple[int, bool, float, float]] = []
        for index, trade in enumerate(raw_trades):
            if not isinstance(trade, dict) or not isinstance(
                trade.get("isBuyer"), bool
            ):
                raise StateValidationError(
                    f"Binance trade {index} for {self.symbol} is malformed."
                )
            try:
                timestamp = int(trade["time"])
                qty = float(trade["qty"])
                quote_qty = float(trade["quoteQty"])
                commission = float(trade.get("commission", 0))
            except (KeyError, TypeError, ValueError) as exc:
                raise StateValidationError(
                    f"Binance trade {index} for {self.symbol} is incomplete."
                ) from exc
            if (
                timestamp <= 0
                or not math.isfinite(qty)
                or not math.isfinite(quote_qty)
                or not math.isfinite(commission)
                or qty <= 0
                or quote_qty <= 0
                or commission < 0
            ):
                raise StateValidationError(
                    f"Binance trade {index} for {self.symbol} has invalid values."
                )
            commission_asset = str(trade.get("commissionAsset") or "").upper()
            net_qty = (
                qty - commission
                if trade["isBuyer"] and commission_asset == self.base
                else qty
            )
            net_quote_qty = (
                quote_qty + commission
                if trade["isBuyer"] and commission_asset == "USDT"
                else quote_qty
            )
            if net_qty <= 0 or net_quote_qty <= 0:
                raise StateValidationError(
                    f"Binance trade {index} for {self.symbol} has invalid "
                    "post-commission values."
                )
            parsed.append(
                (timestamp, trade["isBuyer"], net_qty, net_quote_qty)
            )

        parsed.sort(key=lambda item: item[0])
        last_sell_index = max(
            (index for index, item in enumerate(parsed) if not item[1]),
            default=-1,
        )
        if len(parsed) == 1000 and last_sell_index == -1:
            raise StateValidationError(
                f"Binance trade history for {self.symbol} is truncated before "
                "the latest sell; recovery is ambiguous."
            )
        unmatched_buys = [
            item for item in parsed[last_sell_index + 1:] if item[1]
        ]
        if not unmatched_buys:
            logger.info("No unmatched Binance buys to recover for %s", self.symbol)
            return None

        bought_qty = sum(item[2] for item in unmatched_buys)
        bought_value = sum(item[3] for item in unmatched_buys)
        if bought_qty <= 0 or bought_value <= 0:
            raise StateValidationError(
                f"Recovered Binance totals for {self.symbol} are invalid."
            )
        _, actual_base = self.get_balances()
        recoverable_qty = min(bought_qty, actual_base)
        if recoverable_qty <= 0:
            raise StateValidationError(
                f"Binance shows unmatched buys for {self.symbol}, but no "
                "recoverable base balance."
            )

        entry_price = bought_value / bought_qty
        entry_value = entry_price * recoverable_qty
        current_price = self.get_price()
        if (
            not math.isfinite(entry_price)
            or not math.isfinite(entry_value)
            or not math.isfinite(current_price)
            or entry_price <= 0
            or entry_value <= 0
            or current_price <= 0
        ):
            raise StateValidationError(
                f"Recovered Binance position for {self.symbol} is invalid."
            )

        self.position = Position(
            symbol=self.symbol,
            qty=recoverable_qty,
            entry_price=entry_price,
            entry_value=entry_value,
            high_watermark=max(entry_price, current_price),
        )
        self._save_position()
        self._record_trade(
            "buy",
            entry_price,
            recoverable_qty,
            entry_value,
            reason="Recovered unmatched Binance fills during Railway migration",
        )
        logger.warning(
            "Recovered unmatched Binance position for %s and persisted it "
            "before worker activation",
            self.symbol,
        )
        return self.position

    def _record_trade(self, action: str, price: float, qty: float, value: float,
                      pnl: float = 0.0, confidence: float = 0.0,
                      tp_pct: float = 0.0, sl_pct: float = 0.0, reason: str = "") -> None:
        trades = []
        if self._trades_file.exists():
            try:
                trades = _validated_trade_payload(
                    json.loads(self._trades_file.read_text())
                )
            except Exception as exc:
                raise StateValidationError(
                    f"Could not read trade history {self._trades_file}."
                ) from exc
        from datetime import datetime, timezone
        trades.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "symbol": self.symbol,
            "price": round(price, 6),
            "qty": qty,
            "value": round(value, 4),
            "pnl": round(pnl, 4),
            "confidence": round(confidence, 3),
            "tp_pct": round(tp_pct, 2),
            "sl_pct": round(sl_pct, 2),
            "reason": reason,
        })
        _atomic_write_json(self._trades_file, trades[-500:])

    # ── Exchange helpers ──────────────────────────────────────────────────────

    def _fetch_filters(self) -> dict:
        try:
            info = self.client.get_symbol_info(self.symbol) or {}
            return {f["filterType"]: f for f in info.get("filters", [])}
        except Exception as e:
            logger.warning(f"Could not fetch filters for {self.symbol}: {e}")
            return {}

    def _round_qty(self, qty: float) -> float:
        step = float(self._filters.get("LOT_SIZE", {}).get("stepSize", "0.001"))
        decimals = max(0, -int(math.floor(math.log10(step)))) if step > 0 else 3
        return math.floor(qty * 10**decimals) / 10**decimals

    def _min_qty(self) -> float:
        return float(self._filters.get("LOT_SIZE", {}).get("minQty", "0.001"))

    def _min_notional(self) -> float:
        return float(self._filters.get("MIN_NOTIONAL", {}).get("minNotional", "10.0"))

    # ── Market data ───────────────────────────────────────────────────────────

    def get_price(self) -> float:
        return float(self.client.get_symbol_ticker(symbol=self.symbol)["price"])

    def get_klines(self, interval: str = "15m", limit: int = 100) -> tuple[list[float], list[float]]:
        """Returns (closes, volumes)."""
        klines = self.client.get_klines(symbol=self.symbol, interval=interval, limit=limit)
        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        return closes, volumes

    def get_balances(self) -> tuple[float, float]:
        account = self.client.get_account()
        usdt = base = 0.0
        for b in account["balances"]:
            if b["asset"] == "USDT":
                usdt = float(b["free"])
            elif b["asset"] == self.base:
                base = float(b["free"])
        return usdt, base

    def get_summary(self) -> dict:
        price = self.get_price()
        usdt, base = self.get_balances()
        trades = self._load_trades()
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        sells = [t for t in trades if t.get("action") == "sell"]
        wins = sum(1 for t in sells if t.get("pnl", 0) > 0)
        return {
            "symbol": self.symbol,
            "price": price,
            "usdt_balance": usdt,
            "base_balance": base,
            "position": asdict(self.position) if self.position else None,
            "unrealized_pnl_pct": self.position.pnl_pct(price) if self.position else 0.0,
            "total_realized_pnl": total_pnl,
            "total_trades": len(trades),
            "win_rate": f"{wins / len(sells) * 100:.0f}%" if sells else "N/A",
            "recent_trades": trades[-20:],
        }

    def _load_trades(self) -> list[dict]:
        if not self._trades_file.exists():
            return []
        try:
            return _validated_trade_payload(
                json.loads(self._trades_file.read_text())
            )
        except Exception as exc:
            raise StateValidationError(
                f"Could not read trade history {self._trades_file}."
            ) from exc

    # ── Order execution ───────────────────────────────────────────────────────

    def buy(self, usdt_amount: float | None = None, confidence: float = 0.5) -> dict:
        usdt_bal, _ = self.get_balances()
        if usdt_amount is None:
            # Dynamic position sizing: scale between 30% and 95% of budget by confidence.
            # Low confidence (0.0) → 30% | High confidence (1.0) → 95%
            # This means weak signals get small positions; strong setups get full capital.
            scale = 0.30 + 0.65 * min(1.0, max(0.0, confidence))
            usdt_amount = min(self.budget_usdt * scale, usdt_bal * 0.95)
            logger.info(f"[{self.symbol}] Sizing: confidence={confidence:.2f} → scale={scale:.0%} → ${usdt_amount:.2f}")

        min_notional = self._min_notional()
        if usdt_amount < min_notional:
            raise ValueError(f"Amount ${usdt_amount:.2f} below exchange minimum (${min_notional})")

        order = self.client.order_market_buy(symbol=self.symbol, quoteOrderQty=round(usdt_amount, 2))
        qty = float(order["executedQty"])
        value = float(order["cummulativeQuoteQty"])
        avg_price = value / qty if qty else 0

        self.position = Position(
            symbol=self.symbol,
            qty=qty,
            entry_price=avg_price,
            entry_value=value,
            high_watermark=avg_price,
        )
        self._save_position()
        self._record_trade("buy", avg_price, qty, value, confidence=confidence)
        logger.info(f"BUY {qty} {self.base} @ ${avg_price:.4f} (${value:.2f}) conf={confidence:.2f}")
        return {"qty": qty, "price": avg_price, "value": value, "confidence": confidence}

    def sell(self) -> dict:
        if not self.position:
            raise ValueError("No open position")

        # Use the actual exchange balance — fees/slippage make stored qty slightly higher
        _, actual_base = self.get_balances()
        safe_qty = min(self.position.qty, actual_base)
        sell_qty = self._round_qty(safe_qty)
        if sell_qty < self._min_qty():
            # Nothing sellable — clear stale position and bail
            logger.warning(
                f"Sell qty {sell_qty} below min {self._min_qty()} "
                f"(stored={self.position.qty}, actual={actual_base:.6f}). "
                f"Clearing stale position."
            )
            self.position = None
            self._save_position()
            raise ValueError(f"No sellable balance for {self.symbol} — position cleared")

        order = self.client.order_market_sell(symbol=self.symbol, quantity=sell_qty)
        qty = float(order["executedQty"])
        value = float(order["cummulativeQuoteQty"])
        avg_price = value / qty if qty else 0
        pnl = value - self.position.entry_value
        pnl_pct = pnl / self.position.entry_value * 100
        entry = self.position.entry_price

        self._record_trade("sell", avg_price, qty, value, pnl)
        self.position = None
        self._save_position()
        logger.info(f"SELL {qty} {self.base} @ ${avg_price:.4f} | P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
        return {"qty": qty, "price": avg_price, "value": value, "pnl": pnl, "pnl_pct": pnl_pct, "entry": entry}

    # Expose for strategy callers
    def pnl_pct(self, price: float) -> float:
        if not self.position:
            return 0.0
        return (price - self.position.entry_price) / self.position.entry_price * 100
