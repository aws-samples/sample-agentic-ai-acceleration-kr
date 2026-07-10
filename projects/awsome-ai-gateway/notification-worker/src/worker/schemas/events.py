# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    BUDGET_THRESHOLD = "budget_threshold"
    KEY_EXPIRING = "key_expiring"
    KEY_EXPIRED = "key_expired"
    KEY_REVOKED = "key_revoked"
    AUTH_FAILURE_SPIKE = "auth_failure_spike"
    PERMISSION_VIOLATION = "permission_violation"
    SUSPICIOUS_USAGE = "suspicious_usage"
    DEGRADATION_MODE = "degradation_mode"
    PROVIDER_ERROR = "provider_error"
    SERVICE_HEALTH_CHANGE = "service_health_change"


class Channel(str, Enum):
    EMAIL = "email"


class ServiceSource(str, Enum):
    GATEWAY_PROXY = "gateway-proxy"
    ADMIN_API = "admin-api"
    COST_RECORDER_WORKER = "cost-recorder-worker"


class NotificationEvent(BaseModel):
    event_id: str
    type: EventType
    timestamp: datetime
    source: ServiceSource
    payload: dict[str, Any]

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v: Any) -> Any:
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


def parse_pubsub_message(raw_data: str | bytes) -> NotificationEvent | None:
    """Parse a raw Redis Pub/Sub message into a NotificationEvent.

    Returns None on parse failure so callers can skip and continue.
    """
    try:
        data = json.loads(raw_data)
        return NotificationEvent.model_validate(data)
    except Exception as exc:
        logger.error("pubsub_parse_failed", extra={"error": str(exc), "raw": str(raw_data)[:200]})
        return None
