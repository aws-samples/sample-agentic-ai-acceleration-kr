"""Platform settings an admin edits from the Settings page.

Today: which sidebar menus a plain user sees, and the per-team settings (label,
allowed models/tools, cost alert). The read is open to every signed-in
user because the sidebar needs it before anything else renders; the write is
admin-only. The model rate card, the other Settings section, has its own routes
under `/api/insights/rates` because it reprices the ledger.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthUser, current_user, require_admin
from core.dependencies import nav_service, team_service
from services.team_service import TeamStorageUnavailable
from models.team import TeamConfig, TeamConfigUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])

_nav_override = None


def _nav():
    return _nav_override or nav_service


class NavVisibilityRequest(BaseModel):
    hidden: Optional[List[str]] = None


@router.get("/nav")
def get_nav_visibility(_: AuthUser = Depends(current_user)) -> dict:
    """The menus hidden from plain users. Readable by anyone signed in."""
    return _nav().get()


@router.put("/nav")
def put_nav_visibility(body: NavVisibilityRequest, user: AuthUser = Depends(require_admin)) -> dict:
    """Hide or show menus for plain users. Unknown keys are dropped, not stored."""
    return _nav().put(body.hidden, by=user.username or user.sub)


_teams_override = None


def _teams():
    return _teams_override or team_service


@router.get("/teams")
def get_teams(user: AuthUser = Depends(current_user)) -> dict:
    """Admins: every team with its execution role. Others: their own teams, and
    no role ARN - a person needs the label and the tool/model lists to compose
    a harness, not the IAM wiring behind it."""
    svc = _teams()
    if user.is_admin:
        teams = [t.model_dump() for t in svc.list()]
    else:
        teams = [t.model_copy(update={"execution_role_arn": ""}).model_dump() for t in svc.for_user(user)]
    return {"teams": teams, "persisted": svc.configured}


@router.put("/teams/{name}", response_model=TeamConfig)
def put_team(name: str, body: TeamConfigUpdate, user: AuthUser = Depends(require_admin)) -> TeamConfig:
    try:
        return _teams().put(name, body, by=user.username or user.sub)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TeamStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
