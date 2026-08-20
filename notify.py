"""
Lightweight Telegram notifier — no full bot needed, just POST to sendMessage.
Also handles the /status, /pause, /resume command bot.
"""
from __future__ import annotations

import logging
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

import requests
from runtime_owner import current_runtime_ownership

logger = logging.getLogger("notify")

TOKEN = re.sub(r"\s+", "", os.getenv("CRYPTO_BOT_TOKEN", ""))
_BASE = f"https://api.telegram.org/bot{TOKEN}"

_lock = threading.Lock()
_health_lock = threading.Lock()
_last_poll_success_monotonic: float | None = None
_last_poll_error: str | None = None
_consecutive_poll_failures = 0
_state_dir = Path(
    os.getenv("CRYPTO_STATE_DIR", str(Path(__file__).parent))
).expanduser().resolve()
_state_file = _state_dir / "crypto_notify_state.json"
_delivery_enabled = False


def _load_state() -> tuple[set[int], int]:
    try:
        if not _state_file.exists():
            return set(), 0
        raw = json.loads(_state_file.read_text())
        subscribers = {
            int(chat_id) for chat_id in raw.get("subscribers", []) if chat_id
        }
        offset = max(0, int(raw.get("offset", 0)))
        return subscribers, offset
    except Exception as exc:
        logger.warning("Could not load Telegram subscriber state: %s", exc)
        return set(), 0


def _save_state_locked() -> None:
    try:
        _state_dir.mkdir(parents=True, exist_ok=True)
        temp = _state_file.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "subscribers": sorted(_subscribers),
                    "offset": _offset,
                },
                handle,
                indent=2,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, _state_file)
    except Exception as exc:
        logger.warning("Could not persist Telegram subscriber state: %s", exc)


_subscribers, _offset = _load_state()


def set_delivery_enabled(enabled: bool) -> None:
    global _delivery_enabled
    _delivery_enabled = bool(enabled)


def _can_use_telegram() -> bool:
    return (
        _delivery_enabled
        and current_runtime_ownership().is_designated_service
    )


def _post(method: str, **kwargs) -> dict:
    if not TOKEN:
        return {"ok": False, "description": "Telegram token is not configured"}
    try:
        request_timeout: float | tuple[float, float] = 10
        if method == "getUpdates":
            long_poll_timeout = max(0, int(kwargs.get("timeout", 0)))
            request_timeout = (5, max(10, long_poll_timeout + 10))
        r = requests.post(
            f"{_BASE}/{method}",
            json=kwargs,
            timeout=request_timeout,
        )
        payload = r.json()
        if not isinstance(payload, dict):
            return {"ok": False, "description": "Telegram returned a malformed response"}
        return payload
    except Exception as e:
        logger.warning("Telegram %s request failed: %s", method, e)
        return {
            "ok": False,
            "description": f"Telegram {method} request failed: {type(e).__name__}",
        }


def validate_configuration() -> str:
    """Verify the configured token before active workers are allowed to start."""
    response = _post("getMe")
    if response.get("ok") is not True:
        raise RuntimeError(
            response.get("description") or "Telegram rejected the configured bot token"
        )
    username = response.get("result", {}).get("username")
    if not isinstance(username, str) or not username:
        raise RuntimeError("Telegram getMe response did not include a bot username")
    return username


def _record_poll_success() -> None:
    global _last_poll_success_monotonic, _last_poll_error
    global _consecutive_poll_failures
    with _health_lock:
        _last_poll_success_monotonic = time.monotonic()
        _last_poll_error = None
        _consecutive_poll_failures = 0


def _record_poll_failure(message: str) -> None:
    global _last_poll_error, _consecutive_poll_failures
    with _health_lock:
        _last_poll_error = message
        _consecutive_poll_failures += 1


def telegram_health_snapshot(max_staleness_seconds: float = 60.0) -> dict:
    with _health_lock:
        last_success = _last_poll_success_monotonic
        last_error = _last_poll_error
        failures = _consecutive_poll_failures
    age = None if last_success is None else max(0.0, time.monotonic() - last_success)
    polling_healthy = bool(
        _delivery_enabled
        and age is not None
        and age <= max_staleness_seconds
        and failures == 0
    )
    return {
        "configured": bool(TOKEN),
        "delivery_enabled": _delivery_enabled,
        "polling_healthy": polling_healthy,
        "last_poll_success_age_seconds": None if age is None else round(age, 1),
        "consecutive_poll_failures": failures,
        "last_error": last_error,
    }


def broadcast(text: str) -> int:
    if not TOKEN or not _can_use_telegram():
        return 0
    with _lock:
        subs = set(_subscribers)
    delivered = 0
    for chat_id in subs:
        response = _post(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if response.get("ok") is True:
            delivered += 1
    return delivered


def poll_commands(
    on_status: Callable,
    on_pause: Callable,
    on_resume: Callable,
    *,
    timeout_seconds: int = 20,
) -> bool:
    """Long-poll Telegram for commands in a background thread."""
    global _offset
    if not TOKEN or not _can_use_telegram():
        message = "Telegram polling is disabled or this runtime is not the owner"
        _record_poll_failure(message)
        return False
    try:
        resp = _post(
            "getUpdates",
            offset=_offset,
            timeout=timeout_seconds,
            allowed_updates=["message"],
        )
        if resp.get("ok") is not True:
            message = resp.get("description") or "Telegram getUpdates failed"
            _record_poll_failure(message)
            logger.warning("Telegram polling failed: %s", message)
            return False
        for update in resp.get("result", []):
            _offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text = (msg.get("text") or "").strip().lower()
            if not chat_id:
                continue
            with _lock:
                if text in ("/start", "start"):
                    _subscribers.add(chat_id)
                subscribed = chat_id in _subscribers
                _save_state_locked()
            if not subscribed:
                _post(
                    "sendMessage",
                    chat_id=chat_id,
                    text="Send /start first to subscribe.",
                )
                continue
            if text in ("/start", "start"):
                _post("sendMessage", chat_id=chat_id, parse_mode="HTML",
                      text="🤖 <b>Crypto Trading Bot</b>\n\nYou're registered for trade alerts.\n\n"
                           "/status — current position & P&amp;L")
            elif text in ("/status", "status"):
                _post("sendMessage", chat_id=chat_id, parse_mode="HTML", text=on_status())
            elif text in ("/pause", "pause"):
                _post(
                    "sendMessage",
                    chat_id=chat_id,
                    text="Trading controls are disabled for safety.",
                )
            elif text in ("/resume", "resume"):
                _post(
                    "sendMessage",
                    chat_id=chat_id,
                    text="Trading controls are disabled for safety.",
                )
        _record_poll_success()
        return True
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        _record_poll_failure(message)
        logger.warning("Telegram command polling failed: %s", message)
        return False
