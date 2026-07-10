# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

from pydantic import BaseModel


class RoutingProfileSchema(BaseModel):
    client: str
    backend: str            # "invoke" | "mantle"
    account_role_arn: str | None = None
    region: str
    default_model: str | None = None   # a model_aliases.alias
    external_id: str | None = None
    enabled: bool = True
    # Server-side web search (AgentCore Gateway) for this client. Orthogonal to
    # `enabled` (a profile can be enabled with search off). Default False (opt-in).
    web_search_enabled: bool = False
