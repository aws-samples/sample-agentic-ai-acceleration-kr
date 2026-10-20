"""The Organization page: teams, people, drift and the audit ledger. Admin only.

Teams here are created for real — Cognito group, IAM role, Cedar policies — by
services/team_provisioner.py, in a background thread so the request returns a
`provisioning` row the page polls. People are read live from the pool and
written to it (services/org_directory_service.py); an Entra person is shown but
not editable. Everything an admin does lands in the audit ledger.

501 when TEAMS_TABLE is unset: the page is a feature of the table, not of auth.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core import clock
from core.auth import AuthUser, require_admin
from core.config import MCP_GATEWAY_ARN, TEAM_CONDITIONED_ACTIONS, TEAM_SHARED_ACTIONS
from core.dependencies import audit_service, org_directory_service, team_provisioner, team_service, usage_service
from models.team import TeamConfig, TeamConfigUpdate, TeamCreate, is_tool_pattern, validate_team_name
from services.org_directory_service import InviteFailed, NotCognitoUser, UserGone
from services.org_drift import compute_drift
from services.registry_service import ReadThroughCache, RegistryNotConfigured
from services.team_access import team_of
from services.team_provisioner import ProvisionError
from services.team_service import PROVISION_STALE_SECONDS, TeamStorageUnavailable, is_stale

logger = logging.getLogger(__name__)

COST_WINDOW_DAYS = 30

_teams_override = _provisioner_override = _directory_override = _audit_override = None
_usage_override = _registry_override = _known_tools_override = None


def _teams():
    return _teams_override or team_service


def _provisioner():
    return _provisioner_override or team_provisioner


def _directory():
    return _directory_override or org_directory_service


def _audit():
    return _audit_override or audit_service


def _usage():
    return _usage_override or usage_service


_registry_singleton = None
_harness_singleton = None


def _registry():
    """routes/registry.py keeps its own RegistryService rather than a
    dependencies singleton, so this one is built on first use."""
    global _registry_singleton
    if _registry_override is not None:
        return _registry_override
    if _registry_singleton is None:
        from services.registry_service import RegistryService
        _registry_singleton = RegistryService()
    return _registry_singleton


def _harness_control():
    global _harness_singleton
    if _harness_singleton is None:
        from services.harness_service import HarnessService
        _harness_singleton = HarnessService()
    return _harness_singleton.control


def _thread_background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


# Tests replace this with a synchronous call.
_run_in_background: Callable[[Callable[[], None]], None] = _thread_background


def _require_table() -> None:
    if not _teams().configured:
        raise HTTPException(status_code=501, detail="TEAMS_TABLE 이 설정되지 않아 조직 관리를 쓸 수 없습니다.")


# Every route is admin-only and needs the table. Without the table a PATCH
# would answer 200 and persist nothing, and audit entries would be dropped, so
# the whole router is 501. `require_admin` is listed first so a non-admin still
# gets 401/403, never a 501 that tells them about the deployment; the handlers
# keep their own `Depends(require_admin)` for the `user` value (FastAPI caches
# the dependency per request, so it runs once).
router = APIRouter(prefix="/api/org", tags=["org"],
                   dependencies=[Depends(require_admin), Depends(_require_table)])


def _actor(user: AuthUser) -> str:
    return user.email or user.username or user.sub


# ── gateway tools (the allow-list the UI picks from) ───────────────────────
def known_gateway_tools() -> List[str]:
    """`<target>___<tool>` for every Lambda target's inline schema on the
    platform gateway, plus the shared connector actions. Read with
    GetGatewayTarget rather than tools/list so it needs no gateway auth."""
    if _known_tools_override is not None:
        return _known_tools_override()
    names: List[str] = []
    if MCP_GATEWAY_ARN:
        control = _harness_control()
        gateway_id = MCP_GATEWAY_ARN.rsplit("/", 1)[-1]
        token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"gatewayIdentifier": gateway_id, "maxResults": 100}
            if token:
                params["nextToken"] = token
            resp = control.list_gateway_targets(**params)
            for item in resp.get("items", []):
                target = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=item["targetId"])
                schema = (((target.get("targetConfiguration") or {}).get("mcp") or {}).get("lambda") or {}).get("toolSchema") or {}
                for tool in schema.get("inlinePayload") or []:
                    if tool.get("name"):
                        names.append(f"{item['name']}___{tool['name']}")
            token = resp.get("nextToken")
            if not token:
                break
    for action in TEAM_SHARED_ACTIONS:
        if action not in names:
            names.append(action)
    return names


def _check_tools(tools: List[str]) -> None:
    """Gateway names must be on the gateway; harness patterns (`@builtin`,
    `@<server>/<tool>`, `*`) are the harness's syntax and pass as-is, so an
    externally written or pre-page row holding them stays editable."""
    tools = [t for t in tools if not is_tool_pattern(t)]
    if not tools:
        return
    known = set(known_gateway_tools())
    unknown = [t for t in tools if t not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"게이트웨이에 없는 툴입니다: {', '.join(unknown)}")
    conditioned = [t for t in tools if t in TEAM_CONDITIONED_ACTIONS]
    if conditioned:
        raise HTTPException(status_code=400, detail=(
            f"{', '.join(conditioned)} 은 인자 조건이 붙은 정책으로만 허용되는 툴이라 팀 정책에 넣을 수 없습니다."))


# ── teams ──────────────────────────────────────────────────────────────────
class TeamRow(TeamConfig):
    members: int = 0
    turns_30d: int = 0
    model_cost_micros_30d: int = 0
    # Today's (business-date, still being written) model cost, to read against
    # `daily_cost_alert_usd`. None when the ledger could not be read: a 0 there
    # would say "nothing spent" about a day nobody measured.
    today_cost_micros: Optional[int] = None
    policy_linked: bool = False


def _cost_window():
    w = clock.window(COST_WINDOW_DAYS)
    return w["start_date"], w["end_date"]


class _TeamCosts:
    """The two ledger reads every team row joins: 30-day totals and today's spend."""

    def __init__(self) -> None:
        self.totals: Dict[str, Dict[str, int]] = {}
        self.today: Optional[Dict[str, int]] = None

    @classmethod
    def read(cls) -> "_TeamCosts":
        out = cls()
        usage = _usage()
        if not getattr(usage, "configured", False):
            return out
        usage.begin_read()
        start, end = _cost_window()
        try:
            out.totals = usage.team_totals(start, end)
        except Exception:
            logger.warning("Team cost totals unavailable", exc_info=True)
        try:
            today = clock.today()
            out.today = {team: days.get(today, 0) for team, days in usage.team_daily_costs(today, today).items()}
        except Exception:
            logger.warning("Team daily costs unavailable", exc_info=True)
        return out


def _row(team: TeamConfig, members: int, costs: _TeamCosts) -> TeamRow:
    t = costs.totals.get(team.name, {})
    return TeamRow(**team.model_dump(), members=members, turns_30d=int(t.get("turns") or 0),
                   model_cost_micros_30d=int(t.get("model_cost_micros") or 0),
                   today_cost_micros=None if costs.today is None else int(costs.today.get(team.name, 0)),
                   policy_linked=bool(team.policy_ids))


@router.get("/teams")
def list_teams(user: AuthUser = Depends(require_admin)) -> dict:
    costs = _TeamCosts.read()
    try:
        sizes = _directory().team_sizes()
    except Exception:
        sizes = {}
    rows = [_row(team, sizes.get(team.name, 0), costs).model_dump() for team in _teams().list()]
    return {"teams": rows, "persisted": _teams().configured, "provisioner_enabled": _provisioner().enabled,
            "policies_enabled": _provisioner().policies_enabled}


@router.get("/teams/{name}")
def get_team(name: str, user: AuthUser = Depends(require_admin)) -> dict:
    team = _teams().get(name)
    if team is None:
        raise HTTPException(status_code=404, detail="팀이 없습니다.")
    return _row(team, _directory().team_sizes().get(name, 0), _TeamCosts.read()).model_dump()


def _provision_now(name: str, actor: AuthUser) -> None:
    """Runs in the background: provision, then mark ready or error."""
    svc, prov = _teams(), _provisioner()
    team = svc.get(name)
    if team is None:
        return
    try:
        done = prov.provision(team)
        # The provisioner ran for seconds; keep edits made meanwhile and take only what it produced.
        fresh = svc.get(name) or team
        svc.save(fresh.model_copy(update={"execution_role_arn": done.execution_role_arn,
                                          "policy_ids": done.policy_ids}), by=_actor(actor))
        svc.set_status(name, "ready")
        _audit().record(actor, "team.provision", name, after={"execution_role_arn": done.execution_role_arn,
                                                               "policy_ids": done.policy_ids})
    except ProvisionError as exc:
        svc.set_status(name, "error", {"step": exc.step, "message": exc.message})
        _audit().record(actor, "team.provision", name, result="error", message=f"{exc.step}: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - the row must say what happened
        logger.exception("Team provisioning crashed for %s", name)
        svc.set_status(name, "error", {"step": "unknown", "message": str(exc)})
        _audit().record(actor, "team.provision", name, result="error", message=str(exc))


@router.post("/teams", response_model=TeamConfig, status_code=202)
def create_team(body: TeamCreate, user: AuthUser = Depends(require_admin)) -> TeamConfig:
    _require_table()
    if not _provisioner().enabled:
        raise HTTPException(status_code=501, detail="TEAM_ROLE_BOUNDARY_ARN 이 없어 서버가 팀 역할을 만들 수 없습니다.")
    try:
        validate_team_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _check_tools(body.allowed_tools)
    try:
        team = _teams().create(body, by=_actor(user))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except TeamStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    _audit().record(user, "team.create", team.name, after=body.model_dump())
    _run_in_background(lambda: _provision_now(team.name, user))
    return _teams().get(team.name) or team


def _in_flight(team: TeamConfig) -> bool:
    """A `provisioning`/`deleting` row young enough that its step may still run."""
    return not is_stale(team.updated_at, datetime.now(timezone.utc), PROVISION_STALE_SECONDS)


@router.post("/teams/{name}/provision", response_model=TeamConfig)
def provision_team(name: str, user: AuthUser = Depends(require_admin)) -> TeamConfig:
    """Retry an errored team, or register a console-made `team:` group."""
    _require_table()
    if not _provisioner().enabled:
        # Otherwise the row flips to provisioning and the thread fails on an empty boundary/pool.
        raise HTTPException(status_code=501, detail="TEAM_ROLE_BOUNDARY_ARN 이 없어 서버가 팀 역할을 만들 수 없습니다.")
    svc = _teams()
    team = svc.get(name)
    if team is None:
        if name not in _directory().team_groups():
            raise HTTPException(status_code=404, detail="팀도 그룹도 없습니다.")
        try:
            validate_team_name(name)
            team = svc.create(TeamCreate(name=name), by=_actor(user))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _audit().record(user, "team.create", name, after={"registered_from_group": True})
    elif team.managed_by != "platform":
        raise HTTPException(status_code=409, detail="외부에서 만든 팀은 여기서 프로비저닝하지 않습니다.")
    else:
        if team.status == "provisioning" and _in_flight(team):
            raise HTTPException(status_code=409, detail="이미 프로비저닝 중입니다.")
        svc.set_status(name, "provisioning")
    _run_in_background(lambda: _provision_now(name, user))
    return svc.get(name) or team


@router.patch("/teams/{name}", response_model=TeamConfig)
def update_team(name: str, body: TeamConfigUpdate, user: AuthUser = Depends(require_admin)) -> TeamConfig:
    svc = _teams()
    before = svc.get(name)
    if before is None:
        raise HTTPException(status_code=404, detail="팀이 없습니다.")
    if before.status in ("provisioning", "deleting"):
        raise HTTPException(status_code=409, detail="프로비저닝 중인 팀은 끝난 뒤 수정할 수 있습니다.")
    if body.allowed_tools is not None:
        _check_tools(body.allowed_tools)
    try:
        merged = svc.put(name, body, by=_actor(user))
    except TeamStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if merged.managed_by == "platform" and merged.status == "ready" and _provisioner().enabled:
        try:
            merged = svc.save(_provisioner().apply_settings(merged), by=_actor(user))
        except ProvisionError as exc:
            svc.set_status(name, "error", {"step": exc.step, "message": exc.message})
            raise HTTPException(status_code=502, detail=f"설정은 저장됐지만 {exc.step} 반영에 실패했습니다: {exc.message}")
    _audit().record(user, "team.update", name, before=before.model_dump(include=set(body.model_dump(exclude_none=True))),
                    after=body.model_dump(exclude_none=True))
    return merged


@router.delete("/teams/{name}", status_code=204)
def delete_team(name: str, user: AuthUser = Depends(require_admin)) -> None:
    svc = _teams()
    team = svc.get(name)
    if team is None:
        raise HTTPException(status_code=404, detail="팀이 없습니다.")
    if team.status == "provisioning" and _in_flight(team):
        raise HTTPException(status_code=409, detail="프로비저닝 중인 팀은 끝난 뒤 지울 수 있습니다.")
    if team.status == "deleting" and _in_flight(team):
        raise HTTPException(status_code=409, detail="이미 삭제 중입니다.")
    if team.status == "provisioning":
        raise HTTPException(status_code=409, detail="프로비저닝이 멈춘 팀입니다. 먼저 다시 시도해 마무리하세요.")
    # Raw live group members, not the snapshot-decorated list: a person the
    # snapshot has not seen is still a person, and an unreadable group is unknown.
    try:
        member_subs = _directory().member_subs(name)
    except Exception:
        logger.warning("Member check failed for team %s", name, exc_info=True)
        raise HTTPException(status_code=503, detail="구성원을 확인하지 못해 지울 수 없습니다. 잠시 후 다시 시도하세요.")
    if member_subs:
        raise HTTPException(status_code=409, detail=f"구성원 {len(member_subs)}명이 남아 있습니다. 먼저 팀에서 빼세요.")
    try:
        records = _team_records(name)
    except RegistryReadFailed:
        raise HTTPException(status_code=503, detail="레지스트리를 읽지 못해 이 팀의 레코드를 확인할 수 없습니다. 잠시 후 다시 시도하세요.")
    if records:
        raise HTTPException(status_code=409, detail=f"이 팀의 승인 레코드 {len(records)}개가 있습니다. 먼저 팀을 바꾸거나 폐기하세요.")
    if team.managed_by != "platform":
        # Made outside the server: the role, policies and group are whoever
        # wrote the row's. Only the row goes.
        try:
            svc.delete(name)
        except TeamStorageUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        _audit().record(user, "team.delete", name, before=team.model_dump(), after={"reason": "external_row_only"})
        return
    svc.set_status(name, "deleting")
    try:
        _provisioner().deprovision(team)
        svc.delete(name)
    except ProvisionError as exc:
        svc.set_status(name, "error", {"step": exc.step, "message": exc.message})
        _audit().record(user, "team.delete", name, result="error", message=f"{exc.step}: {exc.message}")
        raise HTTPException(status_code=502, detail=f"{exc.step} 삭제에 실패했습니다: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - a row left in `deleting` is stuck; say what happened
        logger.exception("Team delete crashed for %s", name)
        svc.set_status(name, "error", {"step": "unknown", "message": str(exc)})
        _audit().record(user, "team.delete", name, result="error", message=str(exc))
        raise HTTPException(status_code=502, detail=f"삭제에 실패했습니다: {exc}")
    _audit().record(user, "team.delete", name, before=team.model_dump())


# ── members ────────────────────────────────────────────────────────────────
@router.get("/teams/{name}/members")
def list_members(name: str, user: AuthUser = Depends(require_admin)) -> dict:
    if _teams().get(name) is None:
        raise HTTPException(status_code=404, detail="팀이 없습니다.")
    members = [m.model_dump() for m in _directory().group_members(name)]
    entra = [u.model_dump() for u in getattr(_directory(), "list_users", lambda: [])() if u.provider != "cognito" and name in u.teams]
    return {"members": members + entra, "sources": {"cognito": "live", "entra": "last_login"}}


def _member_target(name: str, sub: str):
    if _teams().get(name) is None:
        raise HTTPException(status_code=404, detail="팀이 없습니다.")
    person = _directory().get_user(sub)
    if person is None:
        raise HTTPException(status_code=404, detail="사용자가 없습니다.")
    return person


@router.put("/teams/{name}/members/{sub}", status_code=204)
def add_member(name: str, sub: str, user: AuthUser = Depends(require_admin)) -> None:
    person = _member_target(name, sub)
    try:
        _directory().add_to_team(person, name)
    except NotCognitoUser as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit().record(user, "member.add", name, after={"sub": sub, "email": person.email})


@router.delete("/teams/{name}/members/{sub}", status_code=204)
def remove_member(name: str, sub: str, user: AuthUser = Depends(require_admin)) -> None:
    person = _member_target(name, sub)
    try:
        _directory().remove_from_team(person, name)
    except NotCognitoUser as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit().record(user, "member.remove", name, before={"sub": sub, "email": person.email})


# ── records ────────────────────────────────────────────────────────────────
class RegistryReadFailed(Exception):
    pass


def _team_records(name: str) -> List[Dict[str, Any]]:
    """Approved records of a team. An unconfigured registry has none; a registry
    that fails is unknown, which callers must not mistake for empty."""
    registry = _registry()
    if registry is None or not getattr(registry, "list_records", None):
        return []
    try:
        records = registry.list_records(status="APPROVED")
    except RegistryNotConfigured:
        return []
    except Exception as exc:
        logger.warning("Registry unavailable for team record check", exc_info=True)
        raise RegistryReadFailed(str(exc)) from exc
    return [
        {"record_id": r.record_id, "name": getattr(r, "name", ""), "descriptor_type": getattr(r, "descriptor_type", None)}
        for r in records if team_of(r) == name
    ]


@router.get("/teams/{name}/records")
def team_records(name: str, user: AuthUser = Depends(require_admin)) -> dict:
    if _teams().get(name) is None:
        raise HTTPException(status_code=404, detail="팀이 없습니다.")
    try:
        return {"records": _team_records(name)}
    except RegistryReadFailed:
        raise HTTPException(status_code=503, detail="레지스트리를 읽지 못했습니다. 잠시 후 다시 시도하세요.")


# ── users ──────────────────────────────────────────────────────────────────
ROLES_ALLOWED = ("admin", "user")


class InviteRequest(BaseModel):
    emails: List[str]
    teams: List[str] = []
    role: str = "user"
    send_email: bool = True


class UserUpdate(BaseModel):
    role: Optional[str] = None
    teams: Optional[List[str]] = None
    enabled: Optional[bool] = None


def _known_team_names() -> set:
    return {t.name for t in _teams().list()} | set(_directory().team_groups())


def _check_teams(teams: List[str]) -> None:
    unknown = [t for t in teams if t not in _known_team_names()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"없는 팀입니다: {', '.join(unknown)}")


def _check_role(role: str) -> None:
    if role not in ROLES_ALLOWED:
        raise HTTPException(status_code=400, detail="역할은 admin 또는 user 여야 합니다.")


# The 30-day per-user totals are the same for every admin looking at the page, and
# reading them scans every USERS# partition of the window: 4 paged DynamoDB Queries,
# 4.6 s at 1,000 users, on every load of the people list. One read serves a minute.
USAGE_CACHE_SECONDS = 60
_USAGE_CACHE = ReadThroughCache(USAGE_CACHE_SECONDS)


def _usage_by_user() -> Dict[str, Dict[str, int]]:
    usage = _usage()
    if not getattr(usage, "configured", False):
        return {}
    start, end = _cost_window()
    # Keyed on the service object as well as the window: a read that could not
    # get every partition is answered but not kept, so the next load asks again.
    complete = getattr(usage, "reads_complete", lambda: True)
    try:
        return _USAGE_CACHE.get(
            (usage, start, end),
            lambda: _read_usage_by_user(usage, start, end),
            cacheable=lambda _: complete(),
        )
    except Exception:
        logger.warning("User usage unavailable", exc_info=True)
        return {}


def _read_usage_by_user(usage, start: str, end: str) -> Dict[str, Dict[str, int]]:
    usage.begin_read()
    # Totals and costs read the same USERS# month partitions; fetching them
    # concurrently makes the cold read one round trip deep, not one per partition.
    prefetch = getattr(usage, "prefetch", None)
    if prefetch is not None:
        prefetch(["USERS#"], start, end)
    totals = usage.user_totals(start, end)
    costs = usage.user_costs(start, end)
    out: Dict[str, Dict[str, int]] = {}
    for sub in set(totals) | set(costs):
        out[sub] = {"turns_30d": int(totals.get(sub, {}).get("turns") or 0),
                    "model_cost_micros_30d": int(costs.get(sub, {}).get("model_cost_micros") or 0)}
    return out


def _user_row(person, usage: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    return {**person.model_dump(), **usage.get(person.sub, {"turns_30d": 0, "model_cost_micros_30d": 0})}


@router.get("/users")
def list_users(q: str = Query(""), team: str = Query(""), role: str = Query(""), provider: str = Query(""),
               status: str = Query(""), refresh: bool = Query(False), user: AuthUser = Depends(require_admin)) -> dict:
    """`status` is `disabled`, or a Cognito UserStatus such as FORCE_CHANGE_PASSWORD."""
    # The usage read is independent of the pool read, so it runs alongside it
    # rather than after (cold, the two were 5 s + 4.6 s back to back).
    with ThreadPoolExecutor(max_workers=1) as pool:
        usage_read = pool.submit(_usage_by_user)
        people = _directory().list_users(refresh=refresh)
        usage = usage_read.result()
    needle = q.strip().lower()
    rows = []
    for p in people:
        if needle and needle not in p.email.lower() and needle not in p.name.lower():
            continue
        if team and team not in p.teams:
            continue
        if role and p.role != role:
            continue
        if provider and p.provider != provider:
            continue
        if status == "disabled" and p.enabled:
            continue
        if status and status != "disabled" and p.status != status:
            continue
        rows.append(p)
    return {"users": [_user_row(p, usage) for p in rows], "total": len(people),
            "sources": {"cognito": "live", "entra": "last_login"}}


@router.post("/users", status_code=201)
def invite_users(body: InviteRequest, user: AuthUser = Depends(require_admin)) -> dict:
    _check_role(body.role)
    _check_teams(body.teams)
    invited, failed = [], []
    for raw in body.emails:
        email = raw.strip().lower()
        if not email or "@" not in email:
            failed.append({"email": raw, "detail": "이메일 형식이 아닙니다."})
            continue
        try:
            person, temp = _directory().invite(email, teams=body.teams, role=body.role, send_email=body.send_email)
        except InviteFailed as exc:
            # InviteHalfCreated: the Cognito user exists but a group add failed; the detail names the group.
            # Only the service's admin-facing `detail` reaches the response, never the exception text.
            failed.append({"email": email, "detail": exc.detail})
            _audit().record(user, "user.invite", email, result="error", message=exc.detail)
            continue
        row = {"sub": person.sub, "email": person.email, "teams": person.teams, "role": person.role}
        if temp:
            row["temporary_password"] = temp
        invited.append(row)
        _audit().record(user, "user.invite", email, after={"teams": body.teams, "role": body.role, "send_email": body.send_email})
    return {"invited": invited, "failed": failed}


def _person_or_404(sub: str):
    person = _directory().get_user(sub)
    if person is None:
        raise HTTPException(status_code=404, detail="사용자가 없습니다.")
    return person


@router.get("/users/{sub}")
def get_user(sub: str, user: AuthUser = Depends(require_admin)) -> dict:
    return _user_row(_person_or_404(sub), _usage_by_user())


def _admins_left_without(sub: str) -> int:
    return sum(1 for p in _directory().list_users() if p.role == "admin" and p.sub != sub and p.enabled)


@router.patch("/users/{sub}")
def update_user(sub: str, body: UserUpdate, user: AuthUser = Depends(require_admin)) -> dict:
    if body.role is not None:
        _check_role(body.role)
    if body.teams is not None:
        _check_teams(body.teams)
    person = _person_or_404(sub)
    is_self = sub == user.sub
    if is_self and body.enabled is False:
        raise HTTPException(status_code=400, detail="자기 자신을 비활성화할 수 없습니다.")
    # Spec §5.1: an admin cannot demote themself, other admins or not — they
    # would lose this page mid-session with no confirmation.
    if is_self and body.role == "user" and person.role == "admin":
        raise HTTPException(status_code=400, detail="자기 자신의 관리자 역할은 내릴 수 없습니다.")
    if body.role == "user" and person.role == "admin" and _admins_left_without(sub) == 0:
        raise HTTPException(status_code=400, detail="마지막 관리자의 역할은 내릴 수 없습니다.")
    try:
        if body.role is not None and body.role != person.role:
            _directory().set_role(person, body.role)
            _audit().record(user, "user.update_role", person.email or sub, before={"role": person.role}, after={"role": body.role})
        if body.teams is not None and sorted(body.teams) != sorted(person.teams):
            _directory().set_teams(person, body.teams)
            _audit().record(user, "user.update_teams", person.email or sub, before={"teams": person.teams}, after={"teams": body.teams})
        if body.enabled is not None and body.enabled != person.enabled:
            _directory().set_enabled(person, body.enabled)
            _audit().record(user, "user.enable" if body.enabled else "user.disable", person.email or sub)
    except NotCognitoUser as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UserGone:
        raise HTTPException(status_code=404, detail="사용자가 없습니다.")
    return _user_row(_directory().get_user(sub) or person, _usage_by_user())


@router.post("/users/{sub}/reset-password", status_code=204)
def reset_user_password(sub: str, user: AuthUser = Depends(require_admin)) -> None:
    person = _person_or_404(sub)
    try:
        _directory().reset_password(person)
    except NotCognitoUser as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UserGone:
        raise HTTPException(status_code=404, detail="사용자가 없습니다.")
    _audit().record(user, "user.reset_password", person.email or sub)


@router.delete("/users/{sub}", status_code=204)
def delete_user(sub: str, user: AuthUser = Depends(require_admin)) -> None:
    person = _person_or_404(sub)
    if sub == user.sub:
        raise HTTPException(status_code=400, detail="자기 자신은 지울 수 없습니다.")
    if person.role == "admin" and _admins_left_without(sub) == 0:
        raise HTTPException(status_code=400, detail="마지막 관리자는 지울 수 없습니다.")
    try:
        _directory().delete(person)
    except NotCognitoUser as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UserGone:
        raise HTTPException(status_code=404, detail="사용자가 없습니다.")
    _audit().record(user, "user.delete", person.email or sub, before=person.model_dump())


# ── drift / audit / tools ──────────────────────────────────────────────────
def _probe(team: TeamConfig) -> Dict[str, Any]:
    prov = _provisioner()
    try:
        return {"role": prov.role_exists(team), "policies": prov.policies_exist(team)}
    except Exception:
        logger.warning("Drift probe failed for %s", team.name, exc_info=True)
        return {}


@router.get("/drift")
def drift(user: AuthUser = Depends(require_admin)) -> dict:
    teams = _teams().list()
    groups: Optional[List[str]]
    if not _directory().has_pool:
        # Entra-only: no `team:` groups exist, so comparing would flag every team.
        groups = None
    else:
        try:
            groups = _directory().team_groups()
        except Exception:
            logger.warning("Group listing failed; drift reports rows only", exc_info=True)
            groups = None
    probes = {t.name: _probe(t) for t in teams if t.managed_by == "platform" and t.status == "ready"}
    now = datetime.now(timezone.utc)
    items = compute_drift(groups, teams, probes, now=now,
                          stale_after_seconds=PROVISION_STALE_SECONDS)
    return {"items": items, "checked_at": now.isoformat()}


@router.get("/audit")
def audit(from_: str = Query("", alias="from"), to: str = Query(""), actor: str = Query(""),
          target: str = Query(""), user: AuthUser = Depends(require_admin)) -> dict:
    now = datetime.now(timezone.utc)
    end = to or now.isoformat()
    start = from_ or (now - timedelta(days=30)).isoformat()
    return {"entries": _audit().list(start, end, actor=actor, target=target), "from": start, "to": end}


@router.get("/tools")
def tools(user: AuthUser = Depends(require_admin)) -> dict:
    try:
        names = known_gateway_tools()
    except Exception as exc:
        logger.warning("Gateway tool listing failed", exc_info=True)
        raise HTTPException(status_code=502, detail=f"게이트웨이 툴 목록을 읽지 못했습니다: {exc}")
    return {"tools": names, "shared": [a for a in TEAM_SHARED_ACTIONS if a in names],
            "conditioned": [a for a in TEAM_CONDITIONED_ACTIONS if a in names]}
