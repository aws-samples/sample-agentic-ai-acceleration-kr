# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from worker.handlers.base import BaseHandler


class SecurityHandler(BaseHandler):
    """auth_failure_spike / permission_violation / suspicious_usage 이벤트 핸들러 (admin 전용)."""
