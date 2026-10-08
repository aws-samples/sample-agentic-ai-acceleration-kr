#!/usr/bin/env python3
"""
Manage an AWS Agent Registry for Terraform.

Agent Registry has no CloudFormation type and no Terraform (aws/awscc)
resource, and even the AWS CLI omits the registry operations — boto3 is the
only available surface. The service lives in the `agent-registry` namespace
(control plane `agent-registry-control`, served at `.api.aws`); the older
`bedrock-agentcore` preview namespace shuts down on 2026-10-30. This script is
therefore driven by the module's `null_resource` provisioners.

Usage:
  registry.py create --name N [--description D] [--auto-approval true|false]
  registry.py delete --name N
  registry.py lookup --name N     # prints {"registry_id":..,"registry_arn":..} for external data source
"""
import argparse
import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

CONTROL_SERVICE = "agent-registry-control"

# CreateRegistry is asynchronous: it returns while status is CREATING.
READY_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 5


def client(region):
    # botocore resolves agent-registry-control to .amazonaws.com, but the service
    # is served at .api.aws (registry-faq) — set the endpoint explicitly.
    return boto3.client(
        CONTROL_SERVICE,
        region_name=region,
        endpoint_url=f"https://{CONTROL_SERVICE}.{region}.api.aws",
    )


def find_by_name(ctl, name):
    """Return the registry summary matching name, or None. Names are not unique to AWS, so pick the oldest."""
    matches = []
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = ctl.list_registries(**kwargs)
        for reg in resp.get("registries", []):
            if reg.get("name") == name and reg.get("status") not in ("DELETING", "DELETE_FAILED"):
                matches.append(reg)
        token = resp.get("nextToken")
        if not token:
            break
    if not matches:
        return None
    return sorted(matches, key=lambda r: r.get("createdAt") or 0)[0]


def wait_ready(ctl, registry_id):
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        reg = ctl.get_registry(registryId=registry_id)
        status = reg.get("status")
        if status == "READY":
            return reg
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"):
            raise SystemExit(
                f"registry {registry_id} entered {status}: {reg.get('statusReason')}"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"registry {registry_id} not READY within {READY_TIMEOUT_SECONDS}s")


def approval_config(auto_approval: str) -> dict:
    """CreateRegistry shape: empty rules = manual curator approval."""
    return {
        "autoApprovalRules": ["APPROVE_ALL"] if auto_approval == "true" else []
    }


def sync_approval(ctl, registry_id: str, auto_approval: str) -> None:
    """Apply approval policy to an existing registry (UpdateRegistry PATCH).

    Create is idempotent and used to adopt existing registries; without this,
    flipping auto_approval in Terraform would never change a live registry.

    Agent Registry expects autoApprovalRules (same shape as CreateRegistry),
    not a boolean autoApproval field.
    """
    ctl.update_registry(
        registryId=registry_id,
        approvalConfiguration={
            "optionalValue": approval_config(auto_approval)
        },
    )
    wait_ready(ctl, registry_id)


def cmd_create(args):
    ctl = client(args.region)

    # Idempotent: adopt an existing registry with the same name instead of
    # creating a duplicate, so re-running apply after a partial failure is safe.
    existing = find_by_name(ctl, args.name)
    if existing:
        registry_id = existing["registryId"]
        wait_ready(ctl, registry_id)
        sync_approval(ctl, registry_id, args.auto_approval)
        print(
            f"registry already exists: {registry_id} "
            f"(auto_approval={args.auto_approval})",
            file=sys.stderr,
        )
        return

    params = {
        "name": args.name,
        "discoveryConfiguration": {"authorizerType": "AWS_IAM"},
        "approvalConfiguration": approval_config(args.auto_approval),
    }
    if args.description:
        params["description"] = args.description

    resp = ctl.create_registry(**params)
    registry_arn = resp["registryArn"]
    registry_id = registry_arn.rsplit("/", 1)[-1]
    wait_ready(ctl, registry_id)
    print(f"created registry {registry_id}", file=sys.stderr)


def cmd_delete(args):
    ctl = client(args.region)
    existing = find_by_name(ctl, args.name)
    if not existing:
        print(f"registry {args.name} not found, nothing to delete", file=sys.stderr)
        return
    registry_id = existing["registryId"]

    # A registry cannot be deleted while it still holds records.
    token = None
    while True:
        kwargs = {"registryId": registry_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = ctl.list_registry_records(**kwargs)
        for rec in resp.get("registryRecords", []):
            try:
                ctl.delete_registry_record(registryId=registry_id, recordId=rec["recordId"])
            except ClientError as exc:
                print(f"failed to delete record {rec['recordId']}: {exc}", file=sys.stderr)
        token = resp.get("nextToken")
        if not token:
            break

    ctl.delete_registry(registryId=registry_id)
    print(f"deleted registry {registry_id}", file=sys.stderr)


def cmd_lookup(args):
    ctl = client(args.region)
    existing = find_by_name(ctl, args.name)
    if not existing:
        # `external` data sources must emit a flat string map; empty values let
        # Terraform surface a readable error rather than crashing on a null.
        print(json.dumps({"registry_id": "", "registry_arn": ""}))
        return
    print(
        json.dumps(
            {
                "registry_id": existing["registryId"],
                "registry_arn": existing["registryArn"],
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("create", cmd_create), ("delete", cmd_delete), ("lookup", cmd_lookup)):
        p = sub.add_parser(name)
        p.add_argument("--name", required=True)
        # terraform always passes --region; the default is for hand runs, where
        # a fixed us-east-1 would silently address a different deployment.
        p.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
        p.add_argument("--description", default="")
        p.add_argument("--auto-approval", default="true", choices=["true", "false"])
        p.set_defaults(func=handler)

    args = parser.parse_args()
    try:
        args.func(args)
    except ClientError as exc:
        raise SystemExit(f"AWS error: {exc}")


if __name__ == "__main__":
    main()
