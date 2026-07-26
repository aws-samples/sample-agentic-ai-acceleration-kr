"""goldset 로딩·질문 매칭 (순수 로직).

goldset 은 패키지에 동봉된 JSONL(`goldset/goldset-v1.jsonl`) 이며 각 라인은
``{"id","question","sql","datasource"}`` 형태다. 파일명이 곧 버전(`goldset-v1`).

매칭 전략 (§9.1 EX):
1. 정규화(공백·대소문자·구두점 제거) 후 **완전일치**
2. 실패 시 **부분 매칭** (정규화 문자열의 포함 관계, 가장 긴 후보 우선)

정규화는 한글을 보존해야 하므로 ASCII 구두점만 제거한다.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("evaluation.goldset")

# 패키지 동봉 goldset 기본 경로 (Lambda asset 에 그대로 포함된다).
DEFAULT_GOLDSET_PATH = Path(__file__).with_name("goldset") / "goldset-v1.jsonl"

# 정규화에서 제거할 문자 카테고리: 공백 + 구두점/기호.
_DROP_CATEGORIES = ("P", "Z", "C")


@dataclass(frozen=True)
class GoldEntry:
    """goldset 한 문항."""

    id: str
    question: str
    sql: str
    datasource: str = "aurora"

    @property
    def normalized_question(self) -> str:
        return normalize_question(self.question)


def normalize_question(question: str) -> str:
    """질문 정규화: 소문자화 + 공백·구두점·제어문자 제거 (한글은 보존)."""
    if not question:
        return ""
    # 유니코드 정규화로 전각/조합 문자 차이를 흡수.
    text = unicodedata.normalize("NFKC", question).casefold()
    return "".join(
        ch for ch in text if unicodedata.category(ch)[0] not in _DROP_CATEGORIES
    )


def load_goldset(path: str | Path | None = None) -> list[GoldEntry]:
    """JSONL goldset 을 로드. 깨진 라인은 경고 후 건너뛴다(전체 실패 방지)."""
    target = Path(path) if path is not None else DEFAULT_GOLDSET_PATH
    entries: list[GoldEntry] = []
    with open(target, encoding="utf-8") as fp:
        for lineno, line in enumerate(fp, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                raw = json.loads(stripped)
                entries.append(
                    GoldEntry(
                        id=str(raw["id"]),
                        question=str(raw["question"]),
                        sql=str(raw["sql"]),
                        datasource=str(raw.get("datasource", "aurora")),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("goldset 라인 파싱 실패 (%s:%d) — 건너뜁니다", target, lineno)
    return entries


class GoldsetMatcher:
    """정규화 질문 → GoldEntry 매칭기."""

    def __init__(self, entries: list[GoldEntry] | None = None) -> None:
        self._entries = entries if entries is not None else load_goldset()
        self._exact = {e.normalized_question: e for e in self._entries if e.normalized_question}

    @property
    def entries(self) -> list[GoldEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def match(self, question: str) -> GoldEntry | None:
        """완전일치 우선, 실패 시 부분 매칭. 없으면 None."""
        key = normalize_question(question)
        if not key:
            return None
        exact = self._exact.get(key)
        if exact is not None:
            return exact
        # 부분 매칭: 서로 포함 관계인 후보 중 정규화 길이가 가장 긴 것(가장 구체적인 것).
        candidates = [
            entry
            for entry in self._entries
            if entry.normalized_question
            and (entry.normalized_question in key or key in entry.normalized_question)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: len(e.normalized_question))
