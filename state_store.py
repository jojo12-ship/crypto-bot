"""Persistent state directory selection for the Crypto Bot."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from runtime_owner import determine_runtime_ownership


class StateStorageError(RuntimeError):
    pass


def position_recovery_marker_matches(
    marker_path: Path,
    symbols: list[str],
) -> bool:
    """Return true only for a valid marker covering every configured symbol."""
    try:
        payload = json.loads(marker_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return False
    marker_symbols = payload.get("symbols")
    if not isinstance(marker_symbols, list) or any(
        not isinstance(symbol, str) or not symbol.strip()
        for symbol in marker_symbols
    ):
        return False
    completed_at = payload.get("completed_at")
    if not isinstance(completed_at, str) or not completed_at.strip():
        return False
    expected = sorted({symbol.strip().upper() for symbol in symbols})
    recorded = sorted({symbol.strip().upper() for symbol in marker_symbols})
    return recorded == expected and len(recorded) == len(marker_symbols)


def configured_state_dir(
    environment: Mapping[str, str] | None = None,
    fallback_dir: Path | None = None,
) -> tuple[Path, bool]:
    env = os.environ if environment is None else environment
    ownership = determine_runtime_ownership(env)
    explicit = str(env.get("CRYPTO_STATE_DIR", "")).strip()
    volume_raw = str(env.get("RAILWAY_VOLUME_MOUNT_PATH", "")).strip()

    if ownership.is_designated_service:
        if not volume_raw:
            raise StateStorageError(
                "The Railway crypto-bot service requires a mounted persistent volume."
            )
        volume = Path(volume_raw).expanduser().resolve()
        selected = Path(explicit).expanduser().resolve() if explicit else volume
        if selected != volume and volume not in selected.parents:
            raise StateStorageError(
                "CRYPTO_STATE_DIR must be inside RAILWAY_VOLUME_MOUNT_PATH."
            )
        selected.mkdir(parents=True, exist_ok=True)
        return selected, True

    selected = (
        Path(explicit).expanduser().resolve()
        if explicit
        else (fallback_dir or Path(__file__).parent).resolve()
    )
    selected.mkdir(parents=True, exist_ok=True)
    return selected, False