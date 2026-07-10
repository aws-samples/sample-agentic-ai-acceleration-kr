# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""통합 테스트 픽스처.

pytest.mark.integration으로 마킹된 테스트는 실제 PostgreSQL과 Redis가 필요하다.
Docker 없이 실행할 경우 --integration 옵션 없이는 자동으로 스킵된다.

실행 방법:
    # 로컬 (Docker 있을 때):
    pytest tests/integration/ -m integration --integration

    # CI 환경:
    DB_URL=postgresql+asyncpg://... REDIS_URL=redis://... pytest tests/integration/ --integration
"""
from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires running PostgreSQL and Redis)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--integration"):
        skip_integration = pytest.mark.skip(reason="Pass --integration to run integration tests")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)


@pytest.fixture(scope="session")
def db_url() -> str:
    return os.environ.get(
        "DB_URL",
        "postgresql+asyncpg://notification_worker_user:notification_worker_password_change_me@localhost:5432/gateway",
    )


@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")
