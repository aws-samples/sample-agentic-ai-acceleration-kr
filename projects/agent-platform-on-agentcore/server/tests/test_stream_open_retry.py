"""A throttled stream open is retried; nothing else is.

300 chats started together hit AgentCore's 25/s "new Runtime session" cap and,
with the streaming client's retries off, 4 got an error frame (2026-10-18). The
open is refused before anything runs, so retrying it cannot run a turn twice."""
import asyncio
import os
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.agentcore_client as ac  # noqa: E402


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeAgentRuntime")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(ac.asyncio, "sleep", instant)


def test_throttling_is_retried_until_the_open_succeeds():
    calls = []

    def invoke(**kw):
        calls.append(kw)
        if len(calls) < 3:
            raise _err("ThrottlingException")
        return {"ok": True}

    assert asyncio.run(ac.open_stream_with_retry(invoke, agentRuntimeArn="a")) == {"ok": True}
    assert len(calls) == 3


def test_other_errors_are_not_retried():
    calls = []

    def invoke(**kw):
        calls.append(1)
        raise _err("AccessDeniedException")

    with pytest.raises(ClientError):
        asyncio.run(ac.open_stream_with_retry(invoke))
    assert calls == [1]


def test_it_gives_up_after_the_attempts():
    calls = []

    def invoke(**kw):
        calls.append(1)
        raise _err("ThrottlingException")

    with pytest.raises(ClientError):
        asyncio.run(ac.open_stream_with_retry(invoke))
    assert len(calls) == ac._OPEN_ATTEMPTS
