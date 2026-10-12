"""Runs a shell command inside a harness session's own container.

`InvokeAgentRuntimeCommand` executes in the same container, filesystem and
environment as the agent, on the same `runtimeSessionId` — which is how a file the
agent produced becomes reachable at all. Verified live on 2026-08-14: root, cwd
`/home`, a file created during the turn still present afterwards, round trip
0.4–0.8s, and the session alive for `idleRuntimeSessionTimeout` (900s) past the
turn.

Two API shapes are easy to get wrong and both fail only at runtime:

- **`agentRuntimeArn` must be the harness ARN.** The companion runtime ARN — the
  one `GetHarness` reports and every other path in this codebase uses — is
  rejected: "managed by a harness … use the relevant harness ID instead".
- **`body` is a structure**, `{command, timeout}`, not the bytes blob
  `InvokeAgentRuntime` takes.

This is the only file that touches the command API. It is also arbitrary command
execution as root inside the agent's container, so nothing here builds a command:
callers pass one, and the only builder is `harness_sandbox_scripts`, whose output
contains no interpolated data.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

import boto3

from agents.agentcore_client import stream_boto_config
from agents.agent_config import AgentConfig

logger = logging.getLogger(__name__)

# How many presigned URLs may ride in one upload command. The request body (the
# command string) has a hard limit of 65536 bytes. Measured on 2026-08-14 with
# fixed-length padded URLs: n=40 accepted (command_bytes=53925), n=80 rejected
# (command_bytes=106781). Real presigned URLs vary in length; 20 is deliberate
# headroom under the 65536 limit, not a value that could be raised to 40 safely.
UPLOAD_BATCH_SIZE = 20


@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    status: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.status == "COMPLETED"


class HarnessCommandClient:
    """Thin, single-purpose wrapper over InvokeAgentRuntimeCommand."""

    def __init__(self, region_name: Optional[str] = None):
        self.region_name = AgentConfig.get_region(region_name)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            # Same long read timeout as the streaming clients: the response is an
            # event stream and a slow command would otherwise be severed.
            self._client = boto3.client(
                "bedrock-agentcore",
                region_name=self.region_name,
                config=stream_boto_config(),
            )
        return self._client

    def run_sync(
        self,
        harness_arn: str,
        session_id: str,
        command: str,
        timeout: int = 60,
    ) -> CommandResult:
        """Blocking. Used directly only from paths that have no event loop."""
        response = self.client.invoke_agent_runtime_command(
            agentRuntimeArn=harness_arn,
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
            contentType="application/json",
            accept="application/vnd.amazon.eventstream",
            body={"command": command, "timeout": timeout},
        )

        stdout: List[str] = []
        stderr: List[str] = []
        result = CommandResult()
        for event in response.get("stream", []):
            chunk = event.get("chunk") or event
            delta = chunk.get("contentDelta")
            if delta:
                stdout.append(delta.get("stdout") or "")
                stderr.append(delta.get("stderr") or "")
            stop = chunk.get("contentStop")
            if stop:
                result.exit_code = stop.get("exitCode")
                result.status = stop.get("status")

        result.stdout = "".join(stdout)
        result.stderr = "".join(stderr)
        return result

    async def run(
        self,
        harness_arn: str,
        session_id: str,
        command: str,
        timeout: int = 60,
    ) -> CommandResult:
        """Off the loop's thread: the call blocks for its whole duration."""
        return await asyncio.to_thread(
            self.run_sync, harness_arn, session_id, command, timeout
        )
