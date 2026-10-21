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
#: TPM 키의 TTL — rate_limit_tpm_check.lua 의 ``window_sec * 2 + 60`` (enforce 가 쓰는 60초 창).
#: 정산이 이 수정 이전 코드가 TTL 없이 남긴 키를 만났을 때만 쓴다.
_TPM_KEY_TTL = 60 * 2 + 60
#: 정산 표지(한 예약을 한 번만 정산)의 수명. 같은 예약을 두 번 정산하려는 경우는 (1) 응답을
#: 받지 못한 명령을 redis-py 가 재시도하는 것(밀리초~초)과 (2) 한 요청 안에서 정산 상태의
#: 사본으로 다시 부르는 것(release 직후의 finalize, 1초 안)뿐이다 — 120초는 둘 다를 두 자릿수
#: 넘게 덮는다. 메모리: 표지는 정산된 스코프마다 키 하나다. redis 7 실측 MEMORY USAGE 136바이트
#: (비용 표지) ~ 200바이트(TPM 표지, 긴 모델 id), TPM 표지 6만 개를 실제로 만들면 used_memory
#: +13.6MB. 초당 100요청 × 스코프 5(TPM 3 + 비용 2) 이면 120초 동안 6만 개 ≈ 12MB 가 상한이고
#: 늘 그만큼 있다(만료가 생성과 같은 속도). 시간 단위로 두면 30배(≈ 360MB)가 된다.
_SETTLE_MARKER_TTL = 120
#: 예약 표지(한 예약을 한 번만 예약)의 수명. 같은 예약 EVAL 이 두 번 오는 경우는 응답이 늦어
#: redis-py 가 다시 보내는 것뿐이고, 그 재시도는 첫 시도의 socket_timeout(운영 2초) + 백오프(최대
#: 0.5초) 뒤에 온다 — redis_retries 를 몇 번으로 올려도 수십 초 안이다. 표지에는 처음 결과(JSON)를
#: 담아 두므로 정산 표지보다 크다: redis 7 실측 MEMORY USAGE TPM 328바이트·비용 200바이트, 초당
#: 100요청 × 스코프 5(TPM 3 + 비용 2) × 60초 = 3만 개를 실제로 만들면 used_memory +9.6MB.
_RESERVE_MARKER_TTL = 60

# 관측성 hook(deepdive Q50 Phase 3) — rate-limit eval 이 예외로 fail-open(집행 못함)
# 한 횟수를 셀 카운터. main.py lifespan 에서 set_fail_open_metric(metrics.rl_fail_open_total)
# 로 주입. 미설정(테스트 등)이면 no-op. degradation_manager.set_metrics 와 동형 패턴.
_fail_open_counter = None


def set_fail_open_metric(counter) -> None:
    """fail-open 카운터 주입(startup). counter 는 OTel Counter(.add(n, attrs))."""
    global _fail_open_counter
    _fail_open_counter = counter


# 정산 이상 관측(카운터 주입은 fail-open 과 같은 방식): 음수가 될 뻔해 0 으로 둔 정산(clamped)과,
# 이미 정산된 예약을 다시 정산하려 한 호출(duplicate). 둘 다 0 이 정상이다 — clamped 는 이 수정 이전
# 코드가 남긴 음수나 축출, duplicate 는 재시도나 이중 정산 결함의 신호다.
_settle_clamped_counter = None
_settle_duplicate_counter = None


def set_settle_metrics(*, clamped, duplicate) -> None:
    """정산 이상 카운터 주입(startup). None 이면 no-op(테스트 등)."""
    global _settle_clamped_counter, _settle_duplicate_counter
    _settle_clamped_counter = clamped
    _settle_duplicate_counter = duplicate


def _count(counter, kind: str) -> None:
    if counter is not None:
        try:
            counter.add(1, {"kind": kind})
        except Exception:  # 메트릭 실패가 정산을 깨면 안 됨
            pass


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


def _cost_marker(cpm_key: str, reservation_id: str, kind: str) -> str:
    """``rl:cost:<scope>:{<id>}:cpm:<ts>`` → ``rl:cost-<kind>:<scope>:{<id>}:<예약 id>``.

    kind = ``settled``(정산 표지) 또는 ``reserved``(예약 표지). 같은 hash tag({<id>}) = 대상
    키와 같은 슬롯이다. ``rl:cost:*`` 패턴에는 걸리지 않는다.
    """
    head = cpm_key.rsplit(":cpm:", 1)[0]  # rl:cost:<scope>:{<id>}
    return f"rl:cost-{kind}:" + head[len("rl:cost:"):] + ":" + reservation_id


def _tpm_marker(cur_key: str, reservation_id: str, kind: str) -> str:
    """``{SCOPE:id:model}:tpm:cur`` → ``{SCOPE:id:model}:tpm-<kind>:<예약 id>`` (같은 hash tag).

    kind = ``settled`` 또는 ``reserved``. ``{*}:tpm:*`` 패턴에는 걸리지 않는다.
    """
    return cur_key.rsplit(":tpm:", 1)[0] + f":tpm-{kind}:" + reservation_id


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
        reservation_id: str | None = None,
    ) -> RateLimitResult:
        """TPM Sliding Window Counter Pre-reserve 체크 — **스코프별 1회 Lua 호출**.

        각 스코프당 3개 키 (cur/prev/window)는 동일 hash tag 를 공유하므로 한 scope
        의 3키는 같은 슬롯이다. 그러나 USER/TEAM/GLOBAL 끼리는 tag 가 달라(=다른 슬롯)
        과거처럼 3 scope×3키=9키를 단일 eval 에 묶으면 cluster 모드 CROSSSLOT →
        fail-open 으로 TPM 무음 미집행(deepdive Q50). scope 별로 나눠 각 eval 에
        그 scope 의 3키(단일 슬롯)만 넘긴다. fast-fail USER→TEAM→GLOBAL 유지.

        `reserved_tokens`는 `estimate_reserved_tokens()`로 사전 계산 (input +
        cache_creation 추정 + max_output). 응답 완료 후 `settle_tpm()`으로 차액 조정.

        통과 결과의 ``reserved_keys`` 는 **실제로 예약한** 스코프마다 (cur, prev, window 키,
        예약한 버킷 번호)다. eval 이 실패해 fail-open 한 스코프는 들어가지 않는다. 정산은 이것만,
        그 버킷에서만 조정한다(:meth:`settle_tpm`).

        뒤 스코프가 거부하면(또는 fail-closed 로 막으면) 이 요청이 앞 스코프에 넣은 예약을 그
        자리에서 되돌리고 거부를 돌려준다. 거부된 요청은 정산 상태를 받지 않으므로 다른 곳에서
        되돌릴 수 없다 — 예전에는 그 예약이 버킷이 넘어갈 때까지(가중 추정으로 최대 약 2분) 남아
        과잉제한했다.
        """
        effective = [d for d in descriptors if d.tpm_limit and d.tpm_limit > 0]
        if not effective or reserved_tokens <= 0:
            return RateLimitResult(allowed=True, remaining=-1, limit=-1)

        now_sec = int(time.time())
        # rate_limit_tpm_check.lua 의 current_bucket = floor(now / window_sec) 와 같은 값.
        bucket = now_sec // window_sec
        rid = reservation_id or uuid4().hex
        script = LuaScriptLoader.get("rate_limit_tpm_check")

        reserved: list[tuple[str, str, str, int]] = []
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
                # 예약 표지 — 응답이 늦어 클라이언트가 같은 EVAL 을 다시 보내도 한 번만 예약하고
                # 처음 결과를 돌려받는다(스크립트 docstring).
                raw = await _guarded_eval(
                    redis, script, 4, cur, prev, win, _tpm_marker(cur, rid, "reserved"),
                    *argv, str(_RESERVE_MARKER_TTL),
                )
                result = json.loads(raw)
            except Exception:
                # eval 실패 — 정책(rl_fail_mode)에 따라 통과(open)/차단(closed). 카운트.
                logger.exception("rate_limit_tpm_scope_failed", scope=d.scope.value)
                blocked = _on_eval_failure(d.scope.value, "tpm")
                if blocked is not None:
                    await self._settle_tpm_keys(
                        redis, tuple(reserved), -int(reserved_tokens), rid
                    )
                    return blocked
                continue
            if not result["allowed"]:
                # 거부 — 앞 스코프에 이 요청이 넣은 예약을 되돌린다(docstring).
                await self._settle_tpm_keys(redis, tuple(reserved), -int(reserved_tokens), rid)
                return RateLimitResult(
                    allowed=False,
                    remaining=result.get("remaining", -1),
                    limit=result.get("limit", -1),
                    retry_after=result.get("retry_after"),
                    window_reset=result.get("window_reset") or 0,
                    scope=result.get("scope"),
                    limit_type=result.get("limit_type"),
                )
            # 통과 — 이 scope 의 eval 이 cur 에 reserved_tokens 를 INCRBY 했다(그 버킷에).
            reserved.append((cur, prev, win, bucket))
            last = result

        if last is not None:
            return RateLimitResult(
                allowed=True,
                remaining=last.get("remaining", -1),
                limit=last.get("limit", -1),
                window_reset=last.get("window_reset") or 0,
                reserved_keys=tuple(reserved),
            )
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def settle_tpm(
        self,
        redis,
        descriptors: list[ScopeDescriptor],
        reserved_tokens: int,
        actual_tokens: int,
        reserved_keys: tuple[tuple[str, str, str, int], ...] | None = None,
        reservation_id: str | None = None,
    ) -> None:
        """TPM 사후 정산 — 실제 토큰과 예약분의 차액을 **예약한 버킷에만** 반영한다 (설계 §D2).

        단, 추가분(실제 > 예약)인데 예약한 버킷이 이미 지났으면 **지금 버킷**에 더한다 — 그 토큰은
        응답이 끝난 지금 쓴 것이다(예약한 분은 prev 로 가중치만큼 줄어들거나 이미 사라졌다). 환불은
        예약한 버킷에서만 한다.

        ``reserved_keys`` = :meth:`check_multi_scope_tpm` 이 돌려준 ``reserved_keys``(스코프마다
        cur·prev·window 키와 예약한 버킷 번호). 스코프마다 EVAL 한 번(rate_limit_tpm_settle.lua,
        같은 hash tag 의 세 키 — 다른 스코프와 한 EVAL 에 섞으면 Redis Cluster CROSSSLOT 이다).

        ⚠️ 예전 구현은 TPM 한도가 있는 스코프마다 지금의 cur 를 INCRBY 했다. 그래서 (1) 그 사이
           버킷이 회전했으면 **다른 분의** 카운터에서 환불해 음수로 만들었고 예약한 분(이제 prev)은
           그대로 남았으며, (2) cur 키가 만료된 뒤(스트림이 180초 TTL 을 넘긴 경우)에는 TTL 없는
           음수 키를 새로 만들었고(실측 −3800, TTL −1), (3) eval 이 실패해 예약하지 않은 스코프도
           조정했다.

        ``descriptors`` 는 하위 호환으로 받기만 한다(조정할 키는 ``reserved_keys`` 뿐이다).
        ``reserved_keys`` 가 None(이 필드 이전 모양의 정산 상태)이면 아무것도 쓰지 않는다 —
        settle_cost 와 같은 이유다. 실패는 경고만 남긴다.

        ``reservation_id`` = 예약할 때 정한 id(``rate_limit_state["reservation_id"]``). 같은 예약은
        스크립트의 표지로 **한 번만** 정산된다 — 두 번째 호출은 아무것도 쓰지 않고 경고를 남긴다.
        """
        adjustment = int(actual_tokens) - int(reserved_tokens)
        if reserved_keys is None:
            if adjustment:
                logger.warning("tpm_settle_skipped_no_reserved_keys", adjustment=adjustment)
            return
        # 차액이 0 이어도 스크립트를 돌린다 — 쓰는 것은 정산 표지뿐이다(INCRBY 0). 그래야 그 예약을
        # 다시 정산하려는 호출(결함)이 와도 표지가 막는다. 실제 == 예약일 때만이라 드물고, 흔한
        # 경로(차액 ≠ 0)는 예전 그대로 파이프라인 한 번이다.
        await self._settle_tpm_keys(
            redis, reserved_keys, adjustment, reservation_id or uuid4().hex
        )

    async def _settle_tpm_keys(
        self,
        redis,
        reserved_keys: tuple[tuple[str, str, str, int], ...],
        adjustment: int,
        reservation_id: str,
    ) -> None:
        if not reserved_keys:
            return
        # 파이프라이닝(deepdive Q50): 스코프별 EVAL 을 순차 await 대신 한 파이프라인으로.
        # 비트랜잭션 파이프라인은 cluster 에서 명령마다 그 키의 노드로 라우팅된다.
        # 실패는 경고뿐이다 — 거부 경로의 되돌리기도 이것을 쓴다(429 를 다른 응답으로 바꾸지 않게).
        try:
            script = LuaScriptLoader.get("rate_limit_tpm_settle")
            pipe = redis.pipeline(transaction=False)
            for cur, prev, win, bucket in reserved_keys:
                pipe.eval(
                    script,
                    4,
                    cur,
                    prev,
                    win,
                    _tpm_marker(cur, reservation_id, "settled"),
                    str(adjustment),
                    str(bucket),
                    str(_TPM_KEY_TTL),
                    str(_SETTLE_MARKER_TTL),
                    # 지금 버킷 — 예약한 버킷이 지난 추가분이 간다(스크립트 docstring).
                    str(int(time.time()) // 60),
                )
            results = await pipe.execute(raise_on_error=False)
        except Exception:
            logger.warning("tpm_settle_failed", adjustment=adjustment)
            return
        for (cur, _prev, _win, bucket), res in zip(reserved_keys, results):
            if isinstance(res, Exception):
                logger.warning(
                    "tpm_settle_failed",
                    key=cur,
                    adjustment=adjustment,
                    error=type(res).__name__,
                )
                continue
            applied, clamped, duplicate = (int(x) for x in res)
            if duplicate:
                # 같은 예약을 두 번째로 정산하려 했다 — 스크립트가 아무것도 쓰지 않았다.
                logger.warning(
                    "tpm_settle_duplicate", key=cur, reservation_id=reservation_id
                )
                _count(_settle_duplicate_counter, "tpm")
            elif clamped:
                logger.warning("tpm_settle_clamped", key=cur, adjustment=adjustment)
                _count(_settle_clamped_counter, "tpm")
            elif not applied:
                # 예약한 버킷이 이미 회전해 나갔거나 만료됐다 — 아무것도 쓰지 않았다(스크립트 참조).
                logger.debug(
                    "tpm_settle_bucket_gone", key=cur, bucket=bucket, adjustment=adjustment
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
        reservation_id: str | None = None,
    ) -> CostLimitResult:
        """CPM/CPH 사전 예약 (USER + TEAM) — **스코프별 1회 Lua 호출**.

        한도가 모두 미설정(None)이면 Lua 호출 스킵 — unlimited.

        **Redis Cluster CROSSSLOT 대응(deepdive Q50)**: USER/TEAM 키는 hash tag 가
        달라 슬롯이 다르므로 한 eval 에 묶으면 cluster 모드 CROSSSLOT → 과거엔
        fail-open 으로 CPM/CPH **무음 미집행**됐다(team 없을 때 dummy team 키조차
        cross-slot). cost_rate_limit_scope.lua 로 한 scope 의 cpm/cph 2키(동일 hash
        tag = 단일 슬롯)만 넘겨 scope 별 호출. USER 통과 후에만 TEAM 검사(fast-fail).
        USER 통과·TEAM 거부(또는 fail-closed) 시에는 이 요청이 USER 에 넣은 예약을 그 자리에서
        되돌리고 거부를 돌려준다. 거부된 요청은 정산 상태를 받지 않으므로 다른 곳에서 되돌릴 수
        없다 — 예전에는 그 예약이 창이 끝날 때까지(cpm 1분, cph 1시간) 남아 과잉제한했다.
        budget_service.check 와 동형 패턴.

        반환값의 ``reserved_keys`` 는 **실제로 예약한** 스코프의 (cpm 키, cph 키)다. 한도가 없어
        건너뛴 스코프, eval 이 실패해 fail-open 한 스코프, 예약액이 0 이라 Lua 가 쓰지 않은 경우는
        들어가지 않는다. 정산은 이 키만, 이 창에서만 조정한다(:meth:`settle_cost`). 예약한 키가
        하나도 없으면 ``reserved_cost`` 도 0 이다 — 되돌릴 예약이 없으므로 정산이 돌지 않는다.
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
        rid = reservation_id or uuid4().hex

        # 검사할 스코프 목록 (USER → TEAM fast-fail 순서). 한도 미설정 scope 는 스킵.
        scopes: list[tuple[str, str, str, Decimal, Decimal]] = []
        if has_user_limit:
            scopes.append(("USER", "user", user_id, user_cpm, user_cph))
        if has_team_limit:
            scopes.append(("TEAM", "team", team_id, team_cpm, team_cph))

        reserved: list[tuple[str, str]] = []
        for label, prefix, sid, cpm, cph in scopes:
            cpm_key = f"rl:cost:{prefix}:{{{sid}}}:cpm:{cpm_window_ts}"
            cph_key = f"rl:cost:{prefix}:{{{sid}}}:cph:{cph_window_ts}"
            try:
                raw = await _guarded_eval(
                    redis,
                    script,
                    3,
                    cpm_key,
                    cph_key,
                    # 예약 표지 — 늦은 응답의 재시도도 한 번만 예약한다(스크립트 docstring).
                    _cost_marker(cpm_key, rid, "reserved"),
                    str(estimated_cost),
                    str(cpm),
                    str(cph),
                    label,
                    str(_RESERVE_MARKER_TTL),
                )
                result = json.loads(raw)
            except Exception:
                # eval 실패 — 정책(rl_fail_mode)에 따라 통과(open)/차단(closed). 카운트.
                logger.exception("cost_reserve_scope_failed", scope=label, user_id=user_id)
                _record_fail_open(label, "cost")
                if _fail_closed():
                    await self._settle_cost_keys(
                        redis, tuple(reserved), -estimated_cost, user_id, rid
                    )
                    return CostLimitResult(
                        allowed=False,
                        scope=label,
                        limit_type="cpm",
                        retry_after=60,
                        reserved_cost=Decimal("0"),
                    )
                continue
            if not result["allowed"]:
                # 거부 — 앞 스코프에 이 요청이 넣은 예약을 되돌린다(docstring).
                await self._settle_cost_keys(
                    redis, tuple(reserved), -estimated_cost, user_id, rid
                )
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
            # 통과 — 이 scope 의 eval 이 두 키에 INCRBYFLOAT + EXPIRE 를 커밋했다
            # (예약액 > 0 일 때만, cost_rate_limit_scope.lua 의 `if cost > 0`).
            # 정산이 조정할 키는 이것뿐이다.
            if estimated_cost > 0:
                reserved.append((cpm_key, cph_key))

        # 전 scope 통과. 예약한 키가 없으면(전부 fail-open, 또는 예약액 0) 되돌릴 것도 없다.
        return CostLimitResult(
            allowed=True,
            reserved_cost=estimated_cost if reserved else Decimal("0"),
            reserved_keys=tuple(reserved),
        )

    async def settle_cost(
        self,
        redis,
        *,
        user_id: str,
        actual_cost: Decimal,
        reserved_cost: Decimal,
        team_id: str | None = None,
        reserved_keys: tuple[tuple[str, str], ...] | None = None,
        reservation_id: str | None = None,
    ) -> None:
        """CPM/CPH 사후 정산 — 실제 비용과 예약분의 차액을 **예약한 키에만** 반영한다.

        ``reserved_keys`` = :meth:`reserve_cost` 가 돌려준 ``reserved_keys``(스코프마다 (cpm 키,
        cph 키)). 정산은 그 스코프만, 그 키 이름의 창에서 조정한다 — 예약하지 않은 스코프는 건드리지
        않고, 환불은 정산 시각으로 창을 다시 고르지 않는다. 단, 추가 청구(실제 > 예약)인데 예약한
        창이 이미 지났으면 같은 스코프의 **지금** 창 키에 더한다(없으면 만들고 TTL 을 건다) — 지난
        창의 키에 더하면 어떤 검사도 읽지 않아 그 비용이 한도에서 사라진다.

        ⚠️ 예전 구현은 USER 키를 무조건, TEAM 키를 ``team_id`` 만 있으면 조정했고 창을 정산
           시각의 ``now`` 로 골랐으며 EXPIRE 를 걸지 않았다. 그래서 (1) 한도가 없어 예약하지 않은
           스코프에 음수 카운터가 생기고, (2) 분·시 경계를 넘긴 정산은 **다음 창에** 새 키를
           만들었으며 — 예약한 창의 예약은 그대로 남는다 — (3) 그 키들에는 TTL 이 없었다(실측:
           user 한도만 있을 때 zero-usage 환불 뒤 팀 키 −0.015078, TTL −1). TTL 없는 키는 만료되지
           않고 volatile-lru 도 축출하지 못해 활성 분·시마다 쌓이며, 메모리가 차면 TTL 이 있는
           키(rate-limit 카운터·모델 캐시·예산 설정)가 먼저 축출된다. 나중에 그 스코프에 한도를
           켜면 쌓인 음수만큼 그 창의 한도가 느슨해진다.

        스코프마다 EVAL 한 번(cost_settle_scope.lua, 같은 hash tag 의 두 키) — USER 와 TEAM 키는
        슬롯이 달라 한 EVAL 에 섞으면 Redis Cluster 에서 CROSSSLOT 이다(deepdive Q50). 키가 이미
        없으면(긴 스트림이 cpm 키의 120초 TTL 을 넘긴 경우 등) 그 키는 건너뛰고 되살리지 않는다 —
        TTL 이 끝났다면 그 창도 끝나 어떤 검사도 그 키를 읽지 않는다. 결과가 음수면 0 으로 둔다.
        스크립트 docstring 참조.

        ``reserved_keys`` 가 없으면(None/빈 값 — 이 필드 이전 모양의 정산 상태) 아무것도 조정하지
        않는다. 어느 스코프의 어느 창을 예약했는지 모르는 채로 쓰는 것은 정확히 위의 결함이다.
        그 대가는 그 예약이 창 끝까지 남는 것(과잉제한, 최대 cpm 1분·cph 1시간)이다. 정산 상태는
        요청을 받은 프로세스 안에만 있으므로(라우터 → finalize / release_reservations), 이 모양은
        이 코드가 만든 예약에서는 생기지 않는다.

        ``reservation_id`` = 예약할 때 정한 id(``rate_limit_state["reservation_id"]``). 같은 예약은
        스크립트의 표지로 **한 번만** 정산된다. 두 번째 호출 — redis-py 가 응답을 못 받은
        파이프라인을 다시 보내는 경우, 정산 상태의 사본으로 다시 부르는 경우 — 은 아무것도 쓰지
        않고 경고를 남긴다. 예전에는 0 하한이 이중 환불을 가렸다: 같은 창 키에 다른 요청의 예약이
        있으면 그것을 조용히 깎았고(실측 0.031337 → 0.019337), 음수도 로그도 남지 않았다.
        """
        adjustment = actual_cost - reserved_cost
        if not reserved_keys:
            if adjustment != Decimal("0"):
                logger.warning(
                    "cost_settle_skipped_no_reserved_keys",
                    user_id=user_id,
                    adjustment=str(adjustment),
                )
            return

        # 차액이 0 이어도 스크립트를 돌린다 — 쓰는 것은 정산 표지뿐이다(settle_tpm 의 같은 주석).
        await self._settle_cost_keys(
            redis, reserved_keys, adjustment, user_id, reservation_id or uuid4().hex
        )

    async def _settle_cost_keys(
        self,
        redis,
        reserved_keys: tuple[tuple[str, str], ...],
        adjustment: Decimal,
        user_id: str,
        reservation_id: str,
    ) -> None:
        """예약한 스코프마다 cost_settle_scope EVAL 한 번(정산·되돌리기 공용)."""
        if not reserved_keys:
            return
        # 파이프라이닝(deepdive Q50): 스코프별 EVAL 을 순차 await 대신 한 파이프라인으로.
        # 비트랜잭션 파이프라인은 cluster 에서 명령마다 그 키의 노드로 라우팅되고, 각 EVAL 의
        # 두 키는 한 슬롯이다. 실패는 경고뿐이다(거부 경로의 되돌리기도 이것을 쓴다).
        try:
            script = LuaScriptLoader.get("cost_settle_scope")
            pipe = redis.pipeline(transaction=False)
            now = int(time.time())
            for cpm_key, cph_key in reserved_keys:
                pipe.eval(
                    script,
                    5,
                    cpm_key,
                    cph_key,
                    _cost_marker(cpm_key, reservation_id, "settled"),
                    # 같은 스코프의 지금 창 키 — 예약한 창이 지난 추가 청구가 간다(스크립트
                    # docstring). 예약한 창이 아직이면 위 두 키와 같다.
                    f"{cpm_key.rsplit(':', 1)[0]}:{(now // 60) * 60}",
                    f"{cph_key.rsplit(':', 1)[0]}:{(now // 3600) * 3600}",
                    str(adjustment),
                    str(_SETTLE_MARKER_TTL),
                )
            results = await pipe.execute(raise_on_error=False)
        except Exception:
            logger.warning("cost_settle_failed", user_id=user_id, adjustment=str(adjustment))
            return
        for (cpm_key, _cph_key), res in zip(reserved_keys, results):
            if isinstance(res, Exception):
                logger.warning(
                    "cost_settle_failed",
                    user_id=user_id,
                    key=cpm_key,
                    adjustment=str(adjustment),
                    error=type(res).__name__,
                )
                continue
            applied_cpm, applied_cph, clamped, duplicate = (int(x) for x in res)
            if duplicate:
                # 같은 예약을 두 번째로 정산하려 했다 — 스크립트가 아무것도 쓰지 않았다.
                logger.warning(
                    "cost_settle_duplicate", key=cpm_key, reservation_id=reservation_id
                )
                _count(_settle_duplicate_counter, "cost")
                continue
            if clamped:
                logger.warning("cost_settle_clamped", key=cpm_key, adjustment=str(adjustment))
                _count(_settle_clamped_counter, "cost")
            if not (applied_cpm and applied_cph):
                # 예약한 창의 키가 이미 만료됐다 — 그 키는 조정하지 않았다(스크립트 docstring).
                logger.debug(
                    "cost_settle_window_expired",
                    key=cpm_key,
                    applied=[applied_cpm, applied_cph],
                    adjustment=str(adjustment),
                )


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
