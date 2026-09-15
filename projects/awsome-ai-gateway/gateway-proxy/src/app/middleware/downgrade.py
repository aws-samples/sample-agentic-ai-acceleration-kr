# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.metrics import GatewayMetrics
from app.services.downgrade_loader import DowngradePolicyLoader, apply_chain
from app.services.router_service import RouterService
from app.services.thinking_normalizer import normalize_thinking

logger = structlog.get_logger(__name__)

DOWNGRADE_PATH_PREFIXES = (
    "/v1/messages",
    "/v1/chat",
    "/v1/completions",
    # ⚠️ Codex 는 이 경로로만 온다(Responses API). 빠져 있으면 그 클라이언트의 트래픽은
    #    예산 임계 자동 강등을 **통째로 무시**한다 — 관리자는 강등 규칙을 저장하고 화면에서
    #    활성으로 보지만, 정작 비용이 큰 클라이언트에만 적용되지 않는다. 설정이 없는 것보다
    #    나쁘다: 적용되고 있다고 믿게 된다.
    "/v1/responses",
    "/model/",
)

#: 본문의 모델 필드 이름이 OpenAI 계열인 경로들. Responses 도 `model` 키를 쓰므로 함께 둔다.
_OPENAI_PATH_PREFIXES = ("/v1/chat", "/v1/completions", "/v1/responses")


def _path_eligible(path: str) -> bool:
    return any(path.startswith(p) for p in DOWNGRADE_PATH_PREFIXES)


def _is_openai_path(path: str) -> bool:
    return any(path.startswith(p) for p in _OPENAI_PATH_PREFIXES)


class DowngradeMiddleware:
    """Pure ASGI 미들웨어 — request body의 model alias를
    팀 다운그레이드 정책에 따라 rewrite한다.

    체인 위치: auth → budget → DowngradeMiddleware → rate_limit → router
    """

    def __init__(
        self,
        app: ASGIApp,
        loader: DowngradePolicyLoader | None = None,
        router: RouterService | None = None,
    ) -> None:
        self.app = app
        self.loader = loader or DowngradePolicyLoader()
        self.router = router or RouterService()
        self._metrics: GatewayMetrics | None = None

    def _get_metrics(self) -> GatewayMetrics | None:
        # GatewayMetrics는 OTel meter provider 초기화 후에만 생성. lazy.
        if self._metrics is None:
            try:
                self._metrics = GatewayMetrics()
            except Exception:
                self._metrics = None
        return self._metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not _path_eligible(path):
            await self.app(scope, receive, send)
            return

        logger.info("downgrade_mw_entered", path=path)

        state = scope.setdefault("state", {})

        # Cowork(client='cowork')는 routing profile 이 모델을 무조건 cowork-opus(Mantle)로
        # override 하므로(messages.py:_select_backend) 다운그레이드가 실효가 없다. 여기서
        # body model 을 바꿔봐야 _select_backend 가 무시 → 무의미한 DB 룩업 + body 재작성
        # + resolve_bedrock_model(cowork-opus 는 BEDROCK_MANTLE) lookup_failed 로그만 남는다.
        # 명시적으로 skip 한다. (다운그레이드는 사실상 Claude Code/Bedrock 경로 전용.)
        if state.get("client") == "cowork":
            logger.info("downgrade_mw_skip_cowork")
            await self.app(scope, receive, send)
            return

        auth_context = state.get("auth_context")
        budget_status = state.get("budget_status")
        team_id = getattr(auth_context, "team_id", None) if auth_context else None
        if not budget_status or not team_id:
            logger.info(
                "downgrade_mw_skip_precheck",
                has_budget_status=bool(budget_status),
                team_id=str(team_id) if team_id else None,
                has_auth_context=bool(auth_context),
            )
            await self.app(scope, receive, send)
            return

        body_bytes = await _drain_body(receive)
        try:
            payload = json.loads(body_bytes) if body_bytes else None
        except json.JSONDecodeError:
            logger.info("downgrade_mw_skip_invalid_json")
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        original = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(original, str) or not original:
            logger.info("downgrade_mw_skip_no_model_field", payload_type=type(payload).__name__)
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        # 정책 from_alias 는 standard alias (예: 'claude-sonnet-4-6') 로 저장되지만
        # 클라이언트가 보내는 model 값은 변형(예: 'global.anthropic.claude-sonnet-4-6',
        # 'claude-sonnet-4-6[1m]', cross-region prefix 등) 일 수 있다. router 와
        # 동일한 alias resolver 로 standard alias 로 정규화한 뒤 매칭한다.
        # 정규화 실패(미등록 alias)면 raw 문자열 그대로 매칭에 시도 — 기존 동작 유지.
        session_factory = state.get("_session_factory")
        redis = state.get("_redis")

        normalized = original
        try:
            if session_factory is not None:
                async with session_factory() as db:
                    if _is_openai_path(path):
                        cfg = await self.router.resolve_openai_model(redis, db, original)
                    else:
                        cfg = await self.router.resolve_bedrock_model(redis, db, original)
            else:
                if _is_openai_path(path):
                    cfg = await self.router.resolve_openai_model(redis, None, original)
                else:
                    cfg = await self.router.resolve_bedrock_model(redis, None, original)
            normalized = cfg.alias or original
        except Exception:
            # 미등록/INACTIVE/DB 미가용 — raw 매칭으로 fallback
            normalized = original

        try:
            if session_factory is not None:
                async with session_factory() as db:
                    rules = await self.loader.get_active_rules(
                        redis,
                        db,
                        _coerce_uuid(team_id),
                    )
            else:
                rules = await self.loader.get_active_rules(
                    redis,
                    None,
                    _coerce_uuid(team_id),
                )
        except Exception as exc:
            logger.warning(
                "downgrade_lookup_failed",
                reason=type(exc).__name__,
                exc_info=True,
            )
            metrics = self._get_metrics()
            if metrics:
                metrics.downgrade_lookup_failed_total.add(1, {"reason": type(exc).__name__})
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        current_pct = int(getattr(budget_status, "threshold_pct", 0) or 0)
        effective, hops = apply_chain(normalized, rules, current_pct)

        logger.info(
            "downgrade_mw_evaluated",
            team_id=str(team_id),
            original_model=original,
            normalized_model=normalized,
            effective_model=effective,
            hops=hops,
            current_pct=current_pct,
            rules_count=len(rules),
            rules=[
                {"from": r.from_alias, "to": r.to_alias, "th": r.threshold_pct}
                for r in rules
            ],
        )

        if effective == normalized:
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        payload["model"] = effective

        # 강등 대상 모델이 받는 형태로 `thinking` 을 맞춘다.
        #
        # ⚠️ 예전에는 `effective.startswith("claude-haiku-4-5")` 일 때 **지웠다.** 두 가지가
        #    틀렸다: (1) 운영자가 만든 alias(예: team-haiku-cheap)는 그 접두사로 시작하지
        #    않아 판정을 빠져나가고 강등 뒤 400 이 됐다. (2) 지우는 것은 haiku 가 실제로
        #    받는 `{"type":"enabled"}` 까지 없애서, 사용자는 200 을 받으면서 extended
        #    thinking 만 조용히 잃는다.
        #
        # ⚠️ 이 계층은 모델 **해석 전**이라 provider_model_id 를 모른다 — alias 로 계열을
        #    추정한다(_family_from_alias). 모르는 alias 는 그대로 통과시킨다(fail-open).
        _before = json.dumps(payload.get("thinking"), sort_keys=True)
        payload = normalize_thinking(
            payload, None, alias=effective, request_id=state.get("request_id", "")
        )
        if json.dumps(payload.get("thinking"), sort_keys=True) != _before:
            logger.info("downgrade_thinking_normalized", target=effective, original=original)

        # ensure_ascii=False: 한국어/일본어 content 보존; separators: 컴팩트 JSON
        new_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        state["downgraded_from"] = original

        # Content-Length header 갱신 — body byte 길이가 바뀌었음
        new_headers = [
            (k, v) for (k, v) in scope.get("headers", []) if k.lower() != b"content-length"
        ]
        new_headers.append((b"content-length", str(len(new_body)).encode()))
        scope = dict(scope)
        scope["headers"] = new_headers

        metrics = self._get_metrics()
        if metrics:
            metrics.downgrade_applied_total.add(1, {"from_alias": original, "to_alias": effective})
            metrics.downgrade_chain_depth.record(hops)

        await self.app(scope, _replay(new_body, receive), send)


async def _drain_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        else:
            # disconnect 등은 그대로 흡수
            break
    return b"".join(chunks)


def _replay(body: bytes, original_receive: Receive) -> Receive:
    """body 1회 재생 후에는 원본 receive 로 위임.

    이전 구현은 두 번째 호출부터 `http.disconnect` 를 반환했는데,
    StreamingResponse 의 listen_for_disconnect 가 이를 즉시 client 끊김
    으로 오해해 stream task 를 cancel 시키는 버그가 있었다. 원본 receive
    로 위임하면 진짜 disconnect 이벤트를 기다리게 되어 streaming 정상 동작.
    """
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original_receive()

    return receive


def _coerce_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
