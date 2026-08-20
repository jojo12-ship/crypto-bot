"""Volume-backed exclusive process lease for active crypto-bot workers."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import IO


class SingletonLease:
    def __init__(self, path: Path):
        self.path = path
        self._handle: IO[str] | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        json.dump(
            {
                "pid": os.getpid(),
                "deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID", ""),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            handle,
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return True

    def close(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None