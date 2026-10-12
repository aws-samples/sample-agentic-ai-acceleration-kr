"""Record visibility by team — pure functions, no I/O.

A record's team is the `team` field of its custom metadata (the registry's
default schema declares it as a free string). No value, or the literal
`shared`, means every signed-in person may see and run it; a team name means
its members and admins. The same rule is applied in three places so the
browser can never widen it: the list and search routes, and the execution
binding of a turn (services/agent_access.py).

Two switches narrow the rule:

- `teams_enabled` — a deployment that declared no teams (TeamService.enabled
  False) has no team visibility at all. The registry's custom-metadata schema
  has carried a free-string `team` since before teams existed, so a curator's
  `team=search` label must not suddenly hide records.
- `known` — whether the record's metadata could be read at all (a failed
  BatchGet, a deployed harness whose tags could not be read). This is an
  authorization filter, so an unknown team fails closed for non-admins rather
  than reading as shared.

Changing a record's team is an ordinary edit: it opens a DRAFT revision that
curation approves, and until then the approved revision — and its team — is
what the list and chat see. That is deliberate: moving an agent between teams
is a governance decision, not a flip of a switch.
"""
from typing import Any, Iterable, List, Optional

SHARED = "shared"
TEAM_KEY = "team"


def normalize_team(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    return None if name in ("", SHARED) else name


def team_of(record: Any) -> Optional[str]:
    """The record's team from its custom metadata; None when shared or unknown."""
    meta = getattr(record, "custom_metadata", None)
    if not isinstance(meta, dict):
        return None
    return normalize_team(meta.get(TEAM_KEY))


def can_see(
    record_team: Optional[str], caller: Any, *, teams_enabled: bool, known: bool = True
) -> bool:
    """`caller` is an AuthUser or None. None = server-internal, nothing to hide."""
    if not teams_enabled:
        return True
    if caller is None or getattr(caller, "is_admin", False):
        return True
    if not known:
        return False
    team = normalize_team(record_team)
    if team is None:
        return True
    return team in (getattr(caller, "teams", None) or [])


def visibility_known(record: Any) -> bool:
    return getattr(record, "visibility_known", True) is not False


def filter_visible(records: Iterable[Any], caller: Any, *, teams_enabled: bool) -> List[Any]:
    return [
        r
        for r in records
        if can_see(team_of(r), caller, teams_enabled=teams_enabled, known=visibility_known(r))
    ]
