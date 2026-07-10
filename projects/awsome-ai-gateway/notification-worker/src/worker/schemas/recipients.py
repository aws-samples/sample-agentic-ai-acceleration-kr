# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class RecipientRole(str, Enum):
    AFFECTED_USER = "affected_user"
    TEAM_LEADER = "team_leader"
    ADMIN = "admin"


class Recipient(BaseModel):
    email: str
    name: str
    user_id: str | None = None
    role: RecipientRole


class RenderedEmail(BaseModel):
    subject: str
    html_body: str
    recipient: Recipient
