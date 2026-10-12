"""Failure text -> what to do next.

A data table on purpose: a newly discovered trap should cost one entry, not a
code change. Anything unmatched returns None so the caller shows the original
output — a swallowed error is worse than an uninterpreted one.
"""
from __future__ import annotations

import re

_LOCK_ID = re.compile(r"ID:\s*([0-9a-f-]{8,})")


def _state_lock(output: str) -> str:
    match = _LOCK_ID.search(output)
    target = match.group(1) if match else "<LOCK_ID>"
    return (
        "이전 실행이 강제 종료되어 state 잠금이 남아 있습니다. "
        f"다음으로 해제하십시오:\n  terraform force-unlock {target}\n"
        "다른 사람이 apply 중일 수도 있으니 먼저 확인하십시오."
    )


_RULES: tuple[tuple[tuple[str, ...], object], ...] = (
    (
        ("BucketAlreadyExists", "BucketAlreadyOwnedByYou", "AlreadyExistsException",
         "EntityAlreadyExists"),
        "이 계정에 같은 이름의 리소스가 이미 있습니다. 다른 스택이 `bap-*` 를 쓰고 "
        "있다면 설정 화면(s)의 Advanced 탭에서 `project` 접두사를 바꾸고, S3 버킷 "
        "이름만 겹치면 `bucket_suffix` 를 지정하십시오.",
    ),
    (("Error acquiring the state lock",), _state_lock),
    (
        ("Inconsistent dependency lock file", "Backend initialization required",
         "Module not installed", "please run \"terraform init\""),
        "이 디렉터리에서 `terraform init` 이 먼저 필요합니다. 대시보드의 "
        "`terraform init` 단계를 실행하거나, 해당 디렉터리에서 직접 "
        "`terraform init` 을 실행한 뒤 다시 시도하십시오.",
    ),
    (
        ("no valid credential sources", "No valid credential sources",
         "Unable to locate credentials", "ExpiredToken", "InvalidClientTokenId"),
        "AWS 자격증명이 없거나 만료되었습니다. `aws sso login` 또는 "
        "`AWS_PROFILE` 설정을 확인하십시오.",
    ),
    (
        ("ConflictException", "another operation is in progress"),
        "AWS 가 직전 작업을 아직 정리하고 있습니다 (게이트웨이 삭제 후 ~2초). "
        "잠시 후 재시도하십시오.",
    ),
    (
        ("exec format error", "CannotPullContainerError"),
        "컨테이너 이미지 아키텍처가 맞지 않습니다. ECS 태스크는 x86_64 로 뜨므로 "
        "`--platform linux/amd64` 로 빌드해야 합니다. "
        "로그: /ecs/<project>/server (CloudWatch)",
    ),
    (
        ("ResourceNotFoundException", "ValidationException: Table"),
        "참조하는 리소스가 아직 없습니다. 선행 단계가 실제로 끝났는지 확인하십시오 "
        "(대시보드에서 프로브를 다시 돌리십시오).",
    ),
)


def explain(output: str) -> str | None:
    if not output:
        return None
    for needles, advice in _RULES:
        if any(needle in output for needle in needles):
            return advice(output) if callable(advice) else advice
    return None
