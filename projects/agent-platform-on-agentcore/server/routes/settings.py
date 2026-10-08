"""Platform settings an admin edits from the Settings page.

Today: which sidebar menus a plain user sees. The read is open to every signed-in
user because the sidebar needs it before anything else renders; the write is
admin-only. The model rate card, the other Settings section, has its own routes
under `/api/insights/rates` because it reprices the ledger.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.auth import AuthUser, current_user, require_admin
from core.dependencies import nav_service

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
