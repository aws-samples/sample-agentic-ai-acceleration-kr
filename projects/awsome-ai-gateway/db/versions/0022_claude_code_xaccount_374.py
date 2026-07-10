# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""claude-code → 374 cross-account Bedrock native (routing_profiles 업데이트)

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-05

원래 멀티계정 설계: claude-code→374, codex→859, cowork→905 (각기 다른 계정). clean 배포에서
claude-code 가 859 in-account 로 남아 있던 것을 설계대로 374 로 분리.

claude-code 는 Bedrock **native**(bedrock-runtime, boto3 invoke_model). gateway-proxy 가 이 row 의
account_role_arn 을 보고 374 role 을 AssumeRole → 374 bedrock-runtime 클라이언트로 호출
(BedrockAccountClientProvider + BedrockAdapter client_resolver). backend='invoke' 유지(Mantle 아님).
region 은 클라이언트 엔드포인트용; claude-code alias 가 global.anthropic.* 면 model-id rewrite 는 no-op.

**즉시 롤백**: downgrade = account_role_arn/external_id NULL → 다음 요청부터 859 in-account 복귀
(gateway 코드가 account_role_arn NULL 이면 in-account adapter 사용). 무배포 롤백.
적용 후 Redis 캐시 키 `routing_profile:claude-code` 플러시(TTL 5분).
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# 374 계정의 cross-account-assumable 역할 (trust=859 gateway-proxy IRSA + ExternalId).
# terraform var.claude_code_374_role_arn 및 실제 생성 역할과 일치해야 함.
_ROLE_374 = "arn:aws:iam::345678901234:role/llm-gateway-claude-code-bedrock"
_EXTERNAL_ID = "claude-code-bedrock"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET account_role_arn = '{_ROLE_374}',
               region           = 'ap-northeast-2',
               external_id      = '{_EXTERNAL_ID}'
         WHERE client = 'claude-code'
        """
    )


def downgrade() -> None:
    # 즉시 859 in-account 복귀 (account_role_arn NULL → gateway 가 in-account adapter 사용).
    op.execute(
        """
        UPDATE model.routing_profiles
           SET account_role_arn = NULL, external_id = NULL, region = 'ap-northeast-2'
         WHERE client = 'claude-code'
        """
    )
