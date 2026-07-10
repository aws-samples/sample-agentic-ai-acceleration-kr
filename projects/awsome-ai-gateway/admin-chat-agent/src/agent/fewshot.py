# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""DAIL-SQL few-shot selection + injection for the SQL Specialist.

`fewshot_bank.json` 의 (question, sql) 쌍에서 사용자 질문과 가장 비슷한 top-k 를
골라 SQL Specialist 프롬프트에 주입한다. DAIL-SQL 논문의 핵심(유사 예시 in-context)
을 의존성 없이(stdlib only) 구현 — 런타임 컨테이너엔 pyyaml/embedding 모델이
없으므로 키워드 Jaccard 유사도를 쓴다.

오염 방지 (train/test contamination):
  - 뱅크 질문은 golden 평가 질문과 의도적으로 다르다(fewshot_bank.json 참조).
  - 추가로 LOO 가드: 주입 직전 사용자 질문과 정규화 일치하는 예시는 제외한다.
    (혹시 뱅크에 평가 질문과 같은 게 들어와도 정답을 보여주지 않도록 이중 방어.)

main.py 가 import 해 ask_sql_specialist 에서 호출. 로드 실패/빈 뱅크면 빈
문자열을 반환 → 기존 동작(주입 없음)으로 graceful degrade.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_BANK_PATH = Path(__file__).parent / "fewshot_bank.json"

# 한국어 조사/불용어 — 토큰 유사도에서 노이즈 제거(가벼운 normalization).
_STOPWORDS = frozenset(
    {
        "이번", "지난", "최근", "가장", "누구", "보여줘", "찾아줘", "해줘",
        "분석", "사용", "사용자", "목록", "패턴", "대한", "위한", "그리고",
    }
)


def _normalize(text: str) -> str:
    """비교용 정규화 — 공백 단일화 + 소문자 + 양끝 공백 제거."""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _tokens(text: str) -> set[str]:
    """질문을 비교 토큰 집합으로. 한글/영문/숫자 단위로 쪼개고 불용어 제거."""
    raw = re.findall(r"[0-9a-zA-Z가-힣]+", str(text).lower())
    return {t for t in raw if t and t not in _STOPWORDS}


def _similarity(a: set[str], b: set[str]) -> float:
    """Jaccard 유사도 — |교집합| / |합집합|. 둘 다 비면 0."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@lru_cache(maxsize=1)
def _load_bank() -> list[dict]:
    """fewshot_bank.json 로드(캐시). 실패 시 빈 리스트 → 주입 비활성."""
    try:
        data = json.loads(_BANK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    examples = data.get("examples", []) if isinstance(data, dict) else []
    # 토큰을 미리 계산해 둠(매 호출 재계산 방지).
    out = []
    for ex in examples:
        if isinstance(ex, dict) and ex.get("question") and ex.get("sql"):
            ex = dict(ex)
            ex["_tokens"] = _tokens(ex["question"])
            ex["_norm_q"] = _normalize(ex["question"])
            out.append(ex)
    return out


def select_examples(question: str, k: int = 3) -> list[dict]:
    """사용자 질문과 가장 비슷한 top-k 예시. LOO 오염 가드 적용.

    - 정규화 일치(같은 질문)는 제외 — 평가 질문의 정답 SQL 노출 방지.
    - 유사도 0(전혀 안 겹침)인 예시는 주입해도 도움 안 되므로 제외.
    """
    bank = _load_bank()
    if not bank:
        return []
    q_norm = _normalize(question)
    q_tokens = _tokens(question)

    scored = []
    for ex in bank:
        if ex["_norm_q"] == q_norm:  # LOO: 동일 질문 제외(오염 방지)
            continue
        sim = _similarity(q_tokens, ex["_tokens"])
        if sim > 0.0:
            scored.append((sim, ex))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [ex for _, ex in scored[:k]]


def build_fewshot_block(question: str, k: int = 3) -> str:
    """선택된 예시를 SQL Specialist 프롬프트에 붙일 텍스트 블록으로.

    빈 선택이면 빈 문자열(주입 없음 → 기존 동작). 형식은 sql_specialist.md 의
    'Few-shot Examples' 섹션과 일관되게 question→SQL 쌍.
    """
    examples = select_examples(question, k=k)
    if not examples:
        return ""
    lines = [
        "## Few-shot Examples (유사 질문→검증된 SQL — 스키마/패턴 참고용)",
        "아래는 *다른* 질문의 정답 SQL 이다. 그대로 베끼지 말고 스키마 사용법"
        "(테이블·컬럼명, KST 변환, 집계 idiom)만 참고해 현재 질문에 맞게 작성하라.",
        "",
    ]
    for ex in examples:
        lines.append(f"### Q: {ex['question']}")
        lines.append("```sql")
        lines.append(ex["sql"].strip())
        lines.append("```")
        if ex.get("code_hint"):
            lines.append(f"<!-- Tier B code hint: {ex['code_hint']} -->")
        lines.append("")
    return "\n".join(lines)
