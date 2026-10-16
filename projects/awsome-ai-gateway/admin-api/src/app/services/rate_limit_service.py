# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import NotFoundError, ValidationError
from app.models.auth import Team, User
from app.models.model import ModelAlias, ModelStatus, RateLimitConfig, RateLimitScope
from app.repositories.model_repository import ModelRepository, RateLimitConfigRepository
from app.repositories.user_repository import UserRepository
from app.schemas.rate_limits import (
    RateLimitConfigItem,
    RateLimitDeleteResponse,
    RateLimitResponse,
    RateLimitSetRequest,
    RateLimitTreeNode,
)

logger = structlog.get_logger()

# Redis glob 메타문자. SCAN MATCH 패턴에 **리터럴**로 넣는 값(model alias, URL 의 scope_id)은
# 반드시 이스케이프한다 — 이 레포에는 실제로 `claude-sonnet-4-6[1m]` alias 가 있고, `[1m]` 은
# glob 문자 클래스('1' 또는 'm' 한 글자)라 이스케이프 없이는 자기 자신의 키와 매치되지 않는다
# (= 조용히 0). scope_id 자리에 `*` 를 넣으면 남의 카운터까지 합산된다.
_GLOB_META = "*?[]\\"


def _glob_escape(value: str) -> str:
    return "".join(f"\\{ch}" if ch in _GLOB_META else ch for ch in value)


#: ``session.info`` 키 — 커밋 뒤에 지울 gateway-proxy 정책 캐시(키 목록, 패턴 목록).
_PENDING_INVALIDATIONS = "rate_limit.pending_cache_invalidations"


def _queue_invalidation(
    session: AsyncSession, *, keys: tuple[str, ...] = (), patterns: tuple[str, ...] = ()
) -> None:
    pending = session.info.setdefault(_PENDING_INVALIDATIONS, {"keys": [], "patterns": []})
    pending["keys"].extend(keys)
    pending["patterns"].extend(patterns)


class RateLimitService:
    def __init__(self, cache_mgr: CacheInvalidationManager) -> None:
        self._cache_mgr = cache_mgr

    async def invalidate_after_commit(self, session: AsyncSession) -> None:
        """이 세션의 rate-limit 변경이 쌓아 둔 캐시 무효화를 실행한다. **``session.commit()`` 뒤에** 호출한다.

        ⚠️ 커밋 전에 지우면 안 된다. 지운 뒤 · 커밋 전 사이에 들어온 게이트웨이 요청의 캐시 미스는
           자기 커넥션으로 **커밋된(=옛)** 행을 읽어 300초 다시 캐시한다. 바꾼 GLOBAL 한도는 콘솔이 새
           값을 보여주는 동안 옛 값으로 5분 더 집행됐고, 지운 개인 한도·새로 건 한도도 같은 창에서
           5분씩 어긋났다(실 PG+Redis 재현:
           tests/regression/test_high_rate_limit_invalidate_after_commit_real.py).
           실패는 CacheInvalidationManager 가 세션에 기록한다 — 커밋 뒤라 새 트랜잭션이 열리고
           CommittingRoute 가 응답 전에 커밋한다.
        """
        pending = session.info.pop(_PENDING_INVALIDATIONS, None)
        if not pending:
            return
        await self._cache_mgr.invalidate(pending["keys"], session=session)
        for pattern in pending["patterns"]:
            await self._cache_mgr.invalidate_pattern(pattern, session=session)

    async def set_user_rate_limit(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        data: RateLimitSetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitResponse:
        return await self._set_rate_limit(
            session,
            scope=RateLimitScope.USER,
            scope_id=user_id,
            data=data,
            actor=actor,
            cache_key=f"ratelimit:config:user:{user_id}",
            ip_address=ip_address,
            request_id=request_id,
        )

    async def set_team_rate_limit(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        data: RateLimitSetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitResponse:
        # BR-RL-01: CPM/CPH allowed for USER and TEAM scopes (GLOBAL 제외).
        # 단기 비용 폭주 방어는 개별/팀 레벨에서. GLOBAL은 월 예산 엔진이 커버.

        return await self._set_rate_limit(
            session,
            scope=RateLimitScope.TEAM,
            scope_id=team_id,
            data=data,
            actor=actor,
            cache_key=f"ratelimit:config:team:{team_id}",
            ip_address=ip_address,
            request_id=request_id,
        )

    async def set_global_rate_limit(
        self,
        session: AsyncSession,
        *,
        model_alias: str,
        data: RateLimitSetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitResponse:
        # BR-RL-01: CPM/CPH not allowed for GLOBAL scope
        # (단기 비용 폭주 방어는 USER/TEAM 레벨에서. GLOBAL은 월 예산 엔진이 커버.)
        if data.cpm is not None or data.cph is not None:
            raise ValidationError("CPM/CPH limits are only allowed for USER/TEAM scopes")

        alias_exists = await session.scalar(
            select(ModelAlias.alias).where(ModelAlias.alias == model_alias)
        )
        if alias_exists is None:
            raise NotFoundError("ModelAlias", model_alias)

        # GLOBAL 은 모델별이다(0046) — upsert 는 이 alias 의 활성 행만 바꾸고 다른 모델의 GLOBAL 한도는
        # 그대로 둔다. 해제할 것이 없으므로 확인(예전의 409 + release_alias)도 없다.
        return await self._set_rate_limit(
            session,
            scope=RateLimitScope.GLOBAL,
            scope_id=None,
            data=data,
            actor=actor,
            model_alias=model_alias,
            cache_key=f"ratelimit:config:global:{model_alias}",
            ip_address=ip_address,
            request_id=request_id,
        )

    async def get_rate_limit_tree(self, session: AsyncSession) -> list[RateLimitTreeNode]:
        """RL 화면이 읽는 **집행 표면 전체**를 트리로 반환.

        루트는 세 부류이며 형제로 나란히 놓인다(GLOBAL 은 모델 축, TEAM/USER 는 주체 축이라
        부모/자식 관계가 아니다):

          1. GLOBAL — 모델 alias 당 1개. ``id`` 는 UUID 가 아니라 **alias 문자열**이다.
             admin-ui 가 ``PUT /admin/rate-limits/{scope}/{node.id}`` 로 URL 을 만들기 때문에,
             이 노드가 없던 동안 GLOBAL 쓰기 경로는 콘솔에서 **도달 불가**였고, API/IaC 로 건
             한도는 gateway-proxy 가 매 요청 집행하는데도(rate_limit_config_loader) 화면에서
             "설정 없음" 으로 보였다. 한도 없는 활성 모델도 노드로 낸다 — 그래야 새 한도를
             걸 수 있다. 비활성 모델이라도 한도가 걸려 있으면 낸다(집행은 계속되므로).
          2. TEAM — 팀당 1개, 멤버(USER)를 자식으로.
          3. USER(팀 미배정) — ``auth.users.team_id`` 는 NULL 을 허용하므로 어느 팀의 members
             에도 없는 사용자가 있을 수 있다. USER 스코프 한도는 그들에게도 집행되고
             (gateway-proxy 는 이들에게 TEAM 한도를 적용하지 않는다 — 키 인증이면 team_id 가
             None 이 아니라 "" 라서 TEAM 조회를 시도했다가 실패하고 rl_config_db_query_failed
             로 남긴 뒤 무제한으로 취급한다) PUT 도 받으므로 트리에 있어야 한다. 상속할 TEAM
             이 없으니 inherited_from 은 항상 None 이다.

        GLOBAL 한도는 **모델마다 따로** 활성이다(0046: 활성 GLOBAL 행은 model alias 당 1개 —
        ``uq_rate_limit_configs_global_active``). 한 노드를 저장해도 다른 모델의 GLOBAL 노드는 그대로다
        — upsert 가 그 alias 의 행만 바꾼다. gateway-proxy 도 GLOBAL 을 요청의 alias 로 읽고 카운터를
        alias 별로 둔다(``{GLOBAL:*:<alias>}``). 그래서 같은 Bedrock model ID 를 공유하는 alias(예:
        ``[1m]`` 변형)는 GLOBAL 한도와 카운터가 각각이다 — 합이 쿼터 아래가 되게 각자 잡아야 한다.
        """
        user_repo = UserRepository(session)
        rl_repo = RateLimitConfigRepository(session)
        model_repo = ModelRepository(session)

        teams = await user_repo.list_all_teams()
        teamless_users = await user_repo.list_teamless_users()
        team_configs = await rl_repo.list_active_by_scope(RateLimitScope.TEAM)
        user_configs = await rl_repo.list_active_by_scope(RateLimitScope.USER)
        global_configs = await rl_repo.list_active_by_scope(RateLimitScope.GLOBAL)
        active_aliases = await model_repo.list_alias_names(status=ModelStatus.ACTIVE)

        team_config_map: dict[str, RateLimitConfig] = {
            str(c.scope_id): c for c in team_configs if c.scope_id
        }
        user_config_map: dict[str, RateLimitConfig] = {
            str(c.scope_id): c for c in user_configs if c.scope_id
        }
        global_config_map: dict[str, RateLimitConfig] = {
            c.model_alias: c for c in global_configs if c.model_alias
        }

        def _make_config(
            cfg: RateLimitConfig, scope: str, *, target_id: str | None = None
        ) -> RateLimitConfigItem:
            return RateLimitConfigItem(
                target_id=(
                    target_id
                    if target_id is not None
                    else (str(cfg.scope_id) if cfg.scope_id else "")
                ),
                scope=scope,
                rpm=cfg.rpm_limit,
                tpm=cfg.tpm_limit,
                cpm=cfg.cpm_limit_usd,
                cph=cfg.cph_limit_usd,
            )

        def _user_node(user: User, *, team_cfg: RateLimitConfig | None) -> RateLimitTreeNode:
            user_id = str(user.id)
            user_cfg = user_config_map.get(user_id)
            inherited_from: str | None = None
            effective_cfg: RateLimitConfigItem | None = None
            if user_cfg:
                effective_cfg = _make_config(user_cfg, "USER")
            elif team_cfg:
                # 자기 행이 없다. 팀 설정을 '실효값'으로 싣되 config.scope/target_id 는 TEAM 을
                # 가리키고 inherited_from 을 채운다 — admin-ui 는 이 표식으로 빌려온 값을 폼에
                # 프리필하지 않는다(프리필하면 저장 시 USER 행으로 굳는다).
                effective_cfg = _make_config(team_cfg, "TEAM")
                inherited_from = str(team_cfg.scope_id)
            return RateLimitTreeNode(
                id=user_id,
                label=user.display_name,
                scope="USER",
                is_active=user.is_active,
                config=effective_cfg,
                children=[],
                inherited_from=inherited_from,
            )

        nodes: list[RateLimitTreeNode] = []

        # 1) GLOBAL — 활성 모델 ∪ 한도가 걸린 모델
        for alias in sorted(set(active_aliases) | set(global_config_map)):
            global_cfg = global_config_map.get(alias)
            nodes.append(
                RateLimitTreeNode(
                    id=alias,
                    label=alias,
                    scope="GLOBAL",
                    is_active=True,
                    config=(
                        _make_config(global_cfg, "GLOBAL", target_id=alias) if global_cfg else None
                    ),
                    children=[],
                    inherited_from=None,
                )
            )

        # 2) TEAM (+ 멤버)
        for team in teams:
            team_id = str(team.id)
            team_cfg = team_config_map.get(team_id)

            member_nodes = [_user_node(member, team_cfg=team_cfg) for member in team.members]

            has_active_members = any(m.is_active for m in team.members)
            nodes.append(
                RateLimitTreeNode(
                    id=team_id,
                    label=team.name,
                    scope="TEAM",
                    is_active=has_active_members,
                    config=_make_config(team_cfg, "TEAM") if team_cfg else None,
                    children=member_nodes,
                    inherited_from=None,
                )
            )

        # 3) 팀 미배정 USER
        for user in teamless_users:
            nodes.append(_user_node(user, team_cfg=None))

        return nodes

    async def get_live_usage(
        self,
        scope: str,
        scope_id: str,
        *,
        session: AsyncSession | None = None,
        window_ms: int = 60_000,
    ) -> dict:
        """gateway-proxy 가 적재하는 **실시간 RPM 카운터**(Redis ZSET)를 읽어 현재
        사용량/잔여를 반환(§60.9). 설정값만 보던 RL 화면에 실시간 상태를 더한다.

        proxy 키 규약(gateway-proxy/rate_limit_scope.py:build_rl_key):
          `{{SCOPE:scope_id:model_alias}}:rpm`  (ZSET, member=request_id, score=now_ms)
          GLOBAL 은 scope_id 자리에 리터럴 ``*``(GLOBAL_WILDCARD) 이 들어가고 모델은
          model_alias 자리로 갈린다. 그래서 GLOBAL 조회의 ``scope_id`` 인자는 **model alias**
          다(트리의 GLOBAL 노드 id 와 같다) — 한 모델의 GLOBAL 사용량에 다른 모델을 섞지 않는다.
        현재 사용량 = window(60s) 안의 ZSET 항목 수 = ZCOUNT(key, now-window, +inf).

        429 누적은 usage_logs 에 없다(status enum=SUCCESS/ERROR/TIMEOUT). 실시간
        429 는 proxy 메트릭 영역이므로 여기선 'rpm 현재/한도/잔여'만 정확히 제공.
        fail-soft: Redis 오류 → available=false (config 화면은 그대로).

        ``measured`` — 왜 필요한가:
            gateway-proxy 는 **자기 rpm 한도가 양수인 스코프에만** ZSET 을 만든다
            (gateway-proxy services/rate_limit_service.py ``check_multi_scope_rpm``:
            ``[d for d in descriptors if d.rpm_limit and d.rpm_limit > 0]``). 한도가 없는
            사용자/팀은 요청을 아무리 보내도 키가 없다. 키 0건과 '측정된 0' 을 구별하지 않으면
            화면은 "최근 60초간 요청 없음" 을 **단정**하는데, 그 대상이 바로 운영자가 한도를
            걸지 판단하려고 보는 모집단이다. 그래서 자기 스코프의 활성 rpm 한도를 읽어
            ``measured``/``limit_rpm`` 으로 함께 내린다(``session`` 이 없으면 카운터가 실제로
            잡힌 경우에만 measured=true).
        """
        import time

        redis = self._cache_mgr._redis
        sc = (scope or "").upper()
        if sc not in ("USER", "TEAM", "GLOBAL"):
            return {"available": False, "measured": False, "reason": "invalid scope"}
        # ⚠️ GLOBAL 의 scope_id 자리는 proxy 가 `*` 로 적는다(rate_limit_scope.GLOBAL_WILDCARD).
        #    예전엔 `__global__` 을 조립해 SCAN 이 0건이었다. `[*]` 는 Redis 글롭에서 **리터럴
        #    별표** 문자 클래스라 `*` 키만 정확히 잡는다. alias/scope_id 는 _glob_escape 로 리터럴화.
        if sc == "GLOBAL":
            pattern = f"{{GLOBAL:[*]:{_glob_escape(scope_id)}}}:rpm"
        else:
            pattern = f"{{{sc}:{_glob_escape(scope_id)}:*}}:rpm"  # 이 주체의 모든 모델 rpm ZSET
        now_ms = int(time.time() * 1000)
        window_start = now_ms - window_ms

        own_rpm_limit = (
            await self._own_active_rpm_limit(session, sc, scope_id) if session is not None else None
        )

        try:
            per_model: list[dict] = []
            total = 0
            async for key in redis.scan_iter(match=pattern, count=200):
                k = key.decode() if isinstance(key, (bytes, bytearray)) else key
                # 윈도우 내 항목 수(만료분 제외) — proxy 의 ZCARD-after-cleanup 과 동치.
                cnt = await redis.zcount(k, window_start, "+inf")
                cnt = int(cnt or 0)
                if cnt <= 0:
                    continue
                # 키에서 model_alias 추출: {SCOPE:sid:MODEL}:rpm — alias 에 ':' 가 있어도
                # 보존한다(Bedrock 모델 id 는 `...-v1:0` 처럼 ':' 를 품는다).
                inner = k[k.find("{") + 1 : k.find("}")]
                parts = inner.split(":", 2)
                model_alias = parts[2] if len(parts) >= 3 else "*"
                per_model.append({"model_alias": model_alias, "rpm_used": cnt})
                total += cnt
            return {
                "available": True,
                "scope": sc,
                "scope_id": scope_id,
                "window_sec": window_ms // 1000,
                # 카운터가 실제로 잡혔다면(total>0) 한도 조회 결과와 무관하게 측정된 것이다.
                "measured": own_rpm_limit is not None or total > 0,
                "limit_rpm": own_rpm_limit,
                "rpm_used_total": total,
                "by_model": sorted(per_model, key=lambda x: -x["rpm_used"]),
            }
        except Exception as exc:  # noqa: BLE001 — 실시간 조회 실패가 화면을 막지 않게
            logger.warning("rl_live_usage_failed", error=str(exc), scope=sc, scope_id=scope_id)
            return {"available": False, "measured": False, "reason": f"{type(exc).__name__}"}

    async def _own_active_rpm_limit(
        self, session: AsyncSession, scope: str, scope_id: str
    ) -> int | None:
        """이 스코프 **자신의** 활성 rpm 한도(양수). 없으면 None = 카운터가 존재할 수 없다.

        상속은 보지 않는다 — gateway-proxy 는 스코프별로 독립 집행하므로 USER 행이 없는
        사용자에게는 USER ZSET 이 생기지 않는다(팀 한도는 TEAM 키에 쌓인다). 상속을 끌어오면
        카운터가 없는 사용자를 measured=true 로 만들어 "요청 없음" 단정이 되살아난다.
        """
        try:
            conditions = [RateLimitConfig.is_active.is_(True)]
            if scope == "GLOBAL":
                conditions += [
                    RateLimitConfig.scope == RateLimitScope.GLOBAL,
                    RateLimitConfig.scope_id.is_(None),
                    RateLimitConfig.model_alias == scope_id,
                ]
            else:
                conditions += [
                    RateLimitConfig.scope == RateLimitScope(scope),
                    RateLimitConfig.scope_id == uuid.UUID(scope_id),
                ]
            result = await session.execute(select(RateLimitConfig).where(*conditions))
            rows = [r for r in result.scalars().all() if r.rpm_limit and r.rpm_limit > 0]
            if not rows:
                return None
            # USER/TEAM 은 활성 행이 하나뿐이다(0024 유니크 인덱스). 그래도 여러 행이 보이면
            # 전 모델 행(model_alias IS NULL)을 대표값으로 쓴다 — 화면 게이지 분모는 하나여야 한다.
            for row in rows:
                if row.model_alias is None:
                    return int(row.rpm_limit)
            return max(int(r.rpm_limit) for r in rows)
        except Exception as exc:  # noqa: BLE001 — 한도 조회 실패가 사용량 표시를 막지 않게
            logger.warning(
                "rl_own_limit_lookup_failed", error=str(exc), scope=scope, scope_id=scope_id
            )
            return None

    async def delete_user_rate_limit(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitDeleteResponse:
        """USER 오버라이드 **제거** → 팀 설정 상속으로 복귀.

        PUT 은 전체 교체이므로 "입력을 모두 비우고 저장" 은 전부 NULL 인 USER 행을 남긴다.
        집행상으로는 행이 없는 것과 같지만(전부 None → 그 스코프 스킵, gateway-proxy
        rate_limit_config_loader), 트리에는 ``config`` 가 있는 노드로 보여 상속 표식이 사라진다
        — 팀 한도가 걸려 있는데도 콘솔에서는 무제한처럼 읽힌다. 되돌릴 경로가 없었다.
        멱등이다: 원래 없었으면 deactivated=0.
        """
        repo = RateLimitConfigRepository(session)
        # 지우기 전 값을 감사 기록에 남긴다 — ``deactivated: 1`` 만으로는 무엇이 사라졌는지 복원할 수 없다.
        # (scope, scope_id) 의 활성 행은 0024 유니크 인덱스로 최대 1개다.
        before = await repo.get_active(RateLimitScope.USER, user_id)
        removed = await repo.deactivate_configs(RateLimitScope.USER, user_id)

        # _set_rate_limit 이 심는 스냅샷 키와 gateway-proxy 정책 캐시를 함께 지운다 — 남겨 두면
        # 제거 후에도 최대 5분(TTL) 동안 옛 정책이 집행된다. ⚠️ 지우는 것은 라우터가 커밋한 **뒤**다
        # (invalidate_after_commit). 여기서는 대상만 쌓는다.
        _queue_invalidation(
            session,
            keys=(f"ratelimit:config:user:{user_id}",),
            patterns=(f"rl:config:{RateLimitScope.USER.value}:{user_id}:*",),
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="DELETE_RATE_LIMIT",
            resource_type="RateLimitConfig",
            resource_id=str(user_id),
            changes={
                "before": None
                if before is None
                else {
                    "model_alias": before.model_alias,
                    "rpm": before.rpm_limit,
                    "tpm": before.tpm_limit,
                    "cpm": str(before.cpm_limit_usd) if before.cpm_limit_usd is not None else None,
                    "cph": str(before.cph_limit_usd) if before.cph_limit_usd is not None else None,
                },
                "after": {"scope": RateLimitScope.USER.value, "deactivated": removed},
            },
            ip_address=ip_address,
            request_id=request_id,
        )

        return RateLimitDeleteResponse(
            scope=RateLimitScope.USER.value,
            scope_id=str(user_id),
            deactivated=removed,
        )

    async def _set_rate_limit(
        self,
        session: AsyncSession,
        *,
        scope: RateLimitScope,
        scope_id: uuid.UUID | None,
        data: RateLimitSetRequest,
        actor: CurrentUser,
        cache_key: str,
        model_alias: str | None = None,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> RateLimitResponse:
        repo = RateLimitConfigRepository(session)

        config = RateLimitConfig(
            id=uuid.uuid4(),
            scope=scope,
            scope_id=scope_id,
            model_alias=model_alias,
            rpm_limit=data.rpm,
            tpm_limit=data.tpm,
            cpm_limit_usd=data.cpm,
            cph_limit_usd=data.cph,
            is_active=True,
            created_by=actor.user_id,
        )
        await repo.upsert(config)

        # Write config to Redis for Gateway Proxy
        config_json = json.dumps({
            "rpm": data.rpm,
            "tpm": data.tpm,
            "cpm": str(data.cpm) if data.cpm else None,
            "cph": str(data.cph) if data.cph else None,
        })
        await self._cache_mgr._redis.set(cache_key, config_json)

        # Invalidate gateway-proxy's rate-limit policy cache so the new policy
        # takes effect on the next request instead of waiting for the 5-min TTL.
        # Gateway uses keys: rl:config:{SCOPE}:{scope_id_or_NULL}:{model_alias}
        # USER/TEAM with model_alias=None covers all models → wildcard delete.
        # ⚠️ 지우는 것은 라우터가 커밋한 **뒤**다(invalidate_after_commit). 여기서는 대상만 쌓는다.
        sid = str(scope_id) if scope_id is not None else "NULL"
        if model_alias is not None:
            # GLOBAL: upsert 는 이 alias 의 행만 바꾼다(0046) — 다른 모델의 캐시는 여전히 맞으므로 이
            # 모델의 키 하나만 지운다. alias 는 위에서 model_aliases 에 정확히 있는지 확인했으므로
            # gateway-proxy 가 쓰는 키(model_config.alias = 그 행의 alias)와 글자까지 같다.
            _queue_invalidation(session, keys=(f"rl:config:{scope.value}:{sid}:{model_alias}",))
        else:
            _queue_invalidation(session, patterns=(f"rl:config:{scope.value}:{sid}:*",))

        after: dict = {"scope": scope.value, "rpm": data.rpm, "tpm": data.tpm}
        if model_alias is not None:
            # GLOBAL 은 모델별이다 — resource_id(새 행 id)만으로는 어느 모델의 한도인지 알 수 없다.
            after["model_alias"] = model_alias
        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_RATE_LIMIT",
            resource_type="RateLimitConfig",
            resource_id=str(config.id),
            changes={"after": after},
            ip_address=ip_address,
            request_id=request_id,
        )

        return RateLimitResponse(
            scope=scope.value,
            scope_id=str(scope_id) if scope_id else None,
            model_alias=model_alias,
            rpm_limit=config.rpm_limit,
            tpm_limit=config.tpm_limit,
            cpm_limit_usd=config.cpm_limit_usd,
            cph_limit_usd=config.cph_limit_usd,
            is_active=config.is_active,
        )
