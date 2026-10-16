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
        # 정산 이상(kind = cost | tpm). 둘 다 0 이 정상이다.
        #  - settle_clamped: 정산 결과가 음수가 될 뻔해 0 으로 둔 횟수 — 이 수정 이전 코드가
        #    남긴 음수나 축출된 예약의 신호다. 예전에는 이 하한이 이중 환불을 조용히 가렸다.
        #  - settle_duplicate: 이미 정산된 예약을 다시 정산하려 해 무시한 횟수 — 재시도된 명령이나
        #    정산 상태 사본의 이중 정산 결함 신호다.
        self.rl_settle_clamped_total: Counter = meter.create_counter(
            "gateway_rl_settle_clamped_total",
            description="Rate-limit settlements clamped at 0 instead of going negative",
            unit="1",
        )
        self.rl_settle_duplicate_total: Counter = meter.create_counter(
            "gateway_rl_settle_duplicate_total",
            description=(
                "Rate-limit settlements ignored because the reservation was already settled"
            ),
            unit="1",
        )
        self.cache_hits_total: Counter = meter.create_counter(
            "gateway_cache_hits_total",
            description="Total Redis cache hits",
        )
        # ⚠️ request(분모)와 error(분자)는 **함께** 있어야 의미가 있다. 예전엔 error 만
        #    선언돼 있고 그것마저 어디서도 증가하지 않아, 모델별 에러율을 낼 수 없었다.
        #    라벨 구성은 observability/provider_metrics.py 한 곳에서만 만든다 —
        #    호출부마다 다른 라벨을 붙이면 시계열이 섞여 어느 모델이 죽는지 알 수 없다.
        self.provider_request_total: Counter = meter.create_counter(
            "gateway_provider_request_total",
            description="Total provider calls (denominator for provider error rate)",
        )
        self.provider_error_total: Counter = meter.create_counter(
            "gateway_provider_error_total",
            description="Total provider errors (numerator; labels match provider_request_total)",
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
        # ── `gateway_streaming_chunk_duration_seconds` 는 제거했다 ──
        #
        # 선언만 되어 있고 어디서도 기록되지 않았다. 그리고 이 형태 자체가 맞지 않는다:
        # 청크 간 지연을 **전부** 히스토그램에 넣으면 정상 스트림의 수천 개 짧은 간격이
        # 분포를 지배해, 정작 보고 싶은 "업스트림이 멈췄다" 가 꼬리에 묻힌다.
        #
        # 올바른 형태는 keepalive 핑 간격을 넘긴 구간만 재는 별도 지표
        # (`stream_idle_gap_seconds` + `stream_ping_total`)이고, 그건 스트리밍
        # 제너레이터 3개에 지표 접근을 넣는 별도 작업이다. 그때까지 이름만 남겨 두면
        # "스트림 지연 지표가 있다" 는 잘못된 인상을 준다.

        # Gauges (4개)
        self.active_connections: UpDownCounter = meter.create_up_down_counter(
            "gateway_active_connections",
            description="Number of active connections",
        )
        self.degradation_level: UpDownCounter = meter.create_up_down_counter(
            "gateway_degradation_level",
            description="Current degradation level (0=healthy, 1=db, 2=redis, 3=both)",
        )

        # Observable gauge — 콜백 기반.
        #
        # ⚠️ 콜백을 **등록하지 않으면 값이 하나도 나가지 않는다.** 예전엔
        #    `register_buffer_size_callback` 이 코드베이스 어디에서도 호출되지 않아
        #    이 게이지가 이름만 존재했다. Prometheus 에 시계열이 없으면 대시보드는
        #    "No data" 를, 알람은 영구히 거짓을 낸다 — 사람은 둘 다 "정상" 으로 읽는다.
        #    지표를 선언만 해 두는 것은 없는 것보다 나쁘다.
        #    등록은 main.py 의 lifespan 에서 한다(UsageBufferQueue.size 를 읽는다).
        self._buffer_size_callback: list = []

        self.usage_buffer_size: ObservableGauge = meter.create_observable_gauge(
            "gateway_usage_buffer_size",
            callbacks=self._buffer_size_callback,
            description="Current usage buffer queue size",
        )

        # ── `gateway_budget_remaining_usd` 는 제거했다 ──
        #
        # 남은 예산은 **사용자·팀 단위** 값이다. 라벨 없는 단일 게이지로는 무엇의 잔액인지
        # 말할 수 없고, 라벨을 붙이면 사용자 수만큼 시계열이 생긴다 — 이 Prometheus 는
        # prometheus-adapter 로 HPA 도 먹이므로 카디널리티 폭발은 관측성과 함께
        # 오토스케일링을 죽인다(provider_metrics.py 의 카디널리티 계약 참조).
        # 예산 잔액의 진실의 원천은 admin-api 의 예산 API/DB 이고, 게이트웨이는 요청
        # 시점 판정만 한다. 여기서 또 내보내면 두 개의 진실이 생긴다.

    def register_buffer_size_callback(self, callback) -> None:
        """OTel observable gauge 콜백 등록. main.py lifespan 에서 호출한다.

        콜백 시그니처는 `(CallbackOptions) -> Iterable[Observation]` 이다.
        """
        self._buffer_size_callback.append(callback)
