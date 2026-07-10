# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Admin API Virtual Key issuance client (BR-VK-01, US-01)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import requests
import structlog

from api_key_helper.config import HelperConfig

log = structlog.get_logger(component="api-key-helper")


@dataclass
class VirtualKeyResponse:
    virtual_key: str
    expires_at: datetime
    gateway_endpoint: str = ""
    otel_endpoint: str = ""
    user_id: str = ""
    team_id: str = ""
    max_budget_usd: Decimal = Decimal("0")
    used_usd: Decimal = Decimal("0")
    tpm_limit: int = 0
    rpm_limit: int = 0


def request_virtual_key(
    config: HelperConfig,
    sts_request: dict,
    device_name: str,
    sso_session_expires_at: datetime | None = None,
) -> VirtualKeyResponse:
    """POST /cli/auth/virtual-key to get a Virtual Key (US-01).

    Raises requests.HTTPError or RuntimeError on failure.
    """
    url = f"{config.gateway_url}/cli/auth/virtual-key"

    log.info("requesting_virtual_key", url=url, device_name=device_name)

    body: dict = {
        "sts_request": sts_request,
        "device_name": device_name,
    }
    if sso_session_expires_at is not None:
        body["sso_session_expires_at"] = sso_session_expires_at.isoformat()

    resp = requests.post(
        url,
        json=body,
        timeout=(config.connect_timeout, config.read_timeout),
    )
    resp.raise_for_status()
    data = resp.json()

    expires_at_str = data.get("expires_at", "")
    try:
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        expires_at = datetime.now()

    return VirtualKeyResponse(
        virtual_key=data["virtual_key"],
        expires_at=expires_at,
        gateway_endpoint=data.get("gateway_endpoint", ""),
        otel_endpoint=data.get("otel_endpoint", ""),
        user_id=data.get("user_id", ""),
        team_id=data.get("team_id", ""),
        max_budget_usd=Decimal(str(data["max_budget_usd"])) if data.get("max_budget_usd") is not None else Decimal("0"),
        used_usd=Decimal(str(data["used_usd"])) if data.get("used_usd") is not None else Decimal("0"),
        tpm_limit=int(data["tpm_limit"]) if data.get("tpm_limit") is not None else 0,
        rpm_limit=int(data["rpm_limit"]) if data.get("rpm_limit") is not None else 0,
    )
