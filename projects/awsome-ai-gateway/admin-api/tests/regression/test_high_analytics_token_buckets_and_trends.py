# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Regression: 토큰 합계 버킷 누락 + /admin/analytics 의 trends/requests 미대입.

두 결함군을 한 파일에서 막는다.

1) **버킷 누락** — cost_usd 는 캐시(생성/읽기)까지 포함해 청구되는데, 토큰 합계를
   input+output 만으로 계산한 자리가 4곳 있었다(analytics_repository.total_tokens,
   routers/analytics.py 의 cost_per_1k_tokens, routers/my.py ×2, monitoring.py).
   결과는 총 토큰 과소보고(dev 실측 -29.2%)와, 분자/분모 버킷 불일치로 두 Opus
   모델의 "1k 토큰당 비용" 가격 순위가 역전돼 보이는 현상이었다.
   routers/dashboard.py 는 처음부터 4버킷을 다 더했으므로 그것이 기준(reference).
   ⚠️ reasoning_tokens 는 output_tokens 안에 이미 포함(models/usage.py:61)이라
   더하면 이중계상 — 이 테스트도 포함을 요구하지 않는다.

2) **미대입** — AnalyticsResponse.trends / ModelBreakdown.requests 는 스키마
   기본값(=[], =0)이 있어서, 서비스가 값을 한 번도 대입하지 않아도 200 OK 로
   "데이터 없음"·"요청 0건" 이 나갔다(에러 없음 = 무증상 오답). 그래서 대시보드의
   두 추이 차트는 옆 KPI 가 실제 금액을 보여주는 동안 영구히 비어 있었다.
   trends 집계의 scope_id 격리도 함께 못박는다 — 빠지면 TEAM_LEADER 가 전사
   일별 비용을 받아가는 권한 누출이다.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "app"

# usage_logs 의 과금 토큰 버킷 4개. 하나라도 빠진 합계는 과소보고다.
BILLED_BUCKETS = {
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
}

# 토큰을 합산하는 모듈들 (dashboard.py 는 기준 구현으로 함께 검사).
TOKEN_SUM_MODULES = [
    "repositories/analytics_repository.py",
    "routers/dashboard.py",
    "routers/my.py",
    "routers/monitoring.py",
]


def _token_attrs(node: ast.AST) -> set[str]:
    """서브트리에서 참조된 <obj>.<*_tokens> 속성명 집합.

    `UsageLog.input_tokens`(컬럼)와 `row.input_tokens`(결과행) 둘 다 잡는다 —
    같은 결함이 ORM 식과 파이썬 산술 양쪽에서 났다.
    """
    return {
        sub.attr
        for sub in ast.walk(node)
        if isinstance(sub, ast.Attribute) and sub.attr.endswith("_tokens")
    }


def _maximal_token_add_chains(tree: ast.AST) -> list[tuple[int, set[str]]]:
    """토큰을 더하는 **최대** `+` 식들 → (lineno, 버킷집합).

    문자열 grep 이 아니라 AST 로 본다 — 줄바꿈/괄호/coalesce 중첩에 견딘다.
    '최대' 여야 하는 이유: analytics_repository 는
    `coalesce(sum(a)) + coalesce(sum(b)) + ...` 처럼 버킷별로 sum() 을 따로
    호출한다. func.sum() 단위로 세면 각 호출의 버킷이 1개라 누락을 놓친다
    (이 테스트의 초기 버전이 실제로 놓쳤다 — 음성대조군으로 확인).
    """
    add_nodes = [
        n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add)
    ]
    token_adds = [n for n in add_nodes if _token_attrs(n)]
    # 다른 토큰 Add 의 자식인 노드는 제외 → 체인의 뿌리만 남는다.
    inner = {
        id(sub)
        for n in token_adds
        for sub in ast.walk(n)
        if sub is not n and isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Add)
    }
    return [(n.lineno, _token_attrs(n)) for n in token_adds if id(n) not in inner]


@pytest.mark.unit
@pytest.mark.parametrize("rel", TOKEN_SUM_MODULES)
def test_every_token_sum_covers_all_billed_buckets(rel: str):
    """input/output 만 더하는 합계가 하나도 남아있지 않아야 한다."""
    path = SRC / rel
    tree = ast.parse(path.read_text())
    chains = _maximal_token_add_chains(tree)
    # 버킷이 2개 이상 더해지는 식 = '총 토큰' 계산. 1개짜리(개별 컬럼을 별도
    # 열로 노출하는 SELECT)는 총합이 아니라 제외.
    totals = [(lineno, attrs) for lineno, attrs in chains if len(attrs & BILLED_BUCKETS) >= 2]
    assert totals, f"{rel}: 토큰 합산식을 못 찾음 — 테스트가 낡았는지 확인"

    offenders = []
    for lineno, attrs in totals:
        missing = BILLED_BUCKETS - attrs
        if missing:
            offenders.append(f"{rel}:{lineno} 누락={sorted(missing)}")
        if "reasoning_tokens" in attrs:
            offenders.append(
                f"{rel}:{lineno} reasoning_tokens 를 더했다 — output_tokens 에 이미 "
                "포함(models/usage.py:61)이라 이중계상"
            )
    assert not offenders, (
        "토큰 합계가 과금 버킷을 누락했다(총 토큰 과소보고 / 단가 왜곡):\n  "
        + "\n  ".join(offenders)
    )


def _get_analytics_body() -> ast.AsyncFunctionDef:
    tree = ast.parse((SRC / "services" / "analytics_service.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_analytics":
            return node
    raise AssertionError("AnalyticsService.get_analytics 를 못 찾음")


@pytest.mark.unit
@pytest.mark.parametrize("field", ["trends", "by_model", "by_team", "by_user"])
def test_analytics_response_fields_are_explicitly_assigned(field: str):
    """스키마 기본값에 의존해 조용히 빈 값이 나가지 못하게 한다."""
    fn = _get_analytics_body()
    ret = next(
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "AnalyticsResponse"
    )
    kwargs = {kw.arg for kw in ret.value.keywords}
    assert field in kwargs, (
        f"AnalyticsResponse(...) 에 {field} 가 없다 — pydantic 기본값이 조용히 "
        f"빈 값/0 을 응답으로 내보낸다(200 OK 무증상 오답)"
    )


@pytest.mark.unit
def test_trend_aggregation_is_scope_isolated():
    """trends 집계 WHERE 에 scope_id 격리가 있어야 한다(TEAM_LEADER 누출 방지)."""
    fn = _get_analytics_body()
    assigns = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "trend_where" for t in n.targets)
    ]
    assert assigns, "trends 집계에 trend_where 가 없다 — 필터 구성을 확인"
    src = ast.unparse(fn)
    idx = src.index("trend_where")
    window = src[idx : idx + 600]
    assert "scope_id" in window and "team_id" in window, (
        "trends 집계가 scope_id 로 팀을 좁히지 않는다 — TEAM_LEADER 가 전사 일별 "
        "비용을 받아가는 권한 누출"
    )


@pytest.mark.unit
def test_requests_by_model_reuses_the_shared_scope_filter():
    """요청수 집계가 비용 집계와 **같은** scope 필터를 써야 한다.

    WHERE 를 손으로 다시 쓰면 by_model 의 cost 와 requests 가 서로 다른
    모집단에서 나와 export CSV/JSON 의 두 열이 어긋난다.
    """
    tree = ast.parse((SRC / "repositories" / "analytics_repository.py").read_text())
    fns = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert "count_requests_by_model" in fns, (
        "analytics_repository.count_requests_by_model 부재 — analytics_service 가 "
        "호출하므로 AttributeError 로 /admin/analytics 가 500 이 된다"
    )
    for name in ("count_requests_by_model", "sum_usage_by_model"):
        body = ast.unparse(fns[name])
        assert "_apply_scope_filter" in body, f"{name} 이 공용 scope 필터를 쓰지 않는다"
        assert "cost_period_filter" in body, f"{name} 이 KST 기간 필터를 쓰지 않는다"
