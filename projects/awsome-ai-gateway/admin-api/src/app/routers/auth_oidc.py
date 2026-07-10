# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""POST /v1/auth/exchange — OIDC JWT → Virtual Key.

cli (gateway-cli) 가 IDP 로부터 받은 access JWT 를 이 엔드포인트로 교환.
Admin JWT 인증 불필요 (사용자 신원은 OIDC JWT 자체로).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.core.db import get_db_session
from app.core.oidc_verifier import OIDCConfigError
from app.schemas.cli import VirtualKeyIssueResponse
from app.schemas.oidc import OIDCExchangeRequest
from app.services.oidc_service import (
    OIDCAuthError,
    OIDCNotProvisionableError,
    OIDCService,
)

router = APIRouter(prefix="/v1/auth", tags=["OIDC Auth"])


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "invalid_token", "message": "Missing Bearer token"}},
        )
    token = auth[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "invalid_token", "message": "Empty Bearer token"}},
        )
    return token


@router.post("/exchange", response_model=VirtualKeyIssueResponse)
async def exchange_oidc_jwt(
    request: Request,
    body: OIDCExchangeRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """OIDC access JWT 를 Virtual Key 로 교환.

    Auth: Authorization: Bearer <OIDC access JWT>
    """
    svc: OIDCService | None = request.app.state.oidc_service
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "oidc_disabled",
                    "message": "OIDC is not configured. Set OIDC_ISSUER_URL + OIDC_AUDIENCE.",
                }
            },
        )

    token = _extract_bearer(request)
    redis = request.app.state.redis

    try:
        return await svc.exchange_jwt_for_vk(
            session,
            redis=redis,
            token=token,
            device_name=body.device_name,
            sso_session_expires_at=body.sso_session_expires_at,
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=request.headers.get("x-request-id", ""),
        )
    except OIDCAuthError as e:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "invalid_token", "message": str(e)}},
        )
    except OIDCNotProvisionableError as e:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "not_provisionable", "message": str(e)}},
        )
    except OIDCConfigError as e:
        raise HTTPException(
            status_code=503,
            detail={"error": {"type": "idp_unavailable", "message": str(e)}},
        )
