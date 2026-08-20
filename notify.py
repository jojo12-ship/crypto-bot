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
from pathlib import Path
from typing import Callable

import requests
from runtime_owner import current_runtime_ownership

logger = logging.getLogger("notify")

TOKEN = re.sub(r"\s+", "", os.getenv("CRYPTO_BOT_TOKEN", ""))
_BASE = f"https://api.telegram.org/bot{TOKEN}"

_lock = threading.Lock()
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
        return {}
    try:
        r = requests.post(f"{_BASE}/{method}", json=kwargs, timeout=10)
        return r.json()
    except Exception as e:
        logger.debug(f"Telegram {method} failed: {e}")
        return {}


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
) -> None:
    """Long-poll Telegram for commands in a background thread."""
    global _offset
    if not TOKEN or not _can_use_telegram():
        return
    try:
        resp = _post("getUpdates", offset=_offset, timeout=20, allowed_updates=["message"])
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
    except Exception as e:
        logger.debug(f"poll_commands error: {e}")
