"""Team settings an admin edits; the execution role comes from terraform only."""
from typing import List, Optional

from pydantic import BaseModel


class TeamConfig(BaseModel):
    name: str
    label: str = ""
    # Read-only here: terraform creates one harness execution role per team and
    # hands the map to the server as TEAM_EXECUTION_ROLES.
    execution_role_arn: str = ""
    # Empty = no team-level model restriction (the global allow-list still applies).
    allowed_models: List[str] = []
    # Harness `allowedTools` patterns the team's harnesses are composed with.
    allowed_tools: List[str] = []
    daily_cost_alert_usd: Optional[float] = None


class TeamConfigUpdate(BaseModel):
    label: Optional[str] = None
    allowed_models: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = None
    daily_cost_alert_usd: Optional[float] = None
