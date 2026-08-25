"""Entry point for the TradeForge Kalshi–Polymarket US alert-only scanner."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse

from adapters import KalshiPublicSource, PolymarketUSPublicSource
from scanner import DurableOpportunityState, ReadOnlyArbitrageScanner, ScannerConfig
from telegram_notifier import TelegramNotifier


ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "8008"))
STATE_DIR = Path(os.getenv("PREDICTION_ARB_STATE_DIR", ROOT / "state"))
REGISTRY_PATH = Path(os.getenv("PREDICTION_ARB_MATCH_REGISTRY", ROOT / "reviewed_matches.json"))
SCAN_SECONDS = max(10, int(os.getenv("PREDICTION_ARB_SCAN_SECONDS", "30")))


def int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def optional_nonnegative_int_env(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
        return value if value >= 0 else None
    except ValueError:
        return None


scanner = ReadOnlyArbitrageScanner(
    kalshi_source=KalshiPublicSource(),
    polymarket_source=PolymarketUSPublicSource(),
    registry_path=REGISTRY_PATH,
    state=DurableOpportunityState(STATE_DIR / "opportunity_dedup.json"),
    config=ScannerConfig(
        max_source_age_seconds=int_env("PREDICTION_ARB_MAX_SOURCE_AGE_SECONDS", 30),
        minimum_contracts=max(1, int_env("PREDICTION_ARB_MINIMUM_CONTRACTS", 1)),
        kalshi_max_taker_fee_cents=optional_nonnegative_int_env("PREDICTION_ARB_KALSHI_MAX_TAKER_FEE_CENTS"),
        polymarket_max_taker_fee_cents=optional_nonnegative_int_env(
            "PREDICTION_ARB_POLYMARKET_MAX_TAKER_FEE_CENTS"
        ),
        kalshi_slippage_cents=max(0, int_env("PREDICTION_ARB_KALSHI_SLIPPAGE_CENTS", 1)),
        polymarket_slippage_cents=max(0, int_env("PREDICTION_ARB_POLYMARKET_SLIPPAGE_CENTS", 1)),
        dedup_minutes=max(1, int_env("PREDICTION_ARB_DEDUP_MINUTES", 60)),
        fees_verified=os.getenv("PREDICTION_ARB_FEES_VERIFIED", "").strip().casefold() == "true",
        minimum_event_horizon_hours=int_env("PREDICTION_ARB_MIN_EVENT_HORIZON_HOURS", 1),
        maximum_event_horizon_hours=int_env("PREDICTION_ARB_MAX_EVENT_HORIZON_HOURS", 72),
    ),
)
telegram_notifier = TelegramNotifier(
    token=os.getenv("CRYPTO_BOT_TOKEN", "").strip(),
    chat_id=os.getenv("PREDICTION_ARB_TELEGRAM_CHAT_ID", "").strip(),
    enabled=os.getenv("PREDICTION_ARB_TELEGRAM_DELIVERY_ENABLED", "").strip().casefold() == "true",
)


def scan_loop() -> None:
    while True:
        try:
            snapshot = scanner.scan_once()
            for opportunity in snapshot["new_alerts"]:
                delivered = telegram_notifier.deliver(opportunity)
                print(
                    "prediction scanner Telegram alert "
                    + ("delivered" if delivered else "not delivered (disabled, unconfigured, or rejected)"),
                    flush=True,
                )
        except Exception as exc:  # scanner remains fail-closed and reports the last safe snapshot
            scanner.fail_closed(f"Unexpected scan failure; all opportunities withdrawn: {exc}")
            print(f"prediction scanner scan failed safely: {exc}", flush=True)
        time.sleep(SCAN_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        snapshot = scanner.snapshot()
        if path in {"/health", "/api/status"}:
            payload = {**snapshot, "telegram": telegram_notifier.status()}
        elif path == "/api/opportunities":
            payload = {
                "mode": snapshot["mode"],
                "status": snapshot["status"],
                "stand_down_reason": snapshot["stand_down_reason"],
                "last_scan_at": snapshot["last_scan_at"],
                "opportunities": snapshot["opportunities"],
                "new_alerts": snapshot["new_alerts"],
            }
        elif path == "/":
            payload = {
                "service": "Kalshi–Polymarket US scanner",
                "mode": "alert_only_read_only",
                "health": "/health",
                "opportunities": "/api/opportunities",
            }
        else:
            self.send_error(404, "Not found")
            return
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


if __name__ == "__main__":
    print(
        f"Starting alert-only prediction arbitrage scanner on {PORT}; "
        "it has no order, position, wallet, or fund-movement capability; "
        "the retail access probe checks only HTTP status and discards the body.",
        flush=True,
    )
    threading.Thread(target=scan_loop, name="prediction-arb-scan", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
