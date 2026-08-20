"""
Smart Trader — RSI + EMA + MACD momentum strategy with dynamic sizing.

Entry modes:
  1. Dip buy    — RSI < 52, uptrend (EMA9 > EMA21), volume >= 0.05x avg
                  (was RSI < 60 — tightened to require a real dip)
  2. Breakout   — MACD crosses up, RSI < 65, volume >= 0.15x avg (was 0.08x)
  3. Recovery   — price within 5% below EMA200, MACD crossing up, RSI < 40

Exit modes:
  - Take profit  : dynamic (2–8% based on recent volatility; high-vol → wider TP)
  - Stop loss    : dynamic (0.8–2.5% based on volatility; tight in calm markets)
  - Trailing stop: dynamic from peak (activates after 1× ATR gain)
  - Overbought   : RSI > 70 + MACD turning bearish
  - MACD exit    : MACD crosses down while in profit (> +0.5%)

Confidence score (0.0–1.0):
  Combines RSI depth, MACD strength, trend alignment, and volume.
  Used by the trader to scale position size — more conviction = larger bet.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
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


def _volatility_pct(closes: list[float], period: int = 20) -> float:
    """
    Std dev of last `period` close-to-close % returns.
    Returns 0.5 as a safe default if insufficient data.
    E.g. BTC on 5m bars ≈ 0.3–0.8%; SOL ≈ 0.5–1.2%.
    """
    if len(closes) < period + 1:
        return 0.5
    tail = closes[-(period + 1):]
    rets = [(tail[i] - tail[i - 1]) / tail[i - 1] * 100 for i in range(1, len(tail))]
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(variance)


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

    # Dynamic risk parameters (computed from recent volatility)
    vol_pct:        float = 0.5   # std dev of 20 close-to-close % returns
    dynamic_tp:     float = 3.0   # take-profit % (vol-scaled)
    dynamic_sl:     float = 1.2   # stop-loss % (vol-scaled)
    dynamic_trail:  float = 1.5   # trailing-stop % from peak (vol-scaled)
    dynamic_trigger: float = 1.2  # trailing-stop activation % (vol-scaled)
    confidence:     float = 0.0   # 0.0–1.0 signal confidence

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


def _compute_confidence(snap: "Snapshot") -> float:
    """
    0.0–1.0 signal confidence score.
    Combines RSI depth, MACD strength, trend alignment, and volume.
    """
    score = 0.0

    # RSI component — deeper oversold = stronger mean-reversion signal
    if snap.rsi < 25:
        score += 0.35
    elif snap.rsi < 35:
        score += 0.28
    elif snap.rsi < 45:
        score += 0.18
    elif snap.rsi < 52:
        score += 0.08

    # MACD component
    if snap.macd_crossing_up:
        score += 0.30
    elif snap.macd_bullish:
        score += 0.12

    # Trend alignment
    if snap.uptrend and snap.long_term_bull:
        score += 0.22
    elif snap.uptrend:
        score += 0.12
    elif snap.long_term_bull:
        score += 0.06

    # Volume confirmation
    if snap.vol_ratio >= 2.0:
        score += 0.13
    elif snap.vol_ratio >= 1.0:
        score += 0.08
    elif snap.vol_ratio >= 0.3:
        score += 0.03

    return min(1.0, score)


def analyze(closes: list[float], volumes: list[float] | None = None) -> Snapshot:
    if len(closes) < 40:
        p = closes[-1] if closes else 0.0
        return Snapshot(p, 50.0, p, p, p, 0.0, 0.0, 1.0)

    hist, hist_prev = _macd_hist(closes)
    ema200 = _ema(closes, min(200, len(closes)))[-1]

    vol_ratio = 1.0
    if volumes and len(volumes) >= 20:
        avg_vol   = sum(volumes[-22:-2]) / 20
        vol_ratio = volumes[-2] / avg_vol if avg_vol > 0 else 1.0

    # Compute recent volatility for dynamic TP/SL
    vol_pct = _volatility_pct(closes, 20)

    # Dynamic TP/SL: scale with volatility
    # Low vol (0.2%): TP=2.0%, SL=0.8%  |  High vol (1.5%): TP=7.5%, SL=2.5%
    dynamic_tp      = max(2.0, min(8.0,  vol_pct * 3.0))
    dynamic_sl      = max(0.8, min(2.5,  vol_pct * 1.5))
    dynamic_trail   = max(1.2, min(3.5,  vol_pct * 2.0))
    dynamic_trigger = max(1.0, min(3.0,  vol_pct * 1.8))

    snap = Snapshot(
        price=closes[-1],
        rsi=_rsi(closes, 14)[-1],
        ema_fast=_ema(closes, 9)[-1],
        ema_slow=_ema(closes, 21)[-1],
        ema_200=ema200,
        macd_hist=hist,
        macd_hist_prev=hist_prev,
        vol_ratio=vol_ratio,
        vol_pct=round(vol_pct, 3),
        dynamic_tp=round(dynamic_tp, 2),
        dynamic_sl=round(dynamic_sl, 2),
        dynamic_trail=round(dynamic_trail, 2),
        dynamic_trigger=round(dynamic_trigger, 2),
    )
    snap.confidence = round(_compute_confidence(snap), 3)
    return snap


# ── Signal logic ───────────────────────────────────────────────────────────────

def get_signal(
    snap:              Snapshot,
    in_position:       bool,
    entry_price:       float | None,
    high_watermark:    float = 0.0,
    # These default to snap's dynamic values; can be overridden for backtesting
    take_profit_pct:   float | None = None,
    stop_loss_pct:     float | None = None,
    trail_pct:         float | None = None,
    trail_trigger_pct: float | None = None,
) -> tuple[Signal, str]:

    tp   = take_profit_pct   if take_profit_pct   is not None else snap.dynamic_tp
    sl   = stop_loss_pct     if stop_loss_pct      is not None else snap.dynamic_sl
    trl  = trail_pct         if trail_pct          is not None else snap.dynamic_trail
    trg  = trail_trigger_pct if trail_trigger_pct  is not None else snap.dynamic_trigger

    # ── EXIT logic ────────────────────────────────────────────────────────────
    if in_position and entry_price:
        pnl = (snap.price - entry_price) / entry_price * 100

        if pnl >= tp:
            return "sell", f"Take profit: +{pnl:.2f}% (target={tp:.1f}%)"

        if pnl <= -sl:
            return "sell", f"Stop loss: {pnl:.2f}% (limit={sl:.1f}%)"

        if high_watermark > entry_price * (1 + trg / 100):
            trail_floor = high_watermark * (1 - trl / 100)
            if snap.price < trail_floor:
                peak_pnl = (high_watermark - entry_price) / entry_price * 100
                return "sell", f"Trailing stop: peak +{peak_pnl:.1f}%, floor ${trail_floor:.4f}"

        if snap.macd_crossing_down and pnl > 0.5:
            return "sell", f"MACD turned bearish — locking in +{pnl:.2f}%"

        if snap.rsi > 70 and not snap.macd_bullish:
            return "sell", f"Overbought RSI={snap.rsi:.1f} + MACD bearish (P&L={pnl:+.2f}%)"

        return "hold", (
            f"Holding P&L={pnl:+.2f}% | RSI={snap.rsi:.1f} | {snap.regime} | "
            f"TP={tp:.1f}% SL={sl:.1f}%"
        )

    # ── ENTRY logic ───────────────────────────────────────────────────────────
    if not in_position:
        near_ema200 = snap.price >= snap.ema_200 * 0.95  # within 5% below EMA200

        # Hard block: genuine downtrend, don't buy
        if not near_ema200:
            return "hold", (
                f"EMA200 filter: price >5% below trend (EMA200={snap.ema_200:.4f}) | {snap.regime}"
            )

        # Mode 3: Recovery bottom — RSI oversold, MACD just flipped, near EMA200
        if not snap.long_term_bull and snap.macd_crossing_up and snap.rsi < 40:
            return "buy", (
                f"Recovery bottom: RSI={snap.rsi:.1f} oversold | MACD crossover near EMA200 | "
                f"vol={snap.vol_ratio:.2f}x | conf={snap.confidence:.2f} | "
                f"TP={tp:.1f}% SL={sl:.1f}%"
            )

        # Still in soft zone but no reversal — wait
        if not snap.long_term_bull:
            return "hold", (
                f"Near EMA200, waiting for reversal (RSI={snap.rsi:.1f}, "
                f"MACD={'↑' if snap.macd_bullish else '↓'}) | {snap.regime}"
            )

        # Mode 1: Dip buy — tightened RSI < 52 (was 60), requires genuine dip
        # MACD not deep negative means momentum isn't collapsing
        if (
            snap.rsi < 52
            and snap.uptrend
            and snap.macd_hist > -0.3
            and snap.vol_ratio >= 0.05
        ):
            strength = "strong" if snap.rsi < 35 else ("moderate" if snap.rsi < 45 else "mild")
            return "buy", (
                f"Dip buy ({strength}): RSI={snap.rsi:.1f} | uptrend | "
                f"vol={snap.vol_ratio:.2f}x | conf={snap.confidence:.2f} | "
                f"TP={tp:.1f}% SL={sl:.1f}%"
            )

        # Mode 2: Momentum breakout — MACD just flipped, higher volume required
        if snap.macd_crossing_up and snap.rsi < 65 and snap.vol_ratio >= 0.15:
            trend = "uptrend" if snap.uptrend else "neutral trend"
            return "buy", (
                f"Breakout: MACD crossover | RSI={snap.rsi:.1f} | {trend} | "
                f"vol={snap.vol_ratio:.2f}x | conf={snap.confidence:.2f} | "
                f"TP={tp:.1f}% SL={sl:.1f}%"
            )

        # No entry — explain blocker
        blocks = []
        if snap.vol_ratio < 0.05:
            blocks.append(f"low vol {snap.vol_ratio:.2f}x")
        if snap.rsi >= 52 and not snap.macd_crossing_up:
            blocks.append(f"RSI={snap.rsi:.1f} (need <52 or MACD cross)")
        if snap.rsi >= 65 and snap.macd_crossing_up:
            blocks.append(f"RSI={snap.rsi:.1f} overbought for breakout")
        if snap.vol_ratio < 0.15 and snap.macd_crossing_up:
            blocks.append(f"low vol {snap.vol_ratio:.2f}x for breakout (need 0.15x)")
        if not snap.uptrend and not snap.macd_crossing_up:
            blocks.append("no uptrend/crossover")
        return "hold", f"Waiting: {', '.join(blocks) or 'conditions not aligned'} | {snap.regime}"

    return "hold", "Default hold"
