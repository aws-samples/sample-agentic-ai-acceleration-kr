# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""폼 입력 ↔ terraform.tfvars 직렬화/파싱 + 플레이스홀더 검증. 순수 함수만."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_TOKENS: tuple[str, ...] = (
    "CHANGE_ME",
    "CHANGE_ACCOUNT_ID",
    "ACCOUNT_ID",
    "YOUR_ROLE",
    "tvly-...",
    "BSA...",
    "sk-...",
)


def find_placeholders(values: dict[str, str]) -> list[str]:
    """값에 플레이스홀더 토큰이 남은 key 목록을 반환."""
    flagged = []
    for key, val in values.items():
        if isinstance(val, str) and any(tok in val for tok in PLACEHOLDER_TOKENS):
            flagged.append(key)
    return flagged


def _hcl_value(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        return "[" + ", ".join(_hcl_value(v) for v in val) + "]"
    return f'"{val}"'


def to_tfvars(values: dict) -> str:
    """dict를 HCL tfvars 문자열로 직렬화."""
    return "\n".join(f"{k} = {_hcl_value(v)}" for k, v in values.items()) + "\n"


def write_tfvars(path: Path, values: dict) -> None:
    path.write_text(to_tfvars(values))


def parse_tfvars(text: str) -> dict:
    """기존 tfvars에서 `key = value`를 단순 파싱(프리필용). 문자열/bool/숫자만."""
    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key, raw = key.strip(), raw.strip()
        if raw in ("true", "false"):
            result[key] = raw == "true"
        elif raw.startswith('"') and raw.endswith('"'):
            result[key] = raw[1:-1]
        elif raw.lstrip("-").isdigit():
            result[key] = int(raw)
        # list/multiline 값은 프리필 대상 아님 — 무시
    return result


@dataclass
class BackendConfig:
    """tfstate 백엔드 — bootstrap과 tf-init에 동일 값 주입하는 단일 소스.

    region이 지정되면 tfstate 버킷 region도 -backend-config로 덮어쓴다. 빈 값이면
    backend.tf에 하드코딩된 region을 그대로 쓴다(하위호환)."""
    bucket: str
    dynamodb_table: str
    region: str = ""

    def backend_args(self) -> list[str]:
        args = [
            f"-backend-config=bucket={self.bucket}",
            f"-backend-config=dynamodb_table={self.dynamodb_table}",
        ]
        if self.region:
            args.append(f"-backend-config=region={self.region}")
        return args
