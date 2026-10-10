#!/usr/bin/env python3
"""
Manage the MCP gateway's targets for Terraform.

`awscc` exposes `awscc_bedrockagentcore_gateway` but has no gateway *target*
resource (as of provider 1.94.0), even though the CloudFormation type exists — so
the targets are reconciled here through boto3, driven by the module's provisioners.

Three target kinds are supported:
  * a Lambda target (--lambda-arn --schema) for the platform's own tools,
  * a native connector target (--connector-id) for a managed AgentCore tool such
    as the built-in Web Search connector, and
  * an MCP server target (--mcp-endpoint) for an MCP server hosted on AgentCore
    Runtime, signed with the gateway role (SigV4, service bedrock-agentcore).

Usage:
  gateway_target.py upsert --gateway-id ID --name N --lambda-arn ARN --schema FILE
  gateway_target.py upsert --gateway-id ID --name N --connector-id web-search
  gateway_target.py upsert --gateway-id ID --name N --mcp-endpoint URL
  gateway_target.py delete --gateway-id ID --name N
"""
import argparse
import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

CONTROL_SERVICE = "bedrock-agentcore-control"
READY_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 5

# The gateway role's grants are eventually consistent: CreateGatewayTarget can
# reject a freshly attached policy as missing, so retry before giving up. A Lambda
# target trips "lacks permission"; a web-search connector fails its InvokeWebSearch
# check with an authorization message instead, so both substrings must be retried.
IAM_PROPAGATION_ATTEMPTS = 12
IAM_PROPAGATION_INTERVAL_SECONDS = 10
IAM_PROPAGATION_ERRORS = ("lacks permission", "not authorized", "AccessDenied")


def client(region):
    return boto3.client(CONTROL_SERVICE, region_name=region)


def find_target(ctl, gateway_id, name):
    token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = ctl.list_gateway_targets(**kwargs)
        for target in resp.get("items", []):
            if target.get("name") == name:
                return target
        token = resp.get("nextToken")
        if not token:
            return None


def wait_ready(ctl, gateway_id, target_id):
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        target = ctl.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )
        status = target.get("status")
        if status == "READY":
            return
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "FAILED"):
            raise SystemExit(
                f"target {target_id} entered {status}: {target.get('statusReasons')}"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"target {target_id} not READY within {READY_TIMEOUT_SECONDS}s")


def _credential_providers(args):
    """Outbound auth: always the gateway role.

    Lambda and connector targets need only the type, because the gateway already
    knows which service it calls. An MCP server target is an arbitrary URL, so the
    SigV4 service name has to be given — `bedrock-agentcore` for a server hosted on
    AgentCore Runtime. Without it the gateway cannot sign the call and the runtime
    answers 403.
    """
    provider = {"credentialProviderType": "GATEWAY_IAM_ROLE"}
    if args.mcp_endpoint:
        provider["credentialProvider"] = {
            "iamCredentialProvider": {"service": args.iam_service, "region": args.region}
        }
    return [provider]


def _create_with_iam_retry(ctl, args, target_configuration, description):
    last_error = None
    for attempt in range(IAM_PROPAGATION_ATTEMPTS):
        try:
            return ctl.create_gateway_target(
                gatewayIdentifier=args.gateway_id,
                name=args.name,
                description=description,
                targetConfiguration=target_configuration,
                credentialProviderConfigurations=_credential_providers(args),
            )
        except ClientError as exc:
            message = exc.response.get("Error", {}).get("Message", "")
            if not any(needle in message for needle in IAM_PROPAGATION_ERRORS):
                raise
            last_error = exc
            print(
                f"waiting for IAM propagation ({attempt + 1}/{IAM_PROPAGATION_ATTEMPTS})",
                file=sys.stderr,
            )
            time.sleep(IAM_PROPAGATION_INTERVAL_SECONDS)
    raise SystemExit(f"gateway role permission never propagated: {last_error}")


def _target_configuration(args):
    """Build the target config for whichever kind was requested.

    A --connector-id yields a native AgentCore connector target (the managed Web
    Search tool), an --mcp-endpoint an MCP server target; otherwise a Lambda target
    carrying the schema in tools.json.
    """
    if args.mcp_endpoint:
        # DYNAMIC forwards every tools/list to the server, so a redeployed server's
        # new tools show up without a resync (SynchronizeGatewayTargets rejects
        # dynamic targets anyway).
        return {
            "mcp": {
                "mcpServer": {
                    "endpoint": args.mcp_endpoint,
                    "listingMode": args.listing_mode,
                }
            }
        }
    if args.connector_id:
        # The connector exposes a single tool whose name is the configuration name
        # (e.g. WebSearch). Pin a connector version when one is given; leaving it
        # empty lets the gateway use the connector's default version. `source.version`
        # is modelled only in newer SDKs (botocore >= 1.43.78) — older ones raise
        # ParamValidationError on it rather than silently dropping it.
        source = {"connectorId": args.connector_id}
        if args.connector_version:
            source["version"] = args.connector_version
        return {
            "mcp": {
                "connector": {
                    "source": source,
                    "configurations": [
                        {"name": args.connector_config_name, "parameterValues": {}}
                    ],
                }
            }
        }
    with open(args.schema) as handle:
        tools = json.load(handle)
    drop = set(args.drop_tool or [])
    if drop:
        tools = [t for t in tools if t.get("name") not in drop]
    return {
        "mcp": {
            "lambda": {
                "lambdaArn": args.lambda_arn,
                "toolSchema": {"inlinePayload": tools},
            }
        }
    }


def cmd_upsert(args):
    ctl = client(args.region)
    target_configuration = _target_configuration(args)
    if args.mcp_endpoint:
        description = "MCP server hosted on AgentCore Runtime"
    elif args.connector_id:
        description = f"AgentCore managed connector: {args.connector_id}"
    else:
        description = "Tools implemented by the platform tool Lambda"


    existing = find_target(ctl, args.gateway_id, args.name)
    if existing:
        target_id = existing["targetId"]
        ctl.update_gateway_target(
            gatewayIdentifier=args.gateway_id,
            targetId=target_id,
            name=args.name,
            targetConfiguration=target_configuration,
            credentialProviderConfigurations=_credential_providers(args),
        )
        wait_ready(ctl, args.gateway_id, target_id)
        print(f"updated target {target_id}", file=sys.stderr)
        return

    resp = _create_with_iam_retry(ctl, args, target_configuration, description)
    target_id = resp["targetId"]
    wait_ready(ctl, args.gateway_id, target_id)
    print(f"created target {target_id}", file=sys.stderr)


def cmd_delete(args):
    ctl = client(args.region)
    existing = find_target(ctl, args.gateway_id, args.name)
    if not existing:
        print("target not found, nothing to delete", file=sys.stderr)
        return
    ctl.delete_gateway_target(
        gatewayIdentifier=args.gateway_id, targetId=existing["targetId"]
    )
    print(f"deleted target {existing['targetId']}", file=sys.stderr)


def cmd_tag(args):
    ctl = client(args.region)
    tags = dict(kv.split("=", 1) for kv in args.tags)
    ctl.tag_resource(resourceArn=args.gateway_arn, tags=tags)
    print(f"tagged {args.gateway_arn} with {tags}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("upsert", cmd_upsert), ("delete", cmd_delete), ("tag", cmd_tag)):
        p = sub.add_parser(name)
        if name == "tag":
            p.add_argument("--gateway-arn", required=True)
            p.add_argument("--tags", nargs="+", required=True)
            p.add_argument("--region", required=True)
        else:
            p.add_argument("--gateway-id", required=True)
            p.add_argument("--name", required=True)
            # terraform always passes --region; the default is for hand runs, where
            # a fixed us-east-1 would silently address a different deployment.
            p.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
            p.add_argument("--lambda-arn", default="")
            p.add_argument("--schema", default="")
            p.add_argument(
                "--connector-id",
                default="",
                help="Native AgentCore connector id (e.g. web-search). Mutually "
                "exclusive with --lambda-arn/--schema.",
            )
            p.add_argument(
                "--connector-config-name",
                default="WebSearch",
                help="Configuration name for the connector; also the exposed tool name.",
            )
            p.add_argument(
                "--connector-version",
                default="",
                help="Pin the connector to a semantic version (e.g. 1.2.0). Empty "
                "uses the connector's default version. Requires botocore >= 1.43.78.",
            )
            p.add_argument(
                "--drop-tool",
                action="append",
                default=[],
                help="Tool name to omit from a Lambda target's schema (repeatable).",
            )
            p.add_argument(
                "--mcp-endpoint",
                default="",
                help="Endpoint of an MCP server hosted on AgentCore Runtime. Mutually "
                "exclusive with --lambda-arn and --connector-id.",
            )
            p.add_argument(
                "--listing-mode",
                default="DYNAMIC",
                choices=("DYNAMIC", "DEFAULT"),
                help="tools/list handling for an MCP server target.",
            )
            p.add_argument(
                "--iam-service",
                default="bedrock-agentcore",
                help="SigV4 service name the MCP server expects.",
            )
        p.set_defaults(func=handler)

    args = parser.parse_args()
    try:
        args.func(args)
    except ClientError as exc:
        raise SystemExit(f"AWS error: {exc}")


if __name__ == "__main__":
    main()
