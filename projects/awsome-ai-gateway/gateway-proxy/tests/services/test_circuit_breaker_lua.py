# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import pytest
from pathlib import Path

LUA = (Path(__file__).resolve().parents[2] / "src/app/redis_scripts/circuit_breaker.lua").read_text()


@pytest.mark.asyncio
async def test_trip_requires_min_calls(fake_redis):
    pmid = "global.anthropic.claude-opus-4-8"
    # 4 failures (< min_calls=5) must NOT open
    for _ in range(4):
        opened = await fake_redis.eval(
            LUA, 3,
            f"cb:{pmid}:fail", f"cb:{pmid}:total", f"cb:{pmid}:open",
            "30", "5", "0.5", "30000", "1", "1000",
        )
        assert opened == 0
    # 5th failure: total>=5 and rate=1.0>=0.5 -> open
    opened = await fake_redis.eval(
        LUA, 3,
        f"cb:{pmid}:fail", f"cb:{pmid}:total", f"cb:{pmid}:open",
        "30", "5", "0.5", "30000", "1", "1000",
    )
    assert opened == 1
    assert await fake_redis.exists(f"cb:{pmid}:open") == 1


@pytest.mark.asyncio
async def test_successes_dilute_rate(fake_redis):
    # 5 successes then 1 failure: total=6, fails=1, rate=0.166 < 0.5 -> stays closed
    pmid = "m2"
    for _ in range(5):
        await fake_redis.eval(LUA, 3, f"cb:{pmid}:fail", f"cb:{pmid}:total", f"cb:{pmid}:open",
                              "30", "5", "0.5", "30000", "0", "1000")
    opened = await fake_redis.eval(LUA, 3, f"cb:{pmid}:fail", f"cb:{pmid}:total", f"cb:{pmid}:open",
                                   "30", "5", "0.5", "30000", "1", "1000")
    assert opened == 0


@pytest.mark.asyncio
async def test_already_open_short_circuits(fake_redis):
    pmid = "m3"
    await fake_redis.set(f"cb:{pmid}:open", "1")
    opened = await fake_redis.eval(LUA, 3, f"cb:{pmid}:fail", f"cb:{pmid}:total", f"cb:{pmid}:open",
                                   "30", "5", "0.5", "30000", "0", "1000")
    assert opened == 1  # returns 1 because already open
    assert await fake_redis.exists("cb:m3:total:1") == 0
