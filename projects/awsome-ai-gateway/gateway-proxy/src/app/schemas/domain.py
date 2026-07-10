# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class AuthType(str, Enum):
    VIRTUAL_KEY = "VIRTUAL_KEY"
    JWT = "JWT"


class Role(str, Enum):
    USER = "USER"
    TEAM_LEADER = "TEAM_LEADER"
    ADMIN = "ADMIN"


class ProviderType(str, Enum):
    BEDROCK = "BEDROCK"
    OPENMODEL = "OPENMODEL"
    BEDROCK_MANTLE = "BEDROCK_MANTLE"  # Cowork → 905 Bedrock Mantle (Tokyo Opus 4.8)
    BEDROCK_MANTLE_OPENAI = "BEDROCK_MANTLE_OPENAI"  # Codex → 859 Bedrock Mantle GPT-5.5 (Ohio, Responses API)


class ApiFormat(str, Enum):
    BEDROCK_NATIVE = "BEDROCK_NATIVE"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    ANTHROPIC_MESSAGES = "ANTHROPIC_MESSAGES"  # Mantle /anthropic/v1/messages
    OPENAI_RESPONSES = "OPENAI_RESPONSES"  # Mantle /openai/v1/responses (GPT-5.x Responses API)


class BudgetPolicy(str, Enum):
    HARD_BLOCK = "hard_block"
    SOFT_WARNING = "soft_warning"
    THROTTLE = "throttle"


class ModelStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class DegradationLevel(str, Enum):
    HEALTHY = "HEALTHY"
    DB_DEGRADED = "DB_DEGRADED"
    REDIS_DEGRADED = "REDIS_DEGRADED"
    BOTH_DEGRADED = "BOTH_DEGRADED"


class SecurityEventType(str, Enum):
    AUTH_FAILURE_SPIKE = "AUTH_FAILURE_SPIKE"
    PERMISSION_VIOLATION = "PERMISSION_VIOLATION"
    SUSPICIOUS_USAGE = "SUSPICIOUS_USAGE"


class AuthContext(BaseModel):
    user_id: str
    team_id: str
    dept_id: str
    roles: list[Role]
    auth_type: AuthType
    key_id: str | None = None
    allowed_models: list[str] | None = None  # None = 전체 허용
    allowed_clients: list[str] | None = None  # None/[] = both allowed; subset = whitelist
    sso_subject: str | None = None  # OIDC sub claim or stable user identifier


class ModelPricingSchema(BaseModel):
    input_per_1k: Decimal
    output_per_1k: Decimal
    cache_write_per_1k: Decimal = Decimal("0")       # 5-min TTL (default)
    cache_write_1h_per_1k: Decimal = Decimal("0")    # 1-hour TTL
    cache_read_per_1k: Decimal = Decimal("0")


class ModelConfigSchema(BaseModel):
    provider_model_id: str
    alias: str | None = None
    provider: ProviderType
    api_format: ApiFormat
    endpoint: str = ""
    pricing: ModelPricingSchema
    status: ModelStatus
    created_at: datetime | None = None
    description: str | None = None


class BudgetStatus(BaseModel):
    remaining_usd: Decimal
    limit_usd: Decimal
    used_usd: Decimal
    policy: BudgetPolicy
    soft_limit_pct: int | None = 110
    throttle_rpm_pct: int | None = 50
    threshold_pct: int = 0
    thresholds: list[int] = Field(default_factory=lambda: [80, 90, 100])
    throttle_active: bool = False
    # SOFT_WARNING 정책에서 limit ≤ used < limit × soft_limit_pct/100 구간에 True.
    # 미들웨어가 응답에 X-Budget-Warning 헤더 주입할 때 사용.
    soft_warning: bool = False


class RateLimitResult(BaseModel):
    allowed: bool
    remaining: int
    limit: int
    retry_after: int | None = None
    window_reset: int = 0
    # 멀티 스코프: 거부 시 어느 스코프에서 걸렸는지 표시
    scope: str | None = None              # 'USER' | 'TEAM' | 'GLOBAL'
    limit_type: str | None = None         # 'rpm' | 'tpm' | 'parallel'


class CostLimitResult(BaseModel):
    allowed: bool
    scope: str | None = None              # 'USER' | 'TEAM'
    limit_type: str | None = None         # 'cpm' | 'cph'
    limit: Decimal | None = None
    remaining: Decimal | None = None
    retry_after: int | None = None
    reserved_cost: Decimal = Decimal("0")
    window_reset: int = 0


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Reasoning/thinking tokens — VISIBILITY SUBMETRIC ONLY, never a billing input.
    # GPT-5.x (Responses API) returns these in usage.output_tokens_details.reasoning_tokens
    # and ALREADY counts them inside output_tokens; Anthropic extended-thinking tokens
    # also land here when present. Do NOT add to total/cost/TPM (double-billing). 0 = none.
    reasoning_tokens: int = 0
    # True when any cache_control block in the request used ttl=3600 (1-hour cache).
    # Used by calculate_cost to select cache_write_1h_per_1k vs cache_write_per_1k.
    cache_ttl_1h: bool = False
    # KI-08: 스트리밍 disconnect 시 누적 텍스트로 역산한 경우 True.
    # 진짜 provider usage 이벤트면 False. 감사/청구 정확도 분석 용도.
    estimated: bool = False
    # Web search calls performed by the server-side web-search loop for this request.
    # VISIBILITY/ATTRIBUTION metric ONLY, never a token count or billing input — like
    # reasoning_tokens, it is excluded from total_tokens/cost/TPM. Set by web_search_loop
    # (one increment per successful AgentCore WebSearch tools/call). 0 = no search / feature off.
    web_search_count: int = 0


class BudgetThresholdEvent(BaseModel):
    event_id: str
    type: str = "budget_threshold"
    timestamp: str
    source: str = "gateway-proxy"
    user_id: str
    user_name: str
    team_id: str
    team_name: str
    threshold_pct: int
    current_used_usd: Decimal
    max_budget_usd: Decimal
    remaining_usd: Decimal
    period: str
    policy: BudgetPolicy
    target_type: str  # "user" | "team"


class SecurityEvent(BaseModel):
    event_id: str
    type: SecurityEventType
    timestamp: str
    source: str = "gateway-proxy"
    source_ip: str
    failure_count: int
    window_minutes: int
    auth_type: AuthType
    details: str = ""
