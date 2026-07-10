# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import PaginatedResponse, UserRole


# ── Requests ──


class DepartmentCreateRequest(BaseModel):
    name: str = Field(max_length=255)
    org_id: str | None = None  # defaults to the single org in MVP


class TeamCreateRequest(BaseModel):
    name: str = Field(max_length=255)
    department_id: str


class SetLeaderRequest(BaseModel):
    user_id: str


class TransferUserRequest(BaseModel):
    team_id: str


# ── Responses ──


class DepartmentResponse(BaseModel):
    id: str
    name: str
    org_id: str
    created_at: datetime


class TeamResponse(BaseModel):
    id: str
    name: str
    department_id: str
    leader_user_id: str | None = None
    created_at: datetime


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: UserRole
    team_id: str | None = None
    team_name: str | None = None
    is_active: bool
    created_at: datetime


class UserListResponse(PaginatedResponse[UserResponse]):
    pass


class TeamListItem(BaseModel):
    id: str
    name: str
    department_id: str
    department_name: str | None = None
    leader_user_id: str | None = None
    member_count: int = 0


class TeamListResponse(BaseModel):
    items: list[TeamListItem]


# ── Org Tree ──


class OrgNodeMeta(BaseModel):
    member_count: int | None = None
    leader_name: str | None = None
    email: str | None = None
    role: UserRole | None = None
    team_name: str | None = None


class OrgTreeNode(BaseModel):
    id: str
    name: str
    type: str  # ORGANIZATION | DEPARTMENT | TEAM | USER
    children: list["OrgTreeNode"] = []
    meta: OrgNodeMeta


OrgTreeNode.model_rebuild()
