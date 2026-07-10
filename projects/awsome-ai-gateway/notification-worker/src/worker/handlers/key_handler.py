# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from worker.handlers.base import BaseHandler


class KeyHandler(BaseHandler):
    """key_expiring / key_expired / key_revoked 이벤트 핸들러."""
