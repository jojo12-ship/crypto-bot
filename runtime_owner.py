"""Determine which exact Railway service may own crypto-bot side effects."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


EXPECTED_RAILWAY_SERVICE_ID = "d867f8f1-be24-4319-88cc-668441d977fe"
EXPECTED_RAILWAY_PROJECT_ID = "8211a8fe-c29b-4b29-9ae6-5018ee26a151"
EXPECTED_RAILWAY_ENVIRONMENT_ID = "853f492a-b782-4419-a805-64e93d511415"


@dataclass(frozen=True)
class RuntimeOwnership:
    is_designated_service: bool
    owner: str
    reason: str


def determine_runtime_ownership(
    environment: Mapping[str, str] | None = None,
) -> RuntimeOwnership:
    env = os.environ if environment is None else environment
    actual_service_id = str(env.get("RAILWAY_SERVICE_ID", "")).strip()
    actual_project_id = str(env.get("RAILWAY_PROJECT_ID", "")).strip()
    actual_environment_id = str(env.get("RAILWAY_ENVIRONMENT_ID", "")).strip()
    if (
        actual_service_id == EXPECTED_RAILWAY_SERVICE_ID
        and actual_project_id == EXPECTED_RAILWAY_PROJECT_ID
        and actual_environment_id == EXPECTED_RAILWAY_ENVIRONMENT_ID
    ):
        return RuntimeOwnership(
            True,
            "railway",
            "This is the designated Railway crypto-bot service",
        )
    if actual_service_id or actual_project_id or actual_environment_id:
        return RuntimeOwnership(
            False,
            "health-only",
            "A different Railway service owns this process",
        )
    return RuntimeOwnership(
        False,
        "health-only",
        "Only the designated Railway service may trade or use Telegram",
    )


def current_runtime_ownership() -> RuntimeOwnership:
    return determine_runtime_ownership()