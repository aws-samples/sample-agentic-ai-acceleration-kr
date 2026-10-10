"""정해 둔 anthropic-beta 만 Bedrock 본문으로 넘긴다 (2026-10-05).

Claude Code 는 ``ANTHROPIC_BASE_URL`` 뒤에서 ``anthropic-beta`` 헤더로 beta 를
11개 보낸다. Bedrock InvokeModel 은 beta 를 본문 ``anthropic_beta`` 로만 받고,
모르는 beta 가 하나라도 있으면 요청 전체를 400 으로 거부한다
(``prompt-caching-scope-2026-01-05``). 실측 결과 ``dangerous-tool-use``
(+ ``safeguards``)와 ``per-turn-control`` 만 넘기면 서버측 auto mode 분류기
판정이 오고 400 이 사라졌다.

계약:
- 넘길 목록은 설정(``BEDROCK_FORWARD_BETAS``, ``beta[:field]``)에서 읽는다.
  빈 값 = 아무것도 넘기지 않음(이전 동작과 같음);
- 클라이언트가 보낸 것 중 목록에 있는 것만 넘기고, 짝 필드가 있는 beta 는 그
  필드가 요청에 있을 때만 넘긴다. 짝 필드는 그 beta 와 함께일 때만 넘어간다;
- 원본 요청은 바꾸지 않고, 이미 있는 ``anthropic_beta`` 는 유지한다;
- 버린 beta 이름은 프로세스당 한 번, 상한까지만 기록한다.

English: only the configured anthropic-beta values reach the Bedrock body
(2026-10-05). Claude Code sends 11 betas in the ``anthropic-beta`` header;
Bedrock InvokeModel takes betas only in the body and 400s the whole request on
one it does not know. Contract: the list comes from settings (blank = forward
nothing, the previous behaviour); only betas the client sent and the list names
are forwarded, a paired field travels only with its beta and a paired beta only
when its field is present; the source request is never mutated and an existing
``anthropic_beta`` is kept; dropped names are logged once per process, up to a
cap.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from app.config import get_settings
from app.providers.registry import ProviderRegistry
from app.routers import messages as messages_router
from app.schemas.domain import ProviderType, TokenUsage
from app.services import upstream_compat
from app.services.upstream_compat import (
    apply_forwarded_betas,
    client_betas,
    forward_beta_map,
)
from tests.unit.test_count_tokens_router import _model_config

DTU = "dangerous-tool-use-2026-09-03"
PTC = "per-turn-control-2026-07-01"

#: Claude Code 2.1.289 + Sonnet 5.5 가 실제로 보낸 헤더(2026-10-05 캡처).
#: English: the header Claude Code 2.1.289 + Sonnet 5.5 actually sent.
CLAUDE_CODE_HEADER = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,"
    "thinking-token-count-2026-05-13,context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,"
    "per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01,"
    "effort-2025-11-24,dangerous-tool-use-2026-09-03,afk-mode-2026-01-31"
)
SAFEGUARDS = [{"type": "dangerous_tool_use",
               "classifier_context": {"v": 1, "permission_mode": "auto"}}]


def _fmap():
    from app.config import Settings

    return forward_beta_map(Settings().bedrock_forward_betas)


def _src(**extra):
    body = {"model": "claude-sonnet-5-5", "max_tokens": 1024, "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
            "context_management": {"edits": [
                {"type": "clear_thinking_20251015", "keep": "all"}]}}
    body.update(extra)
    return body


def _out():
    return {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1024,
            "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture(autouse=True)
def _fresh_log_once(monkeypatch):
    monkeypatch.setattr(upstream_compat, "_logged_dropped_betas", set())


# ── settings / parsers ───────────────────────────────────────────────────────
def test_settings_default_forwards_the_measured_betas():
    # inline-tools · thinking-display-updates 는 2026-10-09 추가(test_high_inline_tools_forwarding).
    # English: the last two were added on 2026-10-09 (see test_high_inline_tools_forwarding).
    assert _fmap() == {DTU: "safeguards", PTC: None,
                       "inline-tools-2026-09-15": None,
                       "thinking-display-updates-2026-08-18": None}


def test_blank_setting_is_off(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("BEDROCK_FORWARD_BETAS", "")
    assert Settings().bedrock_forward_betas == ""
    assert forward_beta_map("") == {} and forward_beta_map(None) == {}


def test_forward_beta_map_parses_pairs_spaces_and_empties():
    raw = " a : f , b ,, :x , c: "
    assert forward_beta_map(raw) == {"a": "f", "b": None, "c": None}


def test_client_betas_splits_lines_and_commas_keeps_order_and_dedupes():
    lines = ["b, a", "a,c", " ", "b ,d"]
    assert client_betas(lines) == ["b", "a", "c", "d"]
    assert client_betas(None) == [] and client_betas([]) == []


# ── apply_forwarded_betas ────────────────────────────────────────────────────
def test_claude_code_request_forwards_exactly_two_betas_and_safeguards():
    out, src = _out(), _src(safeguards=SAFEGUARDS)
    betas = client_betas([CLAUDE_CODE_HEADER])
    kept = apply_forwarded_betas(out, src, betas, _fmap())
    assert kept == [PTC, DTU]                       # client order
    assert out["anthropic_beta"] == [PTC, DTU]
    assert out["safeguards"] == SAFEGUARDS
    assert "context_management" not in out         # unpaired fields stay out


def test_helper_request_without_safeguards_does_not_get_the_classifier_beta():
    out = _out()
    betas = client_betas([CLAUDE_CODE_HEADER])
    kept = apply_forwarded_betas(out, _src(), betas, _fmap())
    assert kept == [PTC]
    assert out["anthropic_beta"] == [PTC]
    assert "safeguards" not in out


def test_safeguards_never_travel_without_their_beta():
    out = _out()
    betas = client_betas([CLAUDE_CODE_HEADER.replace(DTU + ",", "")])
    apply_forwarded_betas(out, _src(safeguards=SAFEGUARDS), betas, _fmap())
    assert DTU not in out.get("anthropic_beta", [])
    assert "safeguards" not in out


def test_blank_setting_leaves_the_body_exactly_as_before():
    out, before = _out(), _out()
    betas = client_betas([CLAUDE_CODE_HEADER])
    kept = apply_forwarded_betas(out, _src(safeguards=SAFEGUARDS), betas,
                                 forward_beta_map(""))
    assert kept == [] and out == before


def test_no_header_leaves_the_body_exactly_as_before():
    out, before = _out(), _out()
    src = _src(safeguards=SAFEGUARDS)
    assert apply_forwarded_betas(out, src, [], _fmap()) == []
    assert out == before


def test_source_is_not_mutated_existing_betas_kept_and_idempotent():
    src = _src(safeguards=SAFEGUARDS)
    snapshot = copy.deepcopy(src)
    out = _out()
    out["anthropic_beta"] = ["gateway-added-beta"]
    betas = client_betas([CLAUDE_CODE_HEADER])
    apply_forwarded_betas(out, src, betas, _fmap())
    apply_forwarded_betas(out, src, betas, _fmap())
    assert src == snapshot
    assert out["anthropic_beta"] == ["gateway-added-beta", PTC, DTU]


def test_non_dict_source_forwards_only_unpaired_betas():
    out = _out()
    assert apply_forwarded_betas(out, None, [DTU, PTC], _fmap()) == [PTC]
    assert "safeguards" not in out


# ── log once ─────────────────────────────────────────────────────────────────
def test_dropped_betas_are_logged_once_per_name():
    betas = client_betas([CLAUDE_CODE_HEADER])
    with capture_logs() as logs:
        apply_forwarded_betas(_out(), _src(), betas, _fmap())
        apply_forwarded_betas(_out(), _src(), betas, _fmap())
    dropped = [e for e in logs if e["event"] == "upstream_compat.beta_dropped"]
    assert len(dropped) == 1
    assert set(dropped[0]["betas"]) == set(betas) - {DTU, PTC}
    assert len(dropped[0]["betas"]) == 9


def test_log_once_memory_is_capped():
    cap = upstream_compat._LOG_ONCE_CAP
    many = [f"junk-beta-{i}" for i in range(cap + 50)]
    with capture_logs():
        apply_forwarded_betas(_out(), _src(), many, _fmap())
        apply_forwarded_betas(_out(), _src(), ["one-more"], _fmap())
    assert len(upstream_compat._logged_dropped_betas) == cap


# ── 요청 경로 연결: /v1/messages → Bedrock 으로 나가는 본문 ────────────────────
# English: wiring — /v1/messages → the body that goes to Bedrock.
_OK = json.dumps({"id": "m", "type": "message", "role": "assistant",
                  "content": [], "stop_reason": "end_turn",
                  "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


def _app(adapter, monkeypatch):
    app = FastAPI()
    registry = ProviderRegistry()
    registry.register(ProviderType.BEDROCK, adapter)
    app.state.provider_registry = registry
    recorder = MagicMock()
    recorder.finalize = AsyncMock()
    app.state.cost_recorder = recorder
    svc = messages_router._router_service
    monkeypatch.setattr(svc, "resolve_bedrock_model",
                        AsyncMock(return_value=_model_config()))
    monkeypatch.setattr(svc, "check_key_scope", MagicMock())

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.scope["state"] = {"auth_context": None, "_redis": None,
                                  "_session_factory": None,
                                  "_degradation_manager": None,
                                  "request_id": "test-req"}
        return await call_next(request)

    app.include_router(messages_router.router)
    return app


async def _sent_to_bedrock(monkeypatch, *, header=None, extra=None):
    """요청 하나를 보내고 Bedrock 으로 나간 본문을 돌려준다.

    English: send one request and return the body that went to Bedrock.
    """
    adapter = MagicMock()
    usage = TokenUsage(input_tokens=1, output_tokens=1)
    adapter.invoke = AsyncMock(return_value=(200, _OK, {}, usage))
    body = {"model": "claude-sonnet-4-6", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}], **(extra or {})}
    headers = {"anthropic-beta": header} if header else {}
    transport = ASGITransport(app=_app(adapter, monkeypatch))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post("/v1/messages", json=body, headers=headers)
    assert resp.status_code == 200
    return json.loads(adapter.invoke.call_args.args[0])


async def test_route_sends_two_betas_and_safeguards_to_bedrock(monkeypatch):
    sent = await _sent_to_bedrock(monkeypatch, header=CLAUDE_CODE_HEADER,
                                  extra={"safeguards": SAFEGUARDS})
    assert sent["anthropic_beta"] == [PTC, DTU]
    assert sent["safeguards"] == SAFEGUARDS


async def test_route_with_blank_setting_sends_the_old_body(monkeypatch):
    monkeypatch.setenv("BEDROCK_FORWARD_BETAS", "")
    get_settings.cache_clear()
    try:
        sent = await _sent_to_bedrock(monkeypatch, header=CLAUDE_CODE_HEADER,
                                      extra={"safeguards": SAFEGUARDS})
    finally:
        get_settings.cache_clear()
    assert "anthropic_beta" not in sent and "safeguards" not in sent


async def test_route_without_header_sends_the_old_body(monkeypatch):
    sent = await _sent_to_bedrock(monkeypatch, extra={"safeguards": SAFEGUARDS})
    assert "anthropic_beta" not in sent and "safeguards" not in sent


def test_allowlist_never_lets_betas_or_safeguards_through_alone():
    allowed = messages_router._BEDROCK_ALLOWED_FIELDS
    assert not {"anthropic_beta", "safeguards"} & allowed
