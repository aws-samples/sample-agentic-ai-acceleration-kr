# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations


from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.core.db import get_db_session
from app.schemas.cli import (
    SetupRequest,
    SetupResponse,
    VirtualKeyIssueRequest,
    VirtualKeyIssueResponse,
)

router = APIRouter(prefix="/cli", tags=["CLI Integration"])

@router.post("/auth/virtual-key", response_model=VirtualKeyIssueResponse)
async def issue_virtual_key(
    request: Request,
    body: VirtualKeyIssueRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """Issue a Virtual Key via STS Pre-signed GetCallerIdentity verification.

    No JWT auth required — CLI authenticates via AWS IAM credentials.
    """
    from app.services.cli_service import CLIService

    svc: CLIService = request.app.state.cli_service
    redis = request.app.state.redis
    return await svc.verify_sts_and_issue_key(
        session,
        redis=redis,
        data=body,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.post("/setup", response_model=SetupResponse)
async def get_setup_config(
    request: Request,
    body: SetupRequest,
):
    """Return tool-specific configuration for CLI onboarding."""
    svc: CLIService = request.app.state.cli_service
    return svc.get_setup_config(body)


