# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from opentelemetry.metrics import Counter, Histogram, ObservableGauge, UpDownCounter

from app.observability._setup import get_meter


class GatewayMetrics:
    """Gateway Proxy 커스텀 메트릭 16개."""

    def __init__(self) -> None:
        meter = get_meter("gateway-proxy")

        # Counters (9개)
        self.request_total: Counter = meter.create_counter(
            "gateway_request_total",
            description="Total number of requests",
        )
        self.error_total: Counter = meter.create_counter(
            "gateway_error_total",
            description="Total number of errors",
        )
        self.token_usage_total: Counter = meter.create_counter(
            "gateway_token_usage_total",
            description="Total tokens used",
        )
        self.cost_usd_total: Counter = meter.create_counter(
            "gateway_cost_usd_total",
            description="Total cost in USD",
            unit="USD",
        )
        self.rate_limit_hits_total: Counter = meter.create_counter(
            "gateway_rate_limit_hits_total",
            description="Total rate limit hits",
        )
        # Redis-down 강등 fallback 관측성 (deepdive Q50 Phase 3). 과거엔 "in-memory
        # fallback 진입"과 "CROSSSLOT 등으로 fail-open(집행 무음 정지)"을 운영자가
        # 구분할 수 없었다. 아래 카운터로 가시화한다.
        #  - rl_fallback_entered: in-memory USER RPM fallback 이 실제 동작한 요청 수.
        #  - rl_fallback_429: fallback 이 막아 거절한(degraded 429) 요청 수.
        #  - rl_fail_open: rate-limit eval 예외로 통과시킨(집행 못한) 요청 수
        #    (scope 라벨). 0 이 아니면 enforcement 가 무음 약화 중 → 알람 대상.
        self.rl_fallback_entered_total: Counter = meter.create_counter(
            "gateway_rl_fallback_entered_total",
            description="In-memory rate-limit fallback path taken (Redis degraded)",
            unit="1",
        )
        self.rl_fallback_429_total: Counter = meter.create_counter(
            "gateway_rl_fallback_429_total",
            description="Requests rejected (429) by the degraded-mode in-memory fallback",
            unit="1",
        )
        self.rl_fail_open_total: Counter = meter.create_counter(
            "gateway_rl_fail_open_total",
            description="Rate-limit checks that failed open (enforcement skipped on error)",
            unit="1",
        )
        self.cache_hits_total: Counter = meter.create_counter(
            "gateway_cache_hits_total",
            description="Total Redis cache hits",
        )
        self.provider_error_total: Counter = meter.create_counter(
            "gateway_provider_error_total",
            description="Total provider errors",
        )
        self.background_task_errors_total: Counter = meter.create_counter(
            "gateway_background_task_errors_total",
            description="Total background task permanent failures",
        )
        self.usage_records_dropped_total: Counter = meter.create_counter(
            "gateway_usage_records_dropped_total",
            description="Total usage records dropped from buffer",
        )

        # Downgrade (FR-3.6)
        self.downgrade_applied_total: Counter = meter.create_counter(
            "gateway_downgrade_applied_total",
            description="Auto-downgrade applied (TEAM scope)",
            unit="1",
        )
        self.downgrade_lookup_failed_total: Counter = meter.create_counter(
            "gateway_downgrade_lookup_failed_total",
            description="Downgrade policy lookup failures (fail-open)",
            unit="1",
        )
        self.downgrade_chain_depth: Histogram = meter.create_histogram(
            "gateway_downgrade_chain_depth",
            description="Number of chained downgrade hops applied",
            unit="1",
        )

        # Histograms (3개)
        self.request_duration: Histogram = meter.create_histogram(
            "gateway_request_duration_seconds",
            description="Request duration in seconds",
            unit="s",
        )
        self.model_request_duration: Histogram = meter.create_histogram(
            "gateway_model_request_duration_seconds",
            description="Model request duration in seconds",
            unit="s",
        )
        self.streaming_chunk_duration: Histogram = meter.create_histogram(
            "gateway_streaming_chunk_duration_seconds",
            description="Time between streaming chunks",
            unit="s",
        )

        # Gauges (4개)
        self.active_connections: UpDownCounter = meter.create_up_down_counter(
            "gateway_active_connections",
            description="Number of active connections",
        )
        self.degradation_level: UpDownCounter = meter.create_up_down_counter(
            "gateway_degradation_level",
            description="Current degradation level (0=healthy, 1=db, 2=redis, 3=both)",
        )

        # Observable gauges (buffer_size, budget_remaining은 콜백 기반)
        self._buffer_size_callback: list = []
        self._budget_remaining_callback: list = []

        self.usage_buffer_size: ObservableGauge = meter.create_observable_gauge(
            "gateway_usage_buffer_size",
            callbacks=self._buffer_size_callback,
            description="Current usage buffer queue size",
        )
        self.budget_remaining: ObservableGauge = meter.create_observable_gauge(
            "gateway_budget_remaining_usd",
            callbacks=self._budget_remaining_callback,
            description="Remaining budget in USD",
            unit="USD",
        )

    def register_buffer_size_callback(self, callback) -> None:
        self._buffer_size_callback.append(callback)

    def register_budget_callback(self, callback) -> None:
        self._budget_remaining_callback.append(callback)
