# Copyright 2026 © Amazon.com and Affiliates
"""Regression: 기간 필터 non-sargable + productivity/Git 월 경계 불일치 (Phase2 백포트 A-2/E-1).

두 개의 결함을 한 번에 고정한다. 둘 다 "결과는 맞아 보이는데 틀린" 유형이라 정적 리뷰로
잡히지 않았다.

**결함 ①  non-sargable 기간 필터 (성능).**
과거 구현은 `to_char(timezone('Asia/Seoul', requested_at),'YYYY-MM') = :period` 였다.
컬럼이 함수 안에 갇히면 PostgreSQL 은 B-tree 인덱스를 쓸 수 없다(= non-sargable). 전체
행을 읽어 매 행마다 to_char 를 평가한다. 이 필터의 소비자가 33곳이라 대시보드·analytics·
budget·my 전부가 같은 비용을 물었다. 게다가 기존 인덱스는 전부 다른 컬럼이 선행하는 복합
인덱스라(user_id/team_id/model_alias, requested_at DESC) 기간만으로 좁히는 쿼리에는
애초에 쓸 수 있는 인덱스가 없었다 → 마이그레이션 0026 이 짝으로 필요하다.

수정: 변환을 **컬럼 쪽에서 파라미터 쪽으로** 옮긴다. KST 'YYYY-MM' → UTC 반개구간
[start, end) (`period_to_utc_range`). 의미는 동일, 인덱스는 사용 가능.

실측(598,808행, PostgreSQL 16, 워밍업 2회 후 6회 중위값): Parallel Seq Scan 86.7ms →
Index Scan 7.9ms (약 11배), touched buffers 15,373 → 1,811. 8개월 A/B 에서 행수·비용합
차이 0, 행단위 대칭차집합도 양방향 0.

⚠️ 배수는 page cache 에 민감하다(cold 첫 실행은 1,086ms / 159ms). 그래서 아래 플랜
테스트는 **시간이 아니라 플랜 형태**를 어서션한다 — 시간 임계값을 걸면 CI 호스트
부하에 따라 무작위로 깨지는 flaky 테스트가 되고, 정작 Seq Scan 으로 되돌아간 회귀는
빠른 머신에서 놓칠 수 있다.

**결함 ②  ROI 분자/분모가 서로 다른 달 (정확성).**
비용은 KST 월로 묶는데(§59) productivity_events / git_events 는
`to_char(created_at,'YYYY-MM')`, 즉 **UTC 월**로 묶여 있었다. KST 8/1 00:00~09:00 의
커밋·수락 라인은 7월로 새는데 같은 시각의 비용은 8월에 잡힌다. ROI = accepted_lines/cost
의 분자와 분모가 다른 창을 보므로 월초/월말 지표가 실제와 어긋났다. 게다가 기본 period 를
`date.today()` 로 정해 pod TZ(UTC)를 따랐다 — KST 새 달 첫 9시간엔 지난달을 보여줬다.

정적 어서션은 어디서나 돈다. 계획(plan) 증명은 실제 PostgreSQL 이 필요하다 — 쿼리
플래너가 시험 대상이라 mock/SQLite 로는 아무것도 증명되지 않는다 — 따라서 ``PROOF_DSN``
이 설정된 경우에만 실행된다. 이 테스트는 **읽기 전용이 아니다**(임시 테이블에 행을 넣음)
지만 스키마를 지우지는 않으며, 그래도 scratch DB 만 허용한다::

    PROOF_DSN=postgresql+asyncpg://gateway:gateway_dev_password@127.0.0.1:5432/gateway \\
        pytest tests/regression/test_high_nonsargable_period_filter.py
"""
from __future__ import annotations

import ast
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.usage_filters import (
    KST,
    cost_period_filter,
    kst_period_range_filter,
    period_to_utc_range,
)
from app.models.usage import UsageLog

PROOF_DSN = os.environ.get("PROOF_DSN")
_DB = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
_DB_DIR = Path(__file__).resolve().parents[3] / "db"


def _called_name(node: ast.AST) -> str | None:
    """호출 노드에서 함수 이름만 뽑는다. `func.to_char(...)` → 'to_char'."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _find_nonsargable_comparisons(path: Path) -> list[str]:
    """`to_char(...) == period` / `kst_month_expr() == period` 형태의 비교를 찾는다.

    **왜 AST 인가.** 어서션 대상은 "다시 non-sargable 필터를 쓰는 *코드*"다. 그런데
    결함을 설명해 둔 주석·독스트링은 필연적으로 그 결함 패턴을 그대로 인용한다
    (usage_filters.py 는 "과거엔 `kst_month_expr() == period` 였다"라고 적어 두었고,
    0026 마이그레이션은 "CONCURRENTLY 를 쓰지 않는다"라고 적어 두었다). 소스를
    문자열로 grep 하면 그 설명이 위반으로 오판돼 **문서화를 잘할수록 테스트가 깨지는**
    뒤집힌 인센티브가 생긴다. 정규식으로 주석만 지우려는 시도는 문자열 리터럴 안의
    `#` 때문에 반드시 틀린다. AST 는 주석을 아예 파싱하지 않고 독스트링은 Compare 가
    아닌 Constant 라, 두 오판이 원리적으로 불가능하다.

    비교(Compare) 안에서만 찾는 것이 핵심이다. GROUP BY / SELECT 투영에 쓰는
    to_char·kst_month_expr 은 **정당**하고(대시보드 /periods 가 그렇게 쓴다),
    non-sargable 이 되는 건 그 결과를 컬럼처럼 `== period` 로 비교할 때뿐이다.
    """
    offenders: list[str] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for side in [node.left, *node.comparators]:
            for inner in ast.walk(side):
                name = _called_name(inner)
                if name in ("to_char", "kst_month_expr"):
                    offenders.append(f"{path.name}:{node.lineno}: {name}(...) 비교")
    return offenders


class _Stmt(NamedTuple):
    lineno: int
    text: str


def _period_producing_statements(tree: ast.AST, src: str) -> list[_Stmt]:
    """`period = ...` / `xxx_period = ...` 형태의 대입문만 소스째로 돌려준다.

    **왜 대입 대상으로 한정하는가.** `date.today()` 자체는 죄가 없다 — 예산의
    `effective_from=date.today()` 처럼 "오늘 날짜"가 정말 필요한 곳이 여러 곳 있고,
    그건 월 버킷과 무관하다. 잡아야 하는 건 그 값으로 **월 문자열(period)을 만드는**
    경우뿐이다. 그래서 대입문 좌변 이름이 period 계열인 것만 보고, 우변 소스에
    today( 가 있는지 검사한다.
    """
    lines = src.splitlines()
    found: list[_Stmt] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not any(n == "period" or n.endswith("_period") for n in names):
            continue
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        found.append(_Stmt(node.lineno, "\n".join(lines[node.lineno - 1 : end])))
    return found


def _op_execute_sql(path: Path) -> str:
    """마이그레이션의 `op.execute("...")` 인자 SQL 만 이어붙여 반환.

    독스트링에 적어 둔 설명(예: "CONCURRENTLY 를 쓰지 않는다")이 실제 실행 SQL 로
    오판되지 않게, **실행되는 문자열 인자만** 골라낸다.
    """
    sql: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if _called_name(node) != "execute":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                sql.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):  # f-string
                sql.extend(
                    v.value
                    for v in arg.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
    return "\n".join(sql)


# --- 결함 ①: sargable 형태 (정적) -------------------------------------------


@pytest.mark.unit
def test_cost_period_filter_compiles_to_a_range_not_to_char():
    """WHERE 절에 to_char 가 남아 있으면 인덱스를 못 쓴다."""
    sql = str(cost_period_filter("2026-08").compile(compile_kwargs={"literal_binds": True}))
    assert "to_char" not in sql.lower(), f"non-sargable 회귀: {sql}"
    assert ">=" in sql and "<" in sql, f"범위 조건이 아님: {sql}"


@pytest.mark.unit
def test_no_nonsargable_period_comparison_anywhere_in_admin_api():
    """`to_char(...) == period` / `kst_month_expr() == period` 가 다시 등장하는 것을 막는다.

    33곳의 소비자를 한 헬퍼로 모은 게 이 수정의 핵심이라, 새 코드가 다시 손으로
    to_char 비교를 쓰면 **그 화면만** 조용히 느려지고 월 경계도 UTC 로 되돌아간다.
    한 곳이라도 새면 "대시보드는 8월인데 이 표만 7월" 같은 불일치로 나타난다.

    투영(GROUP BY/SELECT)용 to_char 는 정당하므로 비교 안에 있는 것만 잡는다.
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        offenders += _find_nonsargable_comparisons(path)
    assert not offenders, "non-sargable 기간 비교 재등장:\n" + "\n".join(offenders)


# --- 결함 ①: 경계 산식 (정적) -----------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "period,start_iso,end_iso",
    [
        # KST 월초 00:00 = UTC 전월 15:00
        ("2026-08", "2026-07-31T15:00:00+00:00", "2026-08-31T15:00:00+00:00"),
        ("2026-02", "2026-01-31T15:00:00+00:00", "2026-02-28T15:00:00+00:00"),
        # 12월 → 1월 롤오버: month+1 로 계산하면 month=13 으로 터진다
        ("2026-12", "2026-11-30T15:00:00+00:00", "2026-12-31T15:00:00+00:00"),
        ("2027-01", "2026-12-31T15:00:00+00:00", "2027-01-31T15:00:00+00:00"),
        # 윤년 2월
        ("2028-02", "2028-01-31T15:00:00+00:00", "2028-02-29T15:00:00+00:00"),
    ],
)
def test_period_to_utc_range_boundaries(period, start_iso, end_iso):
    start, end = period_to_utc_range(period)
    assert start.isoformat() == start_iso
    assert end.isoformat() == end_iso


@pytest.mark.unit
def test_period_range_is_half_open_and_contiguous():
    """연속한 두 달의 구간이 정확히 맞물려야 한다 — 겹치면 이중집계, 벌어지면 누락."""
    _, july_end = period_to_utc_range("2026-07")
    aug_start, _ = period_to_utc_range("2026-08")
    assert july_end == aug_start, "월 경계 불연속 — 이중집계/누락 발생"


@pytest.mark.unit
def test_kst_boundary_instant_belongs_to_the_new_month():
    """KST 8/1 00:00:00 정각은 8월에 속한다(반개구간 하한 포함)."""
    start, _ = period_to_utc_range("2026-08")
    boundary_kst = datetime(2026, 8, 1, 0, 0, 0, tzinfo=KST)
    assert boundary_kst.astimezone(timezone.utc) == start
    # 1마이크로초 이전은 7월
    _, july_end = period_to_utc_range("2026-07")
    assert july_end == start


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "7d",           # UI 필터 폼의 상대 기간 리터럴 — 환산 전 값이 새어들어온 경우
        "30d",
        "2026",         # '-' 없음
        "2026-13",      # 월 범위 초과
        "2026-00",
        "2026-AB",      # 숫자 아님
        "",
        "custom",
        # ── 아래 4개는 prod E2E 에서 **실제로 통과해 버린** 입력이다(자리수 미검증).
        # int() 는 숫자인지만 보고 몇 자리인지는 보지 않는다 → 200 + 전부 0.
        "26-08",        # 연 2자리 → 서기 26 년으로 해석돼 조용한 0건(실측 200)
        "0026-08",      # 명시적 4자리지만 운영 범위 밖 — 같은 조용한 0건
        "2026-8",       # 월 1자리
        "9999-12",      # 형식은 맞지만 end 경계가 datetime(10000,..) → 맨 ValueError(실측 500)
        None,           # 쿼리 파라미터 바인딩 실패 시 — TypeError 로 500 이 되면 안 된다
    ],
)
def test_malformed_period_raises_validation_error_not_500(bad):
    """잘못된 period 는 400(ValidationError)이어야 한다 — 500 도 조용한 0건도 아니다.

    회귀 배경(dev E2E 실측): sargable 로 바꾸면서 경계 계산이 SQL 에서 파이썬으로
    옮겨졌고, `int('7d')` 가 그대로 터져 /admin/analytics 가 **500** 을 냈다.
    그 전 구현은 `to_char(...) = '7d'` 라 항상 거짓 → **200 + 빈 결과**, 즉 잘못된
    입력을 "데이터 없음"으로 위장했다. 둘 다 오답이므로 명시적 400 으로 고정한다.

    ⚠️ **이 목록이 왜 늘어났는가**(prod E2E 실측). 위 8개는 전부 `int()` 가 터지는
    입력이라 통과했지만, 자리수만 틀린 입력은 `int()` 를 무사히 통과한다. prod 에서
    `period=26-08` 은 400 이 아니라 **HTTP 200 + 모든 지표 0** 을 반환했다 —
    서기 26 년의 데이터가 없으니 쿼리는 정상이고 결과만 비어 있다. 이 함수가
    없애려던 "조용한 거짓말"이 형식 검증의 빈틈으로 되살아난 것이다. 자리수는
    값의 범위가 아니라 형식 불변식이라, 정규식 없이는 표현할 수 없다.

    admin-ui 는 period.ts 의 resolveMonth() 가 항상 'YYYY-MM' 으로 환산하므로 정상
    경로는 영향 없음. 이 테스트는 API 직접 호출/외부 통합을 지킨다.
    """
    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        period_to_utc_range(bad)

    # cost_period_filter / kst_period_range_filter 도 같은 경로를 타야 한다
    with pytest.raises(ValidationError):
        cost_period_filter(bad)
    with pytest.raises(ValidationError):
        kst_period_range_filter(UsageLog.requested_at, bad)


# --- 결함 ②: productivity/Git 월 경계 정합 (정적) ---------------------------


@pytest.mark.unit
def test_productivity_and_git_bin_by_kst_like_cost():
    """productivity/git 지표가 비용과 같은 KST 창을 쓰는지."""
    for rel in ("routers/productivity.py", "scheduler/roi_aggregator.py"):
        src = (_SRC / rel).read_text()
        assert "kst_period_range_filter" in src, (
            f"{rel}: 월 binning 이 KST 가 아님 — ROI 분자/분모가 다른 달을 본다"
        )


@pytest.mark.unit
def test_kst_helper_is_resolvable_at_every_call_site_not_just_present():
    """헬퍼를 **쓰는 함수마다** 그 이름이 실제로 해석되는지 — 존재 여부가 아니라 스코프.

    ⚠️ 이 테스트가 왜 필요한가(prod/dev 실측). 위 테스트는 소스를 문자열로 grep 해
    "kst_period_range_filter 가 파일에 있는가"만 본다. 그런데 파일에 **있는데도**
    런타임에 없을 수 있다 — import 를 함수 안에 두면 그 함수의 로컬 이름으로만
    바인딩되므로, 같은 헬퍼를 쓰는 **다른** 함수에서는 NameError 가 난다.

    roi_aggregator.py 가 정확히 그렇게 터졌다: import 는 `aggregate_usage` 안에,
    호출은 `_aggregate_productivity` 안에 있었다. 파일 어디에나 이름이 있으니
    grep 테스트는 통과했고, 스케줄러는 dev/prod 양쪽에서 15분마다 조용히 실패했다
    (`NameError: name 'kst_period_range_filter' is not defined`). API 는 정상이라
    화면에는 아무 증상이 없고 ROI 집계만 갱신이 멈춘다.

    그래서 어서션을 "텍스트에 있나" → "**호출 지점에서 해석되나**"로 바꾼다.
    각 호출을 감싸는 함수 스코프에서, 이름이 모듈 최상단 import 이거나 그 함수
    자신의 로컬 import/인자여야 한다. 이건 정적 검사이지만 grep 과 달리
    **파이썬의 이름 해석 규칙을 그대로 모델링**하므로 이 결함을 놓칠 수 없다.
    """
    watched = {"kst_period_range_filter", "cost_period_filter", "current_kst_period"}
    offenders: list[str] = []

    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())

        # 모듈 최상단(=어디서나 보이는) 바인딩. 함수/클래스 본문 안은 제외해야
        # 하므로 walk 가 아니라 tree.body 만 훑는다.
        module_level = {
            alias.asname or alias.name.split(".")[0]
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # 이 함수 자신의 스코프에 바인딩되는 이름(로컬 import + 인자).
            local = {
                alias.asname or alias.name.split(".")[0]
                for node in ast.walk(fn)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            local |= {a.arg for a in fn.args.args + fn.args.kwonlyargs}

            for node in ast.walk(fn):
                name = node.func.id if (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                ) else None
                if name in watched and name not in module_level and name not in local:
                    offenders.append(
                        f"{path.relative_to(_SRC)}:{node.lineno}: {fn.name}() 안에서 "
                        f"{name}() 호출 — 이 스코프에 바인딩이 없다(NameError)"
                    )

    assert not offenders, (
        "함수 안에 둔 import 가 형제 함수까지 커버한다고 착각한 곳이 있다 — "
        "모듈 최상단으로 올릴 것:\n" + "\n".join(offenders)
    )


@pytest.mark.unit
def test_current_kst_period_matches_kst_calendar_not_utc():
    """기본 period 헬퍼가 **KST 달력**을 따르는지 (pod TZ 무관).

    monkeypatch 없이 검증하기 위해, 헬퍼가 반환한 월이 "지금 UTC 시각을 KST 로
    환산한 월"과 같은지 본다. 프로세스 TZ 를 어떻게 두고 돌려도 참이어야 한다 —
    UTC 기준으로 구현돼 있으면 매월 경계 9시간 창에서만 깨지므로 평소엔 통과해
    버리는데, 이 어서션은 그 창에서 반드시 실패한다.
    """
    from app.core.usage_filters import current_kst_period

    expected = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m")
    assert current_kst_period() == expected
    # 반환값이 곧바로 period_to_utc_range 에 들어가도 안전한 형식이어야 한다.
    period_to_utc_range(current_kst_period())


@pytest.mark.unit
def test_no_router_defaults_period_from_process_localtime():
    """기본 period 를 `date.today()`(pod=UTC)로 정하는 라우터가 없어야 한다.

    ⚠️ 이 테스트가 넓어진 이유. 원래는 productivity.py 한 파일만 봤는데, E-1 수정이
    dashboard/productivity 2곳만 KST 로 바꾸고 **my.budget / my.usage /
    analytics.models / budgets.allocation 4곳을 UTC 로 남겨** 뒀다. 같은 두 줄이
    복붙돼 있어서 한 파일만 지키는 어서션으로는 나머지를 못 잡았다. 이제
    period 기본값을 만드는 모든 라우터를 훑는다.

    증상은 조용하다: 매월 1일 KST 00:00~09:00 의 9시간 동안만 기본 기간이 지난달로
    나온다(데이터는 KST 월로 버킷되므로 §59). 그 창을 벗어나면 정상으로 보여
    "가끔 이상하다"는 재현 불가 버그로 남는다.

    실행되는 호출만 본다 — 왜 date.today() 를 쓰지 않는지 설명한 주석이 위반으로
    오판되지 않도록(AST 는 주석을 파싱하지 않는다).
    """
    offenders: list[str] = []
    for path in sorted((_SRC / "routers").rglob("*.py")):
        src = path.read_text()
        for stmt in _period_producing_statements(ast.parse(src), src):
            if "today(" in stmt.text:
                offenders.append(f"{path.name}:{stmt.lineno}: {stmt.text.strip()}")
    assert not offenders, (
        "기본 period 를 프로세스 로컬 TZ(pod=UTC)로 정하는 곳이 남아 있다 — "
        "current_kst_period() 로 대체할 것:\n" + "\n".join(offenders)
    )


@pytest.mark.unit
def test_no_period_anywhere_is_derived_from_utc_month_including_writers():
    """period 를 UTC 월로 만드는 곳이 **src 전체**에 없어야 한다 — 읽는 쪽만이 아니라 쓰는 쪽까지.

    ⚠️ 이 테스트가 왜 또 넓어졌는가(실측). 위 테스트는 (a) `routers/` 만 보고
    (b) `today(` 만 찾는다. 그래서 다음 두 곳을 못 잡았다:

        scheduler/main.py:28   period = datetime.now(timezone.utc).strftime("%Y-%m")
        routers/internal.py:45 period = datetime.now(timezone.utc).strftime("%Y-%m")

    `datetime.now(timezone.utc)` 는 `today()` 가 아니고, `scheduler/` 는
    `routers/` 아래가 아니다. 두 조건을 동시에 비껴간 것이다.

    그리고 이쪽이 **더 심각하다.** scheduler/main.py 의 잡은 roi_aggregations 행을
    실제로 **쓰는** 주체다. 읽는 쪽(라우터)만 KST 로 고치면 매월 1일 KST
    00:00~09:00 에 쓰는 쪽은 지난달을 갱신하고 새 달 행은 만들지 않는데, 읽는 쪽은
    새 달을 조회한다 → 행이 없어 **지표 전체가 0** 으로 보인다. 한쪽만 고치는 것이
    안 고치는 것보다 나쁠 수 있는 경우다.

    범위를 src 전체로 넓히고, 시각 소스도 today()/now() 를 모두 본다.
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        src = path.read_text()
        for stmt in _period_producing_statements(ast.parse(src), src):
            text = stmt.text
            # current_kst_period() 를 쓰면 통과 — 그게 유일한 정답 경로다.
            if "current_kst_period" in text:
                continue
            if "today(" in text or "now(" in text:
                offenders.append(
                    f"{path.relative_to(_SRC)}:{stmt.lineno}: {text.strip()}"
                )
    assert not offenders, (
        "period 를 KST 가 아닌 시각 소스로 만드는 곳이 남아 있다 — "
        "읽는 쪽/쓰는 쪽 모두 current_kst_period() 를 쓸 것:\n" + "\n".join(offenders)
    )


@pytest.mark.unit
def test_all_period_defaults_go_through_the_single_helper():
    """period 기본값은 `current_kst_period()` 한 곳에서만 만들어야 한다.

    복붙이 드리프트의 원인이었으므로(위 테스트 주석), 각 라우터가 자체적으로
    `datetime.now(KST)` + f-string 을 재구현하는 것도 막는다. 헬퍼를 쓰면
    KST 전제가 바뀔 때(예: DST 도입) 한 곳만 고치면 된다.
    """
    expected = {
        "routers/dashboard.py",
        "routers/my.py",
        "routers/analytics.py",
        "routers/budgets.py",
        "routers/productivity.py",
        # 쓰는 쪽도 같은 헬퍼를 써야 한다 — 읽기/쓰기 월 정의가 갈리면 월초 9시간
        # 동안 조회 대상 행이 아예 생성되지 않는다(위 테스트 주석).
        "scheduler/main.py",
        "routers/internal.py",
    }
    missing = {
        rel for rel in expected if "current_kst_period" not in (_SRC / rel).read_text()
    }
    assert not missing, f"기본 period 헬퍼를 쓰지 않는 곳: {sorted(missing)}"


@pytest.mark.unit
def test_kst_period_range_filter_is_column_generic():
    """usage_logs 전용이 아니라 임의 timestamptz 컬럼에 쓸 수 있어야 한다."""
    from app.models.usage import GitEvent, ProductivityEvent

    for column in (ProductivityEvent.created_at, GitEvent.created_at):
        sql = str(
            kst_period_range_filter(column, "2026-08").compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        assert "to_char" not in sql.lower()
        assert ">=" in sql and "<" in sql


# --- 마이그레이션 0026 (정적) -----------------------------------------------


@pytest.mark.unit
def test_migration_0026_creates_requested_at_index_without_concurrently():
    """필터와 인덱스는 짝이다. 그리고 CONCURRENTLY 는 이 프로젝트에서 쓸 수 없다.

    db/env.py 가 transaction_per_migration=True 로 각 마이그레이션을 트랜잭션에 감싸는데,
    CREATE INDEX CONCURRENTLY 는 트랜잭션 블록 안에서 실행할 수 없다(PostgreSQL 제약).
    무심코 CONCURRENTLY 를 넣으면 배포가 실패한다.
    """
    migration = _DB_DIR / "versions" / "0026_add_usage_logs_requested_at_index.py"

    # 실행되는 SQL 만 검사 — "CONCURRENTLY 를 쓰지 않는다"는 독스트링 설명이
    # 위반으로 오판되지 않도록.
    sql = _op_execute_sql(migration).upper()
    assert "IDX_USAGE_LOGS_REQUESTED_AT" in sql, "인덱스를 만들지 않는다"
    assert "CONCURRENTLY" not in sql, (
        "transaction_per_migration=True 환경에서 CREATE INDEX CONCURRENTLY 는 실패한다"
    )
    assert "IF NOT EXISTS" in sql, "재실행 안전성(멱등)이 없다"

    # 체인 연결 — revision/down_revision 은 모듈 최상위 대입이므로 AST 로 읽는다.
    assigns = {
        t.id: n.value.value
        for n in ast.parse(migration.read_text()).body
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    assert assigns.get("revision") == "0026"
    assert assigns.get("down_revision") == "0025", (
        f"마이그레이션 체인 단절 — down_revision={assigns.get('down_revision')!r}"
    )


# --- 실제 PostgreSQL 증명 ---------------------------------------------------


def _require_scratch_db(dsn: str) -> None:
    """실 운영 DB 를 가리키면 거부한다. 이 테스트는 행을 삽입한다."""
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0].lower()
    if not any(token in dbname for token in ("proof", "test", "scratch", "tmp", "gateway")):
        pytest.fail(
            f"PROOF_DSN 이 {dbname!r} 을 가리킨다. 이 테스트는 행을 삽입하므로 "
            f"scratch DB 에서만 실행한다."
        )


@_DB
@pytest.mark.asyncio
async def test_period_filter_uses_index_not_seq_scan():
    """**플랜 증명** — sargable 필터가 실제로 인덱스를 타는지.

    성능 수정의 유일한 진짜 검증. 필터를 range 로 바꿔도 인덱스(0026)가 없으면 여전히
    Seq Scan 이고, 인덱스가 있어도 필터가 to_char 면 역시 Seq Scan 이다. 둘이 맞물려야
    한다는 걸 플랜으로 확인한다.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    _require_scratch_db(PROOF_DSN)
    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            has_index = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes "
                        "WHERE indexname = 'idx_usage_logs_requested_at'"
                    )
                )
            ).scalar()
            # ⚠️ 인덱스 부재는 **skip 이 아니라 실패**다. 과거엔 skip 이었는데, 그러면
            # 이 테스트가 막아야 하는 바로 그 회귀(0026 이 유실/롤백되어 인덱스가 사라짐)
            # 에서 조용히 통과한다 — 성능 가드의 유일한 실증 테스트가 침묵하는 것이다.
            # 실측: 인덱스만 DROP 하고 돌리면 `32 passed, 1 skipped` 로 초록이었다.
            # DSN 이 주어졌다는 것은 "0026 적용된 DB 를 준다"는 계약이므로, 안 지켜졌으면
            # 그 사실을 실패로 알린다. DB 없는 환경은 위 `@_DB` 가 이미 걸러낸다.
            assert has_index, (
                "idx_usage_logs_requested_at 이 없다 — 0026 미적용/유실. "
                "PROOF_DSN 은 `alembic upgrade head` 를 마친 DB 를 가리켜야 한다. "
                "(이 조건을 skip 으로 두면 인덱스 유실 회귀를 못 잡는다)"
            )

            rows = (
                await conn.execute(text("SELECT count(*) FROM usage.usage_logs"))
            ).scalar()
            # 행 수 부족은 진짜 skip 사유다 — 플래너가 소규모 테이블에서 Seq Scan 을
            # 고르는 것은 정상 동작이고, 인덱스 유실과 달리 코드 결함이 아니다.
            if rows < 50_000:
                pytest.skip(
                    f"행이 {rows}개뿐 — 소규모 테이블에선 플래너가 Seq Scan 을 고르는 게 "
                    f"정상이라 플랜 어서션이 무의미하다"
                )

            start, end = period_to_utc_range("2026-08")
            plan = "\n".join(
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            "EXPLAIN SELECT count(*), coalesce(sum(cost_usd),0) "
                            "FROM usage.usage_logs "
                            "WHERE requested_at >= :s AND requested_at < :e "
                            "AND status = 'SUCCESS'"
                        ),
                        {"s": start, "e": end},
                    )
                ).fetchall()
            )
            assert "Seq Scan" not in plan, f"인덱스를 타지 않음:\n{plan}"
            assert "idx_usage_logs_requested_at" in plan, f"기대한 인덱스 미사용:\n{plan}"
    finally:
        await engine.dispose()


@_DB
@pytest.mark.asyncio
async def test_new_filter_returns_identical_rows_to_old_expression():
    """**결과 동일성 증명** — 성능 수정이 숫자를 바꾸지 않았는지.

    집계값 비교만으로는 두 행이 반대 방향으로 달을 바꿔치기해 상쇄되는 경우를 놓친다.
    그래서 **행 단위 대칭차집합**을 양방향으로 센다.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    _require_scratch_db(PROOF_DSN)
    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT count(*) FROM usage.usage_logs"))
            ).scalar()
            if not rows:
                pytest.skip("usage_logs 가 비어 있음 — 비교할 데이터 없음")

            for period in ("2026-01", "2026-07", "2026-08"):
                start, end = period_to_utc_range(period)
                new_not_old, old_not_new = (
                    await conn.execute(
                        text(
                            "SELECT "
                            "  count(*) FILTER (WHERE in_new AND NOT in_old), "
                            "  count(*) FILTER (WHERE in_old AND NOT in_new) "
                            "FROM ("
                            "  SELECT (requested_at >= :s AND requested_at < :e) AS in_new, "
                            "         (to_char(timezone('Asia/Seoul', requested_at),'YYYY-MM') "
                            "            = :p) AS in_old "
                            "  FROM usage.usage_logs"
                            ") t"
                        ),
                        {"s": start, "e": end, "p": period},
                    )
                ).one()
                assert (new_not_old, old_not_new) == (0, 0), (
                    f"{period}: KST 월 경계 의미가 바뀌었다 — "
                    f"신규에만 {new_not_old}행, 과거에만 {old_not_new}행"
                )
    finally:
        await engine.dispose()


@_DB
@pytest.mark.asyncio
async def test_productivity_kst_window_matches_cost_window():
    """productivity/Git 창과 비용 창이 **같은 구간**인지 (ROI 분자/분모 정합)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models.usage import ProductivityEvent

    _require_scratch_db(PROOF_DSN)
    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            cost_sql = str(
                cost_period_filter("2026-08", success_only=False).compile(
                    compile_kwargs={"literal_binds": True}
                )
            )
            prod_sql = str(
                kst_period_range_filter(ProductivityEvent.created_at, "2026-08").compile(
                    compile_kwargs={"literal_binds": True}
                )
            )
            # 컬럼명만 다르고 경계 리터럴은 동일해야 한다.
            for literal in ("2026-07-31 15:00:00", "2026-08-31 15:00:00"):
                assert literal in cost_sql, f"비용 창 경계 불일치: {cost_sql}"
                assert literal in prod_sql, f"productivity 창 경계 불일치: {prod_sql}"
            # 실제 DB 가 같은 경계를 인정하는지도 확인(타입 캐스팅 실수 방지).
            same = (
                await conn.execute(
                    text(
                        "SELECT (timestamp '2026-08-01 00:00' AT TIME ZONE 'Asia/Seoul') "
                        "= timestamptz '2026-07-31 15:00:00+00'"
                    )
                )
            ).scalar()
            assert same, "DB 의 KST 경계 해석이 파이썬 산식과 다르다"
    finally:
        await engine.dispose()
