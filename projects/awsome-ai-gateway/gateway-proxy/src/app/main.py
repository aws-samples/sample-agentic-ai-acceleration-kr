# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import boto3
import httpx
import structlog
from botocore.config import Config as BotoConfig
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.metrics import Observation

from app.config import get_settings
from app.db import create_db_engine, create_session_factory
from app.degradation.manager import DegradationManager
from app.middleware.auth import AuthMiddleware
from app.middleware.budget import BudgetMiddleware
from app.middleware.client_authz import ClientAuthorizationMiddleware
from app.middleware.client_id import ClientIdentificationMiddleware
from app.middleware.downgrade import DowngradeMiddleware
from app.middleware.otel import HeaderInjectorMiddleware, OTelMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.observability import GatewayMetrics, init_otel, shutdown_otel
from app.providers.bedrock_adapter import BedrockAdapter
from app.providers.bedrock_openai_adapter import BedrockOpenAIAdapter
from app.providers.mantle_adapter import MantleAdapter
from app.providers.mantle_openai_adapter import MantleOpenAIAdapter
from app.providers.openmodel_adapter import OpenModelAdapter
from app.providers.registry import ProviderRegistry
from app.redis_client import create_redis_client
from app.resilience.buffer_queue import UsageBufferQueue
from app.resilience.cost_stream_spool import CostStreamSpool
from app.resilience.retry_worker import RetryWorker
from app.routers import bedrock, health, messages, openai_compat, usage
from app.schemas.domain import ProviderType
from app.security.event_detector import SecurityEventDetector
from app.services import bg_tasks
from app.services.agentcore_mcp_client import AgentCoreMcpClient
from app.services.bedrock_account_client import BedrockAccountClientProvider, create_sts_client
from app.services.circuit_breaker import CircuitBreakerService
from app.services.cost_recorder import CostRecorder
from app.services.fallback_resolver import FallbackResolver
from app.services.health_checker import HealthChecker
from app.services.lua_loader import LuaScriptLoader
from app.services.mantle_credentials import MantleCredentialBroker
from app.services.rate_limit_service import set_fail_open_metric, set_settle_metrics
from app.services.routing_profile_loader import RoutingProfileLoader
from app.services.sigv4_signer import SigV4Signer
from app.services.tokenizer import TokenizerService

logger = structlog.get_logger(__name__)


def configure_structlog(settings) -> None:
    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer()
            if settings.log_format == "json"
            else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            __import__("logging").getLevelName(settings.log_level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(),
    )



def build_body_logger(settings):
    """설정에서 BodyLogger 를 만든다.

    ⚠️ `firehose_stream_name` 이 없으면 boto3 클라이언트를 **만들지 않는다** — 그러면
       로거는 큐에 넣고 버리는 no-op 이 되어 로컬/compose 에서 자격증명 없이도 안전하다.
       이게 본문 수집을 막는 두 겹 잠금 중 첫 번째다(두 번째는 런타임 토글, 기본 OFF).
    """
    from app.services.body_logger import BodyLogger

    firehose_client = None
    s3_client = None
    if settings.firehose_stream_name:
        firehose_client = boto3.client("firehose", region_name=settings.aws_region)
        s3_client = boto3.client("s3", region_name=settings.aws_region)
    return BodyLogger(
        firehose_client=firehose_client,
        s3_client=s3_client,
        stream_name=settings.firehose_stream_name,
        bucket=settings.body_log_s3_bucket,
        enabled=settings.body_logging_enabled,
        max_queue=settings.body_log_max_queue,
        batch_size=settings.body_log_batch_size,
        flush_interval=settings.body_log_flush_interval,
        max_record_bytes=settings.body_log_max_record_bytes,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_structlog(settings)

    # 1. OTel 초기화
    init_otel(settings)
    logger.info("otel_initialized", service=settings.otel_service_name)

    # 2. Redis 클라이언트
    redis = await create_redis_client(settings)

    # 3. DB Engine + Session Factory
    db_engine = create_db_engine(settings)
    session_factory = create_session_factory(db_engine)

    # 4. httpx AsyncClient (OpenModel용)
    httpx_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=10, read=settings.stream_timeout, write=10, pool=30),
    )

    # 5. boto3 Bedrock Client
    # retries 배선(견고성 6축검증 축⑥): bedrock_max_attempts 를 BotoConfig 에 실제 연결한다.
    # 과거엔 정의만 있고 미배선(DEAD CONFIG) → botocore 기본 legacy(총 5회 재시도)가 동작해
    # 우리 fallback 루프(최대 6후보)와 곱해져 폭풍을 증폭시켰다. 게이트웨이는 자체 fallback 이
    # 있으므로 upstream 재시도는 억제(기본 1=재시도 없음)한다.
    # read_timeout 은 stream_timeout(300s) 유지 — 동일 클라이언트가 invoke_stream(장시간
    # SSE)에도 쓰이므로 bedrock_inference_read_timeout(60s)로 낮추면 긴 스트림이 끊긴다.
    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=settings.aws_region,
        config=BotoConfig(
            max_pool_connections=50,
            connect_timeout=10,
            read_timeout=settings.stream_timeout,
            retries={"total_max_attempts": settings.bedrock_max_attempts, "mode": "standard"},
        ),
    )

    # 6. Lua Scripts 로드
    script_dir = Path(__file__).parent / "redis_scripts"
    LuaScriptLoader.load_all(script_dir)

    # 7. Degradation Manager
    degradation_manager = DegradationManager()

    # 8. Usage Buffer Queue
    usage_buffer = UsageBufferQueue(max_size=settings.usage_buffer_max)

    # 9. Retry Worker
    retry_worker = RetryWorker(max_retries=settings.background_task_max_retries)

    # 9b. Cost-stream dead-letter spool (P0-②): catches cost:stream XADD failures
    # (Redis down at finalize) and re-publishes on recovery → no permanent loss.
    from app.services.cost_recorder import COST_STREAM_KEY, COST_STREAM_MAXLEN

    cost_stream_spool = CostStreamSpool(
        stream_key=COST_STREAM_KEY,
        maxlen=settings.usage_buffer_max,
        maxlen_field=COST_STREAM_MAXLEN,
    )

    # 10. Health Checker — drains the cost-stream spool on each healthy Redis check.
    health_checker = HealthChecker(
        redis, db_engine, degradation_manager, cost_stream_spool=cost_stream_spool
    )

    # 11. GatewayMetrics
    gateway_metrics = GatewayMetrics()
    degradation_manager.set_metrics(gateway_metrics.degradation_level)
    usage_buffer.set_metrics(gateway_metrics.usage_records_dropped_total)
    # ⚠️ observable gauge 는 콜백을 등록해야 값이 나간다. set_metrics 처럼 카운터를
    #    넘기는 것과는 다른 메커니즘이고, 예전엔 이 한 줄이 없어서
    #    `gateway_usage_buffer_size` 가 이름만 존재했다(시계열 0개 = 대시보드 "No data").
    #    버퍼가 차오르는 것은 cost-recorder 경로가 막혔다는 가장 이른 신호다.
    gateway_metrics.register_buffer_size_callback(
        lambda _options: [Observation(usage_buffer.size)]
    )
    retry_worker.set_metrics(gateway_metrics.background_task_errors_total)
    cost_stream_spool.set_metrics(gateway_metrics.usage_records_dropped_total)
    # rate-limit fail-open 관측성(deepdive Q50 Phase 3) — eval 예외로 집행 못한 횟수.
    set_fail_open_metric(gateway_metrics.rl_fail_open_total)
    # 정산 이상 관측 — 0 하한(clamped)과 이미 정산된 예약의 재정산(duplicate).
    set_settle_metrics(
        clamped=gateway_metrics.rl_settle_clamped_total,
        duplicate=gateway_metrics.rl_settle_duplicate_total,
    )

    # 12. Provider Registry
    provider_registry = ProviderRegistry()

    # 12'. Cross-account Bedrock credential/client broker — created BEFORE the adapters
    # because all three Bedrock planes take it. Original role (12c' below): claude-code's
    # routing-profile cross-account on Bedrock NATIVE. Since migration 0043 it is also the
    # ONE credential cache for per-alias target accounts on every plane (native boto3
    # clients, runtime SigV4, Mantle bearers), keyed (role, ExternalId, session name), so an
    # alias account is assumed once and never carries two expiries. The account clients it
    # builds reuse the in-account client's BotoConfig (streaming timeout parity).
    # The STS client itself has its own short timeouts (connect 3 s, read 5 s, standard
    # retries, 3 attempts — bedrock_account_client.STS_BOTO_CONFIG). With botocore's
    # defaults (60 s + 60 s, legacy retries, 5 attempts) one AssumeRole against an STS that never
    # answers took ~315 s, and every request queued behind it waited that long first.
    sts_client = create_sts_client(settings.mantle_assume_region)
    bedrock_account_client_provider = BedrockAccountClientProvider(
        sts_client=sts_client,  # same STS(mantle_assume_region); creds are account-global
        boto_config=BotoConfig(
            max_pool_connections=50,
            connect_timeout=10,
            read_timeout=settings.stream_timeout,
            # in-account 클라이언트와 동일한 retries 정책(폭풍 증폭 억제) — 축⑥.
            retries={"total_max_attempts": settings.bedrock_max_attempts, "mode": "standard"},
        ),
    )

    # alias 계정 클라이언트는 파드 리전(settings.aws_region, in-account 클라이언트와 같다)에서
    # 만든다 — alias 계정은 서명 주체만 바꾸고 리전은 바꾸지 않는다.
    bedrock_adapter = BedrockAdapter(
        bedrock_client,
        account_clients=bedrock_account_client_provider,
        account_region=settings.aws_region,
    )
    openmodel_adapter = OpenModelAdapter(httpx_client, settings.openmodel_base_url)
    provider_registry.register(ProviderType.BEDROCK, bedrock_adapter)
    provider_registry.register(ProviderType.OPENMODEL, openmodel_adapter)

    # 12b. Mantle (Cowork cross-account) — broker assumes the 905 role at request time
    # (no long-lived 905 keys held). Mantle is HTTP+bearer, NOT boto3 invoke_model.
    # The routing-profile path keeps its own credential cache exactly as before; only
    # per-alias accounts (0043) mint from the shared provider.
    mantle_broker = MantleCredentialBroker(
        sts_client=sts_client, account_provider=bedrock_account_client_provider
    )
    mantle_http = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=5, read=settings.mantle_http_timeout, write=10, pool=30
        )
    )
    mantle_adapter = MantleAdapter(http_client=mantle_http, broker=mantle_broker)
    provider_registry.register(ProviderType.BEDROCK_MANTLE, mantle_adapter)

    # 12c. Mantle OpenAI (Codex in-account) — same broker/http client, OpenAI Responses
    # wire instead of Anthropic Messages. Codex's account == gateway IRSA account (123),
    # so the broker uses the in-account credential path (routing_profiles.account_role_arn
    # IS NULL); no cross-account assume. Region (us-east-2) comes from the routing profile.
    mantle_openai_adapter = MantleOpenAIAdapter(http_client=mantle_http, broker=mantle_broker)
    provider_registry.register(ProviderType.BEDROCK_MANTLE_OPENAI, mantle_openai_adapter)

    # 12c''. Bedrock RUNTIME OpenAI (GPT-5.6 via CRIS, 2026-09-03). Same OpenAI wires as
    # 12c but the STANDARD plane: bedrock-runtime.{region}.amazonaws.com/openai, SigV4
    # instead of a bearer, and a cross-region inference-profile model id (us./global.).
    # Reuses mantle_http — identical traffic shape (long-lived SSE POSTs to an AWS
    # endpoint), so one connection pool with one timeout policy serves both planes.
    # Same STS client as the other cross-account paths; when a routing profile has no
    # account_role_arn the signer uses the pod's own IRSA identity (in-account).
    # Registering unconditionally is safe: nothing routes here until a model row carries
    # provider=BEDROCK_RUNTIME_OPENAI, so this is inert until the catalogue opts in.
    bedrock_openai_adapter = BedrockOpenAIAdapter(
        http_client=mantle_http,
        signer=SigV4Signer(sts_client=sts_client, account_provider=bedrock_account_client_provider),
    )
    provider_registry.register(ProviderType.BEDROCK_RUNTIME_OPENAI, bedrock_openai_adapter)

    # 12c'. Cross-account Bedrock NATIVE (claude-code → 374) uses the provider built in 12'.
    # Assumes the 374 role at request time (no long-lived 374 keys), builds/caches a
    # bedrock-runtime client from the temp creds. boto3 invoke_model, NOT Mantle bearer. The
    # router only uses that path when a routing profile has backend=invoke AND
    # account_role_arn set; otherwise the default in-account bedrock_adapter is used
    # (zero-regression).

    # 12d. AgentCore MCP client (server-side web search, 2026-07-01). Only constructed
    # when a gateway URL is configured — else web_search_loop sees None and the router
    # branch is skipped (zero-regression). SigV4/IRSA to us-east-1; reuses the Mantle
    # httpx client (same bearer/HTTP style). See services/agentcore_mcp_client.py.
    agentcore_mcp_client = None
    if settings.agentcore_gateway_url:
        agentcore_mcp_client = AgentCoreMcpClient(
            http_client=mantle_http,
            gateway_url=settings.agentcore_gateway_url,
            region=settings.agentcore_region,
            target_id=settings.agentcore_target_id,
            timeout=settings.agentcore_http_timeout,
            handshake_timeout=settings.agentcore_handshake_timeout,
            handshake_negative_ttl=settings.agentcore_handshake_negative_ttl,
        )
        logger.info(
            "agentcore_mcp_client_configured",
            region=settings.agentcore_region,
            target_id=settings.agentcore_target_id,
        )

    # 13. Cost Recorder (2026-04-20 리팩터: DB I/O → cost-recorder-worker로 이관,
    # critical path에 inline rate limit/budget enforcement + XADD cost:stream만 남김)
    cost_recorder = CostRecorder(metrics=gateway_metrics, spool=cost_stream_spool)

    # 13b. Tokenizer (KI-08: 스트리밍 disconnect 시 누적 텍스트로 output 토큰 역산)
    tokenizer_service = TokenizerService(bedrock_client=bedrock_client)

    # 14. Security Event Detector
    security_detector = SecurityEventDetector()
    security_detector.set_redis(redis)

    # 14b. Circuit Breaker Service (availability fallback)
    circuit_breaker = CircuitBreakerService(
        window_sec=settings.cb_window_sec,
        min_calls=settings.cb_min_calls,
        error_rate=settings.cb_error_rate,
        open_sec=settings.cb_open_sec,
        halfopen_ttl_ms=settings.cb_halfopen_probe_ttl_ms,
        open_jitter_sec=settings.cb_open_jitter_sec,
    )

    # 14c. Fallback Resolver — load downgrade chain from DB at startup.
    # Uses ALL active downgrade_policies rows (any scope/team) to build the
    # global from_alias -> to_alias chain for availability fallback routing.
    # If DB is unavailable at startup, falls back to an empty chain (= no
    # fallback, safe degradation). A gateway restart picks up policy changes.
    _fallback_chain: dict[str, str] = {}
    try:
        from sqlalchemy import select as sa_select

        from app.models.budget import DowngradePolicy

        async with session_factory() as _db:
            _result = await _db.execute(
                sa_select(
                    DowngradePolicy.from_model_alias,
                    DowngradePolicy.to_model_alias,
                )
                .where(DowngradePolicy.is_active.is_(True))
                # Deterministic order: a from_alias may have active rows across
                # multiple (scope, scope_id) tuples; ORDER BY makes "first wins"
                # stable across restarts instead of relying on row arrival order.
                .order_by(
                    DowngradePolicy.from_model_alias,
                    DowngradePolicy.scope,
                    DowngradePolicy.scope_id,
                )
            )
            for _row in _result.all():
                # First mapping wins (deterministic order); duplicate from_alias
                # entries are deduped keeping the first encountered row.
                if _row.from_model_alias not in _fallback_chain:
                    _fallback_chain[_row.from_model_alias] = _row.to_model_alias
        logger.info("fallback_chain_loaded", entries=len(_fallback_chain))
    except Exception as _exc:
        logger.warning("fallback_chain_load_failed", error=str(_exc))

    fallback_resolver = FallbackResolver(chain=_fallback_chain)

    # 14d. Alias → provider map — loaded at startup so the per-request same_provider
    # predicate (messages.py) can admit same-provider fallback candidates while
    # excluding cross-provider ones.  Covers ALL model_aliases rows so every alias
    # that can appear in a downgrade chain is represented.
    # If DB is down at startup the map is empty → safe: all candidates excluded
    # (same conservative behaviour as an empty fallback chain).
    _alias_provider_map: dict[str, str] = {}
    try:
        from sqlalchemy import select as sa_select_alias

        from app.models.model import ModelAlias

        async with session_factory() as _db:
            _alias_result = await _db.execute(
                sa_select_alias(ModelAlias.alias, ModelAlias.provider)
            )
            for _alias_row in _alias_result.all():
                _alias_provider_map[_alias_row.alias] = _alias_row.provider
        logger.info("alias_provider_map_loaded", entries=len(_alias_provider_map))
    except Exception as _exc:
        logger.warning("alias_provider_map_load_failed", error=str(_exc))

    # 15. Auto-instrumentation
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        RedisInstrumentor().instrument()
        SQLAlchemyInstrumentor().instrument(engine=db_engine.sync_engine)
    except Exception:
        logger.warning("otel_instrumentation_failed")

    # 백그라운드 태스크 시작
    asyncio.create_task(retry_worker.run())
    await health_checker.start()

    # Body logger (요청/응답 본문 → Firehose → S3). 스트림 미설정이면 no-op.
    body_logger = build_body_logger(settings)
    await body_logger.start()

    # 본문 로깅 런타임 토글(관리자 제어, DB+Redis). 요청마다 짧은 in-process TTL 캐시로
    # 읽는다. **기본 OFF** — 배포만으로는 수집이 시작되지 않는다.
    from app.services.body_log_flag import BodyLogFlag

    body_log_flag = BodyLogFlag(
        ttl=settings.body_log_flag_cache_ttl,
        default=settings.body_log_flag_default,
    )

    # app.state에 저장
    app.state.settings = settings
    app.state.redis = redis
    app.state.db_engine = db_engine
    app.state.session_factory = session_factory
    app.state.httpx_client = httpx_client
    app.state.bedrock_client = bedrock_client
    app.state.degradation_manager = degradation_manager
    app.state.usage_buffer = usage_buffer
    app.state.cost_stream_spool = cost_stream_spool
    app.state.retry_worker = retry_worker
    app.state.health_checker = health_checker
    app.state.metrics = gateway_metrics
    app.state.body_logger = body_logger
    app.state.body_log_flag = body_log_flag
    # RouterService 는 라우터 모듈 3곳에서 각각 인스턴스화되므로 **클래스** 속성에 넣는다
    # (router_service.py 의 주석 참조). 캐시 히트율은 Redis 부하와 DB 폴백 빈도를 함께
    # 설명하는 유일한 신호다 — 히트가 떨어지면 그 다음에 오는 것은 DB 커넥션 고갈이다.
    from app.services.router_service import RouterService as _RouterService

    _RouterService.metrics = gateway_metrics
    app.state.provider_registry = provider_registry
    app.state.routing_profile_loader = RoutingProfileLoader()
    app.state.mantle_http_client = mantle_http
    app.state.agentcore_mcp_client = agentcore_mcp_client
    app.state.bedrock_account_client_provider = bedrock_account_client_provider
    app.state.cost_recorder = cost_recorder
    app.state.tokenizer = tokenizer_service
    app.state.security_detector = security_detector
    app.state.circuit_breaker = circuit_breaker
    app.state.fallback_resolver = fallback_resolver
    app.state.alias_provider_map = _alias_provider_map

    logger.info("gateway_proxy_started")
    yield

    # Shutdown
    logger.info("gateway_proxy_shutting_down")
    await health_checker.stop()
    # 떼어 낸 회계 태스크를 **먼저** 끝낸다 — 그것들이 쓰는 싱크를 닫기 전에.
    #
    # shield 는 요청 취소만 막는다(services/bg_tasks.py). 이 drain 이 없으면 회계는 여전히
    # 프로세스 셧다운에 노출되고, 롤링 배포마다 같은 조용한 원장 누락이 생긴다. 순서가 핵심이다:
    #   · body_logger.stop() 앞 — 진행 중인 finalize 가 끝에 본문 로그(on_complete)를 넣는다.
    #     stop() 은 flusher 를 취소한 뒤 큐를 비우므로, 그 뒤에 들어온 레코드는 조용히 버려진다.
    #   · redis.aclose()/db_engine.dispose() 앞 — finalize 가 차감과 원장 XADD 에 Redis 를 쓴다.
    #   · cost_stream_spool.drain() 앞 — 이 태스크들이 spool 한 레코드도 마지막 재발행을 받게.
    # ⚠️ 끝까지 기다리지 못한 것을 그대로 두면 asyncio.run 의 teardown 이 그 finally 를 싱크가
    #    닫힌 **뒤에** 돌린다. 그래서 10s 뒤에도 상류를 읽고 있는 drain 은 한 번 취소해(그때까지
    #    읽은 만큼 회계) 2s 더 기다리고(창 끝에 처음 본 drain 은 0.5s 더), 그래도 남은 것은
    #    이름·요청 id 를 로그한다(bg_tasks.drain). 최대 12.5s — pod 의
    #    terminationGracePeriodSeconds(60) 안이다.
    try:
        completed = await bg_tasks.drain(timeout=10.0, cancel_grace=2.0)
        if completed:
            logger.info("bg_tasks_drained_on_shutdown", count=completed)
    except Exception:
        logger.warning("bg_tasks_shutdown_drain_failed")
    # 큐에 남은 레코드를 flush 한다 — 종료 시 버리면 감사 기록에 구멍이 생긴다.
    await body_logger.stop()
    await retry_worker.stop()
    # P0-②: last-chance drain of the cost-stream spool before closing Redis, so
    # records buffered during a recent blip get one final re-publish attempt.
    try:
        drained = await cost_stream_spool.drain(redis)
        if drained:
            logger.info("cost_stream_spool_drained_on_shutdown", count=drained)
        if cost_stream_spool.size:
            logger.warning(
                "cost_stream_spool_lost_on_shutdown", remaining=cost_stream_spool.size
            )
    except Exception:
        logger.warning("cost_stream_spool_shutdown_drain_failed")
    await httpx_client.aclose()
    await mantle_http.aclose()
    await redis.aclose()
    await db_engine.dispose()
    await shutdown_otel()


def create_app() -> FastAPI:
    app = FastAPI(
        title="LLM Gateway — Gateway Proxy",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    # 실행 순서 (요청 처리 순):
    #   OTel → ClientId → Auth → ClientAuthZ → Budget → Downgrade → RateLimit → HeaderInjector → router
    # 의존성 (변경 시 주의):
    #   - ClientAuthorizationMiddleware는 state["auth_context"] (Auth) + state["client"] (ClientId) 를 읽으므로 Auth/ClientId 뒤에 와야 함
    #   - DowngradeMiddleware는 state["budget_status"].threshold_pct 를 읽으므로 Budget 뒤에 와야 함
    #   - RateLimitMiddleware의 budget-throttle fallback도 budget_status.throttle_* 를 읽으므로
    #     Budget 뒤에 와야 함
    #   - starlette add_middleware는 LIFO이므로 코드 순서는 실행 순서의 역순
    app.add_middleware(HeaderInjectorMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(DowngradeMiddleware)
    app.add_middleware(BudgetMiddleware)
    app.add_middleware(ClientAuthorizationMiddleware)   # runs after Auth+ClientId, before Budget
    app.add_middleware(AuthMiddleware)
    app.add_middleware(ClientIdentificationMiddleware)
    app.add_middleware(OTelMiddleware)

    # 라우터 등록
    app.include_router(health.router)
    app.include_router(bedrock.router)
    app.include_router(messages.router)
    app.include_router(openai_compat.router)
    app.include_router(usage.router)

    # 미들웨어에서 app.state 접근을 위한 scope["state"] 주입 미들웨어 (pure ASGI).
    # BaseHTTPMiddleware (@app.middleware("http")) 는 StreamingResponse 와
    # 호환되지 않아 stream=true 요청이 anyio.EndOfStream → "No response returned"
    # 로 500 을 반환하는 알려진 이슈가 있어 pure ASGI 로 작성. 의존 순서:
    # OTel → Auth → Budget → Downgrade → RateLimit → HeaderInjector → state → router
    # add_middleware 는 LIFO 라 "마지막 등록 = 가장 안쪽" 이므로 state 는
    # 라우터 직전에 위치하도록 등록 순서상 최하단에 둔다.
    class StateInjectionMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            state = scope.setdefault("state", {})
            app_state = scope["app"].state
            state["_redis"] = app_state.redis
            state["_session_factory"] = app_state.session_factory
            state["_degradation_manager"] = app_state.degradation_manager
            state["_security_detector"] = app_state.security_detector

            # 주의: session 은 여기서 열지 않는다. 요청 전체 life cycle 동안
            # session 을 유지하면 SSE 스트리밍 중 "idle in transaction" 으로
            # DB pool 이 회전하지 못해 pool exhausted → 500/degraded cascade 유발.
            # 각 consumer (middleware / router) 가 `_session_factory` 로
            # short-lived session 을 필요 시점에만 열고 닫는다.
            await self.app(scope, receive, send)

    app.add_middleware(StateInjectionMiddleware)

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"type": "internal_error", "message": "Internal server error"}},
        )

    return app


app = create_app()
