# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import pytest


@pytest.fixture
async def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    from fakeredis import aioredis as fr
    client = fr.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()
