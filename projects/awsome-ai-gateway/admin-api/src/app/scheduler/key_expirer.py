# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""VK Expiry Job — marks time-expired Virtual Keys as EXPIRED in DB.

Redis cleanup is handled automatically by the TTL set on key:vk:{hash}
at issuance time (key_service.issue_key). This job only updates DB status
so that admin queries and audit logs reflect the correct state.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import KeyStatus, VirtualKey

logger = structlog.get_logger()


async def expire_virtual_keys(session: AsyncSession) -> int:
    """Bulk-expire ACTIVE VKs whose expires_at has passed.

    Returns the number of rows updated.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        update(VirtualKey)
        .where(VirtualKey.status == KeyStatus.ACTIVE)
        .where(VirtualKey.expires_at < now)
        .values(status=KeyStatus.EXPIRED)
    )
    result = await session.execute(stmt)
    count: int = result.rowcount  # type: ignore[assignment]
    if count > 0:
        logger.info("key_expirer.expired", count=count)
    await session.commit()
    return count
