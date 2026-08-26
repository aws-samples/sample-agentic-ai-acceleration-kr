# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""SSO credential acquisition and SigV4 pre-signed STS request (BR-VK-02~04)."""

from __future__ import annotations

import platform
import socket
import subprocess
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(component="api-key-helper")


def check_sso_session() -> bool:
    """Verify active AWS SSO session via sts get-caller-identity (BR-VK-02).

    Returns True if session is valid, False otherwise.
    """
    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_sso_session_expiry() -> datetime | None:
    """Return the current AWS credential expiry time (FR-2.2 SSO session linkage).

    Uses `aws configure export-credentials --format env-no-export` and parses
    AWS_CREDENTIAL_EXPIRATION. Returns None for IAM users or on any error.
    """
    try:
        result = subprocess.run(
            ["aws", "configure", "export-credentials", "--format", "env-no-export"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("AWS_CREDENTIAL_EXPIRATION="):
                expiration = line.split("=", 1)[1].strip()
                return datetime.fromisoformat(expiration.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def get_device_name() -> str:
    """Get device hostname for VK request (BR-VK-04)."""
    try:
        return socket.gethostname()
    except Exception:
        try:
            return platform.node()
        except Exception:
            return "unknown-device"


def create_presigned_sts_request(region: str | None = None) -> dict:
    """Create a SigV4 pre-signed STS GetCallerIdentity request (BR-VK-03).

    Returns dict with 'url' and 'headers' keys.
    Raises RuntimeError if SSO credentials are unavailable.
    """
    import botocore.session
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest

    session = botocore.session.get_session()

    try:
        credentials = session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available. Run `aws sso login`.")
        frozen = credentials.get_frozen_credentials()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to get AWS credentials. Run `aws sso login`. Detail: {exc}"
        ) from exc

    resolved_region = region or session.get_config_variable("region") or "ap-northeast-2"

    request = AWSRequest(
        method="GET",
        url=f"https://sts.{resolved_region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
    )
    SigV4QueryAuth(frozen, "sts", resolved_region, expires=60).add_auth(request)

    return {
        "url": request.url,
        "headers": {},
    }
