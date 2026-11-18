# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/me`` — API 가 판정한 호출자 신원을 그대로 돌려준다.

admin-ui 는 역할을 IdP 토큰의 그룹만 보고 정했다. API 는 그룹·``ADMIN_EMAILS``·``auth.users`` 의
역할을 함께 본다(``core/auth._resolve_idp_identity``). 그래서 이메일로만 관리자인 사용자와 모든 팀
리더는 API 가 받아 주는데도 화면마다 403 을 봤다. 판정은 ``get_current_user`` 한 곳에서만 한다 —
여기서 다시 계산하지 않고, 거부(401·403)도 그 의존성이 내는 것이 그대로 나간다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.core.auth import CurrentUser, get_current_user
from app.schemas.me import MeResponse

router = APIRouter(prefix="/admin", tags=["Current User"])


@router.get("/me", response_model=MeResponse)
async def get_me(response: Response, user: CurrentUser = Depends(get_current_user)):
    # 역할은 요청 사이에 바뀐다(IdP 그룹 회수·DB 역할 변경). 캐시된 옛 답이 남으면 권한을 잃은
    # 사용자에게 관리자 화면이 계속 열린다.
    response.headers["Cache-Control"] = "no-store"
    return MeResponse(
        user_id=str(user.user_id),
        email=user.email,
        role=user.role,
        team_id=str(user.team_id) if user.team_id is not None else None,
    )
