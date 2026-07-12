# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""배포 전 사전검증 — 도구 설치 여부, AWS 인증."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import paths

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


def check_paths(items) -> list[CheckResult]:
    """(name, Path) 목록의 존재 여부를 확인. Tool Gateway 워크플로우는 별도
    PR에서 오는 provision 스크립트/terraform 파일에 의존하므로, 없는 채로
    워크플로우에 진입해 subprocess가 알 수 없는 에러로 죽는 걸 미리 막는다."""
    results = []
    for name, p in items:
        p = Path(p)
        results.append(CheckResult(name=name, ok=p.exists(),
                                   detail=str(p) if p.exists() else f"없음: {p}"))
    return results


# Tool Gateway 워크플로우가 실제로 호출하는 파일들(이 리포에 함께 있어야 동작).
TOOL_GW_PATHS = [
    ("provision_tool_gateway.sh", paths.script("provision_tool_gateway.sh")),
    ("tool-gateway-dev terraform", paths.TOOL_TF_DIR / "main.tf"),
]


def check_tool_gateway_assets() -> list[CheckResult]:
    """Tool Gateway 배포/삭제에 필요한 스크립트·terraform 존재 확인."""
    return check_paths(TOOL_GW_PATHS)


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
