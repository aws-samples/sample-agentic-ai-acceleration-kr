# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.schemas.domain import DegradationLevel

if TYPE_CHECKING:
    pass


class DegradationState(Protocol):
    level: DegradationLevel

    def is_db_available(self) -> bool: ...
    def is_redis_available(self) -> bool: ...
    def can_serve(self) -> bool: ...


class HealthyState:
    level = DegradationLevel.HEALTHY

    def is_db_available(self) -> bool:
        return True

    def is_redis_available(self) -> bool:
        return True

    def can_serve(self) -> bool:
        return True


class DBDegradedState:
    level = DegradationLevel.DB_DEGRADED

    def is_db_available(self) -> bool:
        return False

    def is_redis_available(self) -> bool:
        return True

    def can_serve(self) -> bool:
        return True


class RedisDegradedState:
    level = DegradationLevel.REDIS_DEGRADED

    def is_db_available(self) -> bool:
        return True

    def is_redis_available(self) -> bool:
        return False

    def can_serve(self) -> bool:
        return True


class BothDegradedState:
    level = DegradationLevel.BOTH_DEGRADED

    def is_db_available(self) -> bool:
        return False

    def is_redis_available(self) -> bool:
        return False

    def can_serve(self) -> bool:
        return False
