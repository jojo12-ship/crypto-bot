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
  MEME_SCAN_SECONDS   — seconds between Solana new-pool scans (default: 60)
  CRYPTO_STATE_DIR    — persistent state directory (Railway volume preferred)
  PORT                — dashboard server port (default: 8004)
"""
from __future__ import annotations

import json
import logging
import os
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

from runtime_owner import current_runtime_ownership
from singleton_lease import SingletonLease
from state_store import configured_state_dir, position_recovery_marker_matches

RUNTIME_OWNERSHIP = current_runtime_ownership()
DESIGNATED_SERVICE = RUNTIME_OWNERSHIP.is_designated_service
STATE_DIR, STATE_IS_DURABLE = configured_state_dir(
    fallback_dir=Path(__file__).parent
)
os.environ.setdefault("CRYPTO_STATE_DIR", str(STATE_DIR))
os.environ.setdefault("MEME_SCANNER_STATE_DIR", str(STATE_DIR))

API_KEY    = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
TELEGRAM_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "").strip()
SYMBOLS    = [s.strip().upper() for s in os.getenv("CRYPTO_SYMBOLS", "SOLUSDT,BTCUSDT,ETHUSDT").split(",") if s.strip()]
TOTAL_BUDGET = float(os.getenv("CRYPTO_BUDGET_USDT", "100"))
INTERVAL   = os.getenv("CRYPTO_INTERVAL", "5m")
SCAN_SECS  = int(os.getenv("CRYPTO_SCAN_SECS", "60"))
PORT       = int(os.getenv("PORT", "8004"))

if DESIGNATED_SERVICE and (not API_KEY or not API_SECRET or not TELEGRAM_TOKEN):
    raise RuntimeError(
        "The Railway crypto-bot service requires BINANCE_API_KEY, "
        "BINANCE_API_SECRET, and CRYPTO_BOT_TOKEN."
    )
if not DESIGNATED_SERVICE:
    logger.info("Health-only runtime: %s", RUNTIME_OWNERSHIP.reason)

BUDGET_PER_PAIR = TOTAL_BUDGET / max(len(SYMBOLS), 1)
_active_owner_event = threading.Event()
_active_lease: SingletonLease | None = None
_activation_state = (
    "waiting_for_lease" if DESIGNATED_SERVICE else "health-only"
)

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
_POSITION_RECOVERY_MARKER = STATE_DIR / "exchange-position-recovery-v1.json"

# ── Telegram ───────────────────────────────────────────────────────────────────

import notify
from meme_scanner import MemeScanner, ScannerConfig

_meme_scanner = MemeScanner(
    config=ScannerConfig.from_env(),
    alert_callback=notify.broadcast,
    delivery_enabled=False,
)

def _fmt_all_status() -> str:
    lines = [
        "📊 <b>Crypto Bot Status</b>",
        f"Runtime: <b>{RUNTIME_OWNERSHIP.owner}</b>",
    ]
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
    return "\n".join(lines) + _meme_scanner.telegram_status()

def _pause_all() -> str:
    if not _active_owner_event.is_set():
        return "ℹ️ This copy is health-only. Railway owns trading controls."
    with _state_lock:
        for st in _pair_states.values():
            st.paused = True
    return "⏸ All pairs paused."

def _resume_all() -> str:
    if not _active_owner_event.is_set():
        return "ℹ️ This copy is health-only. Railway owns trading controls."
    with _state_lock:
        for st in _pair_states.values():
            st.paused = False
    return "▶️ All pairs resumed."

def _fmt_daily_summary() -> str:
    """Format a combined P&L summary across all pairs for Telegram."""
    with _state_lock:
        pairs_snap = {sym: asdict(st) for sym, st in _pair_states.items()}
    total_pnl  = sum(st["daily_pnl"] for st in pairs_snap.values())
    open_count = sum(1 for st in pairs_snap.values() if st["in_position"])

    # Load all-time stats from trade logs
    all_sells, all_wins = [], 0
    for sym in SYMBOLS:
        f = STATE_DIR / f"crypto_trades_{sym}.json"
        if f.exists():
            try:
                trades = json.loads(f.read_text())
                sells  = [t for t in trades if t.get("action") == "sell"]
                all_sells.extend(sells)
                all_wins += sum(1 for t in sells if t.get("pnl", 0) > 0)
            except Exception:
                pass
    all_time_pnl = sum(t.get("pnl", 0) for t in all_sells)
    wr = f"{all_wins / len(all_sells) * 100:.0f}%" if all_sells else "N/A"

    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    lines = [
        f"📊 <b>Crypto Bot — Daily Summary</b>\n",
        f"{pnl_emoji} Today P&amp;L: <b>${total_pnl:+.2f}</b>",
        f"📈 All-time P&amp;L: ${all_time_pnl:+.2f}  |  Win rate: {wr}",
        f"📂 Total trades: {len(all_sells)}",
        f"🔓 Positions open: {open_count}",
    ]
    for sym, st in pairs_snap.items():
        if st["in_position"]:
            lines.append(f"  ↳ {sym} @ ${st['entry_price']:,.4f}  unrealized={st['unrealized_pct']:+.2f}%")
    return "\n".join(lines)


_last_summary_date: str = ""

def _poll_loop():
    global _last_summary_date
    while True:
        try:
            # Daily summary when UTC date rolls over
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != _last_summary_date and _last_summary_date != "":
                notify.broadcast(_fmt_daily_summary())
            _last_summary_date = today

            notify.poll_commands(
                on_status=_fmt_all_status,
                on_pause=_pause_all,
                on_resume=_resume_all,
            )
        except Exception as e:
            logger.debug(f"Poll error: {e}")
        time.sleep(5)

# ── Trading loop (one per pair) ────────────────────────────────────────────────

import strategy
from trader import CryptoTrader

DAILY_LOSS_LIMIT_PCT = 0.10   # pause pair at -10% of its budget

def _trade_pair(symbol: str):
    st = _pair_states[symbol]
    MAX_BACKOFF = 60
    attempt = 0
    trader = _traders.get(symbol)

    while True:
        attempt += 1
        try:
            if trader is None:
                trader = CryptoTrader(
                    API_KEY,
                    API_SECRET,
                    symbol=symbol,
                    budget_usdt=BUDGET_PER_PAIR,
                    recover_unmatched_position=False,
                )
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
                    try:
                        result = trader.buy(confidence=snap.confidence)
                        msg = (
                            f"🟢 <b>BUY {symbol}</b>\n"
                            f"Price: ${result['price']:,.4f}\n"
                            f"Qty: {result['qty']} {trader.base}\n"
                            f"Spent: ${result['value']:.2f}\n"
                            f"Confidence: {snap.confidence:.0%} → size scaled accordingly\n"
                            f"Reason: {reason}\n"
                            f"🎯 TP: +{snap.dynamic_tp:.1f}% | SL: -{snap.dynamic_sl:.1f}% | "
                            f"Trail: -{snap.dynamic_trail:.1f}% from peak | Vol={snap.vol_pct:.2f}%/bar"
                        )
                        notify.broadcast(msg)
                    except Exception as e:
                        code = getattr(e, "code", None) or getattr(e, "status_code", None)
                        logger.warning(f"[{symbol}] BUY skipped: {e}")
                        if "-2010" in str(e) or code == -2010:
                            notify.broadcast(f"⚠️ {symbol}: not enough USDT to buy — skipping this signal.")
                        # Don't re-raise — just skip this signal and continue scanning

                elif sig == "sell":
                    try:
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
                    except Exception as e:
                        logger.warning(f"[{symbol}] SELL error: {e}")
                        notify.broadcast(f"⚠️ {symbol} SELL failed: {e}")
                        # Don't re-raise — position was already cleared if balance was 0

                time.sleep(SCAN_SECS)

        except Exception as exc:
            backoff = min(MAX_BACKOFF, 10 * attempt)
            logger.error(f"[{symbol}] Loop crashed: {exc}. Restarting in {backoff}s…")
            notify.broadcast(f"⚠️ {symbol} bot crashed: {exc}\nRestarting in {backoff}s…")
            trader = None
            time.sleep(backoff)


def _write_position_recovery_marker() -> None:
    payload = {
        "version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "symbols": sorted(set(SYMBOLS)),
        "purpose": "one-time unmatched Binance fill recovery",
    }
    temp = _POSITION_RECOVERY_MARKER.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, _POSITION_RECOVERY_MARKER)


def _prepare_active_traders() -> None:
    recover_positions = not position_recovery_marker_matches(
        _POSITION_RECOVERY_MARKER,
        SYMBOLS,
    )
    prepared: dict[str, object] = {}
    for symbol in SYMBOLS:
        prepared[symbol] = CryptoTrader(
            API_KEY,
            API_SECRET,
            symbol=symbol,
            budget_usdt=BUDGET_PER_PAIR,
            recover_unmatched_position=recover_positions,
        )
    if recover_positions:
        _write_position_recovery_marker()
        logger.info(
            "Completed one-time Binance position recovery preflight for %s",
            ", ".join(SYMBOLS),
        )
    with _state_lock:
        _traders.update(prepared)


def _start_active_workers() -> None:
    notify.set_delivery_enabled(True)
    _meme_scanner.delivery_enabled = True
    _meme_scanner.start()
    threading.Thread(
        target=_poll_loop,
        daemon=True,
        name="telegram-command-poller",
    ).start()
    logger.info("Railway Telegram command polling started")
    for sym in SYMBOLS:
        t = threading.Thread(
            target=_trade_pair,
            args=(sym,),
            daemon=True,
            name=f"trade-{sym}",
        )
        t.start()
        logger.info(f"Started trading thread for {sym}")


def _ownership_supervisor() -> None:
    global _active_lease, _activation_state
    if not DESIGNATED_SERVICE:
        logger.info("Telegram, scanner delivery, and Binance workers are disabled")
        return

    delay = max(0, int(os.getenv("CRYPTO_OWNER_ACTIVATION_DELAY_SECS", "20")))
    _activation_state = "activation_delay"
    logger.info(
        "Designated Railway service will seek the singleton lease in %ss",
        delay,
    )
    time.sleep(delay)
    lease = SingletonLease(STATE_DIR / "crypto-bot-owner.lock")
    while not lease.try_acquire():
        _activation_state = "waiting_for_lease"
        logger.info("Another Railway instance owns active workers; standing by")
        time.sleep(5)

    _active_lease = lease
    _activation_state = "position_recovery_preflight"
    logger.info("Singleton lease acquired; validating and recovering position state")
    try:
        _prepare_active_traders()
    except Exception:
        _activation_state = "blocked_position_recovery"
        logger.exception(
            "Active workers remain disabled because position recovery failed"
        )
        return
    _active_owner_event.set()
    _activation_state = "active"
    logger.info("Position preflight passed; starting active Railway workers")
    _start_active_workers()


if not DESIGNATED_SERVICE:
    for st in _pair_states.values():
        st.status = "Health-only — Railway owns trading"
else:
    for st in _pair_states.values():
        st.status = "Waiting for Railway ownership lease"

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
        f = STATE_DIR / f"crypto_trades_{sym}.json"
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
            "runtime_owner": RUNTIME_OWNERSHIP.owner,
            "designated_service": DESIGNATED_SERVICE,
            "active_owner": _active_owner_event.is_set(),
            "activation_state": _activation_state,
            "state_durable": STATE_IS_DURABLE,
        },
        "recent_trades": all_trades[:50],
        "meme_scanner": _meme_scanner.snapshot(),
    })

@app.get("/crypto/api/pause/{symbol}")
async def pause_pair(symbol: str):
    return JSONResponse(
        {"error": "Trading controls are available only to the Telegram owner"},
        status_code=403,
    )

@app.get("/crypto/api/resume/{symbol}")
async def resume_pair(symbol: str):
    return JSONResponse(
        {"error": "Trading controls are available only to the Telegram owner"},
        status_code=403,
    )

@app.get("/crypto/health")
async def health():
    meme_status = _meme_scanner.snapshot()
    blocked = _activation_state.startswith("blocked_")
    payload = {
        "ok": not blocked,
        "runtime_owner": RUNTIME_OWNERSHIP.owner,
        "designated_service": DESIGNATED_SERVICE,
        "active_owner": _active_owner_event.is_set(),
        "activation_state": _activation_state,
        "telegram_delivery": _active_owner_event.is_set(),
        "binance_trading": _active_owner_event.is_set(),
        "state_dir": str(STATE_DIR),
        "state_durable": STATE_IS_DURABLE,
        "symbols": SYMBOLS,
        "meme_scanner": {
            "state": meme_status["state"],
            "last_success": meme_status["last_success"],
            "last_error": meme_status["last_error"],
            "persistence_error": meme_status["persistence_error"],
        },
    }
    return JSONResponse(payload, status_code=503 if blocked else 200)

def _run_server():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")

threading.Thread(target=_run_server, daemon=True).start()
logger.info(f"Dashboard on port {PORT} at /crypto")

threading.Thread(
    target=_ownership_supervisor,
    daemon=True,
    name="ownership-supervisor",
).start()

def _keep_alive():
    import urllib.request
    domain = os.getenv("REPLIT_DEV_DOMAIN", "")
    if not domain:
        return
    urls = [
        f"https://{domain}/crypto/health",
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
