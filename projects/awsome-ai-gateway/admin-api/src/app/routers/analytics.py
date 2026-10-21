# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin, require_admin_or_team_leader
from app.core.db import get_db_session
from app.models.auth import UserRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["Analytics"])

#: scope 의 허용 형태.
_SCOPE_PATTERN = r"^(all|team:[0-9a-fA-F-]{36})$"

#: /admin/analytics 응답 캐시 TTL(초). dashboard 와 같은 값이지만 정책이 달라 별도로 둔다
#: (여기는 ADMIN + scope='all' 에만 캐시한다 — 아래 get_analytics 주석 참조).
_ANALYTICS_CACHE_TTL = 30


def _analytics_cache_key(
    *,
    period: str,
    group_by: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """전사(ADMIN, scope='all') 응답 전용 캐시 키.

    ⚠️ scope 를 키에 넣지 않는다 — 이 키는 `scope='all'` 인 경우에만 쓰이므로 상수다.
    대신 **호출부가 role 을 검사**해 ADMIN 이 아니면 캐시를 아예 쓰지 않는다.
    키에 scope 만 넣고 role 을 빼면 TEAM_LEADER 가 ADMIN 의 전사 응답을 받는다.

    ⚠️ 반대로 응답을 바꾸는 **나머지 모든** 파라미터는 키에 들어가야 한다. 일자 범위를 빼면
    TTL(30초) 동안 다른 범위를 골라도 앞 범위의 숫자가 그대로 나간다 — 범위를 바꿔도 화면이
    안 바뀐다. 월 질의(범위 없음)의 키 모양은 예전 그대로다.
    """
    key = f"analytics:global:{period}:{group_by}"
    if start_date or end_date:
        key += f":{start_date or ''}:{end_date or ''}"
    return key


async def _cache_get(request: Request, key: str):
    """캐시 조회 — 실패는 miss(fail-open). 캐시는 정합성의 근거가 아니다.

    객체(dict)가 아닌 값도 miss 다. 아래 _cache_set 수정 전 파드가 쓴 항목은 모델의 ``str()``
    문자열이라, 롤링 배포 중 새 파드가 그걸 읽어 내보내면 같은 고장이 TTL 동안 이어진다.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        cached = json.loads(raw) if raw else None
        return cached if isinstance(cached, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("analytics cache get failed key=%s err=%s", key, exc)
        return None


async def _cache_set(request: Request, key: str, value: object) -> None:
    """캐시 저장 — 미스 경로가 보내는 JSON 과 **같은 모양**으로 넣는다.

    ⚠️ ``json.dumps(value, default=str)`` 로 넣으면 안 된다. value 는 Pydantic 모델
    (AnalyticsResponse)이라 JSON 직렬화가 안 되고, ``default=str`` 가 모델의 ``str()``
    (``"period='2026-09' ... cost_summary=CostSummary(...)"``)을 문자열 하나로 넣는다. 그러면
    적중 응답 본문이 객체가 아니라 그 문자열이 되어, admin-ui(당시 분석 화면)가 ``cost_summary`` 를
    못 찾고 Server Components 에러로 깨졌다 — 첫 요청만 정상이고 TTL 동안 같은 요청은 전부.
    ``jsonable_encoder`` 는 FastAPI 가 이 라우트(response_model 없음)의 반환값을 직렬화할 때
    쓰는 바로 그 함수라 적중·미스 응답이 같다(Decimal 은 모델 JSON 모드대로 문자열).
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    try:
        await redis.setex(key, _ANALYTICS_CACHE_TTL, json.dumps(jsonable_encoder(value)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("analytics cache set failed key=%s err=%s", key, exc)



@router.get("")
async def get_analytics(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    # ⚠️ Literal 로 좁혀 잘못된 값이 422 로 실패하게 한다. 예전 description 은
    #    'department' 를 광고했지만 analytics_service 에는 그 분기가 없어서, 요청하면
    #    조용히 by_model 응답이 돌아왔다(쓰레기 값과 바이트 단위로 동일 = 무증상 오답).
    #    admin-ui 의 유일한 호출부(lib/actions/dashboard.ts fetchAnalytics)도 이 집합 안에서만 보낸다.
    group_by: Literal["model", "team", "user"] = Query("model"),
    # ⚠️ scope 는 닫힌 집합이다 — 그 밖의 값은 422. 예전엔 자유 문자열이라 'xyz' 같은 값이
    #    서비스의 어느 분기도 타지 않고 전사 범위로 떨어졌다(서비스도 스스로 400 으로 닫는다).
    scope: str = Query("all", description="all | team:{uuid}", pattern=_SCOPE_PATTERN),
    # ⚠️ 날짜 범위는 Annotated 로 선언한다 — 기본값이 진짜 None 이라 라우트 함수를 직접
    #    부르는 회귀 테스트(test_critical_analytics_cache_role_isolation)가 FieldInfo 객체를
    #    받지 않는다. 둘 다 주면 그 KST 일자 범위만 집계하고, 한쪽만 주면 400 이다.
    start_date: Annotated[
        str | None, Query(description="YYYY-MM-DD (KST). end_date 와 함께 주면 그 일자 범위만 집계")
    ] = None,
    end_date: Annotated[
        str | None, Query(description="YYYY-MM-DD (KST). start_date 와 함께 주면 그 일자 범위만 집계")
    ] = None,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service

    # ⚠️ 캐시는 **ADMIN + scope='all'** 에만 적용한다. 그 조합만이 행위자와 무관한
    #    (전사) 응답이기 때문이다.
    #
    #    캐시를 걸면 안 되는 이유가 성능 트레이드오프가 아니라 **데이터 격리**다:
    #    이 엔드포인트는 require_admin_or_team_leader 이고, 같은 `scope='all'` 이
    #    ADMIN 에겐 GLOBAL·TEAM_LEADER 에겐 본인 팀으로 갈린다
    #    (analytics_service.get_analytics 의 role 분기). 그래서 키에 행위자를 넣지 않고
    #    `analytics:{period}:{group_by}:{scope}` 로 캐시하면, ADMIN 이 먼저 조회해
    #    채워둔 **전사 비용·사용자·모델·추이**를 TTL 안에 동일 파라미터로 요청한
    #    TEAM_LEADER 가 그대로 받는다 — ForbiddenError 격리와 위 fail-closed 불변식이
    #    통째로 무력화된다.
    #    ⇒ TEAM_LEADER 요청과 team: 범위 요청은 아예 캐시하지 않는다(cache_key=None).
    #      role 을 키에 섞는 방법도 있지만, TEAM_LEADER 는 팀마다 응답이 달라
    #      team_id 까지 넣어야 하고 그러면 적중률이 거의 0 이다 — 복잡도만 늘고 이득이 없다.
    cache_key = None
    if user.role == UserRole.ADMIN and scope == "all":
        cache_key = _analytics_cache_key(
            period=period, group_by=group_by, start_date=start_date, end_date=end_date
        )
        if (cached := await _cache_get(request, cache_key)) is not None:
            return cached

    result = await svc.get_analytics(
        session,
        period=period,
        group_by=group_by,
        scope=scope,
        start_date=start_date,
        end_date=end_date,
        actor=user,
    )
    if cache_key is not None:
        await _cache_set(request, cache_key, result)
    return result


@router.get("/usage/by-user-model")
async def get_usage_by_user_model(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    date: str = Query(description="YYYY-MM-DD format (within period month)"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """User × Model 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 메인 테이블용."""
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service
    return await svc.get_usage_by_user_model(session, period=period, date=date)


@router.get("/usage/by-user")
async def get_usage_by_user(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    date: str = Query(description="YYYY-MM-DD format (within period month)"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """User 단위 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 요약 테이블용."""
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service
    return await svc.get_usage_by_user(session, period=period, date=date)
