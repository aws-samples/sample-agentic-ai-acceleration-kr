# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""요청별 추론/도구 추적(observability) 추출 — 순수 함수, hot-path 무영향.

게이트웨이는 그동안 응답에서 토큰/비용만 usage_logs 에 남기고 본문(tool_use,
thinking, plan)은 버렸다. 이 모듈은 응답 본문을 **읽기만** 해서 운영 추적용
요약(trace)을 만든다 — 무엇을 추론했고(요약), 어떤 도구를 어떤 인자로 호출했는지.

설계 원칙:
  - **순수 함수(I/O 없음)** — 응답 dict → trace dict. cost_recorder 가 결과를
    기존 cost:stream 오프로드에 실어 worker 가 DB 에 쓴다(hot-path 는 추출+XADD 만).
  - **reasoning 은 평문으로 옴**(실측 §41): invoke_model 의 thinking 블록 텍스트는
    display=summarized 시 평문, signature 는 별도 무결성 서명(텍스트 아님 → 버림),
    redacted_thinking 은 암호화(읽지 않고 카운트만). raw chain-of-thought 는 API 가
    주지 않으므로 summary 만 보존.
  - **PII/크기 가드** — thinking 요약은 MAX_THINKING_CHARS 로 절단, tool_use input 의
    값은 PII 위험이라 **키만 보존하고 값은 마스킹**(opt-in 으로 값 보존 가능하나 기본 off).
  - **2개 응답 형식 지원** — Anthropic invoke_model(`content[]`)과 Bedrock Converse
    (`output.message.content[]`). 둘 다 실측으로 shape 확인됨(§41).
"""

from __future__ import annotations

import re
from typing import Any

# thinking 요약 절단 상한(문자). 추론 요약이 길어도 DB/로그 폭증 방지.
MAX_THINKING_CHARS = 4000
# tool_use 개수 상한(폭주 응답 방지).
MAX_TOOL_USES = 32
# text(plan) 미리보기 절단 상한.
MAX_TEXT_PREVIEW_CHARS = 2000

# ─────────────────────────────────────────────────────────────────────────────
# PII 패턴 레닥션 (자유 텍스트용 — thinking_summary / text_preview).
#
# 자유 텍스트는 tool input 처럼 구조적 마스킹이 불가하므로 **앵커가 명확한 패턴만**
# 잡는다. "그냥 긴 숫자"는 건드리지 않는다 — over-redaction(정상 텍스트 훼손)이
# under-redaction(PII 누출)보다 디버깅엔 더 치명적일 수 있어 best-effort 로 설계.
# 순서 주의: 이메일을 전화/숫자보다 먼저 치환(이메일 안 숫자가 전화로 오인 방지).
# ─────────────────────────────────────────────────────────────────────────────
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 이메일 — @ 앵커
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "<email>"),
    # 한국 주민등록번호 — 6자리-7자리(하이픈 필수, 앵커 명확)
    (re.compile(r"\b\d{6}-\d{7}\b"), "<rrn>"),
    # 신용카드 — 16자리(하이픈/공백 구분 형식만; 맨숫자 16자리는 제외)
    (re.compile(r"\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}\b"), "<card>"),
    # 한국 휴대폰 — 010-xxxx-xxxx / 011~019 (하이픈/공백 구분 형식만)
    (re.compile(r"\b01[016789][- ]\d{3,4}[- ]\d{4}\b"), "<phone>"),
]


def _redact_pii(text: str) -> str:
    """자유 텍스트에서 앵커가 명확한 PII 패턴만 토큰으로 치환. best-effort.

    구조적 마스킹이 불가한 thinking_summary / text_preview 용. 패턴에 안 걸리는
    PII 는 남을 수 있음(설계상 best-effort — over-redaction 회피 우선).
    """
    for pattern, token in _PII_PATTERNS:
        text = pattern.sub(token, text)
    return text


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """limit 초과 시 절단하고 (잘린문자열, truncated여부) 반환."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _mask_tool_input(value: Any, mask: bool) -> Any:
    """tool_use input 을 PII-safe 형태로. mask=True 면 스칼라 값을 타입 토큰으로.

    mask=False 면 값 보존(신뢰 환경). dict/list 는 재귀로 구조 보존.
    """
    if isinstance(value, dict):
        return {k: _mask_tool_input(v, mask) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_tool_input(v, mask) for v in value]
    if not mask:
        return value
    # 스칼라 값은 타입만 노출(값 자체는 PII 위험 → 마스킹)
    return f"<{type(value).__name__}>"


def _normalize_blocks(response_body: dict) -> list[dict]:
    """invoke_model / Converse 두 형식에서 content 블록 리스트를 통일 추출.

    - Anthropic invoke_model: top-level `content: [...]` (블록 type=thinking/text/
      tool_use/redacted_thinking).
    - Bedrock Converse: `output.message.content: [...]` (블록 키=reasoningContent/
      text/toolUse). 키 이름이 달라 normalize 한다.
    """
    # Anthropic native shape
    blocks = response_body.get("content")
    if isinstance(blocks, list):
        return blocks
    # Bedrock Converse shape
    msg = (response_body.get("output") or {}).get("message") or {}
    conv = msg.get("content")
    if isinstance(conv, list):
        normalized = []
        for b in conv:
            if not isinstance(b, dict):
                continue
            if "reasoningContent" in b:
                rc = b["reasoningContent"] or {}
                rt = rc.get("reasoningText") or {}
                normalized.append({"type": "thinking", "thinking": rt.get("text", "")})
            elif "toolUse" in b:
                tu = b["toolUse"] or {}
                normalized.append(
                    {"type": "tool_use", "name": tu.get("name"), "input": tu.get("input", {})}
                )
            elif "text" in b:
                normalized.append({"type": "text", "text": b.get("text", "")})
        return normalized
    return []


def extract_trace(response_body: dict | None, *, mask_pii: bool = True) -> dict | None:
    """응답 본문 → 추적 요약 dict. 추적할 게 없으면 None.

    반환 형태:
      {
        "thinking_summary": str|None,    # 추론 요약(절단·PII 레닥션), redacted 만 있으면 None
        "thinking_truncated": bool,
        "redacted_thinking_count": int,  # 암호화돼 못 읽은 추론 블록 수
        "tool_uses": [{"name": str, "input": <masked|raw>}],
        "tool_use_truncated": bool,      # MAX_TOOL_USES 초과로 잘렸는지
        "text_preview": str|None,        # 최종 plan/text 미리보기(절단·PII 레닥션)
        "block_types": [str],            # 순서대로 본 블록 타입(관측성)
        "pii_masked": bool,              # 이 trace 에 마스킹이 적용됐는지(감사용)
      }

    mask_pii(기본 True, fail-safe): **세 군데 모두** 마스킹:
      - tool input 값 → 타입 토큰(`{period:'this_month'}` → `{period:'<str>'}`)
      - thinking_summary / text_preview → PII 패턴 레닥션(이메일/전화/주민번호/카드)
    mask_pii=False: 전부 평문(신뢰 환경 디버깅). 운영 토글은 config.trace_mask_pii →
    호출부가 이 인자로 전달(코드 수정 없이 env 로 on/off).
    """
    if not isinstance(response_body, dict):
        return None
    blocks = _normalize_blocks(response_body)
    if not blocks:
        return None

    thinking_parts: list[str] = []
    redacted = 0
    tool_uses: list[dict] = []
    tool_truncated = False
    text_parts: list[str] = []
    block_types: list[str] = []

    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        block_types.append(t)
        if t == "thinking":
            txt = b.get("thinking")
            if isinstance(txt, str) and txt.strip():
                thinking_parts.append(txt.strip())
        elif t == "redacted_thinking":
            redacted += 1
        elif t == "tool_use":
            if len(tool_uses) >= MAX_TOOL_USES:
                tool_truncated = True
                continue
            tool_uses.append(
                {
                    "name": b.get("name"),
                    "input": _mask_tool_input(b.get("input", {}), mask_pii),
                }
            )
        elif t == "text":
            txt = b.get("text")
            if isinstance(txt, str) and txt.strip():
                text_parts.append(txt.strip())

    # 추적할 신호가 전혀 없으면 None(텍스트만 있는 평범한 응답은 trace 불필요)
    if not thinking_parts and not redacted and not tool_uses:
        return None

    # 자유 텍스트(thinking/text)는 mask_pii 면 PII 패턴 레닥션 후 절단.
    thinking_joined = "\n".join(thinking_parts)
    if thinking_joined and mask_pii:
        thinking_joined = _redact_pii(thinking_joined)
    thinking_summary, thinking_trunc = (
        _truncate(thinking_joined, MAX_THINKING_CHARS) if thinking_joined else (None, False)
    )
    text_joined = "\n".join(text_parts)
    if text_joined and mask_pii:
        text_joined = _redact_pii(text_joined)
    text_preview = (
        _truncate(text_joined, MAX_TEXT_PREVIEW_CHARS)[0] if text_joined else None
    )

    return {
        "thinking_summary": thinking_summary,
        "thinking_truncated": thinking_trunc,
        "redacted_thinking_count": redacted,
        "tool_uses": tool_uses,
        "tool_use_truncated": tool_truncated,
        "text_preview": text_preview,
        "block_types": block_types,
        "pii_masked": mask_pii,
    }
