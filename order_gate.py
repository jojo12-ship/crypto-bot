"""Synchronized fail-closed permission gate for exchange order submission."""
from __future__ import annotations

import threading
from typing import Callable, TypeVar


class OrderPermissionError(RuntimeError):
    pass


_Result = TypeVar("_Result")


class SynchronizedOrderGate:
    def __init__(self, permission_check: Callable[[], bool]):
        self._permission_check = permission_check
        self._enabled = False
        self._lock = threading.RLock()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)

    def is_allowed(self) -> bool:
        with self._lock:
            return self._enabled and self._permission_check()

    def submit(self, order_call: Callable[[], _Result]) -> _Result:
        with self._lock:
            if not self._enabled or not self._permission_check():
                raise OrderPermissionError(
                    "Binance order blocked because Telegram polling is not healthy"
                )
            return order_call()