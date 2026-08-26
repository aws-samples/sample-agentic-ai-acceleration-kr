# Copyright 2026 © Amazon.com and Affiliates
"""Regression: Claude 5 등록(0027) + codex 기본모델 전환(0028).

모델 지원은 **DB 주도**다 — router_service 가 model.model_aliases 에서 alias 를 해석하고
(Redis 300s 캐시), 게이트웨이·admin-api·admin-ui 어디에도 하드코딩된 허용목록이 없다.
그래서 "신모델 지원"은 코드 변경이 아니라 마이그레이션 한 줄이고, 반대로 **마이그레이션이
git 에 있어도 적용 안 되면 완전히 보이지 않는다**. 실제로 그 상태였다: 0025(GPT-5.6)가
커밋된 채 dev 는 0022 에 머물러 있어 alias 가 라이브에 없었다.

이 테스트가 지키는 것:

1. **잠정 단가에 임의 추정값이 섞이지 않는다.** Claude 5 는 공시 단가가 없다(Price List
   API 전수 11,008개 스캔 0건, bedrock/pricing 페이지 미기재). 0006 선례대로 "직전 세대
   동일 단가"로 넣었으므로, 그 등가성이 코드로 고정돼 있어야 한다. 누가 나중에 "대충
   이 정도겠지" 하고 숫자를 바꾸면 청구가 틀어진다.
2. **Fable 5 가 다시 들어오지 않는다.** ap-northeast-2(= claude-code 라우팅 리전)에서
   호출이 거부된다(data retention mode 'default' is not available). 등록하면 UI 에는
   뜨지만 전부 실패하는 죽은 선택지가 된다.
3. **0028 의 EXISTS 가드가 살아 있다.** alias 없이 default_model 을 바꾸면 codex 전
   요청이 404 다.
4. **downgrade 가 FK 자식까지 정리한다.** alias 를 참조하는 FK 6개가 전부
   ON DELETE NO ACTION 이라, 팀/사용자 허용(정상적인 사용 개시 절차) 후 downgrade 하면
   ForeignKeyViolationError 로 죽고 transaction_per_migration 때문에 DB 가 그 리비전에
   갇힌다. 0025 가 실측으로 겪은 함정이다.

``PROOF_DSN`` 이 있으면 실제 PostgreSQL 에 대해 상태까지 확인한다.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_DB = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")

_VERSIONS = Path(__file__).resolve().parents[3] / "db" / "versions"
_M0027 = _VERSIONS / "0027_add_claude_5_aliases.py"
_M0028 = _VERSIONS / "0028_codex_default_gpt56_terra.py"


def _module_constants(path: Path) -> dict:
    """마이그레이션 모듈의 최상위 상수/리터럴을 실행 없이 읽는다.

    import 하면 alembic op 컨텍스트가 필요해 실패하므로 AST 로 평가한다.
    """
    out: dict = {}
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError):
                    pass  # 리터럴이 아닌 대입(계산식 등)은 관심 대상이 아니다
    return out


def _op_execute_sql(path: Path, func: str | None = None) -> str:
    """실행되는 ``op.execute`` SQL 을 모듈 상수까지 치환해 렌더링한다.

    두 가지를 정확히 해야 쓸모가 있다.

    **① 독스트링을 SQL 로 오판하지 않는다.** 이 마이그레이션들의 독스트링은 결함·제약을
    설명하려고 SQL 조각과 테이블 이름을 그대로 인용한다(0027 은 FK 6개를 나열하고,
    0028 은 전환 전후 default_model 값을 적어 둔다). 소스를 문자열로 grep 하면 그
    **설명이 어서션을 만족**시켜 버려서, 마이그레이션이 실제로는 아무것도 안 해도
    테스트가 통과한다. 그래서 op.execute 인자만 본다.

    **② f-string 보간값을 복원한다.** ``f"... default_model = '{OLD_DEFAULT}' ..."`` 의
    리터럴 조각에는 ``codex-gpt`` 가 없다 — 값은 FormattedValue 안에 있다. 조각만 이으면
    "전제 확인 WHERE 가 있는지" 같은 어서션이 **실패해야 할 때가 아니라 항상** 실패한다.
    모듈 최상위 상수를 알고 있으므로 이름을 값으로 치환해 실제 실행될 SQL 을 만든다.
    """
    consts = _module_constants(path)
    tree: ast.AST = ast.parse(path.read_text())
    if func is not None:
        scoped = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func]
        assert scoped, f"{path.name}: {func}() 가 없다"
        tree = scoped[0]

    def _render(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if not isinstance(node, ast.JoinedStr):
            return None
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                inner = value.value
                # 모듈 상수는 실제 값으로, 지역 변수(루프 변수 등)는 자리표시자로 남긴다.
                if isinstance(inner, ast.Name) and isinstance(consts.get(inner.id), str):
                    parts.append(consts[inner.id])
                else:
                    parts.append(f"{{{getattr(inner, 'id', '?')}}}")
        return "".join(parts)

    sql: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (node.func.attr if isinstance(node.func, ast.Attribute) else None) != "execute":
            continue
        for arg in node.args:
            rendered = _render(arg)
            if rendered:
                sql.append(rendered)
    return "\n".join(sql)


def _downgrade_child_tables(path: Path) -> list[tuple[str, str]]:
    """downgrade() 의 ``for (table, column) in (...)`` 리터럴을 읽는다.

    자식 테이블 이름은 ``op.execute(f"DELETE FROM {table} ...")`` 처럼 **루프 변수로**
    들어가므로 SQL 문자열에는 나타나지 않는다. 정리 대상 목록을 확인하려면 루프의
    iterable 리터럴을 직접 봐야 한다.
    """
    for node in ast.walk(ast.parse(path.read_text())):
        if not (isinstance(node, ast.FunctionDef) and node.name == "downgrade"):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.For):
                try:
                    pairs = ast.literal_eval(stmt.iter)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if pairs and all(isinstance(p, tuple) and len(p) == 2 for p in pairs):
                    return list(pairs)
    return []


# --- 0027: alias / pricing (정적) -------------------------------------------


@pytest.mark.unit
def test_migration_chain_0027_0028():
    c27, c28 = _module_constants(_M0027), _module_constants(_M0028)
    assert (c27["revision"], c27["down_revision"]) == ("0027", "0026")
    assert (c28["revision"], c28["down_revision"]) == ("0028", "0027")


@pytest.mark.unit
def test_claude5_uses_global_inference_profile_ids():
    """``inferenceTypesSupported`` 가 INFERENCE_PROFILE **뿐**이라 foundation-model ID
    로는 호출되지 않는다. provider_model_id 는 반드시 global. 프리픽스여야 한다.

    ``anthropic.claude-opus-5`` 처럼 프리픽스를 빼면 get-foundation-model 로는 조회되니
    맞는 것처럼 보이지만, 실제 invoke 에서 ValidationException 이 난다. 등록 시점엔
    조용하고 첫 호출에서 터지는 종류의 실수라 정적으로 막는다.
    """
    models = _module_constants(_M0027)["MODELS"]
    assert models, "MODELS 가 비었다"
    for short, profile, *_ in models:
        assert profile.startswith("global.anthropic."), (
            f"{short}: inference profile ID 가 아니다 ({profile}) — 호출 시 ValidationException"
        )
        # 짧은 alias 와 profile ID 가 같은 모델을 가리켜야 한다(오탈자로 교차 등록 방지).
        assert profile.endswith(short.removeprefix("claude-")), (
            f"짧은 alias {short!r} 와 profile {profile!r} 가 다른 모델을 가리킨다"
        )


@pytest.mark.unit
def test_claude5_pricing_matches_previous_generation_exactly():
    """공시 단가가 없으므로 **직전 세대와 정확히 동일**해야 한다(0006 선례).

    임의 추정가는 청구 오류를 유발한다. 값이 흔들리면 이 테스트가 잡는다. 공시 단가가
    나오면 이 테스트와 함께 새 마이그레이션으로 정정하는 것이 올바른 절차다.
    """
    # 0006(Opus 4.8) / seed(Sonnet 4.6) 의 확정 단가 — 근거를 값으로 못박는다.
    expected = {
        "claude-opus-5": ("0.005000", "0.006250", "0.010000", "0.000500", "0.025000"),
        "claude-sonnet-5": ("0.003000", "0.003750", "0.006000", "0.000300", "0.015000"),
    }
    for short, _profile, _disp, _desc, *prices in _module_constants(_M0027)["MODELS"]:
        assert tuple(prices) == expected[short], (
            f"{short}: 단가가 직전 세대와 다르다. 공시 단가 확인 없이 바꾸면 청구가 틀어진다."
        )


@pytest.mark.unit
def test_fable5_is_not_registered():
    """ap-northeast-2 에서 호출 거부(data retention mode)되므로 등록하면 죽은 선택지."""
    aliases = [m[0] for m in _module_constants(_M0027)["MODELS"]]
    profiles = [m[1] for m in _module_constants(_M0027)["MODELS"]]
    assert not any("fable" in x for x in aliases + profiles), (
        "fable-5 는 ap-northeast-2 에서 호출되지 않는다 — 리전 정책 해소 후 별도 마이그레이션"
    )


@pytest.mark.unit
def test_0027_pricing_guard_keys_on_effective_from():
    """model_pricings PK 는 gen_random_uuid() 라 ON CONFLICT 로 dedupe 가 불가능하다.

    alias 만으로 가드하면 나중의 **가격 정정 행이 막힌다** — 잠정 단가를 쓰는 지금은
    특히 치명적이다. (model_alias, effective_from) 로 가드해야 한다.

    ⚠️ 여기서 ``"effective_from" in sql`` 로 검사하면 안 된다. 그 단어는 INSERT 컬럼
    목록과 SELECT 값에도 나오므로, **가드 절을 지워도 어서션이 통과한다**(변이 검증에서
    실제로 걸렸다). 가드 서브쿼리의 WHERE 절만 봐야 한다.
    """
    sql = _op_execute_sql(_M0027, "upgrade")
    assert "ON CONFLICT (alias) DO NOTHING" in sql, "alias 재삽입 멱등성이 없다"

    guard = sql[sql.index("NOT EXISTS") :] if "NOT EXISTS" in sql else ""
    assert guard, "가격 삽입에 멱등 가드(NOT EXISTS)가 없다 — 재실행 시 중복 단가 행"
    where = guard[guard.index("WHERE") :]
    assert "model_alias" in where and "effective_from" in where, (
        "가드가 (model_alias, effective_from) 복합이 아니다 — alias 만으로 막으면 "
        "공시 단가 확정 후 정정 행을 넣을 수 없다"
    )


@pytest.mark.unit
def test_0027_downgrade_cleans_all_fk_children_and_dangling_defaults():
    """FK 6개가 전부 NO ACTION 이므로 자식부터 지워야 downgrade 가 살아남는다.

    실제 위험 시나리오: 운영자가 Claude 5 를 팀에 허용(team_allowed_models)한 뒤 롤백.
    자식이 남아 있으면 ForeignKeyViolationError → transaction_per_migration 때문에 DB 가
    이 리비전에 갇힌다.
    """
    children = _downgrade_child_tables(_M0027)
    assert set(children) == {
        ("model.model_pricings", "model_alias"),
        ("model.team_allowed_models", "model_alias"),
        ("model.user_allowed_models", "model_alias"),
        ("model.rate_limit_configs", "model_alias"),
        ("budget.downgrade_policies", "from_model_alias"),
        ("budget.downgrade_policies", "to_model_alias"),
    }, f"FK 자식 정리 목록 불일치: {children}"

    # routing_profiles.default_model 은 FK 가 없어 DELETE 로 정리되지 않는다.
    # UPDATE 로 되돌리지 않으면 dangling 기본값이 남아 그 클라이언트가 전부 404 다.
    down_sql = _op_execute_sql(_M0027, "downgrade")
    assert "UPDATE model.routing_profiles" in down_sql, (
        "dangling default_model 이 남아 해당 클라이언트가 404 가 된다"
    )
    # 부모(alias) 삭제가 자식 정리보다 뒤에 와야 한다.
    assert down_sql.index("UPDATE model.routing_profiles") < down_sql.index(
        "DELETE FROM model.model_aliases"
    ), "부모를 자식보다 먼저 지우면 FK 위반으로 죽는다"


# --- 0028: codex 기본모델 전환 (정적) ---------------------------------------


@pytest.mark.unit
def test_0028_switches_to_terra_and_guards_on_alias_existence():
    c = _module_constants(_M0028)
    assert c["NEW_DEFAULT"] == "codex-gpt-5.6-terra"
    assert c["OLD_DEFAULT"] == "codex-gpt"

    sql = _op_execute_sql(_M0028, "upgrade")
    assert "SET default_model = 'codex-gpt-5.6-terra'" in sql

    # 0025 미적용 DB 에서 dangling 기본값을 만들지 않기 위한 가드.
    assert "EXISTS" in sql and "status = 'ACTIVE'" in sql, (
        "alias 존재/활성 가드가 없다 — 0025 미적용 DB 에서 codex 전 요청이 404 가 된다"
    )
    # 전제 확인 WHERE — 운영자가 이미 손으로 다른 모델을 넣어 뒀다면 덮지 않는다.
    # 닫는 따옴표까지 포함해야 한다: 그게 없으면 SET 절의
    # `default_model = 'codex-gpt-5.6-terra'` 가 접두사로 걸려, WHERE 가드를 지워도
    # 어서션이 통과한다.
    assert "default_model = 'codex-gpt'" in sql, (
        "전제 확인 없이 UPDATE 하면 운영자의 수동 설정을 덮는다"
    )
    assert "client = 'codex'" in sql, "클라이언트 한정 없이 전 프로파일을 덮어쓴다"


@pytest.mark.unit
def test_0028_documents_the_redis_cache_flush_key():
    """routing profile 은 Redis 300s 캐시라 마이그레이션만으로 즉시 반영되지 않는다.

    키는 ``routing_profile:{client}`` — ``routing:{client}`` 가 아니다. 잘못된 키를
    지우면 조용히 no-op 되고 최대 5분간 옛 프로파일이 서비스된다.
    """
    doc = ast.get_docstring(ast.parse(_M0028.read_text())) or ""
    assert "routing_profile:codex" in doc, "Redis 플러시 키가 문서화되지 않았다"


@pytest.mark.unit
def test_0028_is_reversible():
    """downgrade 가 codex-gpt 로 되돌리는지.

    그 alias 는 이 마이그레이션이 건드리지 않아 항상 존재하고, 실측(2026-08-19)으로
    Mantle ``/v1/responses`` 에서 여전히 status=completed 를 준다. 즉 롤백 경로가
    죽은 모델을 가리키지 않는다.
    """
    sql = _op_execute_sql(_M0028, "downgrade")
    assert "SET default_model = 'codex-gpt'" in sql, "롤백이 옛 기본값을 복원하지 않는다"
    assert "default_model = 'codex-gpt-5.6-terra'" in sql, (
        "전제 확인 없이 되돌리면 운영자가 그 사이 바꿔둔 값을 덮는다"
    )


# --- 실제 PostgreSQL 상태 증명 -----------------------------------------------


@_DB
@pytest.mark.asyncio
async def test_live_registry_state_after_migrations():
    """실제 DB 에 alias·단가·기본값이 기대대로 들어갔는지."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            head = (
                await conn.execute(text("SELECT version_num FROM public.alembic_version"))
            ).scalar()
            if head is None or head < "0028":
                pytest.skip(f"DB head={head} — 0028 미적용")

            rows = dict(
                (r[0], (str(r[1]), str(r[2])))
                for r in (
                    await conn.execute(
                        text(
                            "SELECT a.alias, p.input_price_per_1k_tokens,"
                            "       p.output_price_per_1k_tokens "
                            "  FROM model.model_aliases a "
                            "  JOIN model.model_pricings p ON p.model_alias = a.alias "
                            " WHERE a.alias IN ('claude-opus-5','claude-sonnet-5',"
                            "                   'claude-opus-4-8','claude-sonnet-4-6')"
                        )
                    )
                ).fetchall()
            )
            # 직전 세대와 **실제 DB 값**이 같은지 (마이그레이션 문자열이 아니라 결과로)
            if "claude-opus-4-8" in rows and "claude-opus-5" in rows:
                assert rows["claude-opus-5"] == rows["claude-opus-4-8"], (
                    f"Opus 5 단가가 4.8 과 다르다: {rows}"
                )
            if "claude-sonnet-4-6" in rows and "claude-sonnet-5" in rows:
                assert rows["claude-sonnet-5"] == rows["claude-sonnet-4-6"], (
                    f"Sonnet 5 단가가 4.6 과 다르다: {rows}"
                )

            fable = (
                await conn.execute(
                    text("SELECT count(*) FROM model.model_aliases WHERE alias LIKE '%fable%'")
                )
            ).scalar()
            assert fable == 0, "fable-5 가 등록돼 있다 — ap-northeast-2 에서 전부 실패한다"

            # 모든 alias 는 단가가 있어야 한다. 없으면 cost_usd 가 0 으로 기록돼
            # 예산·ROI 가 조용히 틀어진다.
            orphans = (
                await conn.execute(
                    text(
                        "SELECT a.alias FROM model.model_aliases a "
                        " LEFT JOIN model.model_pricings p ON p.model_alias = a.alias "
                        " WHERE p.id IS NULL"
                    )
                )
            ).fetchall()
            assert not orphans, f"단가 없는 alias: {[o[0] for o in orphans]} → 비용 0 으로 집계"

            default_model = (
                await conn.execute(
                    text("SELECT default_model FROM model.routing_profiles WHERE client='codex'")
                )
            ).scalar()
            assert default_model == "codex-gpt-5.6-terra", (
                f"codex 기본모델이 전환되지 않았다: {default_model!r}"
            )
    finally:
        await engine.dispose()


@_DB
@pytest.mark.asyncio
async def test_live_routing_defaults_all_resolve_to_active_aliases():
    """어떤 라우팅 기본값도 dangling 이 아니어야 한다.

    routing_profiles.default_model 은 FK 가 없는 평범한 컬럼이라 DB 가 무결성을
    지켜주지 않는다. 존재하지 않는/INACTIVE alias 를 가리키면 그 클라이언트의 **모든**
    요청이 404 가 된다 — 마이그레이션 순서를 잘못 밟으면 실제로 일어날 수 있는 사고다.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            bad = (
                await conn.execute(
                    text(
                        "SELECT r.client, r.default_model FROM model.routing_profiles r "
                        " WHERE r.default_model IS NOT NULL "
                        "   AND NOT EXISTS ("
                        "         SELECT 1 FROM model.model_aliases a "
                        "          WHERE a.alias = r.default_model AND a.status = 'ACTIVE')"
                    )
                )
            ).fetchall()
            assert not bad, f"dangling default_model: {[(b[0], b[1]) for b in bad]} → 전 요청 404"
    finally:
        await engine.dispose()
