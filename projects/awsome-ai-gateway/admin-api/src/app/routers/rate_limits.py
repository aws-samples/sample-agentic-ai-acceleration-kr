# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.core.exceptions import ConflictError
from app.schemas.rate_limits import (
    RateLimitDeleteResponse,
    RateLimitResponse,
    RateLimitSetRequest,
    RateLimitTreeNode,
)

router = APIRouter(prefix="/admin/rate-limits", tags=["Rate Limit Management"])


@router.get("/tree", response_model=list[RateLimitTreeNode])
async def get_rate_limit_tree(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.rate_limit_service import RateLimitService

    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.get_rate_limit_tree(session)


# ⚠️ `:path` — GLOBAL 의 scope_id 는 model alias 이고 alias 에는 '/' 가 들어갈 수 있다(제약 없음,
#    VARCHAR(128)). ASGI 는 '%2F' 를 디코딩한 뒤 라우팅하므로 기본 변환기(`[^/]+`)로는 404 였다.
@router.get("/usage/{scope}/{scope_id:path}")
async def get_rate_limit_usage(
    request: Request,
    scope: str,
    scope_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """실시간 RPM 사용량(§60.9) — gateway-proxy 가 Redis 에 적재하는 sliding-window
    카운터를 읽어 현재 사용/모델별 분해 반환. 설정값(tree)과 별개의 라이브 상태.
    fail-soft: 조회 실패 시 {available:false}(화면 막지 않음).

    ``session`` 은 이 스코프 자신의 rpm 한도를 읽어 ``measured``(카운터가 존재할 수 있는가)
    를 판정하는 데 쓴다 — 한도가 없으면 proxy 가 ZSET 을 만들지 않으므로 '키 0건' 을
    '요청 0건' 으로 단정할 수 없다. GLOBAL 의 ``scope_id`` 는 model alias 다."""
    from app.services.rate_limit_service import RateLimitService

    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.get_live_usage(scope, scope_id, session=session)


@router.put("/user/{user_id}", response_model=RateLimitResponse)
async def set_user_rate_limit(
    request: Request,
    user_id: str,
    body: RateLimitSetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.rate_limit_service import RateLimitService

    svc: RateLimitService = request.app.state.rate_limit_service
    result = await svc.set_user_rate_limit(
        session,
        user_id=uuid.UUID(user_id),
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    # 커밋 **뒤에** 캐시를 지운다 — 먼저 지우면 그 창의 게이트웨이 캐시 미스가 옛 행을 300초
    # 다시 캐시한다(RateLimitService.invalidate_after_commit).
    await session.commit()
    await svc.invalidate_after_commit(session)
    return result


@router.delete("/user/{user_id}", response_model=RateLimitDeleteResponse)
async def delete_user_rate_limit(
    request: Request,
    user_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """USER 오버라이드 제거 → 팀 설정 상속으로 복귀(멱등, 없으면 deactivated=0).

    PUT 은 전체 교체라서 '값을 지우고 저장' 은 전부 NULL 인 USER 행을 남긴다. 그 행은
    집행상 무해하지만 트리에서 상속 표식을 지워 팀 한도를 보이지 않게 만든다.
    ``user_id`` 는 UUID 로 선언한다 — 형식이 틀리면 500 이 아니라 422 다."""
    from app.services.rate_limit_service import RateLimitService

    svc: RateLimitService = request.app.state.rate_limit_service
    result = await svc.delete_user_rate_limit(
        session,
        user_id=user_id,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    await session.commit()  # 커밋 뒤에 지운다(set_user_rate_limit 주석)
    await svc.invalidate_after_commit(session)
    return result


@router.put("/team/{team_id}", response_model=RateLimitResponse)
async def set_team_rate_limit(
    request: Request,
    team_id: str,
    body: RateLimitSetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: RateLimitService = request.app.state.rate_limit_service
    result = await svc.set_team_rate_limit(
        session,
        team_id=uuid.UUID(team_id),
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    await session.commit()  # 커밋 뒤에 지운다(set_user_rate_limit 주석)
    await svc.invalidate_after_commit(session)
    return result


@router.put("/global/{model_alias:path}", response_model=RateLimitResponse)  # '/' 든 alias(usage 주석)
async def set_global_rate_limit(
    request: Request,
    model_alias: str,
    body: RateLimitSetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
    release_alias: Annotated[
        str | None,
        Query(
            description=(
                "더 이상 받지 않는다(있으면 409). GLOBAL 한도는 모델별이라(0046) 이 저장은 다른 모델의 "
                "한도를 해제하지 않는다 — 단일 슬롯 시절 콘솔이 보내던 '해제 동의' 다."
            )
        ),
    ] = None,
):
    """이 모델(alias)의 GLOBAL 한도를 저장한다. 다른 모델의 GLOBAL 한도는 바뀌지 않는다(0046).

    ``release_alias`` 를 보내면 409 다. 그 파라미터는 GLOBAL 이 한 번에 한 모델이던 시절, 다른 모델의
    한도를 해제하는 저장에 콘솔이 붙이던 동의였다. 롤링 배포 동안 이전 콘솔은 확인 대화상자에서 "그
    모델의 한도가 해제된다" 고 말한 뒤 이 값을 보낸다. 조용히 무시하면 운영자는 풀었다고 믿는 한도가
    계속 집행되므로, 아무것도 바꾸지 않고 거절한다(이전 콘솔은 409 에 트리를 새로 받는다).
    """
    if release_alias is not None:
        raise ConflictError(
            f"GLOBAL rate limits are per model: saving {model_alias} does not release the limit on "
            f"{release_alias}, so release_alias is not accepted and nothing was changed. "
            "Reload the console, or resend without release_alias."
        )
    svc: RateLimitService = request.app.state.rate_limit_service
    result = await svc.set_global_rate_limit(
        session,
        model_alias=model_alias,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    await session.commit()  # 커밋 뒤에 지운다(set_user_rate_limit 주석)
    await svc.invalidate_after_commit(session)
    return result
