# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Rate Limit 스코프 헬퍼.

스코프: USER / TEAM / GLOBAL (3개).
설계 문서 `requirements-document/design/fr-4-1-rate-limiting.md` §D4 참조.
ROI/reporting만 `usage.roi_scope`에서 DEPT 지원.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol


class _UsageLike(Protocol):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


def compute_tpm_incr(usage: _UsageLike) -> int:
    """TPM 카운트용 토큰 증가분 (설계 §D3 공식).

    공식: ``input + cache_creation + output``
    cache_read_input_tokens는 TPM에서 제외 (Bedrock 쿼터도 cache_read는 감경 적용).
    LiteLLM v3와 수학적으로 동일: ``total − cached_tokens``.

    검증: 본 함수 호출 시 입력 usage는 4개 필드가 서로 배타적으로 집계됨을 가정
    (Anthropic/Bedrock native 응답 구조).
    """
    return (
        int(usage.input_tokens)
        + int(usage.cache_creation_input_tokens)
        + int(usage.output_tokens)
    )


def estimate_reserved_tokens(
    *,
    estimated_input_tokens: int,
    max_output_tokens: int,
    estimated_cache_creation_tokens: int = 0,
) -> int:
    """Pre-reserve 시 예약할 최대 토큰 수.

    응답 전 정확한 output 토큰 수를 알 수 없으므로 ``max_tokens``로 상한을 가정.
    ``estimated_cache_creation_tokens``는 호출 쪽에서 캐시 ``cache_control`` 블록 수
    등을 보고 추정 (Bedrock count_tokens API가 cache_creation 힌트 반환).
    과대 예약은 post-settle 단계에서 차액 환불로 보정.
    """
    return (
        int(estimated_input_tokens)
        + int(estimated_cache_creation_tokens)
        + int(max_output_tokens)
    )


class RateLimitScope(str, enum.Enum):
    USER = "USER"
    TEAM = "TEAM"
    GLOBAL = "GLOBAL"


# Fast-fail 순서: 좁은 스코프 → 넓은 스코프 (첫 violation에서 즉시 429)
SCOPE_ORDER: tuple[RateLimitScope, ...] = (
    RateLimitScope.USER,
    RateLimitScope.TEAM,
    RateLimitScope.GLOBAL,
)

# GLOBAL 스코프의 scope_id wildcard (Redis 키 네이밍용)
GLOBAL_WILDCARD = "*"


def build_rl_key(
    scope: RateLimitScope,
    scope_id: str | None,
    model_alias: str,
    metric: str,
) -> str:
    """Rate Limit Redis 키 빌더.

    네이밍 규약: ``{scope:scope_id:model_alias}:metric``
    단일 중괄호로 hash tag를 감싸 Redis Cluster의 동일 슬롯 라우팅을 유도.
    Redis 공식 스펙: 첫 ``{`` 와 첫 ``}`` 사이를 hash tag로 사용.
    같은 hash tag를 쓰는 관련 키(tpm:cur/prev/window 등)는 동일 슬롯에 모이므로
    하나의 Lua 스크립트에서 안전하게 멀티 키 접근 가능.

    Args:
        scope: USER / TEAM / GLOBAL
        scope_id: UUID (USER/TEAM) 또는 None (GLOBAL)
        model_alias: 모델 alias 문자열 (예: 'claude-opus-4-7'). 전체 모델 합산은 `*`
        metric: 'rpm' | 'tpm' | 'parallel'

    Returns:
        예: ``{USER:550e8400-abc:claude-opus-4-7}:rpm``
    """
    sid = scope_id if scope_id is not None else GLOBAL_WILDCARD
    return f"{{{scope.value}:{sid}:{model_alias}}}:{metric}"


def build_tpm_key_group(
    scope: RateLimitScope,
    scope_id: str | None,
    model_alias: str,
) -> tuple[str, str, str]:
    """TPM Sliding Window Counter에 쓰이는 3-key 그룹 빌더.

    Returns:
        (current_bucket_key, previous_bucket_key, window_marker_key)
        세 키 모두 동일 hash tag를 공유해 Redis Cluster 동일 슬롯 보장.
    """
    sid = scope_id if scope_id is not None else GLOBAL_WILDCARD
    prefix = f"{{{scope.value}:{sid}:{model_alias}}}:tpm"
    return f"{prefix}:cur", f"{prefix}:prev", f"{prefix}:window"


@dataclass(frozen=True)
class ScopeDescriptor:
    """한 요청에 대한 스코프 체크 디스크립터.

    limits 값이 None이면 해당 metric 체크를 건너뜀 (unlimited).
    """

    scope: RateLimitScope
    scope_id: str | None
    model_alias: str
    rpm_limit: int | None = None
    tpm_limit: int | None = None


def build_scope_descriptors(
    *,
    user_id: str,
    team_id: str | None,
    model_alias: str,
    user_rpm: int | None,
    user_tpm: int | None,
    team_rpm: int | None,
    team_tpm: int | None,
    global_rpm: int | None,
    global_tpm: int | None,
) -> list[ScopeDescriptor]:
    """auth_context + 로드된 한도값으로 3-scope 디스크립터 리스트 생성.

    Fast-fail 순서: USER → TEAM → GLOBAL.
    TEAM scope는 team_id가 None이면 건너뜀 (팀 미할당 사용자).
    """
    descriptors: list[ScopeDescriptor] = [
        ScopeDescriptor(
            scope=RateLimitScope.USER,
            scope_id=user_id,
            model_alias=model_alias,
            rpm_limit=user_rpm,
            tpm_limit=user_tpm,
        )
    ]
    if team_id is not None:
        descriptors.append(
            ScopeDescriptor(
                scope=RateLimitScope.TEAM,
                scope_id=team_id,
                model_alias=model_alias,
                rpm_limit=team_rpm,
                tpm_limit=team_tpm,
            )
        )
    descriptors.append(
        ScopeDescriptor(
            scope=RateLimitScope.GLOBAL,
            scope_id=None,
            model_alias=model_alias,
            rpm_limit=global_rpm,
            tpm_limit=global_tpm,
        )
    )
    return descriptors
