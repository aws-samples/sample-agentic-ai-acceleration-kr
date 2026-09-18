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
    """auth.user_role 의 모든 라벨을 포함해야 한다.

    JWT claim 은 auth_service.py 에서 Role(r) 로 강제 변환되므로, DB 에 있는 라벨이
    여기 없으면 그 사용자는 이유 없는 401 을 받는다. 실 DB enum =
    (ADMIN, TEAM_LEADER, DEVELOPER) 이고 dev 39명 중 대부분이 DEVELOPER 다.
    """

    USER = "USER"  # VK 인증용 합성 라벨 — DB 라벨이 아니다 (auth_service.py:140)
    DEVELOPER = "DEVELOPER"  # auth.user_role
    TEAM_LEADER = "TEAM_LEADER"
    ADMIN = "ADMIN"


class ProviderType(str, Enum):
    BEDROCK = "BEDROCK"
    OPENMODEL = "OPENMODEL"
    BEDROCK_MANTLE = "BEDROCK_MANTLE"  # Cowork → 905 Bedrock Mantle (Tokyo Opus 4.8)
    BEDROCK_MANTLE_OPENAI = "BEDROCK_MANTLE_OPENAI"  # Codex → 859 Bedrock Mantle GPT-5.5 (Ohio, Responses API)
    # GPT-5.6 on the STANDARD Bedrock runtime plane (bedrock-runtime.{region}.amazonaws.com),
    # SigV4-signed, addressed through a cross-region inference profile (CRIS: us./global.
    # prefixed model id). Same OpenAI wires as BEDROCK_MANTLE_OPENAI (/v1/responses and
    # /v1/chat/completions) — the difference is the plane, the auth (SigV4 vs bearer) and
    # the model-id form, NOT the request/response dialect. Verified live 2026-09-03.
    BEDROCK_RUNTIME_OPENAI = "BEDROCK_RUNTIME_OPENAI"


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
    """notifications:security 로 나가는 wire 값.

    ⚠️ 값은 notification-worker 의 EventType(notification-worker/src/worker/schemas/
    events.py:21-23) 과 notification.notification_configs.event_type 행
    (db/init/06_seed_notification_configs.sql — 전부 소문자) 과 **정확히** 같아야 한다.
    과거엔 여기가 대문자라 worker 의 pydantic 검증이 매 이벤트를 거부하고
    parse_pubsub_message 가 None 을 반환해, 브루트포스 감지 알림이 한 번도
    전달되지 않았다(dev notification_logs 0행). DB 시드가 tie-breaker 라 생산자를 맞춘다.
    """

    AUTH_FAILURE_SPIKE = "auth_failure_spike"
    PERMISSION_VIOLATION = "permission_violation"
    SUSPICIOUS_USAGE = "suspicious_usage"


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

    # ── Context-band pricing ────────────────────────────────────────────────
    # 일부 모델(현재 GPT-5.6 terra/sol/luna)은 한 요청의 프롬프트가 임계 토큰을 넘으면
    # 그 요청 **전체**를 더 높은 요율로 청구한다. OpenAI 문구를 AWS 모델카드가 그대로
    # 인용: "Prompts with >272K input tokens are priced at 2x input and 1.5x output for
    # the full request." — 계단 하나(초과분 누진 아님).
    #
    # ⚠️ ``threshold`` 가 None 이면 밴드 없음(오늘 동작·Claude/자가호스팅). Anthropic 은
    #    1M 컨텍스트를 표준 단가로 청구하므로 밴드 표가 아예 없다.
    #
    # 배수로 저장하는 이유: short 요율이 유일한 진실원이고 long 은 그로부터 파생된다
    # — 두 요율을 독립 저장하면 서로 어긋날 수 있다(카드가 정확히 2x/2x/1.5x 를 게시).
    long_context_threshold_tokens: int | None = None
    long_context_input_mult: Decimal = Decimal("1")
    long_context_cache_mult: Decimal = Decimal("1")
    long_context_output_mult: Decimal = Decimal("1")


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
    #: 이 모델을 쓸 수 있는 앱 허용목록 — **모델 × 앱** 인가 축(migration 0035).
    #:
    #:   ``None``   미설정 = 제한 없음(하위호환 기본값)
    #:   ``[]``     명시적으로 빈 허용목록 = **어떤 앱도 이 모델을 쓸 수 없다**
    #:   non-empty  허용목록
    #:
    #: ⚠️ 기본값이 ``None`` 이어야 한다. 이 스키마는 Redis 캐시에서
    #:    ``ModelConfigSchema(**json)`` 으로 **검증 없이 복원**되므로, 필드가 없는
    #:    옛 캐시 엔트리는 기본값을 받는다. 기본값을 ``[]`` 로 두면 배포 직후 캐시가
    #:    갈리기 전까지 **모든 모델이 전면 거부**된다.
    allowed_clients: list[str] | None = None


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


def split_cached_input(input_total: int, cached_tokens: int) -> tuple[int, int]:
    """Split an OpenAI-dialect prompt count into ``(non_cached, cached)`` TokenUsage buckets.

    **Dialect asymmetry this exists to absorb.** OpenAI Responses ``usage.input_tokens``
    (and Chat Completions ``usage.prompt_tokens``) is the GRAND TOTAL prompt count and
    ALREADY INCLUDES ``input_tokens_details.cached_tokens``. Anthropic/Bedrock-Converse
    ``input_tokens`` EXCLUDES its cache counters. ``TokenUsage`` uses the Anthropic
    convention (see the class docstring), so OpenAI-dialect parsers must subtract before
    constructing one — otherwise ``calculate_cost`` bills a cached token at ``1.1x`` the
    input rate when ``0.1x`` is correct (an **11x** error on the cached portion), and
    ``compute_tpm_incr`` over-counts TPM by the cached amount.

    Guarantees ``non_cached + cached == max(input_total, 0)``, so a caller that keeps the
    provider's own ``total_tokens`` stays numerically consistent. ``cached`` is clamped to
    ``input_total`` because cached tokens are a SUBSET of the prompt by definition; a
    provider reporting otherwise is upstream corruption, and clamping keeps the invariant
    rather than letting a negative ``input_tokens`` reach budget deduction.
    """
    total = max(int(input_total), 0)
    cached = min(max(int(cached_tokens), 0), total)
    return total - cached, cached


def split_openai_input(
    input_total: int, cached_tokens: int, cache_write_tokens: int
) -> tuple[int, int, int]:
    """Split an OpenAI-dialect prompt count into ``(non_cached, cache_read, cache_write)``.

    The three-bucket sibling of :func:`split_cached_input`, for dialects that report a
    cache-WRITE counter as well. Both Bedrock planes do, and **both nest it inside the
    grand total** — measured live 2026-09-03 with a 3617-token prompt sent three times
    under one ``prompt_cache_key`` (identical on bedrock-runtime and Bedrock Mantle):

        call 1:  input_tokens=3617  cache_write_tokens=3615  cached_tokens=0
        call 2:  input_tokens=3617  cache_write_tokens=0     cached_tokens=3615
        call 3:  input_tokens=3617  cache_write_tokens=0     cached_tokens=3615

    ``input_tokens`` never grows past the prompt, so a cache-write token is one of the
    3617 — not an extra charge on top. :class:`TokenUsage`'s buckets are mutually
    exclusive (see its docstring), so all three counters must be subtracted out of the
    billable input here, at parse time. Splitting only ``cached_tokens`` (what
    :func:`split_cached_input` does) leaves a cache-write prompt billed entirely at the
    input rate and 0 at the cache-write rate — a 20% UNDER-bill on these models, whose
    published cache-write rate is 1.25x input.

    Guarantees ``non_cached + cache_read + cache_write == max(input_total, 0)``, so a
    caller keeping the provider's own ``total_tokens`` stays consistent. ``cache_read``
    is clamped first (it is the money-dominant bucket at 0.1x input, and the one a
    provider reports every steady-state request), then ``cache_write`` takes whatever
    room is left; both are subsets of the prompt by definition, so a sum exceeding
    ``input_total`` is upstream corruption and clamping keeps the invariant rather than
    letting a negative ``input_tokens`` reach budget deduction.
    """
    total = max(int(input_total), 0)
    cache_read = min(max(int(cached_tokens), 0), total)
    cache_write = min(max(int(cache_write_tokens), 0), total - cache_read)
    return total - cache_read - cache_write, cache_read, cache_write


class TokenUsage(BaseModel):
    """Normalized per-request token usage.

    **CONTRACT — the four token fields are MUTUALLY EXCLUSIVE billing buckets.**
    ``input_tokens`` is the non-cached billable input; cached prompt tokens live ONLY in
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens``. Both money paths depend
    on this and neither is dialect-aware: ``cost_recorder.calculate_cost`` bills
    ``input_tokens`` at the full input rate AND ``cache_read_input_tokens`` at the
    cache-read rate, and ``rate_limit_scope.compute_tpm_incr`` sums
    ``input + cache_creation + output``. Overlapping fields double-bill.

    Providers that report a cache-INCLUSIVE prompt count (OpenAI Responses / Chat) MUST
    normalize at parse time via :func:`split_cached_input` — never downstream, because six
    independent consumers read these fields (cost, TPM, OTEL, cost:stream → usage_logs,
    admin analytics, and the client-visible usage rewrite in web_search_loop).

    ``total_tokens`` is a REPORTING/gate field only — no money or enforcement path reads it.
    It is deliberately left as each dialect reports it (Responses: provider grand total,
    i.e. ``input + cache_read + output``; Converse: ``input + output``, cache excluded).
    That inter-dialect inconsistency predates the cache fix and is intentionally NOT
    changed by it; the true grand total is always recoverable from the four stored columns.
    """

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

    def to_envelope(self) -> dict:
        """notification-worker 의 NotificationEvent 모양으로 감싼다.

        ⚠️ worker 의 NotificationEvent(notification-worker/src/worker/schemas/events.py:39-44)
        는 `payload: dict` 를 **필수**로 요구하고, 핸들러/recipient_resolver 는
        payload.get("user_id") 처럼 그 안을 읽는다. 예전엔 model_dump_json() 을 그대로
        발행해 필드가 평평했고, worker 가 "payload Field required" 로 전량 폐기했다.
        envelope 4필드(event_id/type/timestamp/source) 외 도메인 필드는 전부 payload 로.
        """
        d = self.model_dump(mode="json")
        return {
            "event_id": d.pop("event_id"),
            "type": d.pop("type"),
            "timestamp": d.pop("timestamp"),
            "source": d.pop("source"),
            "payload": d,
        }
