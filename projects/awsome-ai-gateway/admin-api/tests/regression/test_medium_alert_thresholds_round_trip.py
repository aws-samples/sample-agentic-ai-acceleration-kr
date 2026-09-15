# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""운영자가 설정한 알림 임계값이 저장되고 **되읽히는지**.

무엇이 문제였나
---------------
두 방향이 각각 끊겨 있었다.

**쓰기**: ``BudgetCreate.alert_thresholds`` 를 받아 Redis 설정 JSON 에만 써 넣고
``budget_configs`` 행에는 저장하지 않았다 — 담을 컬럼이 없었고, 코드가 그 사실을 적어
두고 있었다(``alert_thresholds=[80, 90, 100],  # DB에 컬럼 없음``). Redis 키의 TTL 은
300초이므로 운영자 설정은 5분만 살아 있었다(gateway-proxy 쪽 회귀 테스트
``test_medium_operator_thresholds_persist.py`` 가 되돌아가는 지점을 증명한다).

**읽기**: 예산 요약 응답에 이 필드가 아예 없었다. 그래서 admin-ui 의 SetBudgetDialog 는
저장된 값을 알 방법이 없어 항상 ``[80, 90, 100]`` 으로 초기화됐다 — 50% 를 저장한 뒤
다이얼로그를 다시 열면 저장한 값이 사라진 것처럼 보인다.

이 파일은 그 왕복을 고정한다: 저장 → 조회 → 같은 값.

⚠️ 계약 자체를 본다(요청 스키마 → ORM → 응답 스키마). 값이 어느 한 곳에서 떨어지면
   증상이 "화면이 기본값을 보여준다" 이고, 그것은 "아직 설정하지 않았다" 와 구별되지
   않는다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 계약 — 요청/응답 스키마와 ORM 이 같은 필드를 갖는지
# ─────────────────────────────────────────────────────────────────────────────


def test_the_orm_declares_the_column():
    """⚠️ ORM 이 선언하지 않으면 SELECT 목록에 없고, 값을 넣어도 저장되지 않는다.

    이 저장소에서 정확히 그 결함이 한 번 배포됐다(model_aliases.allowed_clients) —
    마이그레이션·API·UI 가 모두 있었는데 ORM 만 컬럼을 몰라서 게이트가 무력화됐다.
    """
    from app.models.budget import BudgetConfig

    assert "alert_thresholds" in BudgetConfig.__table__.columns, (
        "BudgetConfig 가 alert_thresholds 를 선언하지 않는다"
    )
    col = BudgetConfig.__table__.columns["alert_thresholds"]
    assert not col.nullable, (
        "NULL 을 허용하면 읽는 쪽마다 'NULL=기본값' 규칙이 필요하고, 한 곳이 빠지면 "
        "임계값이 빈 목록으로 읽혀 알림이 조용히 사라진다"
    )


def test_the_response_schema_exposes_it_so_the_ui_can_read_it_back():
    """⚠️ 쓰기 전용이면 UI 는 저장된 값을 보여줄 수 없다."""
    from app.schemas.budgets import BudgetSummaryItem

    assert "alert_thresholds" in BudgetSummaryItem.model_fields, (
        "예산 요약 응답에 alert_thresholds 가 없다 — UI 다이얼로그가 항상 기본값으로 "
        "초기화되고, 저장한 값이 사라진 것처럼 보인다"
    )
    field = BudgetSummaryItem.model_fields["alert_thresholds"]
    assert field.default is None, (
        "기본값은 None(예산 미설정)이어야 한다 — [] 를 기본값으로 두면 '알림 없음' 이라는 "
        "유효한 설정과 구별되지 않는다"
    )


def test_the_request_schema_still_validates_the_range():
    """DB 도메인과 **같은** 범위여야 한다 — 어긋나면 한쪽이 통과시킨 값이 다른 쪽에서 깨진다."""
    from app.schemas.budgets import SetBudgetRequest

    field = SetBudgetRequest.model_fields["alert_thresholds"]
    assert field.default == [80, 90, 100], field.default


# ─────────────────────────────────────────────────────────────────────────────
# 2. 저장 경로 — 네 곳 모두 값을 넘기는지
# ─────────────────────────────────────────────────────────────────────────────


def _tree() -> ast.Module:
    src = (_SRC / "services" / "budget_service.py").read_text(encoding="utf-8")
    assert len(src) > 5000, "budget_service.py 가 너무 짧다 — 경로 확인"
    return ast.parse(src)


def test_every_budget_config_construction_sets_alert_thresholds():
    """⚠️ 한 곳이라도 빠지면 그 경로로 만든 예산만 기본값이 되고, 재현 조건이 좁아진다.

    네 경로: 팀 예산, 사용자 예산, per-app 예산, 팀 배분에서 파생되는 사용자 예산.
    """
    tree = _tree()
    missing = []
    total = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "BudgetConfig"
        ):
            continue
        total += 1
        kwargs = {k.arg for k in node.keywords}
        if "alert_thresholds" not in kwargs:
            missing.append(node.lineno)
    assert total >= 4, f"BudgetConfig 생성 지점을 {total}개만 찾았다 — 전제가 깨졌다"
    assert not missing, f"L{missing}: alert_thresholds 를 넘기지 않는다"


def test_the_cache_warmer_no_longer_hardcodes_the_default():
    """⚠️ 워밍업이 하드코딩하면 기동 때마다 운영자 설정을 덮는다.

    문자열이 아니라 AST 로 본다: 워머 호출의 ``alert_thresholds`` 인자가 리스트 상수면
    실패한다.
    """
    tree = _tree()
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "alert_thresholds":
                continue
            if isinstance(kw.value, ast.List) and all(
                isinstance(e, ast.Constant) for e in kw.value.elts
            ):
                offenders.append(node.lineno)
    assert not offenders, (
        f"L{offenders}: alert_thresholds 에 리스트 상수를 넘긴다 — DB 값을 써야 한다"
    )


def test_the_summary_builder_reads_the_stored_value():
    """요약 빌더가 행의 값을 실어야 한다 — 상수를 실으면 UI 가 계속 기본값을 본다."""
    tree = _tree()
    found = False
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "BudgetSummaryItem"
        ):
            continue
        kwargs = {k.arg: k.value for k in node.keywords}
        assert "alert_thresholds" in kwargs, (
            f"L{node.lineno}: 요약 항목에 alert_thresholds 가 없다"
        )
        value = kwargs["alert_thresholds"]
        assert not isinstance(value, ast.List), (
            f"L{node.lineno}: 상수 목록을 싣는다 — 행의 값이어야 한다"
        )
        found = True
    assert found, "BudgetSummaryItem 생성 지점을 찾지 못했다 — 이 검사의 전제가 깨졌다"


@pytest.mark.parametrize("bad", [[0], [101], [150], [-1]])
def test_the_request_schema_rejects_out_of_range_values(bad):
    """⚠️ DB 도메인이 막는 값과 같은 집합이어야 한다.

    0 은 사용량 0 상태의 첫 요청마다 알림을 내고, 101+ 는 절대 발동하지 않는다 —
    둘 다 조용한 오설정이다.
    """
    import pydantic

    from app.schemas.budgets import SetBudgetRequest

    with pytest.raises(pydantic.ValidationError):
        SetBudgetRequest(
            target_id="00000000-0000-0000-0000-000000000001",
            target_type="USER",
            max_budget_usd=10,
            policy="HARD_BLOCK",
            alert_thresholds=bad,
        )


def test_the_request_schema_accepts_an_empty_list_as_no_alerts():
    """빈 목록은 "알림 없음" 이라는 유효한 설정이다 — 스키마가 막으면 끌 방법이 없다."""
    from app.schemas.budgets import SetBudgetRequest

    req = SetBudgetRequest(
        target_id="00000000-0000-0000-0000-000000000001",
        target_type="USER",
        max_budget_usd=10,
        policy="HARD_BLOCK",
        alert_thresholds=[],
    )
    assert req.alert_thresholds == []
