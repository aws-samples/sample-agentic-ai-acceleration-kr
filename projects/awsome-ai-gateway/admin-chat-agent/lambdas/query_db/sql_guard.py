# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""sql_guard — 결정적(LLM 비결정성 0) text2SQL 정확도 안전망 (DEVLOG §58, L0/L1).

query_db Lambda 의 validate_sql 이 테이블 화이트리스트만 보던 것을 넘어, 실제
Aurora 실행 *전에* sqlglot AST + schema_whitelist.yaml 의 ground-truth 메타로
"조용히 틀린 숫자"를 내는 SQL 을 결정적으로 잡는다. 후보투표(L3)·LLM validator(L2)
같은 확률적 계층의 *하한 안전망* — 모든 후보가 같은 실수를 공유하는 공통모드
실패(타임존·team_id 경유)는 다수결로 못 잡지만 이 결정 룰은 잡는다.

검사 항목 (감사로 확정된 9개 결함 매핑):
  L0-1 컬럼 화이트리스트/해석    → 결함⑦ (ERROR: 유령 컬럼·모호 컬럼은 DB 도 실패)
  L0-2 timezone 앵커 강제        → 결함③ (WARN: 시간필터에 AT TIME ZONE 'Asia/Seoul')
  L0-3 fan-out (1:N JOIN 후 SUM) → 결함② (WARN: 부모측 measure 가 N배 중복합산)
  L0-4 GROUP BY 정합             → 보조
  L1   dashboard 정합(status 필터)→ 결함⑧ (WARN: 총량 질의에 SUCCESS 필터 권고)

errors 는 reject(self-correction 피드백), warnings 는 fail-soft(결과에 동봉해
validator·orchestrator·스트리밍이 노출). 휴리스틱(fan-out/timezone)은 quick=WARN,
deep 은 호출부가 재생성 트리거로 격상할 수 있다.

sqlglot 외 의존성 없음 — query_db Lambda 가 이미 sqlglot/yaml import 중이라
런타임 비용 0(추가 LLM 호출 0, 쿼리당 수 ms CPU).
"""

from __future__ import annotations

from typing import Any

import sqlglot
import sqlglot.expressions as exp
from sqlglot.optimizer.qualify import qualify


class GuardError(Exception):
    """결정적 검증 실패 — message 가 LLM 으로 전달돼 self-correction 유발."""


# ─────────────────────────────────────────────────────────────────────────────
# Whitelist → sqlglot schema / 메타 인덱스
# ─────────────────────────────────────────────────────────────────────────────
def build_schema(whitelist: dict[str, Any]) -> dict[str, Any]:
    """schema_whitelist.yaml → sqlglot qualify 용 중첩 dict {db: {table: {col: type}}}.

    qualify 가 컬럼을 소스 테이블에 귀속시키고, 어느 테이블에도 없는 컬럼은
    OptimizeError 로 던지게 하는 데 쓰인다(결함⑦ 결정 차단).
    """
    schema: dict[str, dict[str, dict[str, str]]] = {}
    for t in whitelist.get("allowed_tables", []):
        sch = t["schema"]
        tbl = t["table"]
        cols = {c["name"]: (c.get("type") or "TEXT").upper() for c in t.get("columns", [])}
        schema.setdefault(sch, {})[tbl] = cols
    return schema


def _norm_dotted(s: str) -> str:
    return s.strip().lower()


# ─────────────────────────────────────────────────────────────────────────────
# AST helpers
# ─────────────────────────────────────────────────────────────────────────────
def _alias_to_table(ast: exp.Expression) -> dict[str, tuple[str, str]]:
    """alias-or-name(소문자) -> (schema, table). 컬럼의 실제 소속 테이블 해석용."""
    m: dict[str, tuple[str, str]] = {}
    for t in ast.find_all(exp.Table):
        sch = (t.text("db") or "").lower()
        name = (t.name or "").lower()
        key = (t.alias or name).lower()
        m[key] = (sch, name)
    return m


def _col_fqn(col: exp.Column, alias_map: dict[str, tuple[str, str]]) -> str | None:
    """qualify 된 Column → 'schema.table.column' 소문자. 해석 불가면 None."""
    tbl_key = (col.table or "").lower()
    name = (col.name or "").lower()
    if not name:
        return None
    if tbl_key in alias_map:
        sch, tbl = alias_map[tbl_key]
        return f"{sch}.{tbl}.{name}" if sch else f"{tbl}.{name}"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# L0-1: 컬럼 화이트리스트 / 해석 (결함⑦) — qualify 기반, ERROR
# ─────────────────────────────────────────────────────────────────────────────
def check_columns(sql: str, schema: dict[str, Any]) -> exp.Expression:
    """qualify 로 컬럼을 해석. 유령/모호 컬럼이면 GuardError(reject).

    반환: qualified AST (후속 fan-out/timezone 검사가 재사용).
    qualify 실패(스키마 불완전 등)는 GuardError 로 승격하지 않고 raw AST 로
    graceful — 단 컬럼 미해석은 명시적으로 잡는다.
    """
    try:
        ast = sqlglot.parse_one(sql, dialect="postgres")
    except Exception as e:  # noqa: BLE001
        raise GuardError(f"SQL parse error: {e}") from e
    if ast is None:
        raise GuardError("Empty SQL")

    try:
        qualified = qualify(
            ast.copy(),
            schema=schema,
            dialect="postgres",
            validate_qualify_columns=True,
            quote_identifiers=False,
        )
        return qualified
    except Exception as e:  # sqlglot OptimizeError 등
        msg = str(e)
        low = msg.lower()
        if "unknown column" in low or "could not be resolved" in low or "ambiguous" in low:
            # 결함⑦: 스키마에 없는/모호한 컬럼 — DB 에서 UndefinedColumn 으로 실패하거나
            # 엉뚱한 테이블 컬럼을 집계해 틀린 숫자. 결정적 reject + 구체 피드백.
            raise GuardError(
                f"Column validation failed: {msg}. "
                f"Use only columns in the schema whitelist; qualify ambiguous columns "
                f"with their table alias."
            ) from e
        # 그 외(스키마 커버리지 부족 등)는 차단하지 않음 — raw AST 로 진행.
        return ast


# ─────────────────────────────────────────────────────────────────────────────
# L0-2: timezone 앵커 강제 (결함③) — WARN
# ─────────────────────────────────────────────────────────────────────────────
def check_timezone(ast: exp.Expression, whitelist: dict[str, Any]) -> list[str]:
    """timestamptz 컬럼이 시간 경계(now()/interval/date_trunc/날짜 비교)와
    AT TIME ZONE 'Asia/Seoul' 앵커 없이 비교되면 WARN.

    raw UTC now()/date_trunc 는 KST 자정과 9시간 어긋남(결함③). few-shot 에
    UTC idiom(fs02 류)이 섞여 있어 모델이 베낄 위험이 상존하므로 결정적으로 잡는다.
    """
    tstz = {_norm_dotted(c) for c in whitelist.get("timestamptz_columns", [])}
    if not tstz:
        return []
    alias_map = _alias_to_table(ast)
    warnings: list[str] = []
    seen: set[str] = set()

    for col in ast.find_all(exp.Column):
        fqn = _col_fqn(col, alias_map)
        if fqn is None:
            # 별칭 해석 실패 시 bare 'table.col' / 'col' 로도 매칭 시도
            bare = f"{(col.table or '').lower()}.{(col.name or '').lower()}".strip(".")
            fqn = next((t for t in tstz if t.endswith("." + bare) or t.endswith("." + (col.name or "").lower())), None)
            if fqn is None:
                continue
        if fqn not in tstz:
            continue
        # 이 컬럼이 시간경계 컨텍스트에 있고 AtTimeZone 으로 안 감싸였는가?
        if _in_time_boundary(col) and not _wrapped_in_tz(col):
            if fqn not in seen:
                seen.add(fqn)
                warnings.append(
                    f"timestamptz `{fqn}` 가 시간 경계 비교에서 KST 앵커 없이 사용됨 "
                    f"— `{fqn.split('.')[-1]} AT TIME ZONE 'Asia/Seoul'` 로 변환해야 "
                    f"KST 자정 기준(결함③ 9시간 오차 방지)."
                )
    return warnings


# 캘린더 경계 신호 — date_trunc / ::date 캐스트 (일·주·월 binning). KST 앵커는
# 이때만 필요. 점-상대 윈도우(now() - interval '24 hours' 류)는 타임존 무관이라
# 제외(false positive 방지 — 결함③은 '캘린더 경계'에서만 9시간 오차).
# sqlglot postgres dialect 는 date_trunc 를 TimestampTrunc(또는 DateTrunc)로 파싱.
_CALENDAR_TRUNC_UNITS = {"day", "week", "month", "quarter", "year", "dow", "doy", "date"}
_TRUNC_CLASSES = tuple(
    c for c in (getattr(exp, n, None) for n in ("DateTrunc", "TimestampTrunc", "TimestamptzTrunc"))
    if c is not None
)


def _in_time_boundary(col: exp.Column) -> bool:
    """col 이 **캘린더 경계** 컨텍스트(date_trunc 일/주/월 인자, 또는 ::date 캐스트,
    또는 그런 캘린더 값과의 비교)인가. 점-상대 시각 윈도우(now()-interval '24h')는
    타임존 무관이라 제외 — 그게 결함③의 정확한 범위(자정 경계 오차).
    """
    node: exp.Expression | None = col
    depth = 0
    while node is not None and depth < 8:
        parent = node.parent
        if parent is None:
            return False
        # date_trunc('day'|'month'|..., col) 의 인자 — 캘린더 binning
        if isinstance(parent, _TRUNC_CLASSES) and _is_calendar_trunc(parent):
            return True
        # (col)::date 캐스트 — 일자 경계
        if isinstance(parent, exp.Cast) and _casts_to_date(parent):
            return True
        # 비교 술어의 한쪽이고, 반대편이 캘린더 경계(date_trunc/::date)면 경계 비교
        if isinstance(parent, (exp.GTE, exp.GT, exp.LTE, exp.LT, exp.Between, exp.EQ)):
            if _other_side_is_calendar(parent, node):
                return True
        node = parent
        depth += 1
    return False


def _is_calendar_trunc(dt: exp.Expression) -> bool:
    """date_trunc 의 unit 이 일·주·월 등 캘린더 단위인가(hour/minute 면 점-상대)."""
    unit = None
    u = dt.args.get("unit")
    if u is not None:
        unit = (getattr(u, "name", None) or str(u)).strip("'\"")
    if not unit:  # fallback: literal 인자에서 추출
        for lit in dt.find_all(exp.Literal):
            if lit.is_string:
                unit = lit.this.strip()
                break
    return (unit or "").lower() in _CALENDAR_TRUNC_UNITS


def _casts_to_date(cast: exp.Expression) -> bool:
    to = cast.args.get("to")
    return to is not None and "date" in to.sql().lower()


def _other_side_is_calendar(pred: exp.Expression, this_side: exp.Expression) -> bool:
    for child in pred.args.values():
        children = child if isinstance(child, list) else [child]
        for c in children:
            if not isinstance(c, exp.Expression) or c is this_side:
                continue
            # 반대편에 캘린더 date_trunc 또는 ::date 캐스트가 있으면 캘린더 경계
            for dt in c.find_all(_TRUNC_CLASSES):
                if _is_calendar_trunc(dt):
                    return True
            for cast in c.find_all(exp.Cast):
                if _casts_to_date(cast):
                    return True
    return False


def _wrapped_in_tz(col: exp.Column) -> bool:
    """col 의 조상 체인에 AtTimeZone(또는 명시적 ::date 후 KST 캐스팅)이 있는가."""
    node: exp.Expression | None = col
    depth = 0
    while node is not None and depth < 8:
        if isinstance(node, exp.AtTimeZone):
            return True
        node = node.parent
        depth += 1
    return False


# ─────────────────────────────────────────────────────────────────────────────
# L0-3: fan-out (1:N JOIN 후 부모측 measure SUM/AVG, 결함②) — WARN
# ─────────────────────────────────────────────────────────────────────────────
def check_fanout(ast: exp.Expression, whitelist: dict[str, Any]) -> list[str]:
    """measure 컬럼을 SUM/AVG 하는데, 그 measure 의 home 테이블이 1:N JOIN 으로
    행이 복제되는 join 그래프에 있으면 WARN(N배 중복합산 위험, 결함②).

    판정(보수적 — false positive 최소):
      - measure home 테이블 T_m 식별(measure_columns 메타).
      - 같은 SELECT scope 의 각 JOIN 이 'to-one(부모 PK 로 join)' 이면 안전,
        아니면(다른 N쪽 테이블을 비-PK 키로 join) T_m 행이 복제 → fan-out 위험.
      - COUNT(DISTINCT request_id) 거나 서브쿼리 선집계면 해당 measure 는 면제.
    """
    measures = {_norm_dotted(c) for c in whitelist.get("measure_columns", [])}
    if not measures:
        return []
    # 부모 PK 집합: foreign_keys 의 parent 들(예: auth.users.id) → 'to-one' 판정용
    pk_cols = {_norm_dotted(fk["parent"]) for fk in whitelist.get("foreign_keys", [])}

    alias_map = _alias_to_table(ast)
    warnings: list[str] = []
    seen_tables: set[str] = set()

    # 최상위 SELECT 만 본다(서브쿼리 선집계는 자체 scope 라 별도). find(Select) 가
    # 가장 바깥 select.
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    if select is None:
        return []

    # 이 scope 의 집계 measure 와 그 home 테이블
    agg_measure_tables: set[str] = set()
    for agg in select.find_all(exp.AggFunc):
        if not isinstance(agg, (exp.Sum, exp.Avg)):
            continue
        for col in agg.find_all(exp.Column):
            fqn = _col_fqn(col, alias_map)
            if fqn and fqn in measures:
                agg_measure_tables.add(".".join(fqn.split(".")[:2]))  # schema.table
    if not agg_measure_tables:
        return []

    # 이 scope 의 join 들 분류
    joins = list(select.find_all(exp.Join))
    if not joins:
        return []

    risky_join = False
    risk_detail = ""
    for j in joins:
        on = j.args.get("on")
        if on is None:
            continue
        # join 대상 테이블
        jt = j.find(exp.Table)
        if jt is None:
            continue
        jt_key = (jt.alias or jt.name or "").lower()
        jt_real = alias_map.get(jt_key, ("", (jt.name or "").lower()))
        jt_fq = f"{jt_real[0]}.{jt_real[1]}".strip(".")
        # ON 절에서 join 대상 테이블의 PK 컬럼이 쓰였는가 → to-one(안전)
        on_cols = [_col_fqn(c, alias_map) for c in on.find_all(exp.Column)]
        on_cols = [c for c in on_cols if c]
        joined_via_pk = any(
            c in pk_cols and ".".join(c.split(".")[:2]) == jt_fq for c in on_cols
        )
        if not joined_via_pk:
            # 대상 테이블을 비-PK 키로 join → 그 테이블이 여러 행 매칭 가능(행 복제)
            risky_join = True
            risk_detail = jt_fq or jt_key

    if risky_join:
        for tm in sorted(agg_measure_tables):
            if tm in seen_tables:
                continue
            seen_tables.add(tm)
            warnings.append(
                f"fan-out 위험: `{tm}` 의 measure 를 SUM/AVG 하는데 `{risk_detail}` 를 "
                f"비-PK 키로 JOIN 함 — 행이 복제돼 합계가 N배로 부풀 수 있음(결함②). "
                f"measure 는 서브쿼리에서 선집계하거나, 건수는 COUNT(DISTINCT request_id) 사용."
            )
    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# L0-4: GROUP BY 정합 (보조) — WARN
# ─────────────────────────────────────────────────────────────────────────────
def check_group_by(ast: exp.Expression) -> list[str]:
    """SELECT 에 집계와 bare 컬럼이 섞였는데 GROUP BY 가 없으면 WARN.

    Postgres 가 실행에러로 잡지만, 실행 전 결정 피드백으로 더 빠른 self-correction.
    """
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    if select is None:
        return []
    has_agg = any(True for _ in select.find_all(exp.AggFunc))
    if not has_agg:
        return []
    group = select.args.get("group")
    if group is not None:
        return []
    # 집계 바깥의 bare 컬럼이 projection 에 있나
    for proj in select.expressions:
        # alias 벗기기
        target = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(target, exp.AggFunc):
            continue
        if isinstance(target, exp.Column):
            return [
                "집계 함수와 비집계 컬럼이 함께 SELECT 됐는데 GROUP BY 가 없음 "
                "— 의도한 그룹 키를 GROUP BY 에 추가하세요."
            ]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# L1: dashboard 정합 (status=SUCCESS 필터, 결함⑧) — WARN
# ─────────────────────────────────────────────────────────────────────────────
def check_dashboard_consistency(
    ast: exp.Expression, whitelist: dict[str, Any]
) -> list[str]:
    """usage_logs 의 cost_usd/요청수/사용자수 '총량' 집계인데 status 필터가 없으면
    WARN. 대시보드(dashboard.py:49)는 status=SUCCESS 로 필터하므로 chat 총량이
    ERROR/TIMEOUT 까지 포함하면 대시보드와 숫자가 안 맞음(결함⑧).

    정책(비즈니스 결정)이라 결정 차단 아님 — 운영자가 의도적으로 전체를 볼 수도
    있으니 WARN 으로만 알린다.
    """
    conv = whitelist.get("dashboard_conventions") or {}
    status_col = _norm_dotted(conv.get("totals_filter_column", "usage.usage_logs.status"))
    if not status_col:
        return []
    alias_map = _alias_to_table(ast)
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    if select is None:
        return []

    # 총량 신호: usage_logs 의 cost_usd SUM 또는 COUNT(*) 가 있고
    measures = {_norm_dotted(c) for c in whitelist.get("measure_columns", [])}
    has_total = False
    for agg in select.find_all(exp.AggFunc):
        if isinstance(agg, exp.Count):
            has_total = True
        if isinstance(agg, (exp.Sum, exp.Avg)):
            for col in agg.find_all(exp.Column):
                fqn = _col_fqn(col, alias_map)
                if fqn and fqn in measures:
                    has_total = True
    if not has_total:
        return []

    # status 컬럼이 WHERE/FILTER 에 등장하나
    status_used = False
    for col in ast.find_all(exp.Column):
        fqn = _col_fqn(col, alias_map)
        if fqn == status_col or (col.name or "").lower() == status_col.split(".")[-1]:
            status_used = True
            break
    if status_used:
        return []
    return [
        "대시보드 정합: usage_logs 총량(비용/건수) 집계인데 status 필터가 없음 "
        "— 대시보드는 status='SUCCESS' 만 합산(결함⑧). '성공 호출만'이면 "
        "`status = 'SUCCESS'` 를 추가, 전체 의도면 답변에 명시하세요."
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────────────────────
def guard(sql: str, whitelist: dict[str, Any]) -> dict[str, list[str]]:
    """모든 결정적 룰 평가. {'errors': [...], 'warnings': [...]}.

    errors 가 비어있지 않으면 호출부(query_db)가 reject(self-correction).
    warnings 는 fail-soft — 결과 envelope 에 동봉해 validator·스트리밍이 노출.
    어떤 룰의 내부 예외도 검증을 죽이지 않는다(graceful — 안전망이 본 경로를
    막으면 안 됨).
    """
    errors: list[str] = []
    warnings: list[str] = []
    schema = build_schema(whitelist)

    # L0-1 컬럼 검증(ERROR 가능) — 동시에 qualified AST 확보
    try:
        ast = check_columns(sql, schema)
    except GuardError as e:
        return {"errors": [str(e)], "warnings": []}
    except Exception:  # noqa: BLE001 — 파싱 실패 등은 기존 validate_sql 이 처리
        return {"errors": [], "warnings": []}

    for rule in (
        lambda: check_timezone(ast, whitelist),
        lambda: check_fanout(ast, whitelist),
        lambda: check_group_by(ast),
        lambda: check_dashboard_consistency(ast, whitelist),
    ):
        try:
            warnings.extend(rule())
        except Exception:  # noqa: BLE001 — 개별 룰 실패는 무시(안전망 비파괴)
            continue

    return {"errors": errors, "warnings": warnings}
