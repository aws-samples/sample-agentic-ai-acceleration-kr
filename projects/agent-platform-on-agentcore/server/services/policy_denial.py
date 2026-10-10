"""Spotting a gateway policy denial in a tool result, and attributing a turn to a team.

AgentCore Policy records its decisions as CloudWatch metrics and gateway spans,
neither of which carries the caller principal (checked 2026-10-10). The one
place a denial can be tied to a team is the stream: the harness receives the
denied tools/call as a tool error and the server sees that error pass by. The
exact wording is pinned from a live deployment (Task 12, 2026-10-10); a match
needs both a denial word and a policy word, so an ordinary tool error ("Cannot
divide by zero") or an authentication failure ("401 Unauthorized") is not
counted.
"""
import json
from typing import Any, Iterable, List, Optional

# Both groups must match, and no word may sit in both groups: "authoriz" used to
# be an authorisation word while "unauthorized" was a denial word, so a plain
# "401 Unauthorized" satisfied both on its own. The second group therefore names
# only the policy layer (Cedar policies on the gateway), never authn/authz in
# general. AWS documents the gateway's denial text as
# "AuthorizeActionException - Tool Execution Denied: Tool call not allowed due
# to policy enforcement [...]" (devguide use-gateway-with-policy, "Policy
# responses"); the live wording is pinned in tests/test_policy_denial.py.
_DENIAL_WORDS = ("deny", "denied", "not allowed", "accessdenied", "not authorized", "unauthorized", "forbidden")
_POLICY_WORDS = ("policy", "cedar")
POLICY_DENY_MARKERS = _DENIAL_WORDS + _POLICY_WORDS
# An explicit check, not `assert`: `python -O` strips asserts.
if any(a in b or b in a for a in _DENIAL_WORDS for b in _POLICY_WORDS):
    raise ImportError("policy_denial word groups overlap; one word would satisfy both")


def _text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except Exception:  # noqa: BLE001
        return str(result)


def is_policy_denial(result: Any) -> bool:
    text = _text(result).lower()
    if not text:
        return False
    return any(w in text for w in _DENIAL_WORDS) and any(w in text for w in _POLICY_WORDS)


def resolve_turn_team(
    caller_teams: List[str],
    record_team: Optional[str],
    declared: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """The team a turn's spend belongs to: the record's team if the caller is in
    it, otherwise the caller's first team, otherwise nobody (admins, teamless).

    `declared` (the deployment's teams) drops `team:` groups terraform never
    declared: their TEAMS# rows are never rendered but would still be
    subtracted from "unattributed", so those turns vanished from both."""
    teams = [t for t in (caller_teams or []) if t]
    if declared is not None:
        allowed = set(declared)
        teams = [t for t in teams if t in allowed]
    if record_team and record_team in teams:
        return record_team
    return teams[0] if teams else None
