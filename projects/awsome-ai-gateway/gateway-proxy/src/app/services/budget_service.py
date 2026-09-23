# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage
from app.schemas.domain import BudgetPolicy, BudgetStatus
from app.services.lua_loader import LuaScriptLoader

logger = structlog.get_logger(__name__)

BUDGET_CONFIG_TTL = None  # 무기한 (Admin 변경 시 DEL로 무효화)

# per-app(client) 예산 대상 클라이언트. client_identifier 토큰과 동일 집합
# (claude-code / cowork / codex). 새 앱 추가 시 여기 + DB CHECK 제약 + admin-api 동기화.
PER_APP_BUDGET_CLIENTS = ("claude-code", "cowork", "codex")

# 정책 파라미터 기본값 — DB 스키마에 없으므로 Python 기본값 사용.
# 향후 budget_configs에 컬럼 추가 시 (requirements.md §6b 장래 추가 항목) DB 값으로 교체.
DEFAULT_SOFT_LIMIT_PCT = 110
DEFAULT_THROTTLE_RPM_PCT = 50
DEFAULT_THRESHOLDS = [80, 90, 100]


def _row_thresholds(config) -> list[int]:
    """``BudgetConfig`` 행의 알림 임계값 — DB 가 진실의 원천이다(migration 0037).

    ⚠️ 빈 배열은 **유효한 설정**이고 "이 예산에는 임계값 알림을 보내지 않는다" 를 뜻한다.
       그래서 ``or DEFAULT_THRESHOLDS`` 로 채우지 않는다 — 그러면 운영자가 의도적으로 비운
       설정을 재수화가 되살린다.

    컬럼이 없는 구 스키마(0037 미적용)에서는 속성 자체가 없으므로 그때만 기본값을 쓴다.
    """
    raw = getattr(config, "alert_thresholds", None)
    if raw is None:
        return list(DEFAULT_THRESHOLDS)
    return sorted({int(v) for v in raw})


def _policy_to_lua_value(policy: BudgetPolicy | str) -> str:
    """Python BudgetPolicy(lowercase value) → Lua/Redis 내부 표현(lowercase)."""
    return policy.value if isinstance(policy, BudgetPolicy) else str(policy).lower()


def _db_policy_to_domain(db_policy) -> BudgetPolicy:
    """DB enum(UPPERCASE) → domain BudgetPolicy(lowercase value).

    admin-api `BudgetPolicy` ORM과 gateway-proxy `schemas.domain.BudgetPolicy`의
    value 표기가 다름(UPPERCASE vs lowercase). 변환 계층.
    """
    raw = db_policy.value if hasattr(db_policy, "value") else str(db_policy)
    return BudgetPolicy(raw.lower())



def _as_client_list(value) -> list[str]:
    """``app_clients`` 필드를 리스트로 정규화한다.

    admin-api 가 이 필드를 Lua 로 병합하는데(core/budget_cache.py — 로그인과 예산
    변경이 서로의 필드를 지우던 클로버를 막기 위해), Redis 의 cjson 은 빈 배열을
    표현할 수 없어 ``[]`` 를 ``{}`` 로 인코딩한다(실측: redis 7.4). 둘 다 "활성
    per-app 예산이 없다" 는 뜻이다.

    ⚠️ 비어 있지 않은 dict 는 **리스트로 바꾸지 않는다** — 그건 예상 밖의 형상이고,
       키를 client 이름으로 착각해 통과시키면 없는 예산을 있다고 판정할 수 있다.
       그런 경우는 빈 리스트로 떨어뜨려(= per-app 평가 없음) 부모 USER 예산만 걸린다.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict) and not value:
        return []
    return []

def _evaluate_layer(
    used: Decimal, limit: Decimal, policy: BudgetPolicy
) -> tuple[str | None, bool, bool]:
    """한 예산 계층의 판정. ``(block_reason_suffix, soft_warning, throttle_active)``.

    ⚠️ 이 함수가 따로 있는 이유. DB 폴백은 오랫동안 **정책을 보기 전에** 무조건
       ``used >= limit`` 에서 차단했다. Redis 경로(``redis_scripts/budget_check.lua``)는
       같은 상태에서 정책을 먼저 본다:

         hard_block    100% 에서 차단
         soft_warning  soft_limit_pct(기본 110%) 까지 허용, 100% 를 넘으면 경고 플래그만
         throttle      **차단하지 않는다** — RPM 만 줄인다

       그래서 SOFT_WARNING 팀이 $1000.50/$1000 인 상태에서 Redis 가 한 번 타임아웃되면,
       Redis 경로였다면 통과했을 요청이 DB 폴백에서는 429 가 됐다 — Redis 가 degrade 된
       동안 팀 전원이 막힌다. THROTTLE 도 같다: 설계상 절대 차단하지 않는 정책인데 폴백에서만
       100% 에서 막혔다.

       부수적으로 ``team_soft_limit_exceeded`` 와 ``soft_warning=True`` 는 폴백 경로에서
       **도달 불가능한 죽은 코드**였다(무조건 raise 가 먼저였다). 즉 ``X-Budget-Warning``
       헤더가 Redis 다운 중에는 나오지 않았다.

    ⚠️ 판정 **순서**가 Lua 와 같아야 한다(hard_block → soft_warning → throttle). 순서가
       달라지면 같은 상태가 경로에 따라 다르게 판정된다 — 그것이 원래의 결함이다.

    ⚠️ 이 변경으로 폴백은 이전보다 **느슨해진다**: Redis 다운 중 SOFT_WARNING 사용자는
       110% 까지 쓸 수 있고 THROTTLE 사용자는 100% 에서 막히지 않는다. 그것이 저장된 정책이
       말하는 바이고 Redis 경로가 이미 그렇게 동작하지만, 돈이 나가는 동작의 변경이다.
    """
    if policy == BudgetPolicy.HARD_BLOCK and used >= limit:
        return "budget_exceeded", False, False

    soft_warning = False
    if policy == BudgetPolicy.SOFT_WARNING and limit > 0:
        effective_limit = limit * Decimal(DEFAULT_SOFT_LIMIT_PCT) / Decimal(100)
        if used >= effective_limit:
            return "soft_limit_exceeded", False, False
        if used >= limit:
            soft_warning = True

    throttle_active = False
    if policy == BudgetPolicy.THROTTLE:
        usage_pct = int(used / limit * 100) if limit > 0 else 0
        for threshold in sorted(DEFAULT_THRESHOLDS, reverse=True):
            if usage_pct >= threshold:
                throttle_active = True
                break

    return None, soft_warning, throttle_active


class BudgetService:
    """예산 정책 확인 서비스."""

    async def check_budget(
        self,
        redis,
        db: AsyncSession | None,
        user_id: str,
        team_id: str,
        period: str,
        client: str | None = None,
    ) -> BudgetStatus:
        """사용자 예산 확인 (Redis 우선, DB fallback).
        예산 미설정 시 PermissionError 발생 (429 no_budget_assigned).
        client 가 'claude-code' 또는 'cowork' 이면 앱별 예산도 추가로 확인한다.
        """
        # Redis Cluster hash tag: {<user_id>} co-locates usage/config on same slot.
        user_key = f"budget:user:{{{user_id}}}:{period}"
        user_config_key = f"budget:config:user:{{{user_id}}}"
        team_key = f"budget:team:{{{team_id}}}:{period}"
        team_config_key = f"budget:config:team:{{{team_id}}}"

        # Redis 예산 설정 확인
        if redis is not None:
            # TEAM config cold-cache fallback: init SQL / alembic backfill 로 DB에
            # 삽입된 TEAM 예산이 Redis에 없으면 DB에서 조회해 캐시 (admin-api startup
            # warmup 의 안전망).
            if db is not None and not await redis.exists(team_config_key):
                await self._hydrate_team_config_cache(redis, db, team_id)

            # P0-③: USER config cold-cache fallback. If admin's best-effort SET of
            # the user-config key (which carries app_clients) was lost, the per-app
            # budget gate below would silently skip → budget bypass. Rehydrate from
            # DB on miss (ensure_config_cached rebuilds app_clients from active
            # per-app BudgetConfig rows) so the gate stays enforced.
            if db is not None and not await redis.exists(user_config_key):
                await self.ensure_config_cached(redis, db, user_id)

            # Redis 키 없으면 DB에서 복구 후 재캐싱 (LRU 삭제 / failover 대비)
            #
            # ⚠️ 이 경로는 **죽은 코드였다 — fail-OPEN 방향으로.** 쿼리에
            #    `client IS NULL` 이 없어서, 그 (user, period) 에 per-app 행이 **하나만**
            #    있어도 `scalar_one_or_none()` 이 MultipleResultsFound 로 터지고, 아래
            #    `except Exception` 이 그것을 삼켰다. 그러면 user_key 가 복원되지 않은
            #    채로 budget_check.lua 가 돌아 `used=0` 을 읽는다 — config 는 별도 키에서
            #    재수화되므로 `config_present` 는 true 다. 즉 **그 사용자의 월 사용액
            #    전체가 0 으로 리셋되고 전부 통과한다.**
            #
            #    per-app 행은 pub 에서 실제로 만들어진다(admin-api seed_spent 가
            #    client 를 받아 INSERT 한다). 그리고 이 파일의 DB 폴백 경로는 이미 같은
            #    교훈을 배워 `client.is_(None)` 을 걸고 있었다 — 복원 경로만 남겨졌다.
            if not await redis.exists(user_key) and db is not None:
                try:
                    from sqlalchemy import func, select, text

                    # 총합 행과 앱별 행을 **한 번에** 읽는다. 총합은 client IS NULL 이고,
                    # 앱별 행은 각자의 카운터 키로 복원해야 한다(아래).
                    result = await db.execute(
                        text(
                            "SELECT client, used_usd FROM budget.budget_usages "
                            "WHERE scope = 'USER' AND scope_id = :uid AND period = :period"
                        ),
                        {"uid": user_id, "period": period},
                    )
                    rows = result.all()
                    by_client = {r[0]: r[1] for r in rows}
                    total_row = by_client.get(None)

                    if total_row is None:
                        # budget_usages 에 총합 행이 없으면 usage_logs 에서 SUM.
                        from app.models.usage import UsageRecord

                        stmt2 = select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).where(
                            UsageRecord.user_id == user_id,
                            func.to_char(UsageRecord.requested_at, "YYYY-MM") == period,
                        )
                        result2 = await db.execute(stmt2)
                        used_from_db = result2.scalar_one()
                    else:
                        used_from_db = total_row

                    if used_from_db and used_from_db > 0:
                        await redis.set(user_key, str(used_from_db))
                        logger.info(
                            "budget_counter_restored",
                            user_id=user_id,
                            period=period,
                            used=str(used_from_db),
                        )

                    # ── 앱별 카운터도 복원한다 ──
                    #
                    # ⚠️ 예전에는 총합 키만 복원했다. 그래서 Redis 데이터 유실/failover
                    #    후 **소진된 앱별 예산이 조용히 0 으로 되돌아갔다** — 그 앱의
                    #    한도가 그 달 내내 사실상 없어진다.
                    #    이미 존재하는 키는 건드리지 않는다(진행 중인 카운트를 덮어쓰면
                    #    그 사이의 사용량을 잃는다).
                    for client_name, used in by_client.items():
                        if client_name is None or not used or used <= 0:
                            continue
                        app_key = f"budget:user:{{{user_id}}}:{client_name}:{period}"
                        if await redis.exists(app_key):
                            continue
                        await redis.set(app_key, str(used))
                        logger.info(
                            "budget_app_counter_restored",
                            user_id=user_id,
                            client=client_name,
                            period=period,
                            used=str(used),
                        )
                except Exception:
                    logger.exception("budget_counter_restore_failed", user_id=user_id)

            # Redis Cluster CROSSSLOT 대응: USER/TEAM 키는 hash tag 가 달라 slot 이
            # 서로 다르므로 단일 Lua 호출에 묶을 수 없다. scope 별로 2번 호출하고
            # Python 에서 결과를 합친다. Lua 내부 체크는 여전히 각 scope 원자적.
            try:
                script = LuaScriptLoader.get("budget_check")

                user_raw = await redis.eval(script, 2, user_key, user_config_key, "user")
                user_result = json.loads(user_raw)

                # USER config 미설정 → pass-through (Q 정책)
                if user_result.get("config_present") and not user_result["allowed"]:
                    raise PermissionError(user_result.get("reason", "user_budget_exceeded"))

                team_raw = await redis.eval(script, 2, team_key, team_config_key, "team")
                team_result = json.loads(team_raw)

                # TEAM config 미설정 → deny (C-1 정책)
                if not team_result.get("config_present"):
                    raise PermissionError("team_budget_unset")

                if not team_result["allowed"]:
                    raise PermissionError(team_result.get("reason", "team_budget_exceeded"))

                # 앱(client) 예산 확인 — 'claude-code' / 'cowork' 이고, USER config 에
                # 이 client 가 app_clients 로 등록된 경우에만 추가 eval (free gate).
                # USER check 결과가 이미 config.app_clients 를 echo 하므로 추가 RTT 없이
                # 게이팅 — 앱 예산 없는 유저는 3번째 eval 자체를 건너뛴다.
                # 불변식(P0-③ review): per-app 예산은 USER 총예산의 하위 서브-리밋이므로
                # 항상 부모 USER 예산이 존재한다(admin-api 가 부모 없는 per-app 생성 거부).
                # → app_clients 게이트는 부모 config 가 있다는 전제에서 신뢰 가능.
                #
                # ⚠️ `app_clients` 는 리스트 또는 **빈 dict** 로 올 수 있다. admin-api 는 이
                #    필드를 Lua 로 병합해 쓰는데(app_clients 클로버를 막기 위해),
                #    Redis 의 cjson 에는 빈 배열 개념이 없어 `[]` 가 `{}` 로 인코딩된다
                #    (실측: redis 7.4, `cjson.empty_array` 미지원). 둘 다 "활성 per-app
                #    예산이 없다" 는 같은 뜻이므로 같게 다뤄야 한다.
                #
                #    `isinstance(list)` 만 보면 빈 dict 가 "타입이 틀렸다" 로 떨어져,
                #    아래 per-app 분기 전체(콜드 캐시 재수화 안전망 포함)를 건너뛴다.
                #    지금은 결과가 같지만(둘 다 per-app 예산 0건), 그 동등성이 우연이라
                #    미래의 독자가 `{}` 를 "알 수 없음" 으로 오독할 수 있다. 여기서 한 번
                #    정규화해 그 여지를 없앤다.
                user_app_clients = _as_client_list(user_result.get("app_clients"))
                if client in PER_APP_BUDGET_CLIENTS and client in user_app_clients:
                    client_key = f"budget:user:{{{user_id}}}:{client}:{period}"
                    client_config_key = f"budget:config:user:{{{user_id}}}:{client}"
                    # P0-③: per-app config cold-cache fallback. If admin's
                    # best-effort SET of this key was lost, the Lua would see
                    # config_present=false → pass-through (bypass). Rehydrate from
                    # DB on miss so the per-app limit stays enforced.
                    if db is not None and not await redis.exists(client_config_key):
                        await self._hydrate_client_config_cache(
                            redis, db, user_id, client
                        )
                    client_raw = await redis.eval(
                        script, 2, client_key, client_config_key, "client"
                    )
                    client_result = json.loads(client_raw)
                    if client_result.get("config_present") and not client_result["allowed"]:
                        raise PermissionError(
                            client_result.get("reason", "client_budget_exceeded")
                        )

                # 둘 다 통과 — TEAM 결과를 우선 반환 (limit/used 정보가 더 의미있음)
                final = team_result
                return BudgetStatus(
                    remaining_usd=Decimal(str(final["remaining_usd"])),
                    limit_usd=Decimal(str(final.get("limit_usd", 0))),
                    used_usd=Decimal(str(final["used_usd"])),
                    policy=BudgetPolicy(final["policy"]),
                    throttle_rpm_pct=final.get("throttle_rpm_pct", 50),
                    threshold_pct=final.get("threshold_pct", 0),
                    throttle_active=final.get("throttle_active", False),
                    soft_warning=final.get("soft_warning", False),
                )
            except PermissionError:
                raise
            except Exception:
                logger.exception("redis_budget_check_failed", user_id=user_id)
                # Redis 실패 시 DB fallback
                pass

        # DB fallback (REDIS_DEGRADED)
        try:
            return await self._check_budget_db(db, user_id, team_id, period, client=client)
        except PermissionError:
            raise
        except Exception:
            logger.exception("budget_db_fallback_failed", user_id=user_id)
            raise PermissionError("no_budget_assigned")

    async def _check_budget_db(
        self,
        db: AsyncSession,
        user_id: str,
        team_id: str,
        period: str,
        client: str | None = None,
    ) -> BudgetStatus:
        """DB SELECT 기반 예산 확인 (Redis fallback 경로).

        DB 컬럼: scope / scope_id / max_budget_usd (KI-09 수정 반영).
        soft_limit_pct, throttle_rpm_pct 는 현재 DB 스키마에 없으므로 Python 기본값 사용.
        thresholds 는 migration 0037 이후 DB 컬럼(``alert_thresholds``)이 원천이다.
        client 가 설정된 경우 앱별 BudgetConfig/BudgetUsage 도 확인한다.
        앱 예산 미설정(config=None) → pass-through.
        """
        if db is None:
            raise PermissionError("no_budget_assigned")

        # Q 정책: USER 예산 미설정 → pass-through (차단 안 함)
        # client IS NULL 로 한정 — per-app(client) row 가 생기면 (scope,scope_id) 당
        # 행이 2개 이상이 되어 scalar_one_or_none() 가 MultipleResultsFound 로 터진다.
        # (REDIS_DEGRADED 시 app 예산 유저가 전부 차단되는 잠복 장애 방지.)
        user_cfg_result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.USER)
            .where(BudgetConfig.scope_id == user_id)
            .where(BudgetConfig.client.is_(None))
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        user_config = user_cfg_result.scalar_one_or_none()

        if user_config is not None:
            user_usage_result = await db.execute(
                select(BudgetUsage)
                .where(BudgetUsage.scope == BudgetScope.USER)
                .where(BudgetUsage.scope_id == user_id)
                .where(BudgetUsage.client.is_(None))
                .where(BudgetUsage.period == period)
            )
            user_usage = user_usage_result.scalar_one_or_none()
            user_used = user_usage.used_usd if user_usage else Decimal("0")
            # 정책을 적용한다 — 무조건 차단은 Redis 경로와 어긋난다(_evaluate_layer 주석).
            user_block, _uw, _ut = _evaluate_layer(
                user_used,
                user_config.max_budget_usd,
                _db_policy_to_domain(user_config.policy),
            )
            if user_block:
                raise PermissionError(f"user_{user_block}")

        # C-1 정책: TEAM 예산 미설정 → 차단
        team_cfg_result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.TEAM)
            .where(BudgetConfig.scope_id == team_id)
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        team_config = team_cfg_result.scalar_one_or_none()
        if team_config is None:
            raise PermissionError("team_budget_unset")

        team_usage_result = await db.execute(
            select(BudgetUsage)
            .where(BudgetUsage.scope == BudgetScope.TEAM)
            .where(BudgetUsage.scope_id == team_id)
            .where(BudgetUsage.period == period)
        )
        team_usage = team_usage_result.scalar_one_or_none()
        team_used = team_usage.used_usd if team_usage else Decimal("0")

        max_budget = team_config.max_budget_usd
        remaining = max_budget - team_used
        threshold_pct = int(team_used / max_budget * 100) if max_budget > 0 else 0

        # ⚠️ 이전에는 이 위에 `if team_used >= max_budget: raise` 가 있었고, 그것이 정책
        #    판정보다 **먼저** 돌아 아래 SOFT_WARNING/THROTTLE 분기를 도달 불가능한 죽은
        #    코드로 만들었다. Redis 경로(budget_check.lua)와 같은 판정을 쓴다.
        policy = _db_policy_to_domain(team_config.policy)
        team_block, soft_warning, throttle_active = _evaluate_layer(
            team_used, max_budget, policy
        )
        if team_block:
            raise PermissionError(f"team_{team_block}")

        # 앱(client) 예산 확인 (REDIS_DEGRADED 경로) — 미설정 시 pass-through.
        if client in PER_APP_BUDGET_CLIENTS:
            client_cfg_result = await db.execute(
                select(BudgetConfig)
                .where(BudgetConfig.scope == BudgetScope.USER)
                .where(BudgetConfig.scope_id == user_id)
                .where(BudgetConfig.client == client)
                .where(BudgetConfig.is_active == True)  # noqa: E712
            )
            client_config = client_cfg_result.scalar_one_or_none()
            if client_config is not None:
                client_usage_result = await db.execute(
                    select(BudgetUsage)
                    .where(BudgetUsage.scope == BudgetScope.USER)
                    .where(BudgetUsage.scope_id == user_id)
                    .where(BudgetUsage.client == client)
                    .where(BudgetUsage.period == period)
                )
                client_usage = client_usage_result.scalar_one_or_none()
                client_used = client_usage.used_usd if client_usage else Decimal("0")
                client_block, _cw, _ct = _evaluate_layer(
                    client_used,
                    client_config.max_budget_usd,
                    _db_policy_to_domain(client_config.policy),
                )
                if client_block:
                    raise PermissionError(f"client_{client_block}")

        return BudgetStatus(
            remaining_usd=remaining,
            limit_usd=max_budget,
            used_usd=team_used,
            policy=policy,
            soft_limit_pct=DEFAULT_SOFT_LIMIT_PCT,
            throttle_rpm_pct=DEFAULT_THROTTLE_RPM_PCT,
            threshold_pct=threshold_pct,
            # Redis degrade 중에도 임계값은 DB 행의 값이어야 한다 — 여기서 기본값을 쓰면
            # degrade 동안만 알림 기준이 달라진다.
            # ⚠️ 이 반환은 **팀** 예산을 서술한다(위 max_budget/policy 가 team_config 에서
            #    온다). 처음에 `config` 라고 썼는데 이 스코프에는 그 이름이 없어서 Redis
            #    degrade 경로에서만 NameError 가 되는 잠재 결함이었다 — import 는 통과한다.
            thresholds=_row_thresholds(team_config),
            throttle_active=throttle_active,
            soft_warning=soft_warning,
        )

    async def ensure_config_cached(
        self,
        redis,
        db: AsyncSession,
        user_id: str,
    ) -> None:
        """예산 설정이 Redis에 없으면 DB에서 조회하여 캐시.

        Lua 스크립트는 lowercase policy 값('hard_block' 등)을 기대하므로
        DB UPPERCASE enum을 변환해서 저장. soft/throttle 파라미터는 DB 스키마에 없어
        Python 기본값을 쓰지만, thresholds 는 migration 0037 이후 DB 컬럼
        (``alert_thresholds``)이 원천이다 — 예전에 여기서 기본값을 쓴 것이 운영자 설정을
        캐시 TTL(300초)마다 되돌린 원인이었다.
        """
        config_key = f"budget:config:user:{{{user_id}}}"
        if redis is None:
            return
        cached = await redis.get(config_key)
        if cached:
            return

        # client IS NULL = USER total config (per-app rows는 별도 키로 캐시됨).
        # 필터 없으면 app 예산 유저에서 MultipleResultsFound.
        result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.USER)
            .where(BudgetConfig.scope_id == user_id)
            .where(BudgetConfig.client.is_(None))
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        config = result.scalar_one_or_none()
        if config:
            # app_clients 는 DB의 활성 per-app budget row에서 재구성 — 이 캐시 재생성이
            # admin-api가 user-config JSON에 써둔 app_clients 를 덮어쓰지 않도록(free gate 보존).
            app_rows = await db.execute(
                select(BudgetConfig.client)
                .where(BudgetConfig.scope == BudgetScope.USER)
                .where(BudgetConfig.scope_id == user_id)
                .where(BudgetConfig.client.isnot(None))
                .where(BudgetConfig.is_active == True)  # noqa: E712
            )
            app_clients = [c for c in app_rows.scalars().all() if c]
            config_data = {
                "limit_usd": str(config.max_budget_usd),
                "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
                "soft_limit_pct": DEFAULT_SOFT_LIMIT_PCT,
                "throttle_rpm_pct": DEFAULT_THROTTLE_RPM_PCT,
                # ⚠️ 여기가 운영자 설정이 되돌아간 지점이다. 이 재수화는 Redis 설정 키가
                #    만료(ex=300)될 때마다 돌고, 예전에는 DB 에 저장된 값이 없어서
                #    DEFAULT_THRESHOLDS 를 써 넣었다 — admin-api 가 방금 써 둔 운영자
                #    임계값을 5분마다 조용히 덮었다. migration 0037 이후 DB 가 원천이다.
                "thresholds": _row_thresholds(config),
                "app_clients": app_clients,
            }
            # TTL 300s to match team/client hydrate + admin warmers. Without it a
            # lost invalidation could leave this parent config stale-forever
            # (P0-③ review fix). admin DEL is the durable path; TTL is the backstop.
            await redis.set(config_key, json.dumps(config_data), ex=300)

    async def _hydrate_team_config_cache(
        self,
        redis,
        db: AsyncSession,
        team_id: str,
    ) -> None:
        """TEAM budget config cache miss 시 DB에서 조회해 Redis에 SET (best-effort).

        admin-api startup warmup 의 안전망:
        - init SQL seed 예산 또는 post-startup 신규 팀 등 admin-api warmup 이후에
          생성된 TEAM 예산이 아직 Redis에 없는 경우를 처리.
        - 키가 없으면 do-nothing → Lua 가 team_budget_unset 을 정상 반환.
        """
        try:
            result = await db.execute(
                select(BudgetConfig)
                .where(BudgetConfig.scope == BudgetScope.TEAM)
                .where(BudgetConfig.scope_id == team_id)
                .where(BudgetConfig.is_active == True)  # noqa: E712
                .limit(1)
            )
            config = result.scalar_one_or_none()
            if config is None:
                return

            config_data = {
                "limit_usd": str(config.max_budget_usd),
                "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
                "thresholds": _row_thresholds(config),
            }
            config_key = f"budget:config:team:{{{team_id}}}"
            # TTL은 admin-api BUDGET_CONFIG_CACHE_TTL 과 일치 (5분)
            await redis.set(config_key, json.dumps(config_data), ex=300)
            logger.info(
                "team_budget_config_hydrated",
                team_id=team_id,
                limit_usd=str(config.max_budget_usd),
            )
        except Exception:
            logger.warning("team_budget_config_hydrate_failed", team_id=team_id)

    async def _hydrate_client_config_cache(
        self,
        redis,
        db: AsyncSession,
        user_id: str,
        client: str,
    ) -> None:
        """Per-app (user, client) budget config cache miss 시 DB에서 조회해 SET.

        P0-③ 안전망: admin 의 best-effort SET 이 유실되어 per-app config 키가
        없을 때, gateway 가 DB 의 활성 per-app BudgetConfig 행으로 키를 재생성해
        per-app 한도가 우회되지 않도록 한다. 키 shape 은 admin
        `_sync_redis_app_config` 와 동일(limit_usd, lowercase policy, thresholds).
        키가 없으면(활성 행 없음) do-nothing → Lua 가 config_present=false 로
        pass-through (정상: 해당 client 에 per-app 예산이 없는 경우).
        """
        try:
            result = await db.execute(
                select(BudgetConfig)
                .where(BudgetConfig.scope == BudgetScope.USER)
                .where(BudgetConfig.scope_id == user_id)
                .where(BudgetConfig.client == client)
                .where(BudgetConfig.is_active == True)  # noqa: E712
                .limit(1)
            )
            config = result.scalar_one_or_none()
            if config is None:
                return

            config_data = {
                "limit_usd": str(config.max_budget_usd),
                "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
                "thresholds": _row_thresholds(config),
            }
            config_key = f"budget:config:user:{{{user_id}}}:{client}"
            await redis.set(config_key, json.dumps(config_data), ex=300)
            logger.info(
                "client_budget_config_hydrated",
                user_id=user_id,
                client=client,
                limit_usd=str(config.max_budget_usd),
            )
        except Exception:
            logger.warning(
                "client_budget_config_hydrate_failed", user_id=user_id, client=client
            )
