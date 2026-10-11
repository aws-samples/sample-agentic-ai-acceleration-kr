"""Team settings — rows in the teams table.

A team is a row (`pk=TEAM, sk=<name>`). Rows the Organization page created
(`managed_by=platform`) own an execution role and Cedar policies that
services/team_provisioner.py made; a row written by anything else (a workshop
stack, a console script — any other `managed_by`) is read as it is: its
`execution_role_arn` is trusted and its lifecycle (provision, role/policy
rewrite, deprovision) is not the server's.

Membership is not here. Cognito groups and the Entra token are the truth; this
service only maps Entra group ids to team names (`teams_for_entra_groups`).

Without a table (TEAMS_TABLE unset) there are no teams and every team feature
is off. A storage failure reads as "no teams" rather than failing the page; a
write after a failed read raises so one blip cannot blank another team's
settings.
"""
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from core.auth import AuthUser
from models.knowledge import now_iso
from models.team import TeamConfig, TeamConfigUpdate, TeamCreate, validate_team_name

logger = logging.getLogger(__name__)

ENTRA_CACHE_SECONDS = 60
# A `provisioning`/`deleting` row older than this is a process that died
# mid-step (a daemon thread lost to a restart, a crash in the error path).
PROVISION_STALE_SECONDS = 300
EDITABLE = ("label", "description", "allowed_models", "allowed_tools", "daily_cost_alert_usd", "entra_group_ids")


def is_stale(updated_at: str, now: datetime, stale_after_seconds: int = PROVISION_STALE_SECONDS) -> bool:
    """True when `updated_at` is older than the window. An unreadable stamp is
    stale: nothing proves a step is still running, and a stuck row must stay
    recoverable."""
    try:
        stamped = datetime.fromisoformat(updated_at)
    except (TypeError, ValueError):
        return True
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return (now - stamped).total_seconds() >= stale_after_seconds


class TeamStorageUnavailable(RuntimeError):
    """The team rows could not be read (a write would clobber them) or written."""


def _strings(value: Any) -> List[str]:
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def _from_row(row: Dict[str, Any]) -> Optional[TeamConfig]:
    name = row.get("name")
    if not isinstance(name, str) or not name:
        return None
    alert = row.get("daily_cost_alert_usd")
    return TeamConfig(
        name=name,
        label=row.get("label") if isinstance(row.get("label"), str) and row.get("label") else name,
        description=str(row.get("description") or ""),
        execution_role_arn=str(row.get("execution_role_arn") or ""),
        allowed_models=_strings(row.get("allowed_models")),
        allowed_tools=_strings(row.get("allowed_tools")),
        daily_cost_alert_usd=float(alert) if isinstance(alert, (int, float, Decimal)) and not isinstance(alert, bool) else None,
        managed_by=str(row.get("managed_by") or "platform"),
        status=str(row.get("status") or "ready"),
        error=row.get("error") if isinstance(row.get("error"), dict) else None,
        policy_ids={k: str(v) for k, v in (row.get("policy_ids") or {}).items()} if isinstance(row.get("policy_ids"), dict) else {},
        entra_group_ids=_strings(row.get("entra_group_ids")),
        created_by=str(row.get("created_by") or ""),
        created_at=str(row.get("created_at") or ""),
        updated_by=str(row.get("updated_by") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


class TeamService:
    def __init__(self, repository=None, clock: Callable[[], float] = time.monotonic):
        self._repository = repository
        self._clock = clock
        self._entra_cache: Optional[Dict[str, List[str]]] = None
        self._entra_cache_at = 0.0

    @property
    def configured(self) -> bool:
        return self._repository is not None

    @property
    def enabled(self) -> bool:
        """False when there is no team at all: every team feature is off."""
        return bool(self._read())

    # ── reads ──────────────────────────────────────────────────────────────
    def _rows(self) -> Dict[str, TeamConfig]:
        teams: Dict[str, TeamConfig] = {}
        for row in self._repository.list_teams():
            team = _from_row(row)
            if team is not None:
                teams[team.name] = team
        return dict(sorted(teams.items()))

    def _read(self) -> Dict[str, TeamConfig]:
        if not self.configured:
            return {}
        try:
            return self._rows()
        except Exception:
            logger.warning("Failed to read team rows", exc_info=True)
            return {}

    def _read_strict(self) -> Dict[str, TeamConfig]:
        try:
            return self._rows()
        except Exception as exc:
            logger.warning("Failed to read team rows before write", exc_info=True)
            raise TeamStorageUnavailable("Team settings storage is unavailable") from exc

    def list(self) -> List[TeamConfig]:
        return list(self._read().values())

    def get(self, name: str) -> Optional[TeamConfig]:
        return self._read().get(name)

    def names(self) -> Set[str]:
        """Every known team name."""
        return set(self._read())

    def role_for(self, name: str) -> str:
        team = self.get(name)
        return team.execution_role_arn if team else ""

    def for_user(self, user: AuthUser) -> List[TeamConfig]:
        teams = self._read()
        return [teams[n] for n in user.teams if n in teams]

    def allowed_models_for(self, team_names: Iterable[str]) -> Optional[Set[str]]:
        """Union of the named teams' model lists; None when nothing restricts."""
        teams = self._read()
        known = [teams[n] for n in team_names if n in teams]
        if not known or any(not t.allowed_models for t in known):
            return None
        return {m for t in known for m in t.allowed_models}

    def teams_for_entra_groups(self, group_ids: List[str]) -> List[str]:
        """Team names whose `entra_group_ids` intersect the token's groups.
        Snapshot of the mapping, refreshed every ENTRA_CACHE_SECONDS."""
        now = self._clock()
        if self._entra_cache is None or now - self._entra_cache_at >= ENTRA_CACHE_SECONDS:
            mapping: Dict[str, List[str]] = {}
            for team in self._read().values():
                for gid in team.entra_group_ids:
                    mapping.setdefault(gid, []).append(team.name)
            self._entra_cache, self._entra_cache_at = mapping, now
        out: List[str] = []
        for gid in group_ids:
            for name in self._entra_cache.get(gid, []):
                if name not in out:
                    out.append(name)
        return out

    # ── writes ─────────────────────────────────────────────────────────────
    def _write(self, team: TeamConfig) -> None:
        try:
            self._repository.put_team(team.model_dump())
        except Exception as exc:
            logger.warning("Failed to write team row %s", team.name, exc_info=True)
            raise TeamStorageUnavailable("Team settings storage is unavailable") from exc
        self._entra_cache = None

    def put(self, name: str, update: TeamConfigUpdate, *, by: str = "") -> TeamConfig:
        """Store the editable fields of one team. Unknown team → ValueError."""
        if not self.configured:
            raise TeamStorageUnavailable("TEAMS_TABLE is not configured")
        teams = self._read_strict()
        if name not in teams:
            raise ValueError(f"Unknown team: {name}")
        # A field the request named is applied even when it is null: that is how
        # the sheet clears daily_cost_alert_usd. An omitted field is left alone.
        # The list fields and strings have no "cleared" meaning for null, so null
        # there still means "no change".
        sent = update.model_dump(include=update.model_fields_set)
        changes = {k: v for k, v in sent.items() if v is not None or k == "daily_cost_alert_usd"}
        merged = teams[name].model_copy(update={**changes, "updated_by": by, "updated_at": now_iso()})
        self._write(merged)
        return merged

    def create(self, req: TeamCreate, *, by: str) -> TeamConfig:
        """A platform team row in `provisioning`; the provisioner fills the rest."""
        if not self.configured:
            raise TeamStorageUnavailable("TEAMS_TABLE is not configured")
        validate_team_name(req.name)
        if req.name in self._read_strict():
            raise ValueError(f"팀 '{req.name}' 은 이미 있습니다.")
        now = now_iso()
        team = TeamConfig(
            name=req.name, label=req.label or req.name, description=req.description,
            allowed_models=list(req.allowed_models), allowed_tools=list(req.allowed_tools),
            daily_cost_alert_usd=req.daily_cost_alert_usd, entra_group_ids=list(req.entra_group_ids),
            managed_by="platform", status="provisioning",
            created_by=by, created_at=now, updated_by=by, updated_at=now,
        )
        self._write(team)
        return team

    def save(self, team: TeamConfig, *, by: str = "") -> TeamConfig:
        if not self.configured:
            raise TeamStorageUnavailable("TEAMS_TABLE is not configured")
        self._read_strict()
        saved = team.model_copy(update={"updated_by": by or team.updated_by, "updated_at": now_iso()})
        self._write(saved)
        return saved

    def set_status(self, name: str, status: str, error: Optional[Dict[str, Any]] = None) -> TeamConfig:
        team = self._read_strict().get(name)
        if team is None:
            raise ValueError(f"Unknown team: {name}")
        payload = {**error, "at": now_iso()} if error else None
        updated = team.model_copy(update={"status": status, "error": payload, "updated_at": now_iso()})
        self._write(updated)
        return updated

    def delete(self, name: str) -> None:
        if not self.configured:
            raise TeamStorageUnavailable("TEAMS_TABLE is not configured")
        self._read_strict()
        self._repository.delete_team(name)
        self._entra_cache = None
