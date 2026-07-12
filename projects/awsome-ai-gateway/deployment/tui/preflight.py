# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""배포 전 사전검증 — 도구 설치 여부, AWS 인증."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

LLM_TOOLS = ["aws", "kubectl", "helm", "terraform", "jq", "python3"]
TOOL_GW_TOOLS = ["terraform", "aws", "jq", "python3"]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def check_tools(tools, which=shutil.which) -> list[CheckResult]:
    results = []
    for t in tools:
        path = which(t)
        results.append(CheckResult(name=t, ok=path is not None, detail=path or "not found in PATH"))
    return results


def check_aws_auth(runner=subprocess.run) -> CheckResult:
    try:
        proc = runner(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True, text=True,
        )
        ok = proc.returncode == 0
    except FileNotFoundError:
        return CheckResult(name="aws-auth", ok=False, detail="aws CLI not found")
    return CheckResult(name="aws-auth", ok=ok, detail="authenticated" if ok else "aws sts failed")
