"""Team settings — one platform-wide document, seeded from terraform's role map.

A team is a Cognito group `team:<name>` (core/auth.py). Which teams *exist* is
decided by terraform (`teams` variable → one harness execution role each →
TEAM_EXECUTION_ROLES env); this document only adds what an admin edits: label,
allowed models, allowed tools, cost alert. The role ARN in the document is never
trusted — the env value wins — so a stale document cannot point a harness at a
role terraform has since replaced.

Stored beside the nav visibility document (`PREFS#platform` / `teams`) with the
same rules: the stored value is a hint, the env map is the authority, a team the
env no longer names is dropped on read, and a storage failure reads as "seeds
only" rather than failing the page.
"""
import logging
from typing import Any, Dict, Iterable, List, Optional, Set

from core.auth import AuthUser
from models.team import TeamConfig, TeamConfigUpdate

logger = logging.getLogger(__name__)

PLATFORM_SUB = "platform"
TEAMS_NAME = "teams"


class TeamStorageUnavailable(RuntimeError):
    """The team document could not be read (a write would clobber it) or written."""


def _strings(value: Any) -> List[str]:
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def reconcile_teams(stored: Any, seeded_roles: Dict[str, str]) -> Dict[str, TeamConfig]:
    """Every seeded team, with the document's editable fields where present."""
    docs: Dict[str, Any] = {}
    if isinstance(stored, dict) and isinstance(stored.get("teams"), dict):
        docs = stored["teams"]
    teams: Dict[str, TeamConfig] = {}
    for name, role_arn in seeded_roles.items():
        raw = docs.get(name) if isinstance(docs.get(name), dict) else {}
        alert = raw.get("daily_cost_alert_usd")
        teams[name] = TeamConfig(
            name=name,
            label=raw.get("label") if isinstance(raw.get("label"), str) and raw.get("label") else name,
            execution_role_arn=role_arn,
            allowed_models=_strings(raw.get("allowed_models")),
            allowed_tools=_strings(raw.get("allowed_tools")),
            daily_cost_alert_usd=float(alert) if isinstance(alert, (int, float)) else None,
        )
    return teams


class TeamService:
    def __init__(self, repository=None, seeded_roles: Optional[Dict[str, str]] = None):
        self._repository = repository
        self._seeded = dict(seeded_roles or {})

    @property
    def configured(self) -> bool:
        return self._repository is not None

    @property
    def enabled(self) -> bool:
        """False when terraform declared no teams: every team feature is off."""
        return bool(self._seeded)

    def _read(self) -> Dict[str, TeamConfig]:
        if not self.configured:
            return reconcile_teams(None, self._seeded)
        try:
            return reconcile_teams(self._repository.get(PLATFORM_SUB, TEAMS_NAME), self._seeded)
        except Exception:
            logger.warning("Failed to read team settings", exc_info=True)
            return reconcile_teams(None, self._seeded)

    def _read_strict(self) -> Dict[str, TeamConfig]:
        """Like _read, but a failed read raises: put must never write a document
        it did not read, or one storage blip blanks every other team."""
        try:
            return reconcile_teams(self._repository.get(PLATFORM_SUB, TEAMS_NAME), self._seeded)
        except Exception as exc:
            logger.warning("Failed to read team settings before write", exc_info=True)
            raise TeamStorageUnavailable("Team settings storage is unavailable") from exc

    def list(self) -> List[TeamConfig]:
        return list(self._read().values())

    def get(self, name: str) -> Optional[TeamConfig]:
        return self._read().get(name)

    def names(self) -> Set[str]:
        """The declared teams (terraform's role map), without reading storage."""
        return set(self._seeded)

    def role_for(self, name: str) -> str:
        return self._seeded.get(name, "")

    def put(self, name: str, update: TeamConfigUpdate, *, by: str = "") -> TeamConfig:
        """Store the editable fields of one team. Unknown team → ValueError."""
        if name not in self._seeded:
            raise ValueError(f"Unknown team: {name}")
        teams = self._read_strict() if self.configured else self._read()
        current = teams[name]
        # A field the request named is applied even when it is null: that is
        # how the Settings tab clears daily_cost_alert_usd. An omitted field is
        # left alone. The list fields and label have no "cleared" meaning for
        # null, so null there still means "no change".
        sent = update.model_dump(include=update.model_fields_set)
        merged = current.model_copy(update={
            k: v for k, v in sent.items() if v is not None or k == "daily_cost_alert_usd"
        })
        teams[name] = merged
        if self.configured:
            document = {
                "teams": {
                    n: t.model_dump(exclude={"name", "execution_role_arn"}) for n, t in teams.items()
                },
                "updated_by": by,
            }
            try:
                self._repository.put(PLATFORM_SUB, TEAMS_NAME, document)
            except Exception as exc:
                # Answering 200 here showed "저장됨" for a value that was never kept.
                logger.warning("Failed to store team settings", exc_info=True)
                raise TeamStorageUnavailable("Team settings could not be stored") from exc
        return merged

    def for_user(self, user: AuthUser) -> List[TeamConfig]:
        teams = self._read()
        return [teams[n] for n in user.teams if n in teams]

    def allowed_models_for(self, team_names: Iterable[str]) -> Optional[Set[str]]:
        """Union of the named teams' model lists; None when nothing restricts.

        A team with an empty list places no restriction, so a person in such a
        team may use any globally allowed model even if their other team is
        restricted — the limit is a team's property, not a person's.
        """
        teams = self._read()
        known = [teams[n] for n in team_names if n in teams]
        if not known or any(not t.allowed_models for t in known):
            return None
        return {m for t in known for m in t.allowed_models}
