# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from worker.handlers.base import BaseHandler


class SystemHandler(BaseHandler):
    """degradation_mode / provider_error / service_health_change 이벤트 핸들러 (admin 전용)."""
