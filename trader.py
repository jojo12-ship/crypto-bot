"""
Binance.US trading client — market buy/sell with position persistence.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trader")

_DATA_DIR = Path(".")


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
    ):
        from binance.client import Client
        self.symbol = symbol.upper()
        self.base = self.symbol.replace("USDT", "")
        self.budget_usdt = budget_usdt
        self.client = Client(api_key, api_secret, tld="us")

        # Per-symbol storage files
        self._pos_file = _DATA_DIR / f"crypto_position_{self.symbol}.json"
        self._trades_file = _DATA_DIR / f"crypto_trades_{self.symbol}.json"
        self._migrate_legacy()

        self.position: Optional[Position] = self._load_position()
        self._filters = self._fetch_filters()
        logger.info(f"Trader ready: {self.symbol} | budget=${budget_usdt}")
        if self.position:
            logger.info(
                f"Restored position: {self.position.qty} {self.base} "
                f"@ ${self.position.entry_price:.4f} | HWM=${self.position.high_watermark:.4f}"
            )

    # ── Migration ─────────────────────────────────────────────────────────────

    def _migrate_legacy(self) -> None:
        """Move legacy single-symbol files to symbol-scoped names."""
        if self.symbol == "SOLUSDT":
            for legacy, new in [
                (Path("crypto_position.json"), self._pos_file),
                (Path("crypto_trades.json"), self._trades_file),
            ]:
                if legacy.exists() and not new.exists():
                    legacy.rename(new)
                    logger.info(f"Migrated {legacy} → {new}")

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_position(self) -> Optional[Position]:
        if not self._pos_file.exists():
            return None
        try:
            d = json.loads(self._pos_file.read_text())
            if d.get("symbol") == self.symbol and d.get("qty", 0) > 0:
                return Position(**{k: d[k] for k in Position.__dataclass_fields__ if k in d})
        except Exception:
            pass
        return None

    def _save_position(self) -> None:
        if self.position:
            self._pos_file.write_text(json.dumps(asdict(self.position)))
        else:
            self._pos_file.write_text("{}")

    def _record_trade(self, action: str, price: float, qty: float, value: float, pnl: float = 0.0) -> None:
        trades = []
        if self._trades_file.exists():
            try:
                trades = json.loads(self._trades_file.read_text())
            except Exception:
                pass
        from datetime import datetime, timezone
        trades.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "symbol": self.symbol,
            "price": round(price, 6),
            "qty": qty,
            "value": round(value, 4),
            "pnl": round(pnl, 4),
        })
        self._trades_file.write_text(json.dumps(trades[-500:]))

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
            return json.loads(self._trades_file.read_text())
        except Exception:
            return []

    # ── Order execution ───────────────────────────────────────────────────────

    def buy(self, usdt_amount: float | None = None) -> dict:
        usdt_bal, _ = self.get_balances()
        if usdt_amount is None:
            usdt_amount = min(self.budget_usdt * 0.95, usdt_bal * 0.95)

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
        self._record_trade("buy", avg_price, qty, value)
        logger.info(f"BUY {qty} {self.base} @ ${avg_price:.4f} (${value:.2f})")
        return {"qty": qty, "price": avg_price, "value": value}

    def sell(self) -> dict:
        if not self.position:
            raise ValueError("No open position")

        sell_qty = self._round_qty(self.position.qty)
        if sell_qty < self._min_qty():
            raise ValueError(f"Qty {sell_qty} below min {self._min_qty()}")

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
