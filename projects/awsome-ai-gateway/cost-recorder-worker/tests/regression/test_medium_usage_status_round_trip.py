# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``usage_logs.status`` 가 실제 결과를 담는지 — 그리고 오류율이 0 에서 벗어나는지.

무엇이 문제였나
---------------
``usage.usage_status`` enum 은 ``('SUCCESS','ERROR','TIMEOUT')`` 세 라벨을 갖고, admin-api
의 세 모니터링 엔드포인트가 그 컬럼으로 ``error_rate_pct`` 를 계산하고, admin-ui 는 그
값을 색으로 칠한다. 그런데 그 컬럼에 값을 넣는 **유일한** 코드가 문자열
``"SUCCESS"`` 하드코딩이었다.

게이트웨이 쪽에는 짝이 되는 결함이 있었다 — 실패로 끝난 요청은 ``finalize`` 를 아예 타지
않아 ``usage_logs`` 에 행이 **없었다**. 둘이 겹치면 오류율은 분자도 0, 분모도 성공뿐이라
**구조적으로 항상 0.00%** 다. 결측보다 나쁘다: 화면은 비어 있지 않고 "0%" 를 녹색으로
칠해서, 상류가 절반씩 5xx 를 뱉는 중에도 정상이라고 적극적으로 주장한다.

⚠️ **실 PostgreSQL** 로 확인한다. 두 가지가 실 DB 에서만 드러난다:

  1. 라벨이 enum 과 다르면 ``CAST(:status AS usage.usage_status)`` 가 거부한다 — 문자열을
     비교하는 테스트는 ``"FAILED"`` 같은 오답도 통과시킨다.
  2. 오류율은 **집계 결과**다. 행이 써졌는지가 아니라, admin-api 가 실제로 쓰는 집계식이
     0 이 아닌 값을 내는지가 결론이다. 그래서 이 파일은 그 식을 그대로 돌린다.

실행:
    PROOF_DSN=postgresql+asyncpg://postgres:...@127.0.0.1:55432/gwproof \\
      pytest tests/regression/test_medium_usage_status_round_trip.py
"""

from __future__ import annotations

import ast
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")

_OWNED_SUFFIX = "_statusproof"
_SRC = Path(__file__).resolve().parents[2] / "src" / "worker"

USER_ID = "51111111-1111-1111-1111-111111111111"
TEAM_ID = "52222222-2222-2222-2222-222222222222"
DEPT_ID = "53333333-3333-3333-3333-333333333333"
ORG_ID = "54444444-4444-4444-4444-444444444444"

_pg_required = pytest.mark.skipif(
    not PROOF_DSN,
    reason=(
        "PROOF_DSN 미설정 — 실 PG 필요. enum 라벨 거부와 집계 결과는 실 DB 에서만 드러난다."
    ),
)


def _entry(request_id: str, *, status: str = "SUCCESS", cost: str = "1.00"):
    from worker.schemas.cost_stream import CostStreamEntry

    return CostStreamEntry(
        request_id=request_id,
        user_id=USER_ID,
        team_id=TEAM_ID,
        dept_id=DEPT_ID,
        model_alias="proof-model",
        provider="BEDROCK",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(cost),
        latency_ms=100,
        status=status,
        requested_at="2026-06-01T00:00:00+00:00",
        completed_at="2026-06-01T00:00:01+00:00",
        period="2026-06",
        date="2026-06-01",
    )


def _split(dsn: str):
    prefix, _, name = dsn.rpartition("/")
    return prefix, name


async def _make_db() -> str:
    """``PROOF_DSN`` 의 DB 를 TEMPLATE 으로 복제한다 — 근거는 replay/orphan 테스트와 동일.

    자기 소유 DB 를 쓰는 이유: 오류율은 테이블 전체에 대한 집계라, 공용 DB 에서 재면 다른
    테스트가 넣은 행에 따라 값이 달라져 재현되지 않는다.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, name = _split(PROOF_DSN)
    owned = f"{name}{_OWNED_SUFFIX}"
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            await c.execute(text(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"'))
    finally:
        await admin.dispose()
    return f"{prefix}/{owned}"


@pytest.fixture(scope="module")
async def dsn():
    asyncpg = pytest.importorskip("asyncpg")
    if not PROOF_DSN:
        pytest.skip("PROOF_DSN 미설정")
    url = await _make_db()
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        # usage_logs 는 org→dept→team→user FK 를 모두 요구한다. 하나라도 없으면
        # per-row 폴백에서 조용히 스킵되고, 그 실패는 "status 가 안 써졌다" 와 구별되지
        # 않는다.
        await conn.execute(
            "INSERT INTO auth.organizations (id, name) VALUES ($1,'status-org') "
            "ON CONFLICT (id) DO NOTHING",
            ORG_ID,
        )
        await conn.execute(
            "INSERT INTO auth.departments (id, org_id, name) VALUES ($1,$2,'status-dept') "
            "ON CONFLICT (id) DO NOTHING",
            DEPT_ID,
            ORG_ID,
        )
        await conn.execute(
            "INSERT INTO auth.teams (id, dept_id, name) VALUES ($1,$2,'status-team') "
            "ON CONFLICT (id) DO NOTHING",
            TEAM_ID,
            DEPT_ID,
        )
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1,$2,'status@example.invalid','status','ADMIN','status-sub') "
            "ON CONFLICT (id) DO NOTHING",
            USER_ID,
            TEAM_ID,
        )
        # 오류율은 테이블 전체 집계다 — 템플릿에서 복제돼 온 기존 행이 있으면 기대값이
        # 흐려진다. 이 DB 는 이 파일 전용이므로 비우고 시작한다.
        await conn.execute("DELETE FROM usage.usage_logs")
    finally:
        await conn.close()
    yield url

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, name = _split(PROOF_DSN)
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(
                text(f'DROP DATABASE IF EXISTS "{name}{_OWNED_SUFFIX}" WITH (FORCE)')
            )
    finally:
        await admin.dispose()


async def _flusher(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from worker.batch_flusher import BatchFlusher

    class _Pipe:
        def __getattr__(self, _n):
            return lambda *a, **k: None

        async def execute(self):
            return None

    class _Redis:
        def pipeline(self, *a, **k):
            return _Pipe()

        async def publish(self, *a, **k):
            return None

    return BatchFlusher(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        redis=_Redis(),
    )


async def _statuses(engine, request_ids: list[str]) -> dict[str, str]:
    from sqlalchemy import text

    async with engine.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT request_id, status::text FROM usage.usage_logs "
                    "WHERE request_id = ANY(:ids)"
                ),
                {"ids": request_ids},
            )
        ).all()
    return {r[0]: r[1] for r in rows}


@_pg_required
async def test_each_status_label_survives_the_round_trip(dsn):
    """세 라벨이 그대로 컬럼에 들어가는지 — CAST 가 거부하면 여기서 드러난다."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        flusher = await _flusher(engine)
        ids = {s: f"{s.lower()}-{uuid.uuid4()}" for s in ("SUCCESS", "ERROR", "TIMEOUT")}
        await flusher.flush([_entry(rid, status=s) for s, rid in ids.items()])

        got = await _statuses(engine, list(ids.values()))
        for label, rid in ids.items():
            assert got.get(rid) == label, (
                f"{label} 로 보낸 행이 {got.get(rid)!r} 로 적혔다 — 하드코딩이 남아 있거나 "
                "CAST 가 라벨을 거부했다"
            )
    finally:
        await engine.dispose()


@_pg_required
async def test_the_error_rate_query_admin_api_actually_runs_is_no_longer_zero(dsn):
    """⚠️ 이 파일의 결론. 행이 써졌는지가 아니라 **집계식이 0 을 벗어나는지**다.

    admin-api ``routers/monitoring.py`` 의 식을 그대로 쓴다:
        count(*) FILTER (WHERE status = 'ERROR') / count(*) * 100
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        flusher = await _flusher(engine)
        # 성공 3 + 오류 1 → 25%
        tag = uuid.uuid4()
        batch = [_entry(f"ok{i}-{tag}") for i in range(3)]
        batch.append(_entry(f"bad-{tag}", status="ERROR", cost="0"))
        await flusher.flush(batch)

        # ⚠️ 집계 범위를 이 테스트가 넣은 행으로 한정한다. 처음에는 테이블 전체로 셌는데,
        #    같은 모듈의 앞선 테스트가 넣은 행까지 들어와 기대값이 순서에 의존했다(실측:
        #    4 대신 7). 검증하는 **식**은 admin-api 와 동일하게 유지한다.
        async with engine.connect() as c:
            row = (
                await c.execute(
                    text(
                        "SELECT count(*) AS total, "
                        "count(*) FILTER (WHERE status = 'ERROR') AS errors "
                        "FROM usage.usage_logs WHERE request_id LIKE :pat"
                    ),
                    {"pat": f"%{tag}"},
                )
            ).one()
        total, errors = row.total, row.errors
        assert total == 4, f"행이 {total}개 — 전제가 깨졌다(FK 시드 누락 시 0 이 된다)"
        rate = errors / total * 100
        assert rate == 25.0, (
            f"오류율이 {rate}% 로 나왔다(기대 25%) — 하드코딩이 남아 있으면 0.0% 가 된다"
        )
    finally:
        await engine.dispose()


@_pg_required
async def test_a_successful_request_is_still_recorded_as_success(dsn):
    """⚠️ 대조군. 위 단정이 "전부 ERROR" 로 통과하는 것이 아님을 보인다.

    이것이 없으면 하드코딩을 ``"ERROR"`` 로 바꿔도 오류율 테스트가 통과한다(그리고 그 편이
    지금보다 더 나쁘다 — 모든 요청이 실패로 보인다).
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        flusher = await _flusher(engine)
        rid = f"ctrl-{uuid.uuid4()}"
        await flusher.flush([_entry(rid)])
        async with engine.connect() as c:
            got = (
                await c.execute(
                    text("SELECT status::text FROM usage.usage_logs WHERE request_id = :r"),
                    {"r": rid},
                )
            ).scalar_one()
        assert got == "SUCCESS", f"성공 요청이 {got!r} 로 적혔다"
    finally:
        await engine.dispose()


@_pg_required
async def test_a_failure_row_carries_zero_cost_and_does_not_inflate_spend(dsn):
    """실패 행의 비용은 0 이어야 한다 — 상류가 거부한 요청에는 청구액이 없다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        flusher = await _flusher(engine)
        rid = f"zero-{uuid.uuid4()}"
        async with engine.connect() as c:
            before = (
                await c.execute(
                    text(
                        "SELECT COALESCE(SUM(used_usd),0) FROM budget.budget_usages "
                        "WHERE scope = 'USER' AND scope_id = :i"
                    ),
                    {"i": USER_ID},
                )
            ).scalar_one()
        await flusher.flush([_entry(rid, status="ERROR", cost="0")])
        async with engine.connect() as c:
            after = (
                await c.execute(
                    text(
                        "SELECT COALESCE(SUM(used_usd),0) FROM budget.budget_usages "
                        "WHERE scope = 'USER' AND scope_id = :i"
                    ),
                    {"i": USER_ID},
                )
            ).scalar_one()
        assert Decimal(str(after)) == Decimal(str(before)), (
            f"실패 행이 사용액을 {before} → {after} 로 늘렸다"
        )
    finally:
        await engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# 구조 — 하드코딩이 되살아나지 않는지
# ─────────────────────────────────────────────────────────────────────────────


def test_the_insert_takes_status_from_the_entry_not_a_constant():
    """⚠️ 문자열 grep 이 아니라 AST 로 본다.

    ``"status": "SUCCESS"`` 를 다시 넣는 것은 한 줄이고, 그렇게 되면 위 실 DB 테스트는
    ERROR 를 보낸 행이 SUCCESS 로 적혀 실패한다 — 하지만 그 실패는 CI 에 PROOF_DSN 이
    없으면 스킵된다. 그래서 구조로도 못을 박는다.
    """
    src = (_SRC / "batch_flusher.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values, strict=False):
            if isinstance(k, ast.Constant) and k.value == "status":
                found.append((node.lineno, v))
    assert found, "params 에서 status 키를 찾지 못했다 — 이 검사의 전제가 깨졌다"
    for lineno, v in found:
        assert not isinstance(v, ast.Constant), (
            f"L{lineno}: status 가 상수 {getattr(v, 'value', v)!r} 다 — 엔트리의 값을 써야 한다"
        )
        assert isinstance(v, ast.Attribute) and v.attr == "status", (
            f"L{lineno}: status 가 엔트리 필드에서 오지 않는다"
        )


def test_the_schema_pins_the_labels_to_the_enum():
    """라벨이 enum 과 어긋나면 INSERT 시점에 배치가 깨진다 — 파싱 시점에 막는다."""
    from typing import get_args

    from worker.schemas.cost_stream import CostStreamEntry

    field = CostStreamEntry.model_fields["status"]
    labels = set(get_args(field.annotation))
    assert labels == {"SUCCESS", "ERROR", "TIMEOUT"}, (
        f"허용 라벨이 {labels} — db/init 의 usage.usage_status 와 같아야 한다"
    )
    assert field.default == "SUCCESS", (
        "기본값이 SUCCESS 여야 한다 — 이 필드가 없는 구버전 엔트리는 성공 경로에서만 "
        "발행됐다"
    )


def test_an_unknown_label_is_rejected_at_parse_time_not_at_insert_time():
    """한 건이 경고와 함께 버려지는 것이, 배치 전체가 깨져 폴백으로 떨어지는 것보다 낫다."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _entry("bad-label", status="FAILED")
