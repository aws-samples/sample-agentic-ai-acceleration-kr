# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt

from app.core.db import AsyncSessionLocal
from app.models.auth import UserRole
from app.services.service_token_service import (
    SERVICE_TOKEN_PREFIX,
    ServiceTokenService,
    hash_token,
)

logger = structlog.get_logger()


@dataclass
class CurrentUser:
    user_id: uuid.UUID
    email: str
    role: UserRole
    team_id: uuid.UUID | None
    is_service_token: bool = False
    service_token_id: uuid.UUID | None = None


class JWTVerifier:
    """Loads Admin JWT public keys at startup and verifies RS256 tokens."""

    def __init__(self) -> None:
        self._public_keys: dict[str, dict] = {}  # kid -> {pem, issuer, audience, algorithm}

    def load_configs(self, configs: list[dict]) -> None:
        for cfg in configs:
            kid = str(cfg["id"])
            self._public_keys[kid] = {
                "pem": cfg["public_key_pem"],
                "issuer": cfg["issuer"],
                "audience": cfg["audience"],
                "algorithm": cfg["algorithm"],
            }
        logger.info("jwt_verifier.loaded", key_count=len(self._public_keys))

    def verify(self, token: str) -> dict:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        # If kid present, look up specific key; otherwise try all active keys
        candidates = [self._public_keys[kid]] if kid and kid in self._public_keys else list(self._public_keys.values())

        if not candidates:
            raise JWTError("No matching public key found")

        last_error: JWTError | None = None
        for key_cfg in candidates:
            try:
                payload = jwt.decode(
                    token,
                    key_cfg["pem"],
                    algorithms=[key_cfg["algorithm"]],
                    issuer=key_cfg["issuer"],
                    audience=key_cfg["audience"],
                )
                return payload
            except JWTError as e:
                last_error = e
                continue

        raise last_error  # type: ignore[misc]


def _extract_token(request: Request) -> str:
    """Extract JWT from Authorization header or admin_jwt cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]

    # Fallback: read from cookie (admin-ui sends cookies via server-side fetch)
    cookie_header = request.headers.get("Cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("admin_jwt="):
            return part[len("admin_jwt="):]

    raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")


def _parse_dev_token(token: str) -> dict | None:
    """Parse dev-mode JWT (format: dev.<base64url-payload>.sig). Returns None if not a dev token."""
    import base64
    import json
    import os

    if not token.startswith("dev.") or os.getenv("DEV_LOGIN_ENABLED") != "true":
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    try:
        payload_b64 = parts[1]
        # Add padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return None


async def get_current_user(request: Request) -> CurrentUser:
    token = _extract_token(request)

    # Dev token shortcut (DEV_LOGIN_ENABLED=true only)
    dev_payload = _parse_dev_token(token)
    if dev_payload is not None:
        return CurrentUser(
            user_id=uuid.UUID("00000000-0000-4000-a000-000000000010"),
            email=dev_payload.get("email", "admin@dev.local"),
            role=UserRole(dev_payload.get("role", "ADMIN")),
            team_id=None,
        )

    # Service token shortcut: external systems call with `Bearer svc-...`.
    # Resolved against auth.service_tokens (sha256 hash). Synthesizes an ADMIN
    # identity. Non-`svc-` tokens fall through to the JWT path below (unchanged).
    if token.startswith(SERVICE_TOKEN_PREFIX):
        svc_service = ServiceTokenService()
        async with AsyncSessionLocal() as session:
            svc_tok = await svc_service.verify(session, hash_token(token))
        if svc_tok is None:
            raise HTTPException(status_code=401, detail="Invalid or expired service token")
        return CurrentUser(
            user_id=uuid.UUID("00000000-0000-4000-a000-000000000011"),
            email="service-token@admin.local",
            role=UserRole.ADMIN,
            team_id=None,
            is_service_token=True,
            service_token_id=svc_tok.id,
        )

    verifier: JWTVerifier = request.app.state.jwt_verifier

    try:
        payload = verifier.verify(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    team_id_raw = payload.get("team_id")
    return CurrentUser(
        user_id=uuid.UUID(payload["sub"]),
        email=payload.get("email", ""),
        role=UserRole(payload["role"]),
        team_id=uuid.UUID(team_id_raw) if team_id_raw else None,
    )


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


async def require_admin_or_team_leader(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role not in (UserRole.ADMIN, UserRole.TEAM_LEADER):
        raise HTTPException(status_code=403, detail="Admin or Team Leader role required")
    return user


def require_team_leader_of(team_id: uuid.UUID):
    """Returns a dependency that verifies the user is ADMIN or TEAM_LEADER of the specified team."""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role == UserRole.ADMIN:
            return user
        if user.role == UserRole.TEAM_LEADER and user.team_id == team_id:
            return user
        raise HTTPException(status_code=403, detail="Not authorized for this team")

    return _check
