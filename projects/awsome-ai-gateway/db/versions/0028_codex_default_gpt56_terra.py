# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""switch codex default_model from codex-gpt (GPT-5.5) to codex-gpt-5.6-terra

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-19

0025 가 GPT-5.6 Sol/Terra/Luna alias 를 등록했지만 default_model 은 일부러
'codex-gpt'(GPT-5.5) 로 남겨 뒀다 — "GPT-5.6 에 아직 접근 못 하는 계정이 깨지지 않도록".
그 유보 조건이 이제 해소됐으므로 기본값을 Terra 로 올린다.

**전환 근거 (실측, 2026-08-19).** 두 모델 모두 살아 있다 — 이건 장애 수습이 아니라
업그레이드다:

    POST https://bedrock-mantle.us-east-2.api.aws/openai/v1/responses  (SigV4 bearer)
      openai.gpt-5.5        → status=completed, usage{in:8, out:5, total:13}
      openai.gpt-5.6-terra  → status=completed, usage{in:8, out:5, total:13}

⚠️ 조사 과정에서 오판할 수 있는 지점: ``bedrock list-foundation-models`` 와
``bedrock-runtime invoke_model`` 은 둘 다 GPT-5.5 를 모른다(전자는 목록에 없고,
후자는 "The provided model identifier is invalid"). **그건 5.5 가 죽은 증거가 아니다.**
Mantle 은 SigV4 로 앞을 막은 OpenAI 형태의 HTTP 서비스라 bedrock-runtime 오퍼레이션이
아니고, 컨트롤플레인 카탈로그도 Mantle 모델을 열거하지 않는다. 실제 운영 경로
(providers/mantle_openai_adapter.py 가 쓰는 /v1/responses)로 찔러야 사실이 나온다.
adjacent API 의 부재는 부재의 증거가 아니다.

전환하는 실제 이유는 **단가의 신뢰도**다:

    codex-gpt (GPT-5.5)     in 0.001250 / out 0.010000  ← 0017 이 명시한 PLACEHOLDER
    codex-gpt-5.6-terra     in 0.002200 / out 0.013200  ← 0025, AWS 공시(2026-08-06)

즉 지금 codex 기본 경로의 비용은 **근거 없는 숫자로 계산되고 있다**. 예산 소진·팀별
비용·ROI 가 전부 그 값을 타므로, 공시 단가가 있는 모델을 기본값으로 두는 것이 비용
정확성 측면에서 옳다. 세대(5.6 > 5.5)도 같은 방향이다.

Sol 이 아니라 Terra 를 고르는 이유: Sol 은 in/out 이 Terra 의 2.5배($5.50/$33.00 per 1M)
라 기본값으로는 과하다. 0025 가 Terra 를 "balanced default" 로 표기한 그대로 따른다.
코딩 난이도가 높은 작업에는 요청에 ``{"model": "codex-gpt-5.6-sol"}`` 을 넣으면
routers/openai_compat.py 의 per-request 선택이 처리한다.

**⚠️ 이 마이그레이션만으로는 즉시 반영되지 않는다.** routing profile 은 Redis 에
300초 TTL 로 캐시된다(services/routing_profile_loader.py:32). 배포 후:

    redis-cli DEL routing_profile:codex

키는 ``routing_profile:{client}`` 다 — ``routing:{client}`` 가 아니다(0025 가 같은
함정을 기록해 뒀다). 잘못된 키를 지우면 조용히 no-op 되고 최대 5분간 옛 프로파일이
서비스된다. 플러시를 생략하면 그냥 5분 뒤에 반영되므로 장애는 아니다.

**롤백.** downgrade() 는 default_model 을 'codex-gpt' 로 되돌린다. 그 alias 는 이
마이그레이션이 건드리지 않으므로 항상 존재하고, 위 실측대로 여전히 200 을 준다.
롤백 후에도 Redis 플러시가 필요하다.

**전제 확인.** WHERE 에 ``default_model = 'codex-gpt'`` 를 걸어, 운영자가 이미 손으로
다른 값을 넣어 둔 경우엔 건드리지 않는다(운영자의 선택을 마이그레이션이 덮지 않는다).
codex-gpt-5.6-terra alias 자체가 없으면(0025 미적용) UPDATE 는 dangling 기본값을
만들어 codex 전 요청을 404 로 만들 수 있으므로, 존재 여부를 먼저 확인하고 없으면
아무것도 하지 않는다.
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

NEW_DEFAULT = "codex-gpt-5.6-terra"
OLD_DEFAULT = "codex-gpt"


def upgrade() -> None:
    # alias 가 실제로 등록돼 있을 때만 전환한다. EXISTS 가드가 없으면 0025 를 건너뛴
    # DB 에서 존재하지 않는 모델을 기본값으로 만들어 codex 요청 전체가 404 가 된다.
    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET default_model = '{NEW_DEFAULT}'
         WHERE client = 'codex'
           AND default_model = '{OLD_DEFAULT}'
           AND EXISTS (
                 SELECT 1 FROM model.model_aliases
                  WHERE alias = '{NEW_DEFAULT}' AND status = 'ACTIVE'
           )
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET default_model = '{OLD_DEFAULT}'
         WHERE client = 'codex'
           AND default_model = '{NEW_DEFAULT}'
           AND EXISTS (
                 SELECT 1 FROM model.model_aliases
                  WHERE alias = '{OLD_DEFAULT}' AND status = 'ACTIVE'
           )
        """
    )
