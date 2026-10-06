"""
Models for user authentication (Cognito login / token refresh, OIDC session)
"""
from typing import List, Optional
from pydantic import BaseModel


class LoginRequest(BaseModel):
    """Request model for user login."""
    username: str
    password: str


class RefreshRequest(BaseModel):
    """Request model for refreshing an access token."""
    refresh_token: str


class AuthResponse(BaseModel):
    """Response model for login / refresh."""
    success: bool
    access_token: Optional[str] = None
    id_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    error: Optional[str] = None


class SessionProfile(BaseModel):
    """POST /api/auth/session — the server's view of who just signed in.

    The browser cannot derive the role from an Entra id_token on its own (the
    admin group is whatever the deployment named it), so after an OIDC login
    it asks here once and renders from this.
    """
    sub: str
    username: str = ""
    email: str = ""
    name: str = ""
    role: str = "user"
    groups: List[str] = []
    provider: str = ""
