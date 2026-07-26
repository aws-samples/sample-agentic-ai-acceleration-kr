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


@dataclass(frozen=True)
class Settings:
    """런타임 설정.

    - sql_mcp_arn / semantic_mcp_arn: AgentCore Runtime 에 호스팅된 MCP 서버 ARN
    - memory_id: AgentCore Memory (STM) ID
    - model_id: Bedrock Claude inference profile
    - region: AWS 리전
    - max_sql_corrections: self-correction 루프 최대 재시도 횟수
    - mode: "graph"(기본, Strands Graph) | "agent"(단일 Agent 폴백)
    """

    sql_mcp_arn: str
    semantic_mcp_arn: str
    memory_id: str | None
    model_id: str
    region: str
    max_sql_corrections: int
    mode: str

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
        )

    def require_mcp_arns(self) -> None:
        """MCP ARN 이 비어 있으면 조기에 실패시킨다(배포 구성 오류 방지)."""
        missing = [
            name
            for name, value in (
                ("SQL_MCP_ARN", self.sql_mcp_arn),
                ("SEMANTIC_MCP_ARN", self.semantic_mcp_arn),
            )
            if not value
        ]
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
