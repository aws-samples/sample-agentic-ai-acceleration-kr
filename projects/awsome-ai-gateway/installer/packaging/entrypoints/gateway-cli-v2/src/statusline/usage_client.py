# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Gateway usage API client for statusline (BR-SL-05)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import requests
import structlog

from statusline.config import StatuslineConfig

log = structlog.get_logger(component="statusline")


@dataclass
class ModelUsage:
    model: str = ""
    cost_usd: Decimal = Decimal("0")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0


@dataclass
class UsageInfo:
    used: Decimal = Decimal("0")
    limit: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")
    percentage: float = 0.0
    period: str = ""
    fetched_at: datetime | None = None
    models: list[ModelUsage] = field(default_factory=list)


def fetch_usage(config: StatuslineConfig, virtual_key: str) -> UsageInfo:
    """GET /v1/usage/me to retrieve current usage (BR-SL-05)."""
    url = f"{config.gateway_url}{config.usage_endpoint}"

    resp = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {virtual_key}",
            "User-Agent": "claude-cli/gateway-cli-statusline",
        },
        timeout=(config.connect_timeout, config.read_timeout),
    )
    resp.raise_for_status()
    data = resp.json()

    budget = data.get("budget", {})
    used = Decimal(str(budget.get("used_usd", "0")))
    limit = Decimal(str(budget.get("max_usd", "0")))
    remaining = Decimal(str(budget.get("remaining_usd", "0")))
    percentage = float(budget.get("pct", 0.0))

    models = []
    for m in data.get("model_breakdown", []):
        models.append(ModelUsage(
            model=m.get("model", ""),
            cost_usd=Decimal(str(m.get("cost_usd", "0"))),
            input_tokens=int(m.get("input_tokens", 0)),
            output_tokens=int(m.get("output_tokens", 0)),
            cache_write_tokens=int(m.get("cache_write_tokens", 0)),
            cache_read_tokens=int(m.get("cache_read_tokens", 0)),
            requests=int(m.get("requests", 0)),
        ))

    return UsageInfo(
        used=used,
        limit=limit,
        remaining=remaining,
        percentage=percentage,
        period=data.get("period", ""),
        fetched_at=datetime.now(timezone.utc),
        models=models,
    )
