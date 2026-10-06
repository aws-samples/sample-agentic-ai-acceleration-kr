"""Reaching into a harness sandbox with InvokeAgentRuntimeCommand.

Two things about this API are counter-intuitive and both were measured against
the live service (2026-08-14, academic_writer-TuVwX10293):

- `agentRuntimeArn` must be the **harness** ARN. Passing the companion runtime
  ARN — the one GetHarness reports, and the one every other code path uses — is
  rejected with "managed by a harness … use the relevant harness ID instead".
- `body` is a modelled structure `{command, timeout}`, not the opaque bytes blob
  InvokeAgentRuntime takes. Sending bytes is a ParamValidationError.

Both are pinned here because either mistake fails only at runtime, in a path that
runs after a turn has already finished.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.harness_command_client import (  # noqa: E402
    CommandResult,
    HarnessCommandClient,
)

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/writer-abc"
SESSION = "thread-1" + "0" * 30


class StubBoto:
    def __init__(self, stream=None, error=None):
        self.calls = []
        self._stream = stream if stream is not None else []
        self._error = error

    def invoke_agent_runtime_command(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return {"stream": self._stream}


def client_with(stub: StubBoto) -> HarnessCommandClient:
    client = HarnessCommandClient(region_name="us-east-1")
    client._client = stub
    return client


def test_passes_the_harness_arn_and_a_structured_body():
    stub = StubBoto()
    client_with(stub).run_sync(HARNESS_ARN, SESSION, "/bin/bash -c 'echo hi'", timeout=45)

    call = stub.calls[0]
    assert call["agentRuntimeArn"] == HARNESS_ARN
    assert call["runtimeSessionId"] == SESSION
    assert call["body"] == {"command": "/bin/bash -c 'echo hi'", "timeout": 45}
    assert isinstance(call["body"], dict)


def test_collects_stdout_stderr_exit_code_and_status():
    stream = [
        {"chunk": {"contentStart": {}}},
        {"chunk": {"contentDelta": {"stdout": '{"files": '}}},
        {"chunk": {"contentDelta": {"stdout": "[]}", "stderr": "warn\n"}}},
        {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
    ]
    result = client_with(StubBoto(stream)).run_sync(HARNESS_ARN, SESSION, "x")

    assert result.stdout == '{"files": []}'
    assert result.stderr == "warn\n"
    assert result.exit_code == 0
    assert result.status == "COMPLETED"
    assert result.ok


def test_a_timed_out_command_is_not_ok():
    stream = [{"chunk": {"contentStop": {"exitCode": None, "status": "TIMED_OUT"}}}]
    assert not client_with(StubBoto(stream)).run_sync(HARNESS_ARN, SESSION, "x").ok


def test_events_without_the_chunk_wrapper_are_read_too():
    """The shape is documented with a `chunk` wrapper; tolerate its absence."""
    stream = [{"contentDelta": {"stdout": "ok"}}, {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}]
    assert client_with(StubBoto(stream)).run_sync(HARNESS_ARN, SESSION, "x").stdout == "ok"


@pytest.mark.asyncio
async def test_run_awaits_the_blocking_call_and_returns_its_result():
    """`run` is what every caller on the event loop uses.

    The blocking work must reach the same code path `run_sync` takes — the
    hand-off to a worker thread is what keeps a 0.5s command from stalling every
    other request in the process.
    """
    stub = StubBoto([{"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}])
    result = await client_with(stub).run(HARNESS_ARN, SESSION, "/bin/bash -c 'echo hi'")

    assert result.ok
    assert stub.calls[0]["agentRuntimeArn"] == HARNESS_ARN
