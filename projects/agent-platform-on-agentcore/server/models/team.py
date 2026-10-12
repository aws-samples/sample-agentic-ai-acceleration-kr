"""Team settings an admin edits; who is in a team is the IdP's business.

A platform team's execution role is what services/team_provisioner.py created
for it; a row written from outside the server carries its own.
"""
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

TEAM_NAME_RE = re.compile(r"[a-z0-9-]{2,32}")
# AgentCore policy names are at most 48 characters; the longest the provisioner
# writes is `<project>_team_<name>_shared` (services/team_provisioner.policy_name).
POLICY_NAME_MAX = 48
# `shared` is the metadata value meaning "every team"; `admin`/`user` are the
# role groups, and a team group `team:admin` would read as a role in a log.
RESERVED_TEAM_NAMES = frozenset({"shared", "admin", "user"})


def validate_team_name(name: str) -> None:
    # fullmatch: `$` in a match() pattern also accepts a trailing "\n".
    if not isinstance(name, str) or not TEAM_NAME_RE.fullmatch(name):
        raise ValueError("팀 이름은 소문자·숫자·하이픈 2~32자여야 합니다.")
    if name in RESERVED_TEAM_NAMES:
        raise ValueError(f"'{name}' 은 예약어라 팀 이름으로 쓸 수 없습니다.")
    # Imported here: team_provisioner imports this module.
    from core import config
    from services.team_provisioner import policy_name
    if len(policy_name(config.PLATFORM, name, "shared")) > POLICY_NAME_MAX:
        longest = POLICY_NAME_MAX - len(policy_name(config.PLATFORM, "", "shared"))
        raise ValueError(f"이 프로젝트({config.PLATFORM})에서는 팀 이름이 {longest}자를 넘을 수 없습니다 "
                         f"(게이트웨이 정책 이름이 {POLICY_NAME_MAX}자 제한을 넘습니다).")


# ── allowed_tools: one field, two kinds of entries ─────────────────────────
# A harness `allowedTools` pattern (`@builtin`, `@<server>/<tool>`, `*`) is the
# harness's own allow-list syntax and means nothing to Cedar. A gateway action
# name (`<target>___<tool>`) is what the Cedar policy permits; on the harness it
# is the pattern `@*/<target>___<tool>`. The harness's `@<server>` is the name of
# the harness tool entry the gateway is attached as (a record name or the gateway
# id, e.g. `bap-gateway-zujfqvy9zs`), never the target, and the tool part is the
# full gateway tool name; `@<target>/<tool>` matches nothing (InvokeHarness probe,
# 2026-10-10). The `*` server keeps the pattern independent of how the gateway was
# attached. Externally written and pre-page rows hold patterns,
# Organization-page teams mostly gateway names; both must keep working.
GATEWAY_TOOL_SEPARATOR = "___"


def is_tool_pattern(entry: str) -> bool:
    return entry == "*" or entry.startswith("@")


def cedar_actions(allowed_tools: List[str]) -> List[str]:
    """The gateway action names in the list — what the team's Cedar policy permits."""
    return [t for t in allowed_tools if t and not is_tool_pattern(t)]


def harness_allowed_tools(allowed_tools: List[str]) -> List[str]:
    """The list as harness `allowedTools` patterns: patterns pass through, a
    gateway name `<target>___<tool>` becomes `@*/<target>___<tool>`. Order kept,
    duplicates dropped."""
    out: List[str] = []
    for entry in allowed_tools:
        if not entry:
            continue
        if not is_tool_pattern(entry) and GATEWAY_TOOL_SEPARATOR in entry:
            entry = f"@*/{entry}"
        if entry not in out:
            out.append(entry)
    return out


class TeamConfig(BaseModel):
    name: str
    label: str = ""
    description: str = ""
    # platform: the role the provisioner created, empty until the iam_role step
    # succeeded. Any other managed_by: whatever the row says.
    execution_role_arn: str = ""
    # Empty = no team-level model restriction (the global allow-list still applies).
    allowed_models: List[str] = []
    # Two kinds of entry (see harness_allowed_tools): harness patterns
    # (`@builtin`, `@<server>/<tool>`, `*`) reach only the harness; gateway
    # names (`<target>___<tool>`) reach the Cedar policy and, rewritten to
    # `@*/<target>___<tool>`, the harness.
    allowed_tools: List[str] = []
    daily_cost_alert_usd: Optional[float] = None
    # "platform": the server made the role and policies and may rewrite or
    # delete them. Any other value (e.g. "workshop"): the row came from outside
    # the server, which then only reads it and, on delete, drops the row.
    managed_by: str = "platform"
    status: str = "ready"  # "provisioning" | "ready" | "error" | "deleting"
    error: Optional[Dict[str, Any]] = None  # {"step", "message", "at"}
    policy_ids: Dict[str, str] = {}  # {"tools": policyId, "shared": policyId}
    # Entra group object IDs whose members belong to this team (token `groups` claim).
    entra_group_ids: List[str] = []
    created_by: str = ""
    created_at: str = ""
    updated_by: str = ""
    updated_at: str = ""


class TeamConfigUpdate(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    allowed_models: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = None
    daily_cost_alert_usd: Optional[float] = None
    entra_group_ids: Optional[List[str]] = None


class TeamCreate(BaseModel):
    name: str
    label: str = ""
    description: str = ""
    allowed_models: List[str] = []
    allowed_tools: List[str] = []
    daily_cost_alert_usd: Optional[float] = None
    entra_group_ids: List[str] = []
