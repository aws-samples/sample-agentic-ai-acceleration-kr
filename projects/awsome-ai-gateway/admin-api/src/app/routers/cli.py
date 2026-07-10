# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
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

# CLI dist directory (relative to project root)
_CLI_DIST_DIR = Path(os.environ.get("CLI_DIST_DIR", "/app/cli-dist"))


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


@router.get("/downloads")
async def list_downloads(request: Request):
    """Return available CLI download items with real file info."""
    import hashlib

    version = "0.1.0"
    items = []
    for os_name, arch, ext in [
        ("linux", "amd64", "tar.gz"),
        ("darwin", "arm64", "tar.gz"),
        ("windows", "amd64", "zip"),
    ]:
        filename = f"gateway-cli-{version}-{os_name}-{arch}.{ext}"
        filepath = _CLI_DIST_DIR / filename
        file_size = filepath.stat().st_size if filepath.is_file() else 0
        checksum = ""
        if filepath.is_file():
            checksum = hashlib.sha256(filepath.read_bytes()).hexdigest()

        items.append({
            "os": os_name,
            "arch": arch,
            "filename": filename,
            "download_url": f"/cli/download/{os_name}/{arch}",
            "version": version,
            "file_size_bytes": file_size,
            "checksum_sha256": checksum,
        })
    return items


@router.get("/download/{os_name}/{arch}")
async def download_binary(os_name: str, arch: str):
    """Download the gateway-cli package for the given OS/arch."""
    version = "0.1.0"
    ext = "zip" if os_name == "windows" else "tar.gz"
    media = "application/zip" if os_name == "windows" else "application/gzip"
    filename = f"gateway-cli-{version}-{os_name}-{arch}.{ext}"
    filepath = _CLI_DIST_DIR / filename

    if not filepath.is_file():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Package not found: {filename}")

    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type=media,
    )
