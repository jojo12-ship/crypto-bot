"""
Smart Trader — RSI + EMA + MACD momentum strategy.

Entry modes:
  1. Dip buy    — RSI < 45, uptrend (EMA9 > EMA21), volume >= 0.3x avg
  2. Breakout   — MACD crosses up, RSI < 58, volume >= 0.4x avg

Exit modes:
  - Take profit  : +3%
  - Stop loss    : -2%
  - Trailing stop: 1.5% from peak (activates after +1.5% gain)
  - Overbought   : RSI > 68 + MACD turning bearish
  - MACD exit    : MACD crosses down while in profit (> +0.5%)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("strategy")

Signal = Literal["buy", "sell", "hold"]


# ── Indicators ─────────────────────────────────────────────────────────────────

def _ema(prices: list[float], period: int) -> list[float]:
    if len(prices) < period:
        return [prices[-1]] * len(prices) if prices else []
    k = 2.0 / (period + 1)
    seed = sum(prices[:period]) / period
    result = [0.0] * (period - 1) + [seed]
    for price in prices[period:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def _rsi(prices: list[float], period: int = 14) -> list[float]:
    if len(prices) < period + 1:
        return [50.0] * len(prices)
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    rsi_vals: list[float] = [50.0] * (period + 1)
    for i in range(period, len(deltas)):
        if avg_l == 0:
            rsi_vals.append(100.0)
        else:
            rsi_vals.append(100 - 100 / (1 + avg_g / avg_l))
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return rsi_vals


def _macd_hist(closes: list[float], fast_p=12, slow_p=26, sig_p=9) -> tuple[float, float]:
    """Return (histogram_current, histogram_prev)."""
    if len(closes) < slow_p + sig_p + 2:
        return 0.0, 0.0
    fast = _ema(closes, fast_p)
    slow = _ema(closes, slow_p)
    macd_line = [fast[i] - slow[i] for i in range(slow_p - 1, len(closes))]
    if len(macd_line) < sig_p + 1:
        return 0.0, 0.0
    sig_line = _ema(macd_line, sig_p)
    curr = macd_line[-1] - sig_line[-1]
    prev = macd_line[-2] - sig_line[-2]
    return curr, prev


# ── Snapshot ───────────────────────────────────────────────────────────────────

@dataclass
class Snapshot:
    price:          float
    rsi:            float
    ema_fast:       float   # EMA(9)
    ema_slow:       float   # EMA(21)
    ema_200:        float   # EMA(200) long-term trend
    macd_hist:      float
    macd_hist_prev: float
    vol_ratio:      float   # current vol / 20-bar avg

    @property
    def uptrend(self) -> bool:
        return self.ema_fast > self.ema_slow

    @property
    def long_term_bull(self) -> bool:
        return self.price > self.ema_200

    @property
    def macd_bullish(self) -> bool:
        return self.macd_hist > 0

    @property
    def macd_crossing_up(self) -> bool:
        return self.macd_hist_prev <= 0 < self.macd_hist

    @property
    def macd_crossing_down(self) -> bool:
        return self.macd_hist_prev >= 0 > self.macd_hist

    @property
    def regime(self) -> str:
        if self.uptrend and self.macd_bullish:
            return "bullish"
        if not self.uptrend and not self.macd_bullish:
            return "bearish"
        return "neutral"


def analyze(closes: list[float], volumes: list[float] | None = None) -> Snapshot:
    if len(closes) < 40:
        p = closes[-1] if closes else 0.0
        return Snapshot(p, 50.0, p, p, p, 0.0, 0.0, 1.0)

    hist, hist_prev = _macd_hist(closes)
    ema200 = _ema(closes, min(200, len(closes)))[-1]

    vol_ratio = 1.0
    if volumes and len(volumes) >= 20:
        avg_vol   = sum(volumes[-21:-1]) / 20
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    return Snapshot(
        price=closes[-1],
        rsi=_rsi(closes, 14)[-1],
        ema_fast=_ema(closes, 9)[-1],
        ema_slow=_ema(closes, 21)[-1],
        ema_200=ema200,
        macd_hist=hist,
        macd_hist_prev=hist_prev,
        vol_ratio=vol_ratio,
    )


# ── Signal logic ───────────────────────────────────────────────────────────────

def get_signal(
    snap:              Snapshot,
    in_position:       bool,
    entry_price:       float | None,
    high_watermark:    float = 0.0,
    take_profit_pct:   float = 3.0,
    stop_loss_pct:     float = 2.0,
    trail_pct:         float = 1.5,
    trail_trigger_pct: float = 1.5,
) -> tuple[Signal, str]:

    # ── EXIT logic ────────────────────────────────────────────────────────────
    if in_position and entry_price:
        pnl = (snap.price - entry_price) / entry_price * 100

        if pnl >= take_profit_pct:
            return "sell", f"Take profit: +{pnl:.2f}%"

        if pnl <= -stop_loss_pct:
            return "sell", f"Stop loss: {pnl:.2f}%"

        if high_watermark > entry_price * (1 + trail_trigger_pct / 100):
            trail_floor = high_watermark * (1 - trail_pct / 100)
            if snap.price < trail_floor:
                peak_pnl = (high_watermark - entry_price) / entry_price * 100
                return "sell", f"Trailing stop: peak +{peak_pnl:.1f}%, floor ${trail_floor:.4f}"

        if snap.macd_crossing_down and pnl > 0.5:
            return "sell", f"MACD turned bearish — locking in +{pnl:.2f}%"

        if snap.rsi > 68 and not snap.macd_bullish:
            return "sell", f"Overbought RSI={snap.rsi:.1f} + MACD bearish (P&L={pnl:+.2f}%)"

        return "hold", f"Holding P&L={pnl:+.2f}% | RSI={snap.rsi:.1f} | {snap.regime}"

    # ── ENTRY logic ───────────────────────────────────────────────────────────
    if not in_position:
        # Mode 1: Dip buy — RSI cooling, uptrend intact, some volume
        if snap.rsi < 45 and snap.uptrend and snap.macd_hist > -0.5 and snap.vol_ratio >= 0.3:
            strength = "strong" if snap.rsi < 35 else "moderate"
            return "buy", (
                f"Dip buy ({strength}): RSI={snap.rsi:.1f} | uptrend | vol={snap.vol_ratio:.2f}x"
            )

        # Mode 2: Momentum breakout — MACD just flipped bullish, RSI not yet overheated
        if snap.macd_crossing_up and snap.rsi < 58 and snap.vol_ratio >= 0.4:
            trend = "uptrend" if snap.uptrend else "neutral trend"
            return "buy", (
                f"Breakout: MACD crossover | RSI={snap.rsi:.1f} | {trend} | vol={snap.vol_ratio:.2f}x"
            )

        # No entry — explain what's blocking
        blocks = []
        if snap.vol_ratio < 0.3:
            blocks.append(f"low vol {snap.vol_ratio:.2f}x")
        if snap.rsi >= 45 and not snap.macd_crossing_up:
            blocks.append(f"RSI={snap.rsi:.1f}")
        if not snap.uptrend and not snap.macd_crossing_up:
            blocks.append("no uptrend")
        return "hold", f"Waiting: {', '.join(blocks) or 'conditions not aligned'} | {snap.regime}"

    return "hold", "Default hold"
