#!/usr/bin/env python3
"""
Manage an AWS Agent Registry for Terraform.

The registry is driven through boto3 from the module's `null_resource`
provisioners. AWS now has CloudFormation (AWS::AgentRegistry::Registry) and a
Terraform resource (aws_agentregistry_registry, provider 6.64+), but this stack
pins aws ~> 5 and relies on adopting an existing registry by name, so the
script stays until that migration. The service lives in the `agent-registry`
namespace (control plane `agent-registry-control`, served at `.api.aws`); the
older `bedrock-agentcore` preview namespace shuts down on 2026-10-30.

Usage:
  registry.py create --name N [--description D] [--auto-approval true|false]
                     [--kms-key-arn ARN] [--custom-metadata-schema-json JSON]
  registry.py delete --name N
  registry.py lookup --name N     # prints {"registry_id":..,"registry_arn":..,"mcp_endpoint":..}

`--custom-metadata-schema-json` is a JSON object: record type ("DEFAULT", "MCP",
"AGENT", "SKILL", "CUSTOM", "GATEWAY") => JSON Schema *string*. The schema is
applied on create and re-synced on adopt; AWS only accepts additive changes.
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


def schema_config(schema_json: str) -> dict:
    """`customMetadataSchemaConfiguration` from the module's record-type => schema map.

    "DEFAULT" becomes `defaultSchema`; every other key is a per-record-type
    override. Each value stays the JSON *string* AWS stores; it is parsed here
    only to fail early on a typo instead of at apply time inside AWS.
    """
    mapping = json.loads(schema_json or "{}")
    if mapping is None:
        # `jsonencode(null)` from Terraform when the caller passes no schema.
        return {}
    if not isinstance(mapping, dict):
        raise SystemExit("custom metadata schema must be a JSON object of recordType => schema")
    config = {}
    overrides = []
    for record_type, schema in mapping.items():
        if not schema:
            continue
        json.loads(schema)  # validate; AWS wants the string form
        if record_type == "DEFAULT":
            config["defaultSchema"] = schema
        else:
            overrides.append({"recordType": record_type, "schema": schema})
    if overrides:
        config["recordTypeSchemaOverrides"] = overrides
    return config


def merge_schema_config(current: dict, desired: dict) -> dict:
    """`desired` laid over the registry's `current` schema configuration so the
    result is additive: every field and enum value the registry already saved
    is kept, overrides it already has stay, and `desired` decides the rest.

    Why: AWS rejects an UpdateRegistry that drops a saved field, so a module
    default that stops listing a field (docs_url was retired on 2026-10-10)
    would otherwise fail against every registry created before the change.
    Required-ness is the one thing AWS lets a schema give up, so `required`
    comes from `desired` alone. A retyped field is left to AWS to reject.
    """
    def merge_one(cur_json, new_json):
        if not new_json:
            return cur_json  # an override the module no longer lists: left as saved
        cur = json.loads(cur_json) if cur_json else {}
        new = json.loads(new_json)
        props = dict(cur.get("properties") or {})
        for name, prop in (new.get("properties") or {}).items():
            merged = dict(prop)
            old_enum = (props.get(name) or {}).get("enum")
            if old_enum and "enum" in merged:
                merged["enum"] = list(merged["enum"]) + [v for v in old_enum if v not in merged["enum"]]
            props[name] = merged
        out = {**cur, **new, "properties": props}
        if "required" in new:
            out["required"] = new["required"]
        else:
            out.pop("required", None)
        return json.dumps(out)

    current_overrides = {o["recordType"]: o["schema"] for o in (current.get("recordTypeSchemaOverrides") or [])}
    desired_overrides = {o["recordType"]: o["schema"] for o in (desired.get("recordTypeSchemaOverrides") or [])}
    merged: dict = {}
    if current.get("defaultSchema") or desired.get("defaultSchema"):
        merged["defaultSchema"] = merge_one(current.get("defaultSchema"), desired.get("defaultSchema"))
    overrides = [
        {"recordType": rt, "schema": merge_one(current_overrides.get(rt), desired_overrides.get(rt))}
        for rt in list(current_overrides) + [rt for rt in desired_overrides if rt not in current_overrides]
    ]
    if overrides:
        merged["recordTypeSchemaOverrides"] = overrides
    return merged


def sync_schema(ctl, registry_id: str, schema_json: str, current=None) -> None:
    """Apply the metadata schema to an existing registry (UpdateRegistry PATCH).

    The desired schema is merged over the live one (`current`, the GetRegistry
    response) so the call is always additive where it can be. AWS allows only
    additive changes (new fields, new enum values, new overrides, toggling
    required-ness) and rejects the rest with a ValidationException. That is
    reported, not raised: an apply that changes an unrelated variable must not
    fail on a schema it cannot shrink.
    """
    config = schema_config(schema_json)
    if not config:
        return
    live = (current or {}).get("customMetadataSchemaConfiguration") or {}
    config = merge_schema_config(live, config)
    try:
        ctl.update_registry(
            registryId=registry_id,
            customMetadataSchemaConfiguration={"optionalValue": config},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ValidationException":
            raise
        print(
            f"custom metadata schema not applied to {registry_id}: {exc}\n"
            "AWS accepts only additive schema changes; keep every saved field and enum value.",
            file=sys.stderr,
        )
        return
    wait_ready(ctl, registry_id)


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
        live = wait_ready(ctl, registry_id)
        sync_approval(ctl, registry_id, args.auto_approval)
        sync_schema(ctl, registry_id, getattr(args, "custom_metadata_schema_json", ""), current=live)
        if getattr(args, "kms_key_arn", ""):
            print(
                "kms_key_arn is creation-time only in AWS; the existing registry keeps its key",
                file=sys.stderr,
            )
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
    kms_key_arn = getattr(args, "kms_key_arn", "")
    if kms_key_arn:
        params["encryptionConfiguration"] = {"kmsKeyArn": kms_key_arn}
    schema = schema_config(getattr(args, "custom_metadata_schema_json", ""))
    if schema:
        params["customMetadataSchemaConfiguration"] = schema

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
        print(json.dumps({"registry_id": "", "registry_arn": "", "mcp_endpoint": ""}))
        return
    print(
        json.dumps(
            {
                "registry_id": existing["registryId"],
                "registry_arn": existing["registryArn"],
                # The registry's own MCP server on the data plane; what the
                # gateway module attaches as a target and IDEs connect to.
                "mcp_endpoint": (
                    f"https://agent-registry.{args.region}.api.aws/registry/"
                    f"{existing['registryId']}/mcp"
                ),
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
        p.add_argument(
            "--kms-key-arn",
            default="",
            help="Customer managed key for the registry; honoured on create only.",
        )
        p.add_argument(
            "--custom-metadata-schema-json",
            default="{}",
            help='JSON object recordType => JSON Schema string ("DEFAULT" = default schema).',
        )
        p.set_defaults(func=handler)

    args = parser.parse_args()
    try:
        args.func(args)
    except ClientError as exc:
        raise SystemExit(f"AWS error: {exc}")


if __name__ == "__main__":
    main()
