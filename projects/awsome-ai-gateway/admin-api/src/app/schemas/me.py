# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/me`` 응답 — API 가 판정한 호출자 신원(``core/auth.CurrentUser``) 그대로."""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.common import UserRole


class MeResponse(BaseModel):
    user_id: str
    email: str
    role: UserRole
    #: 기본값을 두지 않는다 — 팀이 없어도 키는 항상 있다(null).
    team_id: str | None
