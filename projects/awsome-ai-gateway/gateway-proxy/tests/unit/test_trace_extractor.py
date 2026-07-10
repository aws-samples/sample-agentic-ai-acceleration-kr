# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""trace_extractor 단위 테스트.

fixture 는 **실제 Bedrock Opus 4.8 응답 shape**(§41 실측)을 그대로 쓴다:
  - invoke_model: content=[thinking(+signature), text, tool_use]
  - thinking 텍스트는 평문, signature 는 별도 키(텍스트 아님 → 무시돼야 함)
  - tool_use 는 평문 name/input
핵심 불변식: (1) reasoning 요약 추출 (2) signature 는 trace 에 안 샘 (3) tool input
값 마스킹(PII-safe) 기본 (4) redacted_thinking 은 카운트만 (5) Converse shape 정규화.
"""

from __future__ import annotations

import pytest

from app.services import trace_extractor as te

# §41 실측 invoke_model 응답 (thinking+signature, text, tool_use)
LIVE_INVOKE_RESPONSE = {
    "model": "claude-opus-4-8",
    "role": "assistant",
    "stop_reason": "tool_use",
    "content": [
        {
            "type": "thinking",
            "thinking": "I need to pull the active user count for this month using the tool.",
            "signature": "x" * 368,  # 무결성 서명 — 텍스트 아님, trace 에 새면 안 됨
        },
        {"type": "text", "text": "이번 달 활성 사용자 수를 도구로 정확히 확인하겠습니다."},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "get_user_count",
            "input": {"period": "this_month"},
        },
    ],
    "usage": {"input_tokens": 100, "output_tokens": 50},
}


def test_extracts_thinking_summary_plaintext():
    tr = te.extract_trace(LIVE_INVOKE_RESPONSE)
    assert tr is not None
    assert "active user count" in tr["thinking_summary"]
    assert tr["thinking_truncated"] is False


def test_signature_never_leaks_into_trace():
    """signature(368자)는 trace 어디에도 들어가면 안 된다 — 텍스트가 아니라 검증용."""
    tr = te.extract_trace(LIVE_INVOKE_RESPONSE)
    blob = repr(tr)
    assert "x" * 368 not in blob
    assert "signature" not in blob


def test_tool_use_captured_with_masked_values_by_default():
    tr = te.extract_trace(LIVE_INVOKE_RESPONSE)
    assert len(tr["tool_uses"]) == 1
    tu = tr["tool_uses"][0]
    assert tu["name"] == "get_user_count"
    # 기본은 값 마스킹(PII-safe) — 키는 보존, 값은 타입 토큰
    assert tu["input"] == {"period": "<str>"}


def test_tool_use_unmasked_when_mask_off():
    tr = te.extract_trace(LIVE_INVOKE_RESPONSE, mask_pii=False)
    assert tr["tool_uses"][0]["input"] == {"period": "this_month"}
    assert tr["pii_masked"] is False


def test_pii_masked_flag_default_true():
    tr = te.extract_trace(LIVE_INVOKE_RESPONSE)
    assert tr["pii_masked"] is True


def test_text_preview_captured():
    tr = te.extract_trace(LIVE_INVOKE_RESPONSE)
    assert "확인하겠습니다" in tr["text_preview"]


def test_block_types_order_preserved():
    tr = te.extract_trace(LIVE_INVOKE_RESPONSE)
    assert tr["block_types"] == ["thinking", "text", "tool_use"]


def test_plain_text_response_returns_none():
    """thinking/tool 없이 text 만 있는 평범한 응답은 trace 불필요 → None."""
    resp = {"content": [{"type": "text", "text": "안녕하세요"}], "usage": {}}
    assert te.extract_trace(resp) is None


def test_redacted_thinking_counted_not_read():
    resp = {
        "content": [
            {"type": "redacted_thinking", "data": "encrypted-blob-cannot-read"},
            {"type": "text", "text": "답변"},
        ]
    }
    tr = te.extract_trace(resp)
    assert tr is not None
    assert tr["redacted_thinking_count"] == 1
    assert tr["thinking_summary"] is None
    # 암호화 데이터가 trace 에 새면 안 됨
    assert "encrypted-blob" not in repr(tr)


def test_thinking_truncation():
    long_think = "A" * (te.MAX_THINKING_CHARS + 500)
    resp = {"content": [{"type": "thinking", "thinking": long_think}]}
    tr = te.extract_trace(resp)
    assert tr["thinking_truncated"] is True
    assert len(tr["thinking_summary"]) == te.MAX_THINKING_CHARS


def test_tool_use_count_cap():
    tools = [
        {"type": "tool_use", "name": f"t{i}", "input": {}}
        for i in range(te.MAX_TOOL_USES + 5)
    ]
    resp = {"content": tools}
    tr = te.extract_trace(resp)
    assert len(tr["tool_uses"]) == te.MAX_TOOL_USES
    assert tr["tool_use_truncated"] is True


def test_nested_tool_input_masking():
    resp = {
        "content": [
            {
                "type": "tool_use",
                "name": "q",
                "input": {"filters": {"email": "a@b.com", "ids": [1, 2]}, "limit": 10},
            }
        ]
    }
    tr = te.extract_trace(resp)
    masked = tr["tool_uses"][0]["input"]
    # 구조는 보존, 스칼라 값만 마스킹
    assert masked == {"filters": {"email": "<str>", "ids": ["<int>", "<int>"]}, "limit": "<int>"}
    assert "a@b.com" not in repr(tr)


def test_converse_shape_normalized():
    """Bedrock Converse: output.message.content with reasoningContent/toolUse keys."""
    resp = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "reasoningContent": {
                            "reasoningText": {"text": "let me think", "signature": "sig"}
                        }
                    },
                    {"text": "여기 결과입니다"},
                    {"toolUse": {"name": "lookup", "input": {"k": "v"}}},
                ],
            }
        },
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    tr = te.extract_trace(resp)
    assert tr is not None
    assert tr["thinking_summary"] == "let me think"
    assert tr["tool_uses"][0]["name"] == "lookup"
    assert tr["tool_uses"][0]["input"] == {"k": "<str>"}
    assert "여기 결과입니다" in tr["text_preview"]
    # Converse signature 도 새면 안 됨
    assert "sig" not in repr(tr).replace("thinking_summary", "")


@pytest.mark.parametrize("bad", [None, [], "string", 123, {}])
def test_malformed_input_returns_none(bad):
    assert te.extract_trace(bad) is None


def test_thinking_only_no_tools():
    """추론만 있고 tool 없어도 trace 생성(추론 자체가 추적 가치)."""
    resp = {"content": [{"type": "thinking", "thinking": "reasoning here"}]}
    tr = te.extract_trace(resp)
    assert tr is not None
    assert tr["thinking_summary"] == "reasoning here"
    assert tr["tool_uses"] == []


# ─────────────────────────────────────────────────────────────────────────────
# PII 패턴 레닥션 (자유 텍스트 — thinking_summary / text_preview)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,token,leaked",
    [
        ("연락처는 hong@example.com 입니다", "<email>", "hong@example.com"),
        ("주민번호 900101-1234567 확인", "<rrn>", "900101-1234567"),
        ("전화 010-1234-5678 로 연락", "<phone>", "010-1234-5678"),
        ("카드 1234-5678-9012-3456 결제", "<card>", "1234-5678-9012-3456"),
        ("전화 010 1234 5678 공백구분", "<phone>", "010 1234 5678"),
    ],
)
def test_thinking_pii_redacted(raw, token, leaked):
    """thinking_summary 의 PII 패턴이 토큰으로 치환되고 원본이 안 샌다."""
    resp = {"content": [{"type": "thinking", "thinking": raw}]}
    tr = te.extract_trace(resp)  # mask_pii=True 기본
    assert token in tr["thinking_summary"]
    assert leaked not in tr["thinking_summary"]


def test_text_preview_pii_redacted():
    resp = {
        "content": [
            {"type": "tool_use", "name": "x", "input": {}},
            {"type": "text", "text": "사용자 kim@corp.io 의 비용은 높습니다"},
        ]
    }
    tr = te.extract_trace(resp)
    assert "<email>" in tr["text_preview"]
    assert "kim@corp.io" not in tr["text_preview"]


def test_pii_not_redacted_when_mask_off():
    resp = {"content": [{"type": "thinking", "thinking": "연락 hong@example.com"}]}
    tr = te.extract_trace(resp, mask_pii=False)
    assert "hong@example.com" in tr["thinking_summary"]


@pytest.mark.parametrize(
    "text",
    [
        "이번 달 비용은 1234 달러입니다",  # 일반 4자리 숫자 — 전화/카드 아님
        "사용자 수 100명, 모델 010 종류",  # '010' 단독 — 휴대폰 아님(하이픈 없음)
        "30일 평균 12345678 토큰",  # 8자리 맨숫자 — 어떤 PII 패턴도 아님
        "정확도 95.5% 달성",  # 소수/퍼센트
        "주문번호 1234567890123456 입니다",  # 16자리 맨숫자(구분자 없음) — 카드 패턴 아님
    ],
)
def test_normal_numbers_survive_redaction(text):
    """over-redaction 방지: 구분자 없는 일반 숫자는 PII 로 오인하지 않는다."""
    resp = {"content": [{"type": "thinking", "thinking": text}]}
    tr = te.extract_trace(resp)
    # 마스킹 토큰이 안 들어가고 원문 숫자가 보존돼야 함
    assert "<phone>" not in tr["thinking_summary"]
    assert "<card>" not in tr["thinking_summary"]
    assert "<rrn>" not in tr["thinking_summary"]
    assert tr["thinking_summary"] == text


def test_tool_value_and_freetext_both_masked():
    """단일 mask_pii 플래그가 tool 값과 자유텍스트 PII 를 동시에 커버."""
    resp = {
        "content": [
            {"type": "thinking", "thinking": "조회 대상 user@x.com"},
            {"type": "tool_use", "name": "lookup", "input": {"email": "user@x.com"}},
        ]
    }
    tr = te.extract_trace(resp)  # 기본 마스킹
    # 자유 텍스트: 패턴 레닥션
    assert "<email>" in tr["thinking_summary"]
    # tool 값: 타입 토큰
    assert tr["tool_uses"][0]["input"] == {"email": "<str>"}
    # 어디에도 원본 PII 안 샘
    assert "user@x.com" not in repr(tr)
