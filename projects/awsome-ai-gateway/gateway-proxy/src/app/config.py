# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    uvicorn_workers: int = 4
    uvicorn_host: str = "0.0.0.0"
    uvicorn_port: int = 8000
    max_body_size: int = 20_971_520  # 20MB
    app_version: str = "0.1.0"

    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_pool_size: int = 150
    redis_cluster_mode: bool | None = None  # None = auto-detect
    redis_tls_enabled: bool = False

    # Redis 연결 복원력 (부하 강건성, deepdive Q50 Phase 2).
    # 과거 클라이언트는 max_connections 만 줘 socket_timeout 이 None(무한 블로킹)이라,
    # 느린/블랙홀 노드 하나가 모든 awaited 호출을 멈춰 풀(150)을 전 pod 에서 고갈시켰다.
    #  - socket_timeout: 명령(read) 상한. 초과 시 TimeoutError → 상위 fallback(DB) 로.
    #  - socket_connect_timeout: 연결 수립 상한(failover 직후 죽은 노드 빠른 포기).
    #  - retry: 단발 blip 을 백오프 재시도로 흡수(느린 DB 강등 전 1~2회).
    #  - health_check_interval: idle 연결 주기 ping → failover 후 stale 연결 회수.
    # 0/None 비활성(과거 동작). hot-path 라 값 조정 시 load A/B 권장.
    redis_socket_timeout: float = 2.0
    redis_connect_timeout: float = 1.0
    redis_retries: int = 1
    redis_health_check_interval: float = 30.0

    # 읽기를 replica 로 라우팅(deepdive Q50 Phase4-f). cluster 모드 + replica 있을 때만
    # 의미. read 스케일 확보(GET 류를 replica 가 분담)하나 replica lag 으로 약간의
    # stale read 가능 → TTL 캐시·rate-limit ZSET 처럼 lag 허용 워크로드에서만 ON.
    # 기본 False(primary-only, 강한 일관성 — 기존 동작). standalone 에선 무영향.
    redis_read_from_replicas: bool = False

    # rate-limit 회로 차단기(deepdive Q50 — per-request fast-fail). Redis 가 죽었을 때
    # 매 요청이 socket_timeout 을 다 기다리는 대신, 연속 실패가 임계를 넘으면 회로를
    # 열어 즉시 fallback 으로 보낸다. 기본 활성(소켓 타임아웃 대기 누적 방지). 끄려면
    # rl_breaker_enabled=false. 임계/복구는 아래 값으로.
    rl_breaker_enabled: bool = True
    rl_breaker_fail_threshold: int = 5
    rl_breaker_recovery_timeout: float = 5.0

    # rate-limit eval 이 (Redis 장애 등으로) 실패할 때의 정책(deepdive Q50).
    #  - "open"(기본·기존 동작): 통과시킴(가용성 우선, NFR-2.4 graceful degradation).
    #    Redis 장애가 사용자 차단으로 번지지 않게. rl_fail_open_total 로 가시화.
    #  - "closed": 차단함(보안/쿼타 정확성 우선). 무제한 통과를 허용 못 하는 환경용.
    # **무단 전환 금지** — 이건 가용성↔정확성 트레이드오프라 명시 설정으로만 바꾼다.
    # budget/auth 는 이미 fail-closed(예산 미확인 시 차단) — 의도적 비대칭(예산 초과
    # 과금 방지 > 가용성). rate-limit 만 기본 open 인 건 RL 이 남용방지지 과금 게이트가
    # 아니기 때문. 운영 정책에 따라 closed 로 정렬 가능.
    rl_fail_mode: str = "open"

    # Database
    db_url: str = "postgresql+asyncpg://gateway:gateway@postgres:5432/gateway"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_ssl_mode: str = "disable"
    # RDS Proxy 경유 시 0 으로 설정 (PostgreSQL pinning 회피). Aurora 직접 연결 시엔 기본값 유지.
    db_statement_cache_size: int = 100
    # 풀 고갈 시 커넥션 대기 상한. SQLAlchemy 기본은 30s 라 풀 포화 시 요청당 최대 30초
    # 무한대기성 지연("CPU 정상인데 느림" 시그니처)을 유발한다 → 짧은 값으로 fast-fail(TimeoutError).
    # hot-path(gateway-proxy)는 admin-api(core/db.py)와 정합되게 명시. (견고성 6축검증 축③)
    db_pool_timeout: int = 10
    # 커넥션 최대 수명(초). RDS Proxy/Aurora 가 유휴 커넥션을 끊으면 죽은 커넥션이 풀에 잔존.
    # pool_pre_ping 이 1차 방어하나, recycle 로 오래된 커넥션을 선제 폐기(기본 -1=무제한 회피).
    db_pool_recycle: int = 3600

    # Timeouts
    lua_timeout_ms: int = 1000
    stream_timeout: int = 300
    stream_idle_timeout: int = 60
    stream_disconnect_drain_timeout: int = 30

    # Reliability
    usage_buffer_max: int = 10_000
    background_task_max_retries: int = 3

    # Redis-down in-memory rate-limit fallback (middleware/rate_limit.py).
    # 이 fallback 의 USER RPM 카운터는 **프로세스(uvicorn worker)마다 독립**이라,
    # 함대 전체엔 `replicas × uvicorn_workers` 개의 독립 카운터가 존재한다. 사용자
    # 트래픽이 그만큼 분산되므로 각 카운터는 `limit // (replicas × uvicorn_workers)`
    # 를 허용해야 클러스터 총합이 limit 에 맞는다. 과거엔 divisor 가 4(=uvicorn_workers,
    # 1 pod 가정)로 **하드코딩**되어 HPA replica 수(3~30)를 무시 → fallback 진입 시
    # 쿼타가 현실과 불일치(부하테스트 429×6 의 배경, deepdive Q46/Q50).
    #
    # rl_fallback_replicas 를 **현재 환경의 대표 replica 수(예: HPA minReplicas)**로
    # 설정하면 divisor 가 현실을 추종한다. 기본 1 = 과거 동작(divisor=uvicorn_workers)
    # 보존(무행동변경) — hot-path 라 변경 전 load A/B 필요. Redis 장애 시 발동하는
    # 경로라 SCARD 로 실시간 pod 수를 셀 수 없어 env 설정이 현실적 해법.
    rl_fallback_replicas: int = 1

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # OTel
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_traces_sampler_arg: float = 1.0
    otel_service_name: str = "gateway-proxy"

    # AWS
    aws_region: str = "ap-northeast-2"
    aws_profile: str | None = None

    # Mantle (Cowork cross-account) — region for the STS client used to assume the
    # 905 Mantle role; and the httpx timeout for Mantle streaming calls.
    mantle_assume_region: str = "ap-northeast-2"
    mantle_http_timeout: float = 120.0

    # OpenModel
    openmodel_base_url: str = "http://mock-vllm:8080"

    # --- Model availability fallback / circuit breaker (2026-06-22) ---
    cb_window_sec: int = 30
    cb_min_calls: int = 5
    cb_error_rate: float = 0.5
    cb_open_sec: int = 30
    cb_halfopen_probe_ttl_ms: int = 8000
    cb_open_jitter_sec: int = 5
    bedrock_inference_read_timeout: int = 60
    bedrock_max_attempts: int = 1

    # Features
    cors_enabled: bool = False

    # Request tracing (reasoning/tool_use observability — services/trace_extractor.py)
    # trace_enabled: 응답 본문에서 trace 추출 자체를 켤지(미배선 — 추후 hot-path 배선용).
    # trace_mask_pii: PII 마스킹 토글. **기본 True(fail-safe)** — tool input 값 +
    #   thinking/text PII 패턴을 마스킹. 신뢰 환경에서만 false 로 명시 해제(평문 저장).
    #   설정 누락 시 안전(마스킹)으로 동작하도록 기본값이 True.
    trace_enabled: bool = False
    trace_mask_pii: bool = True

    # --- AgentCore Gateway web search (server-side tool-use loop, 2026-07-01) ---
    # We inject a web_search tool, intercept the model's tool_use, call AgentCore
    # Gateway's managed WebSearch connector over MCP (SigV4/IRSA), feed results
    # back, and stream the final answer — emulating Anthropic 1P server-side search.
    # Bedrock does NOT expose native web_search (verified: ValidationException), so
    # this gateway loop is the only path. The managed connector is us-east-1-only,
    # so AGENTCORE_REGION differs from AWS_REGION (ap-northeast-2) — cross-region;
    # IRSA creds are global, only the SigV4 signing scope + endpoint use us-east-1.
    #   agentcore_gateway_url: full MCP endpoint (…gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp)
    #   agentcore_target_id:   Gateway target name → tool is "<target_id>___WebSearch"
    #   web_search_enabled:    global kill-switch (per-client toggle lives in routing_profiles)
    agentcore_gateway_url: str = ""
    agentcore_region: str = "us-east-1"
    agentcore_target_id: str = "web-search-tool"
    agentcore_http_timeout: float = 30.0
    web_search_enabled: bool = False
    web_search_max_iterations: int = 5
    web_search_total_deadline_sec: float = 90.0
    web_search_max_results_default: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
