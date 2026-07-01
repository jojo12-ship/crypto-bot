"""
Crypto Trading Bot — multi-pair entry point.

Required secrets:
  BINANCE_API_KEY     — Binance.US API key
  BINANCE_API_SECRET  — Binance.US secret key
  CRYPTO_BOT_TOKEN    — Telegram bot token for trade alerts (optional)

Optional env:
  CRYPTO_SYMBOLS      — comma-separated pairs (default: SOLUSDT)
  CRYPTO_BUDGET_USDT  — total budget split across pairs (default: 100)
  CRYPTO_INTERVAL     — kline interval (default: 15m)
  CRYPTO_SCAN_SECS    — seconds between scans (default: 300)
  PORT                — dashboard server port (default: 8004)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")

API_KEY    = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
SYMBOLS    = [s.strip().upper() for s in os.getenv("CRYPTO_SYMBOLS", "SOLUSDT").split(",") if s.strip()]
TOTAL_BUDGET = float(os.getenv("CRYPTO_BUDGET_USDT", "100"))
INTERVAL   = os.getenv("CRYPTO_INTERVAL", "15m")
SCAN_SECS  = int(os.getenv("CRYPTO_SCAN_SECS", "300"))
PORT       = int(os.getenv("PORT", "8004"))

if not API_KEY or not API_SECRET:
    logger.error("BINANCE_API_KEY and BINANCE_API_SECRET must be set.")
    sys.exit(1)

BUDGET_PER_PAIR = TOTAL_BUDGET / max(len(SYMBOLS), 1)

# ── Per-pair state ─────────────────────────────────────────────────────────────

@dataclass
class PairState:
    symbol: str
    status: str = "Initializing…"
    signal: str = "hold"
    price: float = 0.0
    rsi: float = 50.0
    macd_hist: float = 0.0
    uptrend: bool = False
    vol_ratio: float = 1.0
    in_position: bool = False
    entry_price: float = 0.0
    unrealized_pct: float = 0.0
    daily_pnl: float = 0.0
    paused: bool = False
    last_scan: str = ""

_pair_states: dict[str, PairState] = {s: PairState(symbol=s) for s in SYMBOLS}
_traders: dict[str, object] = {}
_state_lock = threading.Lock()

# ── Telegram ───────────────────────────────────────────────────────────────────

import notify

def _fmt_all_status() -> str:
    lines = ["📊 <b>Crypto Bot Status</b>"]
    with _state_lock:
        for sym, st in _pair_states.items():
            emoji = "📈" if st.in_position else "⏳"
            lines.append(
                f"\n{emoji} <b>{sym}</b>\n"
                f"  Price: ${st.price:,.4f} | RSI={st.rsi:.1f}\n"
                f"  {'🟢 IN POSITION' if st.in_position else '— watching'}"
                + (f" P&L={st.unrealized_pct:+.2f}%" if st.in_position else "")
                + f"\n  Daily P&L: ${st.daily_pnl:+.2f}"
                + (f"\n  ⏸ PAUSED" if st.paused else "")
            )
    return "\n".join(lines)

def _pause_all() -> str:
    with _state_lock:
        for st in _pair_states.values():
            st.paused = True
    return "⏸ All pairs paused."

def _resume_all() -> str:
    with _state_lock:
        for st in _pair_states.values():
            st.paused = False
    return "▶️ All pairs resumed."

def _poll_loop():
    while True:
        try:
            notify.poll_commands(
                on_status=_fmt_all_status,
                on_pause=_pause_all,
                on_resume=_resume_all,
            )
        except Exception as e:
            logger.debug(f"Poll error: {e}")
        time.sleep(2)

threading.Thread(target=_poll_loop, daemon=True).start()

# ── Trading loop (one per pair) ────────────────────────────────────────────────

import strategy
from trader import CryptoTrader

DAILY_LOSS_LIMIT_PCT = 0.10   # pause pair at -10% of its budget

def _trade_pair(symbol: str):
    st = _pair_states[symbol]
    MAX_BACKOFF = 60
    attempt = 0

    while True:
        attempt += 1
        try:
            trader = CryptoTrader(API_KEY, API_SECRET, symbol=symbol, budget_usdt=BUDGET_PER_PAIR)
            with _state_lock:
                _traders[symbol] = trader

            notify.broadcast(
                f"🤖 <b>Crypto Bot — {symbol}</b> started\n"
                f"Budget: ${BUDGET_PER_PAIR:.0f} | Interval: {INTERVAL} | Scan: {SCAN_SECS}s"
            )
            daily_loss_limit = BUDGET_PER_PAIR * DAILY_LOSS_LIMIT_PCT

            while True:
                with _state_lock:
                    paused = st.paused
                    daily_pnl = st.daily_pnl

                if paused:
                    st.status = "⏸ Paused"
                    time.sleep(30)
                    continue

                if daily_pnl <= -daily_loss_limit:
                    msg = f"⛔ {symbol} daily loss limit hit (${daily_pnl:.2f}). Pausing 1h."
                    logger.warning(msg)
                    notify.broadcast(msg)
                    st.status = msg
                    time.sleep(3600)
                    with _state_lock:
                        st.daily_pnl = 0.0
                    continue

                # ── Fetch candles + analyze ──────────────────────────────────
                closes, volumes = trader.get_klines(INTERVAL, limit=120)
                snap = strategy.analyze(closes, volumes)

                in_pos = trader.position is not None
                entry = trader.position.entry_price if in_pos else None
                hwm   = trader.position.high_watermark if in_pos else 0.0

                # Update trailing high watermark
                if in_pos and snap.price > hwm:
                    trader.position.high_watermark = snap.price
                    trader._save_position()
                    hwm = snap.price

                sig, reason = strategy.get_signal(
                    snap, in_pos, entry,
                    high_watermark=hwm,
                )

                # Update shared state
                with _state_lock:
                    st.price    = snap.price
                    st.rsi      = snap.rsi
                    st.macd_hist = snap.macd_hist
                    st.uptrend  = snap.uptrend
                    st.vol_ratio = snap.vol_ratio
                    st.signal   = sig
                    st.in_position = in_pos
                    st.entry_price = entry or 0.0
                    st.unrealized_pct = trader.pnl_pct(snap.price) if in_pos else 0.0
                    st.last_scan = datetime.now(timezone.utc).isoformat()
                    st.status = f"{'📈 IN POSITION' if in_pos else '⏳ Watching'} | {reason}"

                logger.info(f"[{symbol}] ${snap.price:.4f} RSI={snap.rsi:.1f} "
                            f"MACD={snap.macd_hist:+.3f} uptrend={snap.uptrend} "
                            f"vol={snap.vol_ratio:.2f}x signal={sig} | {reason}")

                # ── Execute signal ───────────────────────────────────────────
                if sig == "buy":
                    result = trader.buy()
                    msg = (
                        f"🟢 <b>BUY {symbol}</b>\n"
                        f"Price: ${result['price']:,.4f}\n"
                        f"Qty: {result['qty']} {trader.base}\n"
                        f"Spent: ${result['value']:.2f}\n"
                        f"Reason: {reason}\n"
                        f"🎯 TP: +3% | SL: -2% | Trail: -1.5% from peak"
                    )
                    notify.broadcast(msg)

                elif sig == "sell":
                    result = trader.sell()
                    pnl, pnl_pct = result["pnl"], result["pnl_pct"]
                    with _state_lock:
                        st.daily_pnl += pnl
                    emoji = "💰" if pnl >= 0 else "🔴"
                    msg = (
                        f"{emoji} <b>SELL {symbol}</b>\n"
                        f"Entry: ${result['entry']:,.4f} → Exit: ${result['price']:,.4f}\n"
                        f"P&amp;L: ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
                        f"Reason: {reason}\n"
                        f"Daily P&amp;L: ${st.daily_pnl:+.2f}"
                    )
                    notify.broadcast(msg)

                time.sleep(SCAN_SECS)

        except Exception as exc:
            backoff = min(MAX_BACKOFF, 10 * attempt)
            logger.error(f"[{symbol}] Loop crashed: {exc}. Restarting in {backoff}s…")
            notify.broadcast(f"⚠️ {symbol} bot crashed: {exc}\nRestarting in {backoff}s…")
            time.sleep(backoff)


# Start one thread per pair
for sym in SYMBOLS:
    t = threading.Thread(target=_trade_pair, args=(sym,), daemon=True, name=f"trade-{sym}")
    t.start()
    logger.info(f"Started trading thread for {sym}")

# ── FastAPI dashboard ─────────────────────────────────────────────────────────

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI(title="Crypto Trading Bot", docs_url=None, redoc_url=None)

_STATIC = Path(__file__).parent / "static"

@app.get("/crypto")
@app.get("/crypto/")
async def dashboard():
    return FileResponse(_STATIC / "index.html")

@app.get("/crypto/api/status")
async def api_status():
    with _state_lock:
        pairs = {sym: asdict(st) for sym, st in _pair_states.items()}

    # Aggregate stats
    total_daily_pnl = sum(st["daily_pnl"] for st in pairs.values())
    positions_open  = sum(1 for st in pairs.values() if st["in_position"])

    # Load all trades for summary
    all_trades: list[dict] = []
    for sym in SYMBOLS:
        f = Path(f"crypto_trades_{sym}.json")
        if f.exists():
            try:
                trades = json.loads(f.read_text())
                all_trades.extend(trades)
            except Exception:
                pass
    all_trades.sort(key=lambda t: t.get("ts", ""), reverse=True)

    sells = [t for t in all_trades if t.get("action") == "sell"]
    total_realized = sum(t.get("pnl", 0) for t in all_trades)
    wins = sum(1 for t in sells if t.get("pnl", 0) > 0)

    return JSONResponse({
        "pairs": pairs,
        "aggregate": {
            "total_daily_pnl": round(total_daily_pnl, 4),
            "positions_open": positions_open,
            "total_realized_pnl": round(total_realized, 4),
            "total_trades": len(all_trades),
            "win_rate": f"{wins / len(sells) * 100:.0f}%" if sells else "N/A",
            "symbols": SYMBOLS,
            "interval": INTERVAL,
            "scan_secs": SCAN_SECS,
            "budget_per_pair": BUDGET_PER_PAIR,
        },
        "recent_trades": all_trades[:50],
    })

@app.get("/crypto/api/pause/{symbol}")
async def pause_pair(symbol: str):
    sym = symbol.upper()
    if sym not in _pair_states:
        return JSONResponse({"error": "Unknown symbol"}, status_code=404)
    with _state_lock:
        _pair_states[sym].paused = True
    return {"symbol": sym, "paused": True}

@app.get("/crypto/api/resume/{symbol}")
async def resume_pair(symbol: str):
    sym = symbol.upper()
    if sym not in _pair_states:
        return JSONResponse({"error": "Unknown symbol"}, status_code=404)
    with _state_lock:
        _pair_states[sym].paused = False
    return {"symbol": sym, "paused": False}

@app.get("/crypto/health")
async def health():
    return {"ok": True, "symbols": SYMBOLS}

def _run_server():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")

threading.Thread(target=_run_server, daemon=True).start()
logger.info(f"Dashboard on port {PORT} at /crypto")

def _keep_alive():
    import urllib.request
    domain = os.getenv("REPLIT_DEV_DOMAIN", "")
    if not domain:
        return
    urls = [
        f"https://{domain}/crypto/health",
        f"https://{domain}/kalshi/health",
    ]
    while True:
        time.sleep(120)  # every 2 minutes
        for url in urls:
            try:
                urllib.request.urlopen(url, timeout=10)
            except Exception:
                pass

threading.Thread(target=_keep_alive, daemon=True).start()
logger.info("Keep-alive pinger started")

# Keep main thread alive
while True:
    time.sleep(60)
