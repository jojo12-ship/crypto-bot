"""
Lightweight Telegram notifier — no full bot needed, just POST to sendMessage.
Also handles the /status, /pause, /resume command bot.
"""
from __future__ import annotations

import logging
import html
import json
import os
import queue
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
_status_render_lock = threading.Lock()
_last_poll_success_monotonic: float | None = None
_last_poll_error: str | None = None
_consecutive_poll_failures = 0
_status_render_inflight = False
_state_dir = Path(
    os.getenv("CRYPTO_STATE_DIR", str(Path(__file__).parent))
).expanduser().resolve()
_state_file = _state_dir / "crypto_notify_state.json"
_delivery_enabled = False
_OPERATIONAL_ERROR_INTERVAL_SECONDS = 3600
_STATUS_KEYBOARD = {
    "keyboard": [[{"text": "📊 Status"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}


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


def _load_last_operational_error_at() -> float:
    try:
        if not _state_file.exists():
            return 0.0
        raw = json.loads(_state_file.read_text())
        return max(0.0, float(raw.get("last_operational_error_at", 0.0)))
    except Exception as exc:
        logger.warning("Could not load operational alert state: %s", exc)
        return 0.0


def _save_state_locked() -> None:
    try:
        _state_dir.mkdir(parents=True, exist_ok=True)
        temp = _state_file.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "subscribers": sorted(_subscribers),
                    "offset": _offset,
                    "last_operational_error_at": _last_operational_error_at,
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
_last_operational_error_at = _load_last_operational_error_at()


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


def _plain_text(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def _send_command_reply(
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: dict | None = None,
) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    response = _post("sendMessage", **payload)
    if response.get("ok") is True:
        return True

    description = response.get("description") or "Telegram rejected the command reply"
    logger.warning("Telegram command reply failed: %s", description)
    if not parse_mode:
        return False

    fallback = _post(
        "sendMessage",
        chat_id=chat_id,
        text=_plain_text(text),
        reply_markup=reply_markup,
    )
    if fallback.get("ok") is True:
        logger.warning("Telegram command reply succeeded after plain-text fallback")
        return True
    logger.warning(
        "Telegram plain-text command reply failed: %s",
        fallback.get("description") or "unknown Telegram error",
    )
    return False


def _render_status(on_status: Callable, timeout_seconds: float = 5.0) -> str:
    global _status_render_inflight
    with _status_render_lock:
        if _status_render_inflight:
            logger.error("Telegram status formatter is still busy after a timeout")
            return (
                "⚠️ Crypto Bot status is temporarily unavailable, but command polling "
                "is active. Binance orders remain safety-gated."
            )
        _status_render_inflight = True
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def render() -> None:
        global _status_render_inflight
        try:
            result_queue.put((True, on_status()))
        except Exception as exc:
            result_queue.put((False, exc))
        finally:
            with _status_render_lock:
                _status_render_inflight = False

    threading.Thread(
        target=render,
        daemon=True,
        name="telegram-status-renderer",
    ).start()
    try:
        succeeded, result = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        logger.error("Telegram status formatter timed out")
        return (
            "⚠️ Crypto Bot status is temporarily unavailable, but command polling "
            "is active. Binance orders remain safety-gated."
        )
    if not succeeded:
        logger.error(
            "Telegram status formatter failed: %s",
            result,
            exc_info=(
                type(result),
                result,
                result.__traceback__,
            ) if isinstance(result, BaseException) else None,
        )
        return (
            "⚠️ Crypto Bot status is temporarily unavailable, but command polling "
            "is active. Binance orders remain safety-gated."
        )
    if not isinstance(result, str) or not result.strip():
        logger.error("Telegram status formatter returned an empty response")
        return (
            "⚠️ Crypto Bot status is temporarily unavailable, but command polling "
            "is active. Binance orders remain safety-gated."
        )
    return result


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
            reply_markup=_STATUS_KEYBOARD,
        )
        if response.get("ok") is True:
            delivered += 1
    return delivered


def broadcast_operational_error(text: str, *, now: float | None = None) -> int:
    """Send at most one operational-error notification per hour globally."""
    global _last_operational_error_at
    if not TOKEN or not _can_use_telegram():
        return 0
    current_time = time.time() if now is None else float(now)
    with _lock:
        elapsed = current_time - _last_operational_error_at
        if elapsed < _OPERATIONAL_ERROR_INTERVAL_SECONDS:
            logger.warning(
                "Suppressed operational Telegram error during hourly cooldown: %s",
                _plain_text(text).replace("\n", " "),
            )
            return 0
        _last_operational_error_at = current_time
        _save_state_locked()
    return broadcast(text)


def send_status_keyboard() -> int:
    """Show subscribed chats the read-only status control."""
    return broadcast(
        "🤖 <b>Crypto Trading Bot</b>\n\n"
        "Tap <b>📊 Status</b> below for current positions and P&amp;L."
    )


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
                if not _send_command_reply(
                    chat_id=chat_id,
                    text="Send /start first to subscribe.",
                ):
                    _record_poll_failure("Telegram could not send the subscription reply")
                    return False
                continue
            if text in ("/start", "start"):
                reply_sent = _send_command_reply(
                    chat_id=chat_id,
                    parse_mode="HTML",
                    text="🤖 <b>Crypto Trading Bot</b>\n\nYou're registered for trade alerts.\n\n"
                         "Tap <b>📊 Status</b> below for current positions and P&amp;L.",
                    reply_markup=_STATUS_KEYBOARD,
                )
            elif text in ("/status", "status", "📊 status"):
                reply_sent = _send_command_reply(
                    chat_id=chat_id,
                    parse_mode="HTML",
                    text=_render_status(on_status),
                    reply_markup=_STATUS_KEYBOARD,
                )
            elif text in ("/pause", "pause"):
                reply_sent = _send_command_reply(
                    chat_id=chat_id,
                    text="Trading controls are disabled for safety.",
                )
            elif text in ("/resume", "resume"):
                reply_sent = _send_command_reply(
                    chat_id=chat_id,
                    text="Trading controls are disabled for safety.",
                )
            else:
                reply_sent = True
            if not reply_sent:
                _record_poll_failure("Telegram could not send the command reply")
                return False
        _record_poll_success()
        return True
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        _record_poll_failure(message)
        logger.warning("Telegram command polling failed: %s", message)
        return False
