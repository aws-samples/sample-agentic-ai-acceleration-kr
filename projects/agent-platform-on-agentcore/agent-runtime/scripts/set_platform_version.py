"""Move an AgentCore Runtime between platform versions (V1 <-> V2).

V2 starts each instance from a prepared snapshot instead of re-initialising the
container, so cold starts stay flat regardless of image size. Nothing in our
deploy path can set it: the starter toolkit (`agentcore launch`, up to 0.3.14)
and CloudFormation/CDK do not expose `platformVersion`, and the aws CLI on the
deploy host predates the flag. UpdateAgentRuntime does accept it, and an update
that omits it keeps the current value — so one explicit flip here survives every
later `agentcore launch --auto-update-on-conflict`.

UpdateAgentRuntime requires `agentRuntimeArtifact` and `roleArn` and overwrites
any optional field it does not receive, so the current configuration is read
back with GetAgentRuntime and replayed verbatim with only `platformVersion`
changed. Each flip creates a new runtime version; the DEFAULT endpoint follows
it, so there is no downtime, but a V2 update prepares a snapshot and takes
minutes to reach READY. Calling update/delete before that raises
ConflictException, hence the waits on both sides.

Usage (deploy.sh calls this after launch; mcp-apps-server/README documents it):
    python scripts/set_platform_version.py --name bap_default --region ap-northeast-1
    python scripts/set_platform_version.py --name bap_default --region ap-northeast-1 --wait-only
"""
import argparse
import sys
import time
from typing import Any, Callable, Dict, Optional

# Fields GetAgentRuntime returns that UpdateAgentRuntime also accepts. Anything
# else in the Get response is read-only and would fail request validation.
_REPLAYED_FIELDS = (
    "agentRuntimeArtifact",
    "roleArn",
    "authorizerConfiguration",
    "capacityProviderConfiguration",
    "description",
    "environmentVariables",
    "filesystemConfigurations",
    "lifecycleConfiguration",
    "metadataConfiguration",
    "networkConfiguration",
    "protocolConfiguration",
    "requestHeaderConfiguration",
)

DEFAULT_PLATFORM_VERSION = "V1"  # what the service assumes when the key is absent
POLL_SECONDS = 10
# A V2 snapshot takes "several minutes" per the docs; give it a generous ceiling.
MAX_WAIT_SECONDS = 30 * 60


def build_update_request(get_response: Dict[str, Any], platform_version: str) -> Dict[str, Any]:
    """Turn a GetAgentRuntime response into an UpdateAgentRuntime request that
    changes only the platform version. Optional fields the runtime does not
    carry are left out rather than sent as empty values."""
    request: Dict[str, Any] = {"agentRuntimeId": get_response["agentRuntimeId"]}
    for key in _REPLAYED_FIELDS:
        if key in get_response:
            request[key] = get_response[key]
    request["platformVersion"] = platform_version
    return request


def _wait_until_terminal(client, agent_runtime_id: str, sleep: Callable[[float], None]) -> Dict[str, Any]:
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while True:
        current = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)
        status = current.get("status", "")
        if status == "READY" or status.endswith("FAILED"):
            return current
        if time.monotonic() > deadline:
            raise RuntimeError(f"{agent_runtime_id} still {status} after {MAX_WAIT_SECONDS}s")
        sleep(POLL_SECONDS)


def ensure_platform_version(
    client,
    agent_runtime_id: str,
    platform_version: str,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Flip `agent_runtime_id` to `platform_version` if it is not there already,
    then block until the runtime is READY. Returns the final state."""
    current = _wait_until_terminal(client, agent_runtime_id, sleep)
    if current["status"] != "READY":
        raise RuntimeError(f"{agent_runtime_id} is {current['status']}; not touching it")

    before = current.get("platformVersion", DEFAULT_PLATFORM_VERSION)
    if before == platform_version:
        return {"changed": False, "platformVersion": before, "status": current["status"]}

    client.update_agent_runtime(**build_update_request(current, platform_version))

    after = _wait_until_terminal(client, agent_runtime_id, sleep)
    if after["status"] != "READY":
        raise RuntimeError(
            f"{agent_runtime_id} ended {after['status']} while moving {before} -> {platform_version}"
        )
    return {
        "changed": True,
        "platformVersion": after.get("platformVersion", DEFAULT_PLATFORM_VERSION),
        "status": after["status"],
    }


def find_runtime_id(client, name: str) -> Optional[str]:
    """Resolve a runtime name to its id. ListAgentRuntimes has no name filter and
    names are only unique per region, so match exactly."""
    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        page = client.list_agent_runtimes(**kwargs)
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == name:
                return rt["agentRuntimeId"]
        token = page.get("nextToken")
        if not token:
            return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="agentRuntimeName (e.g. bap_default)")
    parser.add_argument("--region", required=True)
    parser.add_argument("--version", default="V2", choices=("V1", "V2"), help="target platform version")
    parser.add_argument(
        "--wait-only",
        action="store_true",
        help="only wait until the runtime is READY; do not change the platform version",
    )
    args = parser.parse_args(argv)

    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=args.region)
    runtime_id = find_runtime_id(client, args.name)
    if runtime_id is None:
        print(f"no runtime named {args.name} in {args.region}", file=sys.stderr)
        return 1

    if args.wait_only:
        state = _wait_until_terminal(client, runtime_id, time.sleep)
        print(f"{args.name} ({runtime_id}): {state['status']}")
        return 0 if state["status"] == "READY" else 1

    result = ensure_platform_version(client, runtime_id, args.version)
    verb = "moved to" if result["changed"] else "already on"
    print(f"{args.name} ({runtime_id}): {verb} platform version {result['platformVersion']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
