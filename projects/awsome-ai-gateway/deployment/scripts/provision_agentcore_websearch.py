#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Provision the AgentCore Gateway + managed WebSearch target for gateway-proxy.

Architecture C: gateway-proxy is the MCP *caller* (SigV4/IRSA) → this gateway →
managed WebSearch connector. Inbound auth is AWS_IAM (SigV4), NOT Cognito — the pod
has IRSA creds, so no JWT/Cognito is needed (differs from the sibling
gonsoomoon-ml/web-search-mcp which used CUSTOM_JWT because CoWork can't SigV4).

Idempotent (re-run safe): reuses existing role / gateway / target. The managed
WebSearch connector is us-east-1-only and billed ~$7/1,000 queries (idle ≈ $0).

Subcommands:
    deploy    (default) create/reuse IAM exec role + Gateway(AWS_IAM) + WebSearch target,
              then print AGENTCORE_GATEWAY_URL / AGENTCORE_TARGET_ID for helm values.
    status    print current resource state.
    teardown  delete target → gateway → exec role (stop billing/exposure).

The SDK may not yet model the `mcp.connector` targetConfiguration; on ParamValidationError
we fall back to a raw SigV4 POST to the control-plane REST endpoint (verified pattern).

Env overrides: REGION (default us-east-1), GW_NAME, ROLE_NAME, TARGET_NAME,
CALLER_ROLE_ARN (the gateway-proxy IRSA role allowed to InvokeGateway; informational).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError, ParamValidationError

REGION = os.environ.get("REGION", "us-east-1")  # WebSearch connector is us-east-1 only
GW_NAME = os.environ.get("GW_NAME", "llm-gateway-websearch")
ROLE_NAME = os.environ.get("ROLE_NAME", "llm-gateway-dev-agentcore-websearch-gw")
TARGET_NAME = os.environ.get("TARGET_NAME", "web-search-tool")
CONNECTOR_ID = "web-search"
TOOL_CONFIG_NAME = "WebSearch"
WEB_SEARCH_TOOL_ARN = f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v1"


def log(msg: str) -> None:
    print(f"[provision] {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"[provision] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _control():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


# ── IAM exec role (gateway → WebSearch) ───────────────────────────────────────
def ensure_role(acct: str, dry: bool) -> str:
    arn = f"arn:aws:iam::{acct}:role/{ROLE_NAME}"
    if dry:
        log(f"[dry-run] IAM exec role {ROLE_NAME} (InvokeGateway + InvokeWebSearch)")
        return arn
    iam = boto3.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Sid": "AllowAgentCore", "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": acct},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{acct}:gateway/*"}}}]}
    # The web-search connector doc places BOTH actions on the gateway service role.
    perms = {"Version": "2012-10-17", "Statement": [
        {"Sid": "InvokeGateway", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
         "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{acct}:gateway/*"},
        {"Sid": "InvokeWebSearch", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeWebSearch",
         "Resource": WEB_SEARCH_TOOL_ARN}]}
    try:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                        Description="AgentCore Gateway exec role for managed Web Search")
        log(f"IAM role created: {ROLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            log(f"IAM role reused: {ROLE_NAME}")
        else:
            raise
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="web-search-invoke",
                        PolicyDocument=json.dumps(perms))
    return arn


# ── Gateway (AWS_IAM inbound) ─────────────────────────────────────────────────
def find_gateway():
    gw = _control()
    g = next((x for x in gw.list_gateways().get("items", []) if x.get("name") == GW_NAME), None)
    return gw.get_gateway(gatewayIdentifier=g["gatewayId"]) if g else None


def ensure_gateway(role_arn: str, dry: bool):
    existing = find_gateway()
    if existing:
        log(f"Gateway reused: {existing['gatewayId']} ({existing.get('status')})")
        return existing["gatewayId"], existing["gatewayUrl"]
    if dry:
        log(f"[dry-run] Gateway {GW_NAME} (protocolType=MCP, authorizerType=AWS_IAM)")
        return "<dry-gateway-id>", "<dry-gateway-url>"
    r = _control().create_gateway(
        name=GW_NAME, roleArn=role_arn, protocolType="MCP", authorizerType="AWS_IAM",
        description="Managed WebSearch for llm-gateway (Architecture C, SigV4 inbound)",
    )
    log(f"Gateway created: {r['gatewayId']}")
    return r["gatewayId"], r["gatewayUrl"]


def wait_status(getter, label: str, ok="READY", bad=("FAILED", "DELETING", "DELETED"), n=75, p=4):
    last = "?"
    for _ in range(n):
        last = getter()
        if last == ok:
            log(f"{label} {ok}")
            return
        if last in bad:
            fail(f"{label} bad status: {last}")
        time.sleep(p)
    fail(f"{label} timeout (last={last})")


# ── WebSearch connector target (boto3 → SigV4 fallback) ───────────────────────
def find_target(gw_id: str):
    gw = _control()
    return next((t for t in gw.list_gateway_targets(gatewayIdentifier=gw_id).get("items", [])
                 if t.get("name") == TARGET_NAME), None)


def ensure_target(gw_id: str, dry: bool) -> str:
    if dry:
        log(f"[dry-run] WebSearch connector target (connectorId={CONNECTOR_ID})")
        return "<dry-target-id>"
    ex = find_target(gw_id)
    if ex:
        log(f"Target reused: {ex['targetId']} ({ex.get('status')})")
        return ex["targetId"]
    target_cfg = {"mcp": {"connector": {"source": {"connectorId": CONNECTOR_ID},
                  "configurations": [{"name": TOOL_CONFIG_NAME, "parameterValues": {}}]}}}
    creds = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    gw = _control()
    try:
        r = gw.create_gateway_target(gatewayIdentifier=gw_id, name=TARGET_NAME,
                                     targetConfiguration=target_cfg,
                                     credentialProviderConfigurations=creds)
        log(f"Target created (boto3): {r['targetId']}")
        return r["targetId"]
    except ParamValidationError:
        log("boto3 SDK does not model 'connector' → raw SigV4 fallback")
        return _create_target_sigv4(gw_id, target_cfg, creds)


def _create_target_sigv4(gw_id: str, target_cfg: dict, creds: list) -> str:
    fr = boto3.Session().get_credentials().get_frozen_credentials()
    url = f"https://bedrock-agentcore-control.{REGION}.amazonaws.com/gateways/{gw_id}/targets/"
    body = json.dumps({"name": TARGET_NAME, "clientToken": str(uuid.uuid4()),
                       "targetConfiguration": target_cfg,
                       "credentialProviderConfigurations": creds}).encode()
    req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(fr, "bedrock-agentcore", REGION).add_auth(req)
    p = req.prepare()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(p.url, data=body, headers=dict(p.headers), method="POST"),
            timeout=30,
        ) as r:
            tid = json.load(r)["targetId"]
            log(f"Target created (SigV4): {tid}")
            return tid
    except urllib.error.HTTPError as e:
        fail(f"SigV4 target create failed: HTTP {e.code} {e.read().decode()[:300]}")


# ── subcommands ───────────────────────────────────────────────────────────────
def cmd_deploy(dry: bool):
    acct = account_id()
    log(f"account={acct} region={REGION} gateway={GW_NAME}")
    role_arn = ensure_role(acct, dry)
    gw_id, gw_url = ensure_gateway(role_arn, dry)
    if not dry:
        wait_status(lambda: _control().get_gateway(gatewayIdentifier=gw_id).get("status", "?"), "gateway")
    tid = ensure_target(gw_id, dry)
    if not dry:
        wait_status(lambda: (find_target(gw_id) or {}).get("status", "?"), "target")
    mcp_url = gw_url if gw_url.endswith("/mcp") else gw_url.rstrip("/") + "/mcp"
    print()
    print(f"AGENTCORE_GATEWAY_URL={mcp_url}")
    print(f"AGENTCORE_TARGET_ID={TARGET_NAME}")
    print(f"AGENTCORE_REGION={REGION}")
    if not dry:
        log("Set AGENTCORE_GATEWAY_URL in values-eks-fargate-dev.yaml then redeploy gateway-proxy.")
        log("Verify: python3 deployment/scripts/provision_agentcore_websearch.py status")


def cmd_status():
    g = find_gateway()
    if not g:
        print("gateway: NONE")
        return
    print(f"gateway: {g['gatewayId']} status={g.get('status')} authorizerType={g.get('authorizerType')}")
    print(f"  url: {g['gatewayUrl']}")
    t = find_target(g["gatewayId"])
    print(f"  target: {t['targetId']} status={t.get('status')}" if t else "  target: NONE")


def cmd_teardown():
    g = find_gateway()
    gw = _control()
    if g:
        t = find_target(g["gatewayId"])
        if t:
            gw.delete_gateway_target(gatewayIdentifier=g["gatewayId"], targetId=t["targetId"])
            log(f"target deleted: {t['targetId']}")
            time.sleep(5)
        gw.delete_gateway(gatewayIdentifier=g["gatewayId"])
        log(f"gateway deleted: {g['gatewayId']}")
    else:
        log("gateway NONE — skip")
    iam = boto3.client("iam")
    try:
        iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName="web-search-invoke")
        iam.delete_role(RoleName=ROLE_NAME)
        log(f"IAM role deleted: {ROLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            log("IAM role NONE — skip")
        else:
            raise
    log("teardown complete.")


def main():
    ap = argparse.ArgumentParser(description="Provision AgentCore Gateway + WebSearch target")
    ap.add_argument("command", nargs="?", default="deploy", choices=["deploy", "status", "teardown"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.command == "deploy":
        cmd_deploy(args.dry_run)
    elif args.command == "status":
        cmd_status()
    elif args.command == "teardown":
        cmd_teardown()


if __name__ == "__main__":
    main()
