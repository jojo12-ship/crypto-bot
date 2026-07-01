"""
Lightweight Telegram notifier — no full bot needed, just POST to sendMessage.
Also handles the /status, /pause, /resume command bot.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Callable

import requests

logger = logging.getLogger("notify")

TOKEN = re.sub(r"\s+", "", os.getenv("CRYPTO_BOT_TOKEN", ""))
_BASE = f"https://api.telegram.org/bot{TOKEN}"

_subscribers: set[int] = set()
_offset: int = 0
_lock = threading.Lock()


def _post(method: str, **kwargs) -> dict:
    if not TOKEN:
        return {}
    try:
        r = requests.post(f"{_BASE}/{method}", json=kwargs, timeout=10)
        return r.json()
    except Exception as e:
        logger.debug(f"Telegram {method} failed: {e}")
        return {}


def broadcast(text: str) -> None:
    if not TOKEN:
        return
    with _lock:
        subs = set(_subscribers)
    for chat_id in subs:
        _post("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def poll_commands(
    on_status: Callable,
    on_pause: Callable,
    on_resume: Callable,
) -> None:
    """Long-poll Telegram for commands in a background thread."""
    global _offset
    if not TOKEN:
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
                _subscribers.add(chat_id)
            if text in ("/start", "start"):
                _post("sendMessage", chat_id=chat_id, parse_mode="HTML",
                      text="🤖 <b>Crypto Trading Bot</b>\n\nYou're registered for trade alerts.\n\n"
                           "/status — current position & P&amp;L\n"
                           "/pause — pause trading\n"
                           "/resume — resume trading")
            elif text in ("/status", "status"):
                _post("sendMessage", chat_id=chat_id, parse_mode="HTML", text=on_status())
            elif text in ("/pause", "pause"):
                _post("sendMessage", chat_id=chat_id, parse_mode="HTML", text=on_pause())
            elif text in ("/resume", "resume"):
                _post("sendMessage", chat_id=chat_id, parse_mode="HTML", text=on_resume())
    except Exception as e:
        logger.debug(f"poll_commands error: {e}")
