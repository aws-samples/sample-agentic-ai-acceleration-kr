# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GLOBAL rate limit 을 모델별로 — model alias 당 활성 GLOBAL 행 1개

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-15

## 무엇이 바뀌나

0024 의 ``uq_rate_limit_configs_active`` 는 ``(scope, COALESCE(scope_id, nil)) WHERE is_active``
였다. GLOBAL 행은 ``scope_id`` 가 NULL 이고 모델 alias 는 ``model_alias`` 컬럼에 있으므로, GLOBAL
전체가 키 하나였다 — **한 번에 한 모델**만 GLOBAL 한도를 가질 수 있었다(admin-api upsert 도 alias 와
무관하게 GLOBAL 활성 행을 전부 비활성화했다). 콘솔 트리는 모델마다 GLOBAL 노드를 보여 주는데, 한
모델에 저장하면 다른 모델의 한도가 풀렸다. Bedrock 쿼터는 모델별이다.

  * ``uq_rate_limit_configs_active`` — USER/TEAM 만 덮는다(``AND scope <> 'GLOBAL'``). 키는 그대로다:
    USER/TEAM 의 upsert 는 여전히 alias 와 무관하게 그 주체의 활성 행을 바꾼다.
  * ``uq_rate_limit_configs_global_active`` — ``(COALESCE(model_alias, '')) WHERE is_active AND
    scope = 'GLOBAL'``. 모델 alias 당 활성 GLOBAL 행 1개. COALESCE 는 0024 와 같은 NULL 규칙이다 —
    유니크 인덱스에서 NULL 은 서로 다르므로, alias 없는 GLOBAL 행(콘솔은 만들지 않지만 SQL 로는
    가능하다)도 한 칸으로 묶지 않으면 아무 제약도 받지 않는다.
  * ``rl_global_release_guard`` — 아래 지연 제약 트리거.

## 이전 릴리스의 저장이 다른 모델의 한도를 지우지 못하게 — 지연 제약 트리거

이 리비전이 적용된 DB 에 이전 admin-api 가 붙는 시간이 있다: 롤링 배포 동안, 그리고 이미지만 되돌린
``helm rollback``(또는 ``--atomic`` 의 자동 롤백) 뒤 — 롤백은 마이그레이션 훅을 돌리지 않아 DB 는 0046 에
남는다. 이전 admin-api 의 GLOBAL upsert 는 alias 와 무관하게 활성 GLOBAL 행을 **전부** 비활성화한다. 그
저장 한 번이 모델 N 개 중 N−1 개의 한도를 감사 기록도 없이 해제한다.

``rl_global_release_guard`` 는 활성 GLOBAL 행이 비활성화되는 행마다 커밋 시점에 같은 alias 의 활성
GLOBAL 행이 있는지 본다. 없으면 ``check_violation`` 으로 커밋을 거부한다 — 그 트랜잭션은 통째로 롤백되고
아무것도 바뀌지 않는다. 이 릴리스의 경로는 모두 통과한다: GLOBAL 저장·비우기는 같은 트랜잭션에서 같은
alias 의 대체 행을 넣는다. 이전 admin-api 의 GLOBAL 저장은 다른 모델의 활성 GLOBAL 행이 하나라도 있으면
조용한 해제 대신 **500** 이 된다(다른 모델의 활성 GLOBAL 행이 없으면 그대로 저장된다).

  * ``DEFERRABLE INITIALLY DEFERRED`` — upsert 는 비활성화 **다음에** 대체 행을 넣는다. 행 단위 즉시
    검사면 정상 저장도 막힌다.
  * 의도된 해제는 그 트랜잭션에서만 ``SET LOCAL rl.allow_global_release = 'on'`` 으로 우회한다(세션
    단위 ``SET`` 은 쓰지 않는다 — 풀링된 연결에 남는다). 이 리비전의 downgrade 와 문서의 정리 SQL 이
    그렇게 하고, 감사 로그를 남긴다.
  * ``DELETE`` 는 막지 않는다(``AFTER UPDATE OF is_active``). GLOBAL 행을 지우는 경로는 모델 등록
    마이그레이션의 downgrade(FK 자식 정리)뿐이고, 그것은 이 리비전의 downgrade 가 트리거를 지운 뒤에만
    돈다.

## 왜 옛 이름을 그대로 쓰나

클라우드 마이그레이션 Job 은 매번 ``db/init/*.sql`` 을 alembic **보다 먼저** 돌리고, init SQL 의
``CREATE UNIQUE INDEX IF NOT EXISTS`` 는 **이름만** 본다. USER/TEAM 인덱스에 새 이름을 붙이면, 이
리비전 뒤에 이전 차트의 Job(이전 init SQL)이 한 번이라도 돌 때 옛 정의의
``uq_rate_limit_configs_active`` 가 다시 생긴다 — 활성 GLOBAL 행이 하나뿐이면 GLOBAL 이 조용히 단일
슬롯으로 돌아가고(그 뒤 두 번째 모델 저장은 유니크 위반), 둘 이상이면 그 Job 이 실패한다. 이름이
같으면 그 재실행은 no-op 이다. 대신 이름으로는 옛 정의와 새 정의를 구별할 수 없으므로, 이 리비전은
존재 여부와 무관하게 DROP 후 다시 만든다(한 DB 에서 한 번만 돈다. 테이블은 작다).

## 업그레이드는 기존 데이터를 바꾸지 않는다

0045 까지는 0024 인덱스가 활성 GLOBAL 행을 **전부 합쳐** 최대 1개로 강제했으므로 alias 당 1개는
자동으로 성립하고, USER/TEAM 의 키는 그대로다. 새 인덱스 둘 다 기존 제약에서 따라 나오는 약한
제약이라 생성이 실패할 수 없다(누가 0024 인덱스를 손으로 지웠다면 PostgreSQL 이 중복 키를 이름과
함께 보고하며 실패한다 — 그때는 0024 docstring 의 중복 정리 기준으로 먼저 정리한다).

## 잠금 — ACCESS EXCLUSIVE 를 처음에 잡는다(양방향)

DROP INDEX 는 ACCESS EXCLUSIVE 가 필요하다. 처음 구현은 인덱스 생성(SHARE)부터 하고 DROP 에서
ACCESS EXCLUSIVE 로 올렸는데, 그 사이 이 테이블을 읽은 트랜잭션이 이어서 쓰려 하면 서로를 기다려
교착이 됐다(실 PG16: 그 트랜잭션이 ``DeadlockDetectedError`` 로 죽었다). 그래서 첫 문장에서 ACCESS
EXCLUSIVE 를 잡는다 — 읽고 쓰는 트랜잭션이 있으면 그것이 끝날 때까지 기다리고(PostgreSQL 이 그 쓰기를
대기 중인 우리 요청보다 앞에 세운다), 교착은 없다.

⚠️ 기다리는 동안과 쥐고 있는 동안 이 테이블의 **읽기도 줄을 선다** — 대기 중인 ACCESS EXCLUSIVE 요청
뒤에 새 요청이 선다. ``lock_timeout`` 5s(0043·0045 와 같다)가 그 상한이다: 5초 안에 못 잡으면 이
마이그레이션이 실패하고 아무것도 바뀌지 않으며 Job 이 재시도한다. 잡은 뒤의 DDL 은 작은 테이블이라
짧다. 이 테이블의 읽기는 gateway-proxy 의 한도 캐시 미스(TTL 300s)와 admin-api 의 rate-limit 화면이다.

init SQL(``02_create_tables.sql``)이 같은 인덱스·함수·트리거를 갖는다. 기존 DB 의 클라우드 Job 에서는
init SQL 이 먼저 GLOBAL 인덱스를 만들고(옛 인덱스가 아직 있어 단일 슬롯은 유지된다) 이 리비전이 옛 정의를
바꾸고 트리거를 만든다. init SQL 은 트리거를 **새 설치**(alembic_version 없음)와 이미 0046 이후인 DB 에만
만든다 — 0024 이전 DB 에 먼저 있으면 0024 의 중복 정리가 남긴 지연 이벤트 때문에 0024 의 CREATE INDEX 가
거부되고, 0046 에 닿기 전에 실패한 업그레이드가 0042~0045 DB 에 트리거만 남기면 이전 admin-api 의 정상적인
슬롯 교체가 거부된다.

## 되돌리기(downgrade) — GLOBAL 한도를 말없이 버리지 않는다

0045 의 인덱스는 활성 GLOBAL 행을 하나만 허용한다. 여러 모델이 GLOBAL 한도를 갖고 있을 때 0024 처럼
"최신 하나만 남기기" 로 자동 정리하면 나머지 모델의 Bedrock 쿼터 보호가 **아무 신호 없이** 사라진다.
어느 모델의 한도를 남길지는 운영 판단이다. 그래서:

  * 실제 한도(rpm 또는 tpm)를 가진, alias 있는 활성 GLOBAL 행이 2개 이상이면 **실패한다**. 메시지에
    모델·한도 목록과 정리 방법을 담는다. 트랜잭션이 롤백되므로 아무것도 바뀌지 않고
    ``alembic_version`` 도 0046 그대로다. 메시지의 정리 SQL(``KEEP_ONE_GLOBAL_SQL``)은 남길 alias 에 실제
    한도가 없으면(자리표시자를 그대로 두었거나 오타) 아무것도 해제하지 않고, COMMIT 없이 끝난다.
  * 아무것도 집행하지 않는 활성 GLOBAL 행은 판정에서 빼고 비활성화한다(삭제가 아니다, 이력은 남는다):
    rpm·tpm 이 모두 NULL 인 행(콘솔에서 값을 비우고 저장한 모델 — gateway-proxy 는 GLOBAL 의 cpm/cph 를
    읽지 않는다)과 alias 가 없는 행(SQL 로만 생기고, gateway-proxy 는 GLOBAL 을 alias 로만 읽으며 콘솔
    트리에도 나오지 않는다). 트리거를 ``SET LOCAL`` 로 우회하고, 행마다 ``audit.audit_logs`` 에
    ``DELETE_RATE_LIMIT`` 를 남긴다.
  * 판정 전에 ACCESS EXCLUSIVE 를 잡는다(위 잠금) — 판정과 인덱스 생성 사이에 끼어든 저장이 판정을
    무효화하지 못한다.
  * 인덱스 DDL 전에 ``SET CONSTRAINTS ALL IMMEDIATE`` 로 위 비활성화가 남긴 지연 트리거 이벤트를 처리한다
    (우회가 켜져 있어 통과한다). PostgreSQL 은 지연 이벤트가 남은 테이블에 ``CREATE INDEX`` 를 거부한다
    (``cannot CREATE INDEX ... because it has pending trigger events`` — 실 PG16).

⚠️ 다운그레이드한 DB 는 **0046 이 있는 차트의 다음 마이그레이션 Job 이 돌 때까지만** 그 상태다. 그 Job 의
   init SQL 이 GLOBAL 인덱스를 다시 만들고 alembic 이 이 리비전을 다시 적용한다(트리거는 이 리비전이 다시
   만든다). 되돌릴 대상 리비전은 0045 가 아니라 **이전 차트의 alembic head** 다 — 순서와 명령은
   deployment/docs/eks-fargate/06-upgrade-rollback.md §4.4.
"""

import textwrap

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

NIL_UUID = "00000000-0000-0000-0000-000000000000"
# db/init/03_seed_data.sql 의 시스템 관리자 — 마이그레이션이 남기는 감사 행의 actor.
SYSTEM_ADMIN_ID = "00000000-0000-4000-a000-000000000010"

# 0046 — USER/TEAM: 0024 와 같은 키, GLOBAL 제외.
CREATE_ACTIVE_INDEX = (
    "CREATE UNIQUE INDEX uq_rate_limit_configs_active ON model.rate_limit_configs "
    f"(scope, COALESCE(scope_id, '{NIL_UUID}'::uuid)) "
    "WHERE is_active = true AND scope <> 'GLOBAL'"
)

# 0046 — GLOBAL: model alias 당 활성 행 1개. init SQL 이 먼저 만들었을 수 있다(IF NOT EXISTS).
CREATE_GLOBAL_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_global_active "
    "ON model.rate_limit_configs (COALESCE(model_alias, '')) "
    "WHERE is_active = true AND scope = 'GLOBAL'"
)

# 0045 의 정의(0024) — downgrade 가 되살린다.
CREATE_SINGLE_SLOT_INDEX = (
    "CREATE UNIQUE INDEX uq_rate_limit_configs_active ON model.rate_limit_configs "
    f"(scope, COALESCE(scope_id, '{NIL_UUID}'::uuid)) "
    "WHERE is_active = true"
)

# 활성 GLOBAL 행을 비활성화하는 커밋은 같은 alias 의 활성 GLOBAL 행이 남아 있어야 한다.
# ⚠️ db/init/02_create_tables.sql 에 글자까지 같은 정의가 있다(실 PG 대조:
#    admin-api tests/regression/test_high_global_rate_limit_0046_real_db.py).
CREATE_RELEASE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION model.rl_global_release_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF coalesce(current_setting('rl.allow_global_release', true), '') = 'on' THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM model.rate_limit_configs
         WHERE scope = 'GLOBAL' AND is_active
           AND model_alias IS NOT DISTINCT FROM OLD.model_alias
    ) THEN
        RAISE EXCEPTION 'GLOBAL rate limit of % would be released without a replacement',
            OLD.model_alias
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'rl_global_release_guard',
                  HINT = 'Save that model''s GLOBAL limit instead. An intended release runs '
                         || 'SET LOCAL rl.allow_global_release = ''on'' in its transaction.';
    END IF;
    RETURN NULL;
END
$$
"""

# CONSTRAINT TRIGGER 에는 IF NOT EXISTS 가 없다 — 새 설치에서는 init SQL 이 먼저 만들었다.
CREATE_RELEASE_GUARD_TRIGGER = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'model.rate_limit_configs'::regclass
           AND tgname = 'rl_global_release_guard'
    ) THEN
        CREATE CONSTRAINT TRIGGER rl_global_release_guard
            AFTER UPDATE OF is_active ON model.rate_limit_configs
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            WHEN (OLD.scope = 'GLOBAL' AND OLD.is_active AND NOT NEW.is_active)
            EXECUTE FUNCTION model.rl_global_release_guard();
    END IF;
END
$$
"""

# downgrade — 아무것도 집행하지 않는 활성 GLOBAL 행(rpm·tpm 모두 NULL, 또는 alias 없음)을 비활성화하고
# 행마다 감사 기록을 남긴다. 트리거 우회(SET LOCAL)를 먼저 켠다.
RELEASE_UNENFORCED_GLOBAL_ROWS = f"""
WITH released AS (
    UPDATE model.rate_limit_configs SET is_active = false, updated_at = now()
     WHERE scope = 'GLOBAL' AND is_active = true
       AND (model_alias IS NULL OR (rpm_limit IS NULL AND tpm_limit IS NULL))
    RETURNING id, model_alias, rpm_limit, tpm_limit
), audited AS (
    INSERT INTO audit.audit_logs
        (actor_user_id, actor_role, action, resource_type, resource_id, changes,
         result, ip_address, request_id)
    SELECT '{SYSTEM_ADMIN_ID}'::uuid, 'ADMIN', 'DELETE_RATE_LIMIT', 'RateLimitConfig', r.id::text,
           jsonb_build_object(
               'before', jsonb_build_object(
                   'scope', 'GLOBAL', 'model_alias', r.model_alias,
                   'rpm', r.rpm_limit, 'tpm', r.tpm_limit),
               'after', jsonb_build_object('scope', 'GLOBAL', 'deactivated', 1),
               'reason', 'migration 0046 downgrade: this GLOBAL row enforced nothing'),
           'SUCCESS', '0.0.0.0', 'migration-0046-downgrade'
      FROM released r
    RETURNING 1
)
SELECT r.model_alias, r.rpm_limit, r.tpm_limit FROM released r ORDER BY r.model_alias NULLS FIRST
"""

ACTIVE_GLOBAL_ROWS = (
    "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
    "WHERE scope = 'GLOBAL' AND is_active = true "
    "ORDER BY model_alias NULLS FIRST"
)

# downgrade 거부 메시지의 정리 SQL — 한도 하나만 남기고 나머지를 해제한다. 남길 alias 는 한 곳에만 적는다.
#   · 그 alias 에 실제 한도(rpm 또는 tpm)를 가진 활성 GLOBAL 행이 없으면(자리표시자 그대로·오타) DO 블록이
#     멈춘다 — 예전 형태는 그 경우 남길 한도까지 전부 해제하고 커밋했다.
#   · UPDATE 도 같은 조건을 다시 본다. psql 이 오류 뒤에도 계속 가는 설정(ON_ERROR_ROLLBACK)이어도 아무것도
#     해제하지 않는다.
#   · COMMIT 은 넣지 않는다 — 운영자가 돌려준 행을 보고 COMMIT 또는 ROLLBACK 한다.
# 감사 기록까지 남기는 형태는 deployment/docs/eks-fargate/06-upgrade-rollback.md §4.4 에 있다(실 PG 대조:
# admin-api tests/regression/test_high_global_rate_limit_0046_real_db.py).
KEEP_ONE_GLOBAL_SQL = """\
BEGIN;
SET LOCAL rl.allow_global_release = 'on';
SET LOCAL rl.keep_alias = '<alias to keep>';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM model.rate_limit_configs
         WHERE scope = 'GLOBAL' AND is_active
           AND model_alias = current_setting('rl.keep_alias')
           AND (rpm_limit IS NOT NULL OR tpm_limit IS NOT NULL)
    ) THEN
        RAISE EXCEPTION '% has no active GLOBAL limit. Nothing was changed; run ROLLBACK.',
            current_setting('rl.keep_alias');
    END IF;
END
$$;
UPDATE model.rate_limit_configs SET is_active = false, updated_at = now()
 WHERE scope = 'GLOBAL' AND is_active
   AND model_alias IS DISTINCT FROM current_setting('rl.keep_alias')
   AND EXISTS (
       SELECT 1 FROM model.rate_limit_configs k
        WHERE k.scope = 'GLOBAL' AND k.is_active
          AND k.model_alias = current_setting('rl.keep_alias')
          AND (k.rpm_limit IS NOT NULL OR k.tpm_limit IS NOT NULL))
RETURNING model_alias, rpm_limit, tpm_limit;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 처음부터 ACCESS EXCLUSIVE — SHARE 에서 올리면 읽고-쓰는 트랜잭션과 교착한다(docstring 잠금).
    op.execute("LOCK TABLE model.rate_limit_configs IN ACCESS EXCLUSIVE MODE")
    op.execute(CREATE_GLOBAL_INDEX)
    op.execute("DROP INDEX IF EXISTS model.uq_rate_limit_configs_active")
    op.execute(CREATE_ACTIVE_INDEX)
    op.execute(CREATE_RELEASE_GUARD_FUNCTION)
    op.execute(CREATE_RELEASE_GUARD_TRIGGER)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE model.rate_limit_configs IN ACCESS EXCLUSIVE MODE")
    op.execute("SET LOCAL rl.allow_global_release = 'on'")
    conn = op.get_bind()

    released = conn.exec_driver_sql(RELEASE_UNENFORCED_GLOBAL_ROWS).all()
    held = conn.exec_driver_sql(ACTIVE_GLOBAL_ROWS).all()
    if len(held) > 1:
        listing = "\n".join(f"  {alias}: rpm={rpm}, tpm={tpm}" for alias, rpm, tpm in held)
        raise RuntimeError(
            "[0046] downgrade refused: revision 0045 allows one active GLOBAL rate limit in total, "
            f"but {len(held)} models have one:\n{listing}\n"
            "Downgrading would drop the others' Bedrock quota protection, and which one to keep is "
            "your call. Keep one and clear the rest, then rerun the downgrade:\n"
            "  - in the console: empty RPM and TPM and save (audit-logged; this downgrade then "
            "deactivates the empty rows), or\n"
            "  - in SQL, in an interactive psql session. The SET LOCAL bypass is needed because 0046 "
            "refuses to release a GLOBAL limit otherwise. The alias to keep goes in one place; if it "
            "has no active GLOBAL limit, the block stops and changes nothing. Otherwise it leaves "
            "the transaction open: check the returned rows, then COMMIT (or ROLLBACK):\n"
            f"{textwrap.indent(KEEP_ONE_GLOBAL_SQL, '      ')}"
            "    (deployment/docs/eks-fargate/06-upgrade-rollback.md section 4.4 has the "
            "audit-logged form.)\n"
            "Nothing was changed."
        )

    # 위 UPDATE 가 남긴 지연 트리거 이벤트를 지금 처리한다(우회가 켜져 있어 통과). 남아 있으면 아래
    # CREATE INDEX 가 "pending trigger events" 로 거부된다.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("DROP TRIGGER IF EXISTS rl_global_release_guard ON model.rate_limit_configs")
    op.execute("DROP FUNCTION IF EXISTS model.rl_global_release_guard()")
    op.execute("DROP INDEX IF EXISTS model.uq_rate_limit_configs_active")
    op.execute(CREATE_SINGLE_SLOT_INDEX)
    op.execute("DROP INDEX IF EXISTS model.uq_rate_limit_configs_global_active")
    for alias, rpm, tpm in released:
        print(
            f"[0046] deactivated the active GLOBAL row of {alias if alias is not None else '<no alias>'} "
            f"(rpm={rpm}, tpm={tpm}) — it enforced nothing; audit.audit_logs has a DELETE_RATE_LIMIT entry"
        )
