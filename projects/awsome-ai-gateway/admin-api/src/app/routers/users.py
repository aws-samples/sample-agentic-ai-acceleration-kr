# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.services.user_allowed_client_service import UserAllowedClientService
from app.services.user_allowed_model_service import UserAllowedModelService
from app.schemas.common import PaginationMeta
from app.schemas.models import AllowedModelsResponse, AllowedModelsSetRequest
from app.schemas.users import (
    DepartmentCreateRequest,
    DepartmentResponse,
    OrgTreeNode,
    SetLeaderRequest,
    TeamCreateRequest,
    TeamListResponse,
    TeamResponse,
    TransferUserRequest,
    UserListResponse,
    UserResponse,
)

router = APIRouter(prefix="/admin", tags=["User & Team Management"])


@router.post("/departments", response_model=DepartmentResponse, status_code=201)
async def create_department(
    request: Request,
    body: DepartmentCreateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.user_team_service import UserTeamService

    svc: UserTeamService = request.app.state.user_team_service
    return await svc.create_department(
        session,
        name=body.name,
        org_id=uuid.UUID(body.org_id) if body.org_id else None,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team(
    request: Request,
    body: TeamCreateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: UserTeamService = request.app.state.user_team_service
    return await svc.create_team(
        session,
        name=body.name,
        department_id=uuid.UUID(body.department_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.put("/teams/{team_id}/leader", response_model=TeamResponse)
async def set_team_leader(
    request: Request,
    team_id: str,
    body: SetLeaderRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: UserTeamService = request.app.state.user_team_service
    return await svc.set_team_leader(
        session,
        team_id=uuid.UUID(team_id),
        user_id=uuid.UUID(body.user_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.post("/teams/{team_id}/force-reauth")
async def force_reauth_team(
    request: Request,
    team_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """팀 멤버 전원의 ACTIVE VK 를 일괄 revoke.

    오프보딩/보안 사고/즉시 정책 반영 용도. 사용자는 다음 호출 시 401 을
    받으며 Claude Code 재실행 시 새 VK 가 자동 발급됨 (Cognito 그룹 재평가).
    """
    from app.services.key_service import KeyService

    svc: KeyService = request.app.state.key_service
    revoked_count = await svc.force_reauth_team(
        session,
        team_id=uuid.UUID(team_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    return {"revoked_count": revoked_count}


@router.put("/users/{user_id}/team", response_model=UserResponse)
async def transfer_user(
    request: Request,
    user_id: str,
    body: TransferUserRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: UserTeamService = request.app.state.user_team_service
    return await svc.transfer_user(
        session,
        user_id=uuid.UUID(user_id),
        new_team_id=uuid.UUID(body.team_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.get("/users/teams", response_model=TeamListResponse)
async def list_teams(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.user_team_service import UserTeamService

    svc: UserTeamService = request.app.state.user_team_service
    items = await svc.list_teams(session)
    return TeamListResponse(items=items)


@router.get("/users/tree", response_model=OrgTreeNode | None)
async def get_users_tree(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.user_team_service import UserTeamService

    svc: UserTeamService = request.app.state.user_team_service
    return await svc.get_org_tree(session)


@router.get("/teams/{team_id}/allowed-models", response_model=AllowedModelsResponse)
async def list_team_allowed_models(
    request: Request,
    team_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.team_allowed_model_service import TeamAllowedModelService

    svc: TeamAllowedModelService = request.app.state.team_allowed_model_service
    return await svc.list_for_team(session, team_id=uuid.UUID(team_id))


@router.put("/teams/{team_id}/allowed-models", response_model=AllowedModelsResponse)
async def set_team_allowed_models(
    request: Request,
    team_id: str,
    body: AllowedModelsSetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.team_allowed_model_service import TeamAllowedModelService

    svc: TeamAllowedModelService = request.app.state.team_allowed_model_service
    return await svc.set_for_team(
        session,
        team_id=uuid.UUID(team_id),
        model_aliases=body.model_aliases,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.delete("/teams/{team_id}/allowed-models", response_model=AllowedModelsResponse)
async def clear_team_allowed_models(
    request: Request,
    team_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.team_allowed_model_service import TeamAllowedModelService

    svc: TeamAllowedModelService = request.app.state.team_allowed_model_service
    return await svc.clear_for_team(
        session,
        team_id=uuid.UUID(team_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.post("/users/sync-cognito")
async def sync_cognito(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """Cognito User Pool 에서 그룹/사용자를 가져와 DB 동기화.

    Admin 전용. Cognito ListUsers/ListGroups API 를 호출하여
    로컬 조직 구조를 최신 상태로 갱신합니다.
    """
    from app.core.config import get_settings
    from app.services.cognito_sync_service import CognitoSyncService

    settings = get_settings()
    if not settings.COGNITO_USER_POOL_ID:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=400,
            content={"error": "COGNITO_USER_POOL_ID not configured"},
        )

    import boto3
    cognito_client = boto3.client(
        "cognito-idp", region_name=settings.COGNITO_REGION
    )
    svc = CognitoSyncService(cognito_client)
    result = await svc.sync_all(session)

    return {
        "groups_synced": result.groups_synced,
        "users_created": result.users_created,
        "users_updated": result.users_updated,
        "users_deactivated": result.users_deactivated,
        "teams_deleted": result.teams_deleted,
        "errors": result.errors,
    }


def _build_cognito_sync_service():
    """COGNITO_USER_POOL_ID 확인 + boto3 cognito client 로 CognitoSyncService 생성.

    미설정 시 (None, error_dict) 반환. 정상 시 (svc, None).
    """
    from app.core.config import get_settings
    from app.services.cognito_sync_service import CognitoSyncService

    settings = get_settings()
    if not settings.COGNITO_USER_POOL_ID:
        return None, {"error": "COGNITO_USER_POOL_ID not configured"}
    import boto3
    client = boto3.client("cognito-idp", region_name=settings.COGNITO_REGION)
    return CognitoSyncService(client), None


@router.post("/users/sync-cognito/user/{username}")
async def sync_cognito_user(
    username: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """단일 사용자만 Cognito 와 동기화 (260626_comm_customer 항목1-a).

    사내 관리서비스가 Cognito 신규 생성 직후 호출 — DB 에 없어도 신규 생성된다.
    Cognito 에 없으면(AI도구 미사용 전환으로 삭제된 케이스) DB 에서 email 로 찾아
    is_active=False 로 soft-delete 한다(OIDC 유저 한정). 응답의 user_id 로 후속
    처리(개인별 allowed_clients/allowed_models)를 이어갈 수 있다.
    동기 처리(단일 사용자 ~sub-second). 전역 reconciliation 미수행.
    svc- 서비스 토큰 또는 admin JWT 로 호출 가능(require_admin).
    """
    from fastapi.responses import JSONResponse

    svc, err = _build_cognito_sync_service()
    if err is not None:
        return JSONResponse(status_code=400, content=err)
    result = await svc.sync_user(session, username)
    return {
        "username": username,
        "user_id": result.user_id,
        "users_created": result.users_created,
        "users_updated": result.users_updated,
        "users_deactivated": result.users_deactivated,
        "errors": result.errors,
    }


@router.post("/users/sync-cognito/group/{groupname:path}")
async def sync_cognito_group(
    groupname: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """단일 그룹만 동기화: 팀 확보 + 그 그룹 멤버 upsert. 전역 정리 미수행."""
    from fastapi.responses import JSONResponse

    svc, err = _build_cognito_sync_service()
    if err is not None:
        return JSONResponse(status_code=400, content=err)
    result = await svc.sync_group(session, groupname)
    return {
        "groupname": groupname,
        "groups_synced": result.groups_synced,
        "users_created": result.users_created,
        "users_updated": result.users_updated,
        "errors": result.errors,
    }


@router.get("/users", response_model=UserListResponse)
async def list_users(
    request: Request,
    team_id: str | None = None,
    department_id: str | None = None,
    is_active: bool | None = None,
    email: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """사용자 목록 조회. email 필터는 exact 매칭(대소문자 무시) — Cognito username
    (= gateway email) 으로 단일 유저를 찾는 용도. unique 컬럼이라 0/1건."""
    svc: UserTeamService = request.app.state.user_team_service
    items, has_more = await svc.list_users(
        session,
        team_id=uuid.UUID(team_id) if team_id else None,
        department_id=uuid.UUID(department_id) if department_id else None,
        is_active=is_active,
        email=email,
        cursor=cursor,
        limit=limit,
    )
    last_id = items[-1].id if items else None
    return UserListResponse(
        items=items,
        pagination=PaginationMeta(cursor=last_id if has_more else None, limit=limit, has_more=has_more),
    )


class AllowedClientsBody(BaseModel):
    clients: list[str]  # subset of ["claude-code","cowork"]; [] = both allowed


class AllowedClientsResponse(BaseModel):
    user_id: str
    clients: list[str]   # [] = both allowed (default)


@router.get("/users/{user_id}/allowed-clients", response_model=AllowedClientsResponse)
async def get_allowed_clients(
    user_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = UserAllowedClientService(session)
    return AllowedClientsResponse(user_id=str(user_id), clients=await svc.get(user_id))


@router.put("/users/{user_id}/allowed-clients", response_model=AllowedClientsResponse)
async def set_allowed_clients(
    request: Request,
    user_id: uuid.UUID,
    body: AllowedClientsBody,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    # cache_mgr + key_service 주입 → 변경 즉시 해당 user 의 VK auth 캐시 무효화
    # (미주입 시 VK_CACHE_TTL(~300s) 지연으로 자연 반영).
    svc = UserAllowedClientService(
        session,
        cache_mgr=request.app.state.cache_mgr,
        key_service=request.app.state.key_service,
    )
    try:
        clients = await svc.set(user_id, body.clients, admin.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await session.commit()
    return AllowedClientsResponse(user_id=str(user_id), clients=clients)


@router.delete("/users/{user_id}/allowed-clients", status_code=204)
async def clear_allowed_clients(
    request: Request,
    user_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = UserAllowedClientService(
        session,
        cache_mgr=request.app.state.cache_mgr,
        key_service=request.app.state.key_service,
    )
    await svc.clear(user_id, admin.user_id)
    await session.commit()


# ── per-USER model allow-list (overrides team_allowed_models) ────────────────
# 우선순위 user>team>none 은 AuthContext 스냅샷 시점(key_service/auth_service)에서
# 해결된다. 여기서는 user 행만 관리한다. PUT body 는 team 과 동일 AllowedModelsSetRequest.


class UserAllowedModelsResponse(BaseModel):
    user_id: str
    model_aliases: list[str]  # [] = override 없음 → 팀 정책으로 폴백


@router.get("/users/{user_id}/allowed-models", response_model=UserAllowedModelsResponse)
async def get_user_allowed_models(
    user_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = UserAllowedModelService(session)
    return UserAllowedModelsResponse(
        user_id=str(user_id), model_aliases=await svc.get(user_id)
    )


@router.put("/users/{user_id}/allowed-models", response_model=UserAllowedModelsResponse)
async def set_user_allowed_models(
    request: Request,
    user_id: uuid.UUID,
    body: AllowedModelsSetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = UserAllowedModelService(
        session,
        cache_mgr=request.app.state.cache_mgr,
        key_service=request.app.state.key_service,
    )
    try:
        aliases = await svc.set(
            user_id, body.model_aliases, admin,
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=request.headers.get("x-request-id", ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await session.commit()
    # Codex MF3: invalidate VK auth cache AFTER commit — a gateway request in the
    # delete→commit window must not repopulate the OLD (looser) policy. Post-commit
    # ensures the rebuilt snapshot reads the new restriction.
    await svc.invalidate_user_vk_cache(user_id)
    return UserAllowedModelsResponse(user_id=str(user_id), model_aliases=aliases)


@router.delete("/users/{user_id}/allowed-models", status_code=204)
async def clear_user_allowed_models(
    request: Request,
    user_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = UserAllowedModelService(
        session,
        cache_mgr=request.app.state.cache_mgr,
        key_service=request.app.state.key_service,
    )
    await svc.clear(
        user_id, admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    await session.commit()
    await svc.invalidate_user_vk_cache(user_id)
