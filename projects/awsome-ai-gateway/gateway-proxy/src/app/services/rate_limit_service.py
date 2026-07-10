# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import time
from decimal import Decimal
from uuid import uuid4

import structlog

from app.schemas.domain import CostLimitResult, RateLimitResult
from app.services.lua_loader import LuaScriptLoader
from app.services.rate_limit_scope import (
    RateLimitScope,
    ScopeDescriptor,
    build_rl_key,
    build_tpm_key_group,
)

logger = structlog.get_logger(__name__)

_DEFAULT_WINDOW_MS = 60_000

# 관측성 hook(deepdive Q50 Phase 3) — rate-limit eval 이 예외로 fail-open(집행 못함)
# 한 횟수를 셀 카운터. main.py lifespan 에서 set_fail_open_metric(metrics.rl_fail_open_total)
# 로 주입. 미설정(테스트 등)이면 no-op. degradation_manager.set_metrics 와 동형 패턴.
_fail_open_counter = None


def set_fail_open_metric(counter) -> None:
    """fail-open 카운터 주입(startup). counter 는 OTel Counter(.add(n, attrs))."""
    global _fail_open_counter
    _fail_open_counter = counter


def _record_fail_open(scope: str, limit_type: str) -> None:
    if _fail_open_counter is not None:
        try:
            _fail_open_counter.add(1, {"scope": scope, "limit_type": limit_type})
        except Exception:  # 메트릭 실패가 hot-path 를 깨면 안 됨
            pass


def _fail_closed() -> bool:
    """rate-limit eval 실패 시 차단(True)할지 통과(False)할지 — 설정 정책(deepdive Q50).

    기본 'open'(통과, 기존 동작). 'closed' 명시 시에만 차단. 무단 전환 방지 위해
    설정값으로만 결정. (가용성↔정확성 트레이드오프 — config 주석 참조.)
    """
    from app.config import get_settings

    return get_settings().rl_fail_mode.strip().lower() == "closed"


def _on_eval_failure(scope: str, limit_type: str) -> RateLimitResult | None:
    """eval 실패 공통 처리: 카운트 + 정책에 따른 결과.

    반환 None → 호출자가 '통과/다음 scope 계속'(fail-open). 반환 RateLimitResult
    (allowed=False) → 즉시 차단(fail-closed). 두 경우 모두 fail_open_total 로 가시화
    (closed 여도 '집행 못 함'을 카운트해 Redis 장애를 알린다).
    """
    _record_fail_open(scope, limit_type)
    if _fail_closed():
        return RateLimitResult(
            allowed=False,
            remaining=0,
            limit=-1,
            retry_after=1,
            scope=scope,
            limit_type=limit_type,
        )
    return None


# 회로 차단기(deepdive Q50) — Redis eval 연속 실패 시 회로를 열어 후속 호출을
# socket_timeout 대기 없이 즉시 fast-fail(→ fallback). lazy 싱글톤(설정 1회 로드).
# rl_breaker_enabled=false 면 None → 게이팅 비활성(과거 동작).
_breaker = None
_breaker_init = False


def _get_breaker():
    global _breaker, _breaker_init
    if not _breaker_init:
        _breaker_init = True
        from app.config import get_settings

        s = get_settings()
        if s.rl_breaker_enabled:
            from app.degradation.circuit_breaker import CircuitBreaker

            _breaker = CircuitBreaker(
                fail_threshold=s.rl_breaker_fail_threshold,
                recovery_timeout=s.rl_breaker_recovery_timeout,
                name="rate_limit_redis",
            )
    return _breaker


def reset_breaker_for_test() -> None:
    """테스트 격리용 — breaker 싱글톤 재초기화."""
    global _breaker, _breaker_init
    _breaker = None
    _breaker_init = False


async def _guarded_eval(redis, *args):
    """breaker 를 통과시키는 redis.eval 래퍼.

    회로가 OPEN(allow()=False)이면 eval 을 **호출하지 않고** CircuitOpenError 를
    던진다 → 호출자의 except 가 fail-open(다음 scope/fallback)으로 처리(소켓 타임아웃
    대기 없음). 호출 성공/실패는 breaker 에 보고해 상태를 전이한다. breaker 비활성이면
    그냥 eval(과거 동작).
    """
    breaker = _get_breaker()
    if breaker is None:
        return await redis.eval(*args)

    from app.degradation.circuit_breaker import CircuitOpenError

    if not breaker.allow():
        raise CircuitOpenError("rate_limit redis circuit open")
    try:
        out = await redis.eval(*args)
    except Exception:
        breaker.record_failure()
        raise
    breaker.record_success()
    return out


class RateLimitService:
    """Redis Lua 기반 멀티 스코프 Sliding Window RPM/TPM + CPM/CPH 비용 Rate Limit.

    멀티 스코프 체크는 ``check_multi_scope_rpm`` 사용.
    ``check_rpm``/``check_tpm``는 단일 스코프 호환 래퍼.
    """

    async def check_multi_scope_rpm(
        self,
        redis,
        descriptors: list[ScopeDescriptor],
        request_id: str | None = None,
        window_ms: int = _DEFAULT_WINDOW_MS,
    ) -> RateLimitResult:
        """여러 스코프의 RPM을 **스코프별 1회 Lua 호출**로 체크.

        디스크립터 순서 = fast-fail 순서 (USER → TEAM → GLOBAL).
        첫 violation에서 거부 반환.

        **Redis Cluster CROSSSLOT 대응(deepdive Q50)**: USER/TEAM/GLOBAL 키는 hash
        tag 가 달라 슬롯이 서로 다르므로 단일 eval 에 묶으면 cluster 모드에서
        CROSSSLOT 에러 → 과거엔 그게 fail-open 으로 삼켜져 RPM 이 **무음 미집행**됐다.
        scope 별로 나눠 호출하면 각 eval 은 단일 슬롯(키 1개)이라 CROSSSLOT 불가.
        각 eval 은 단일 scope 원자적(check+increment). cross-scope all-or-nothing
        은 포기 — 앞 scope 통과 후 뒤 scope 거부 시 앞 scope 에 phantom +1 이 남지만
        보수적(과잉제한) 방향이고 다음 윈도우에 자가보정. budget_service 와 동형.
        """
        effective = [d for d in descriptors if d.rpm_limit and d.rpm_limit > 0]
        if not effective:
            return RateLimitResult(allowed=True, remaining=-1, limit=-1)

        now_ms = int(time.time() * 1000)
        req_id = request_id or str(uuid4())
        script = LuaScriptLoader.get("rate_limit_check")

        last: dict | None = None
        for d in effective:
            key = build_rl_key(d.scope, d.scope_id, d.model_alias, "rpm")
            # num_scopes=1 — 동일 Lua 를 단일 스코프로 호출(슬롯 1개).
            argv = [str(now_ms), req_id, str(window_ms), "1", str(d.rpm_limit), d.scope.value]
            try:
                raw = await _guarded_eval(redis, script, 1, key, *argv)
                result = json.loads(raw)
            except Exception:
                # eval 실패 — 정책(rl_fail_mode)에 따라 통과/차단. open(기본): 이 scope
                # 만 통과하고 다음 scope 계속(한 샤드 장애가 전체 집행을 끄지 않게).
                # closed: 즉시 차단. 두 경우 모두 카운트(무음 아님).
                logger.exception("rate_limit_scope_check_failed", scope=d.scope.value)
                blocked = _on_eval_failure(d.scope.value, "rpm")
                if blocked is not None:
                    return blocked
                continue
            if not result["allowed"]:
                return RateLimitResult(
                    allowed=False,
                    remaining=result.get("remaining", 0),
                    limit=result.get("limit", -1),
                    retry_after=result.get("retry_after"),
                    window_reset=result.get("window_reset") or 0,
                    scope=result.get("scope"),
                    limit_type=result.get("limit_type"),
                )
            last = result

        # 전 scope 통과 — 마지막(가장 광역) scope 결과의 remaining/limit 노출.
        if last is not None:
            return RateLimitResult(
                allowed=True,
                remaining=last.get("remaining", -1),
                limit=last.get("limit", -1),
                window_reset=last.get("window_reset") or 0,
            )
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def check_multi_scope_tpm(
        self,
        redis,
        descriptors: list[ScopeDescriptor],
        reserved_tokens: int,
        window_sec: int = 60,
    ) -> RateLimitResult:
        """TPM Sliding Window Counter Pre-reserve 체크 — **스코프별 1회 Lua 호출**.

        각 스코프당 3개 키 (cur/prev/window)는 동일 hash tag 를 공유하므로 한 scope
        의 3키는 같은 슬롯이다. 그러나 USER/TEAM/GLOBAL 끼리는 tag 가 달라(=다른 슬롯)
        과거처럼 3 scope×3키=9키를 단일 eval 에 묶으면 cluster 모드 CROSSSLOT →
        fail-open 으로 TPM 무음 미집행(deepdive Q50). scope 별로 나눠 각 eval 에
        그 scope 의 3키(단일 슬롯)만 넘긴다. fast-fail USER→TEAM→GLOBAL 유지.

        `reserved_tokens`는 `estimate_reserved_tokens()`로 사전 계산 (input +
        cache_creation 추정 + max_output). 응답 완료 후 `settle_tpm()`으로 차액 조정.
        """
        effective = [d for d in descriptors if d.tpm_limit and d.tpm_limit > 0]
        if not effective or reserved_tokens <= 0:
            return RateLimitResult(allowed=True, remaining=-1, limit=-1)

        now_sec = int(time.time())
        script = LuaScriptLoader.get("rate_limit_tpm_check")

        last: dict | None = None
        for d in effective:
            cur, prev, win = build_tpm_key_group(d.scope, d.scope_id, d.model_alias)
            # num_scopes=1 — 이 scope 의 3키만(같은 hash tag = 단일 슬롯).
            argv = [
                str(now_sec),
                str(reserved_tokens),
                str(window_sec),
                "1",
                str(d.tpm_limit),
                d.scope.value,
            ]
            try:
                raw = await _guarded_eval(redis, script, 3, cur, prev, win, *argv)
                result = json.loads(raw)
            except Exception:
                # eval 실패 — 정책(rl_fail_mode)에 따라 통과(open)/차단(closed). 카운트.
                logger.exception("rate_limit_tpm_scope_failed", scope=d.scope.value)
                blocked = _on_eval_failure(d.scope.value, "tpm")
                if blocked is not None:
                    return blocked
                continue
            if not result["allowed"]:
                return RateLimitResult(
                    allowed=False,
                    remaining=result.get("remaining", -1),
                    limit=result.get("limit", -1),
                    retry_after=result.get("retry_after"),
                    window_reset=result.get("window_reset") or 0,
                    scope=result.get("scope"),
                    limit_type=result.get("limit_type"),
                )
            last = result

        if last is not None:
            return RateLimitResult(
                allowed=True,
                remaining=last.get("remaining", -1),
                limit=last.get("limit", -1),
                window_reset=last.get("window_reset") or 0,
            )
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def settle_tpm(
        self,
        redis,
        descriptors: list[ScopeDescriptor],
        reserved_tokens: int,
        actual_tokens: int,
    ) -> None:
        """TPM 사후 정산 — 실제 토큰과 예약분 차이 조정 (설계 §D2).

        차액이 음수면 환불 (예약 > 실제), 양수면 추가 차감 (예약 < 실제).
        adjustment == 0 이면 Redis 호출 생략.
        실패 시 경고만 남기고 무시 — rate limit 의 정확도는 다음 윈도우에서 자연 보정됨.
        """
        adjustment = int(actual_tokens) - int(reserved_tokens)
        if adjustment == 0:
            return

        effective = [d for d in descriptors if d.tpm_limit and d.tpm_limit > 0]
        if not effective:
            return

        # 파이프라이닝(deepdive Q50): N 스코프 incrby 를 순차 await(왕복 N) 대신 한
        # 파이프라인으로 묶어 응답완료 경로 지연을 줄인다. 비트랜잭션 파이프라인은
        # cluster 모드에서 키별로 노드 라우팅되므로 서로 다른 슬롯이어도 안전.
        try:
            pipe = redis.pipeline(transaction=False)
            for d in effective:
                cur, _prev, _win = build_tpm_key_group(d.scope, d.scope_id, d.model_alias)
                pipe.incrby(cur, adjustment)
            await pipe.execute()
        except Exception:
            logger.warning(
                "tpm_settle_failed",
                adjustment=adjustment,
                scopes=[d.scope.value for d in effective],
            )

    async def check_rpm(
        self,
        redis,
        user_id: str,
        model_id: str,
        limit: int,
        request_id: str | None = None,
    ) -> RateLimitResult:
        """단일 USER 스코프 RPM 체크 (하위 호환 래퍼)."""
        descriptor = ScopeDescriptor(
            scope=RateLimitScope.USER,
            scope_id=user_id,
            model_alias=model_id,
            rpm_limit=limit,
        )
        return await self.check_multi_scope_rpm(redis, [descriptor], request_id=request_id)

    async def check_tpm(
        self,
        redis,
        user_id: str,
        model_id: str,
        limit: int,
        estimated_tokens: int,  # noqa: ARG002 — Step 3에서 활용 예정
    ) -> RateLimitResult:
        """단일 USER 스코프 TPM 체크 (하위 호환 래퍼).

        현재는 요청 수 기반 — Step 3에서 토큰 가중치 Lua로 교체 예정.
        """
        descriptor = ScopeDescriptor(
            scope=RateLimitScope.USER,
            scope_id=user_id,
            model_alias=model_id,
            rpm_limit=limit,  # TODO(Step 3): tpm_limit로 전환 + 토큰 가중치
        )
        return await self.check_multi_scope_rpm(redis, [descriptor])

    async def increment_monitor(
        self,
        redis,
        prefix: str,
        entity_id: str,
        model_id: str,
        metric: str,
    ) -> None:
        """팀/부서/글로벌 모니터링 카운터 증가 (거부 안 함)."""
        try:
            now_ms = int(time.time() * 1000)
            key = f"rl:{prefix}:{entity_id}:{model_id}:{metric}"
            member = str(uuid4())
            await redis.zadd(key, {member: now_ms})
            await redis.expire(key, 120)
        except Exception:
            logger.warning("monitor_counter_failed", prefix=prefix, entity_id=entity_id)

    async def reserve_cost(
        self,
        redis,
        *,
        user_id: str,
        estimated_cost: Decimal,
        user_cpm_limit: Decimal | None,
        user_cph_limit: Decimal | None,
        team_id: str | None = None,
        team_cpm_limit: Decimal | None = None,
        team_cph_limit: Decimal | None = None,
    ) -> CostLimitResult:
        """CPM/CPH 사전 예약 (USER + TEAM) — **스코프별 1회 Lua 호출**.

        한도가 모두 미설정(None)이면 Lua 호출 스킵 — unlimited.

        **Redis Cluster CROSSSLOT 대응(deepdive Q50)**: USER/TEAM 키는 hash tag 가
        달라 슬롯이 다르므로 한 eval 에 묶으면 cluster 모드 CROSSSLOT → 과거엔
        fail-open 으로 CPM/CPH **무음 미집행**됐다(team 없을 때 dummy team 키조차
        cross-slot). cost_rate_limit_scope.lua 로 한 scope 의 cpm/cph 2키(동일 hash
        tag = 단일 슬롯)만 넘겨 scope 별 호출. USER 통과 후에만 TEAM 검사(fast-fail).
        USER 통과·TEAM 거부 시 USER 카운터엔 phantom 예약이 남지만 settle_cost 가
        실제 비용과 차액 정산(요청 자체가 거부되니 실제 비용 0 → 다음 settle 로 환불).
        budget_service.check 와 동형 패턴.
        """
        user_cpm = Decimal(str(user_cpm_limit)) if user_cpm_limit else Decimal("0")
        user_cph = Decimal(str(user_cph_limit)) if user_cph_limit else Decimal("0")
        team_cpm = Decimal(str(team_cpm_limit)) if team_cpm_limit else Decimal("0")
        team_cph = Decimal(str(team_cph_limit)) if team_cph_limit else Decimal("0")

        has_user_limit = user_cpm > 0 or user_cph > 0
        has_team_limit = bool(team_id) and (team_cpm > 0 or team_cph > 0)
        if not has_user_limit and not has_team_limit:
            return CostLimitResult(allowed=True, reserved_cost=Decimal("0"))

        now = int(time.time())
        cpm_window_ts = (now // 60) * 60
        cph_window_ts = (now // 3600) * 3600
        script = LuaScriptLoader.get("cost_rate_limit_scope")

        # 검사할 스코프 목록 (USER → TEAM fast-fail 순서). 한도 미설정 scope 는 스킵.
        scopes: list[tuple[str, str, str, Decimal, Decimal]] = []
        if has_user_limit:
            scopes.append(("USER", "user", user_id, user_cpm, user_cph))
        if has_team_limit:
            scopes.append(("TEAM", "team", team_id, team_cpm, team_cph))

        for label, prefix, sid, cpm, cph in scopes:
            cpm_key = f"rl:cost:{prefix}:{{{sid}}}:cpm:{cpm_window_ts}"
            cph_key = f"rl:cost:{prefix}:{{{sid}}}:cph:{cph_window_ts}"
            try:
                raw = await _guarded_eval(
                    redis,
                    script,
                    2,
                    cpm_key,
                    cph_key,
                    str(estimated_cost),
                    str(cpm),
                    str(cph),
                    label,
                )
                result = json.loads(raw)
            except Exception:
                # eval 실패 — 정책(rl_fail_mode)에 따라 통과(open)/차단(closed). 카운트.
                logger.exception("cost_reserve_scope_failed", scope=label, user_id=user_id)
                _record_fail_open(label, "cost")
                if _fail_closed():
                    return CostLimitResult(
                        allowed=False,
                        scope=label,
                        limit_type="cpm",
                        retry_after=60,
                        reserved_cost=Decimal("0"),
                    )
                continue
            if not result["allowed"]:
                return CostLimitResult(
                    allowed=False,
                    scope=result.get("scope"),
                    limit_type=result.get("limit_type"),
                    limit=Decimal(str(result["limit"]))
                    if result.get("limit") is not None
                    else None,
                    remaining=Decimal(str(result["remaining"]))
                    if result.get("remaining") is not None
                    else None,
                    retry_after=result.get("retry_after"),
                    reserved_cost=Decimal("0"),
                )

        # 전 scope 통과 — 예약 커밋됨(각 scope eval 이 INCRBYFLOAT 수행).
        return CostLimitResult(allowed=True, reserved_cost=estimated_cost)

    async def settle_cost(
        self,
        redis,
        *,
        user_id: str,
        actual_cost: Decimal,
        reserved_cost: Decimal,
        team_id: str | None = None,
    ) -> None:
        """CPM/CPH 사후 정산 — 실제 비용과 예약분 차이 조정 (USER + TEAM)."""
        adjustment = actual_cost - reserved_cost
        if adjustment == Decimal("0"):
            return

        now = int(time.time())
        cpm_window_ts = (now // 60) * 60
        cph_window_ts = (now // 3600) * 3600
        adj_float = float(adjustment)

        # 파이프라이닝(deepdive Q50): 2~4 incrbyfloat 를 순차 await 대신 한 파이프라인으로.
        # 비트랜잭션 파이프라인은 cluster 에서 키별 노드 라우팅 → cross-slot 안전.
        try:
            pipe = redis.pipeline(transaction=False)
            pipe.incrbyfloat(f"rl:cost:user:{{{user_id}}}:cpm:{cpm_window_ts}", adj_float)
            pipe.incrbyfloat(f"rl:cost:user:{{{user_id}}}:cph:{cph_window_ts}", adj_float)
            if team_id:
                pipe.incrbyfloat(f"rl:cost:team:{{{team_id}}}:cpm:{cpm_window_ts}", adj_float)
                pipe.incrbyfloat(f"rl:cost:team:{{{team_id}}}:cph:{cph_window_ts}", adj_float)
            await pipe.execute()
        except Exception:
            logger.warning("cost_settle_failed", user_id=user_id, adjustment=str(adjustment))


class InMemoryRateLimiter:
    """Redis 장애 시 Fixed Window 근사치 Rate Limiter (워커당 독립)."""

    def __init__(self, worker_count: int = 4) -> None:
        self._worker_count = max(1, worker_count)
        self._counters: dict[str, tuple[int, float]] = {}

    def check(self, key: str, limit: int, window_sec: int = 60) -> RateLimitResult:
        adjusted_limit = max(1, limit // self._worker_count)
        now = time.time()
        window_start = (now // window_sec) * window_sec

        count, stored_window = self._counters.get(key, (0, window_start))
        if stored_window != window_start:
            count = 0

        if count >= adjusted_limit:
            retry_after = int(window_start + window_sec - now)
            return RateLimitResult(
                allowed=False,
                remaining=0,
                limit=adjusted_limit,
                retry_after=max(1, retry_after),
                window_reset=int(window_start + window_sec),
            )

        self._counters[key] = (count + 1, window_start)
        return RateLimitResult(
            allowed=True,
            remaining=adjusted_limit - count - 1,
            limit=adjusted_limit,
            window_reset=int(window_start + window_sec),
        )
