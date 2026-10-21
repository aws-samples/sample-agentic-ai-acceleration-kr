# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""완결된 응답 뒤에 오류 프레임을 덧붙이지 않는다.

무엇이 문제였나
---------------
두 OpenAI-와이어 스트리밍 어댑터는 상류 iteration 이 예외를 던지면 오류 프레임을 하나
덧붙였다. 문제는 **언제** 던지느냐다: httpx 는 마지막 청크를 넘긴 **뒤** 연결 정리 단계에서
예외를 던지는 일이 잦다(peer 가 깔끔하지 않게 닫거나, 서버가 keep-alive 를 끊는 경우).

그러면 이미 ``response.completed`` / ``data: [DONE]`` 을 받은 클라이언트에게 오류 프레임이
뒤따라 붙는다. 그리고 이 오류는 **나쁜 쪽으로만** 틀린다:

  * Codex 는 완결된 응답을 실패로 처리하고 재시도한다. 우리는 그 턴의 토큰을 이미
    지불했고 재시도분도 지불한다 — 사용자 입장에서는 잘 끝난 요청이 두 번 청구된다.
  * 본문 감사 로그에도 성공한 요청이 오류로 남아, 오류율이 상류 건강과 무관하게 오른다.

이제 종료 프레임을 이미 보냈으면 오류 프레임을 붙이지 않고 경고만 남긴다.

⚠️ 이것은 "받지 못한 성공을 주장하지 않는다"(웹서치 스티처의 잘린 스트림 처리)의
   **거울상**이다. 두 규칙이 함께 있어야 스트림의 종료 신호가 신뢰할 수 있다:
   성공을 지어내지도, 이미 얻은 성공을 덮지도 않는다.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.bedrock_openai_adapter import BedrockOpenAIAdapter
from app.providers.openai_usage import (
    chat_chunk_is_terminal,
    is_terminal_responses_payload,
)

MODEL = "us.openai.gpt-5.6-terra"
ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com"
REQ_ID = "req-abc"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 종료 판정 (순수)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "[DONE]",
        '{"type":"response.completed"}',
        '{"type":"response.incomplete"}',
        '{"type":"response.failed"}',
    ],
)
def test_terminal_payloads_are_recognised(payload: str):
    """``[DONE]`` 과 typed terminal 을 **둘 다** 본다.

    종료 이벤트 직후 ``[DONE]`` 전에 끊기는 경우가 있고, 그때도 응답은 완결됐다.
    """
    assert is_terminal_responses_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"response.created"}',
        '{"type":"response.output_text.delta","delta":"hi"}',
        '{"type":"response.output_item.added"}',
        "",
        "not json at all",
        '["a","list"]',
    ],
)
def test_non_terminal_payloads_are_not_recognised(payload: str):
    """⚠️ 대조군. 판정이 넓으면 진짜 스트림 실패가 조용히 삼켜진다.

    특히 파싱 불가 payload 를 terminal 로 보면, 상류가 쓰레기를 뱉고 끊긴 경우에 클라이언트가
    아무 신호도 받지 못한다.
    """
    assert not is_terminal_responses_payload(payload)


def test_chat_wire_terminal_is_found_inside_a_multi_line_chunk():
    """chat 은 바이트 통과라 파싱하지 않는다 — 한 청크에 여러 SSE 줄이 올 수 있다."""
    assert chat_chunk_is_terminal(b'data: {"x":1}\n\ndata: [DONE]\n\n')
    assert chat_chunk_is_terminal(b"data: [DONE]\n\n")
    assert not chat_chunk_is_terminal(b'data: {"choices":[]}\n\n')
    assert not chat_chunk_is_terminal("data: [DONE]")  # str 은 대상이 아니다


# ─────────────────────────────────────────────────────────────────────────────
# 2. runtime(SigV4) 어댑터 — 제너레이터를 실제로 돌린다
# ─────────────────────────────────────────────────────────────────────────────


def _signer():
    s = MagicMock()
    s.sign = AsyncMock(return_value={"Authorization": "AWS4-HMAC-SHA256 ..."})
    return s


def _profile():
    p = MagicMock()
    p.account_role_arn = None
    p.external_id = None
    p.region = "us-east-2"
    return p


class _RaisingStream:
    """마지막 항목을 넘긴 **뒤** 예외를 던지는 httpx 스트림 흉내.

    ⚠️ 이 형태가 결함의 재현 조건이다. 중간에 던지는 것과 구별해야 한다 — 중간 실패에는
       오류 프레임이 **있어야** 한다.
    """

    def __init__(self, *, lines=None, chunks=None, exc=None):
        self._resp = MagicMock()
        self._resp.status_code = 200
        self._resp.headers = {"x-amzn-requestid": REQ_ID}
        self._resp.aread = AsyncMock(return_value=b"{}")
        exc = exc or RuntimeError("connection reset during teardown")

        async def _aiter_lines():
            for line in lines or []:
                yield line
            raise exc

        async def _aiter_bytes():
            for chunk in chunks or []:
                yield chunk
            raise exc

        self._resp.aiter_lines = _aiter_lines
        self._resp.aiter_bytes = _aiter_bytes
        self.exited = False

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        self.exited = True
        return False


def _http(stream):
    http = MagicMock()
    http.stream = MagicMock(return_value=stream)
    return http


_COMPLETE_LINES = [
    'data: {"type":"response.created"}',
    'data: {"type":"response.output_text.delta","delta":"OK"}',
    'data: {"type":"response.completed","response":{"usage":{"input_tokens":8}}}',
    "data: [DONE]",
]

_TRUNCATED_LINES = [
    'data: {"type":"response.created"}',
    'data: {"type":"response.output_text.delta","delta":"OK"}',
]


def _errors(out: list[bytes]) -> list[dict]:
    got = []
    for chunk in out:
        text = chunk.decode()
        if text.startswith("data: "):
            text = text[len("data: ") :].strip()
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
            got.append(obj["error"])
    return got


async def test_a_teardown_error_after_completed_is_not_forwarded_to_the_client():
    """⚠️ 이 파일의 핵심. 완결된 응답이 오류로 보이면 Codex 가 재시도하고 두 번 청구된다."""
    stream = _RaisingStream(lines=_COMPLETE_LINES)
    status, gen, _h, _rid = await BedrockOpenAIAdapter(
        http_client=_http(stream), signer=_signer()
    ).invoke_stream(b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses")
    assert status == 200
    out = [c async for c in gen]

    assert _errors(out) == [], (
        f"완결 뒤의 정리 예외가 오류 프레임으로 나갔다: {_errors(out)}"
    )
    types = [json.loads(c)["type"] for c in out]
    assert "response.completed" in types, f"완결 이벤트가 사라졌다: {types}"
    assert stream.exited, "스트림 컨텍스트를 닫지 않았다"


async def test_a_mid_stream_error_before_any_terminal_is_still_forwarded():
    """⚠️ 대조군. 이것이 없으면 오류 프레임을 통째로 없애도 위 테스트가 통과한다.

    진짜 스트림 실패에는 신호가 **있어야** 한다 — 없으면 클라이언트가 매달린다.
    """
    stream = _RaisingStream(lines=_TRUNCATED_LINES)
    _s, gen, _h, _rid = await BedrockOpenAIAdapter(
        http_client=_http(stream), signer=_signer()
    ).invoke_stream(b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="responses")
    out = [c async for c in gen]

    errs = _errors(out)
    assert len(errs) == 1, f"잘린 스트림에 오류 프레임이 없다: {[c[:60] for c in out]}"
    assert errs[0]["type"] == "provider_error"


async def test_the_chat_wire_applies_the_same_rule():
    """chat 은 바이트 통과이므로 판정 방식이 다르다 — 규칙은 같아야 한다."""
    done = [b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n', b"data: [DONE]\n\n"]
    stream = _RaisingStream(chunks=done)
    _s, gen, _h, _rid = await BedrockOpenAIAdapter(
        http_client=_http(stream), signer=_signer()
    ).invoke_stream(b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="chat")
    out = [c async for c in gen]
    assert _errors(out) == [], f"완결 뒤 오류 프레임이 나갔다: {_errors(out)}"
    assert b"[DONE]" in b"".join(out), "종료 센티널이 통과되지 않았다"


async def test_the_chat_wire_still_reports_a_truncated_stream():
    """⚠️ 대조군 (chat)."""
    stream = _RaisingStream(chunks=[b'data: {"choices":[]}\n\n'])
    _s, gen, _h, _rid = await BedrockOpenAIAdapter(
        http_client=_http(stream), signer=_signer()
    ).invoke_stream(b"{}", MODEL, profile=_profile(), endpoint=ENDPOINT, wire="chat")
    out = [c async for c in gen]
    errs = _errors(out)
    assert len(errs) == 1, f"잘린 chat 스트림에 오류 프레임이 없다: {out}"
    # chat 오류 프레임은 **프레이밍된** 상태여야 한다 — 통과 경로라 raw 가 그대로 나간다.
    assert out[-1].startswith(b"data: "), f"chat 오류가 프레이밍되지 않았다: {out[-1]!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Mantle 어댑터 — 같은 규칙을 쓰는지
# ─────────────────────────────────────────────────────────────────────────────


def _broker():
    b = MagicMock()
    b.bearer_token = AsyncMock(return_value="tok")
    return b


async def test_the_mantle_adapter_applies_the_same_rule():
    """⚠️ 한쪽만 고치면 그 plane 의 클라이언트만 계속 재시도한다.

    두 plane 은 같은 Codex 클라이언트를 서비스하고, 어느 plane 으로 갈지는 모델 행이
    정한다 — 즉 증상이 모델에 따라 달라지고 재현 조건이 좁아진다.
    """
    from app.providers.mantle_openai_adapter import MantleOpenAIAdapter

    stream = _RaisingStream(lines=_COMPLETE_LINES)
    status, gen, _h, _rid = await MantleOpenAIAdapter(
        http_client=_http(stream), broker=_broker()
    ).invoke_stream(b"{}", "openai.gpt-5.6-terra", profile=_profile(), endpoint=ENDPOINT)
    assert status == 200
    out = [c async for c in gen]
    assert _errors(out) == [], f"완결 뒤 오류 프레임이 나갔다: {_errors(out)}"


async def test_the_mantle_adapter_still_reports_a_truncated_stream():
    """⚠️ 대조군 (Mantle)."""
    from app.providers.mantle_openai_adapter import MantleOpenAIAdapter

    stream = _RaisingStream(lines=_TRUNCATED_LINES)
    _s, gen, _h, _rid = await MantleOpenAIAdapter(
        http_client=_http(stream), broker=_broker()
    ).invoke_stream(b"{}", "openai.gpt-5.6-terra", profile=_profile(), endpoint=ENDPOINT)
    out = [c async for c in gen]
    assert len(_errors(out)) == 1, f"잘린 스트림에 오류 프레임이 없다: {out}"


async def test_both_adapters_share_one_terminal_test():
    """두 어댑터가 각자 판정하면 드리프트한다 — usage 파서가 정확히 그렇게 드리프트했다."""
    import ast
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src" / "app" / "providers"
    for rel in ("bedrock_openai_adapter.py", "mantle_openai_adapter.py"):
        tree = ast.parse((src_root / rel).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "app.providers.openai_usage"
            for alias in node.names
        }
        assert "is_terminal_responses_payload" in imported, (
            f"{rel} 가 공유 종료 판정을 쓰지 않는다 — 자체 판정은 드리프트한다"
        )
