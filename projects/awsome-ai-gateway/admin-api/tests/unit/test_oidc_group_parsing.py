# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OIDCService._parse_group — Cognito 그룹명 파싱 결정론 테스트.

규칙 (underscore 개수 기반):
  - "Claude_<team>"          → (None, "team")      Default Department 팀
  - "Claude_<dept>_<team>"   → ("dept", "team")
  - 그 외                    → None (reject)
"""
from __future__ import annotations

import pytest

from app.services.oidc_service import OIDCService


@pytest.mark.parametrize(
    "group_name,expected",
    [
        # 정상 케이스
        ("Claude_backend", (None, "backend")),
        ("Claude_ML", (None, "ML")),
        ("Claude_ai_platform", ("ai", "platform")),
        ("Claude_test-department_aws-test", ("test-department", "aws-test")),
        ("Claude_AI-Center_S/W-Culture-Office", ("AI-Center", "S/W-Culture-Office")),

        # prefix 불일치
        ("Engineers", None),
        ("", None),
        ("claude_backend", None),  # case-sensitive
        ("ClaudeAdmin", None),     # prefix 없음 (admin 부트스트랩은 별도 로직)

        # underscore 0개 — prefix 만 있고 tail 없음
        ("Claude_", None),

        # underscore 3개+ — 모호
        ("Claude_a_b_c", None),
        ("Claude_a_b_c_d", None),

        # 빈 세그먼트
        ("Claude__team", None),       # 부서 빈 문자열
        ("Claude_dept_", None),       # 팀 빈 문자열
        ("Claude__", None),
    ],
)
def test_parse_group(group_name: str, expected: tuple[str | None, str] | None) -> None:
    assert OIDCService._parse_group(group_name) == expected
