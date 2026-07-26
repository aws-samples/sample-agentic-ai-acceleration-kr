"""오케스트레이터 설정 (환경 변수 기반).

공통 계약(변경 금지)의 env vars 를 단일 지점에서 로드한다.
순수 파싱 로직이므로 단위 테스트로 커버한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 기본 모델: us-west-2 에서 사용 가능한 최신 Claude Sonnet inference profile.
# (2026-07 `aws bedrock list-inference-profiles` 확인 결과)
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-5"
DEFAULT_REGION = "us-west-2"
DEFAULT_MAX_SQL_CORRECTIONS = 3
DEFAULT_TOOL_PLANE_MODE = "direct"


@dataclass(frozen=True)
class Settings:
    """런타임 설정.

    - sql_mcp_arn / semantic_mcp_arn: AgentCore Runtime 에 호스팅된 MCP 서버 ARN
    - memory_id: AgentCore Memory (STM) ID
    - model_id: Bedrock Claude inference profile
    - region: AWS 리전
    - max_sql_corrections: self-correction 루프 최대 재시도 횟수
    - mode: "graph"(기본, Strands Graph) | "agent"(단일 Agent 폴백)

    도구 평면(tool plane) — M3 additive:
    - tool_plane_mode: "direct"(기본, Runtime MCP 직접 SigV4) | "gateway"(Gateway MCP 집약)
    - gateway_url: gateway 모드의 단일 MCP 엔드포인트 URL
    - cognito_client_id / cognito_user / cognito_password_secret_arn / cognito_user_pool_id:
      gateway 모드의 Cognito M2M(USER_PASSWORD_AUTH) 인증 파라미터.
      비밀번호는 Secrets Manager(ARN)에서 읽는다 — 평문 env 노출 금지.
    """

    sql_mcp_arn: str
    semantic_mcp_arn: str
    memory_id: str | None
    model_id: str
    region: str
    max_sql_corrections: int
    mode: str
    tool_plane_mode: str
    gateway_url: str
    cognito_client_id: str
    cognito_user: str
    cognito_password_secret_arn: str
    cognito_user_pool_id: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        return cls(
            sql_mcp_arn=env.get("SQL_MCP_ARN", ""),
            semantic_mcp_arn=env.get("SEMANTIC_MCP_ARN", ""),
            memory_id=env.get("MEMORY_ID") or None,
            model_id=env.get("MODEL_ID", DEFAULT_MODEL_ID),
            region=env.get("AWS_REGION", DEFAULT_REGION),
            max_sql_corrections=_parse_int(
                env.get("MAX_SQL_CORRECTIONS"), DEFAULT_MAX_SQL_CORRECTIONS
            ),
            mode=env.get("ORCHESTRATOR_MODE", "graph").strip().lower() or "graph",
            tool_plane_mode=(
                env.get("TOOL_PLANE_MODE", DEFAULT_TOOL_PLANE_MODE).strip().lower()
                or DEFAULT_TOOL_PLANE_MODE
            ),
            gateway_url=env.get("GATEWAY_URL", ""),
            cognito_client_id=env.get("COGNITO_CLIENT_ID", ""),
            cognito_user=env.get("COGNITO_USER", ""),
            cognito_password_secret_arn=env.get("COGNITO_PASSWORD_SECRET_ARN", ""),
            cognito_user_pool_id=env.get("COGNITO_USER_POOL_ID", ""),
        )

    def is_gateway_mode(self) -> bool:
        return self.tool_plane_mode == "gateway"

    def require_mcp_arns(self) -> None:
        """도구 평면 모드에 맞춰 필수 env 를 조기에 검증한다(배포 구성 오류 방지).

        - direct 모드: `SQL_MCP_ARN` / `SEMANTIC_MCP_ARN` 필수.
        - gateway 모드: `GATEWAY_URL` + Cognito M2M env 4종 필수.
        """
        if self.is_gateway_mode():
            required = (
                ("GATEWAY_URL", self.gateway_url),
                ("COGNITO_CLIENT_ID", self.cognito_client_id),
                ("COGNITO_USER", self.cognito_user),
                ("COGNITO_PASSWORD_SECRET_ARN", self.cognito_password_secret_arn),
                ("COGNITO_USER_POOL_ID", self.cognito_user_pool_id),
            )
        else:
            required = (
                ("SQL_MCP_ARN", self.sql_mcp_arn),
                ("SEMANTIC_MCP_ARN", self.semantic_mcp_arn),
            )
        missing = [name for name, value in required if not value]
        if missing:
            raise ValueError(f"필수 환경 변수 누락: {', '.join(missing)}")


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default
