# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""candidate_select — 실행기반 후보 선택 (DEVLOG §58, L3).

text2SQL SOTA(CHASE-SQL/MBR-Exec/XiYan/C3)의 공통 코어를 우리 스택에 이식한다:
N개 후보 SQL 을 query_db 로 **실제 실행**한 뒤 결과셋이 같은 후보끼리 클러스터링
하고 최대 다수파(majority)를 채택한다. "정답은 다양한 경로로도 같은 결과에 수렴,
오답은 흩어진다"(self-consistency) 를 SQL 텍스트가 아니라 **실행 결과셋**으로 본다.

근거(리서치 §58): CHASE-SQL BIRD dev 단일 63.01% → 실행 self-consistency 68.84%
(+5.84%p), MBR-Exec Spider 50.8→63.6(+12.8%p). 디코딩 제어 불필요 — 우리 핵심
자산(query_db 실제 실행)으로 그대로 재현.

⚠️ '게이트 비강제' 함정 회피: 이 선택은 **코드로 결정적으로 집계**해야 한다(감사
결함 ④/⑨와 동형 — 프롬프트 산문 'vote' 는 미구현이었음). 이 모듈은 순수 함수라
LLM 비결정성 0(동률 tie-break 만 선택적으로 judge 위임).

한계: 모든 후보가 같은 실수를 공유하는 공통모드 실패(타임존·team_id 경유)는
다수결로 못 잡는다 → L0(결정적 가드)가 그걸 잡는 하한 안전망. 계층 방어의 일부.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_resultset(rows: list[dict], *, order_sensitive: bool | None = None) -> str:
    """결과셋 → canonical 직렬화 문자열(해시 입력).

    표면적으로 다른 SQL(별칭·조인순서·컬럼순서)이 같은 결과를 내면 같은 문자열이
    되도록 정규화한다:
      - 각 행의 키를 정렬(컬럼 순서 무관)
      - 숫자는 반올림(부동소수 노이즈 제거, 6자리)
      - order_sensitive=False(기본 추론): 행 집합을 정렬(행 순서 무관)
        단 ORDER BY 가 의미있는 ranking 질의는 order_sensitive=True 로 순서 보존

    order_sensitive=None 이면 휴리스틱: 행이 1개거나 단일 스칼라면 순서 무의미,
    그 외에는 순서 보존 안 함(집합 비교) — ranking 여부를 모르면 보수적으로
    집합 비교(같은 값들이면 같다고 본다)가 false-split 을 줄인다.
    """
    norm_rows = [_normalize_row(r) for r in rows]
    if order_sensitive is None:
        order_sensitive = False
    if not order_sensitive:
        norm_rows = sorted(norm_rows, key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))
    return json.dumps(norm_rows, sort_keys=True, ensure_ascii=False)


def _normalize_row(row: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[str(k)] = _normalize_value(v)
    return out


def _normalize_value(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        # 부동소수 노이즈 제거 — 6자리 반올림(통화/비율 정밀도 보존)
        return round(float(v), 6)
    if v is None:
        return None
    return str(v)


def resultset_hash(rows: list[dict], *, order_sensitive: bool | None = None) -> str:
    canonical = normalize_resultset(rows, order_sensitive=order_sensitive)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class Candidate:
    """후보 1개 — SQL + 실행 결과 envelope."""

    __slots__ = ("sql", "envelope", "ok", "rows", "row_count", "error", "accuracy_warnings")

    def __init__(self, sql: str, envelope: dict):
        self.sql = sql
        self.envelope = envelope or {}
        self.rows = self.envelope.get("rows") or []
        self.row_count = self.envelope.get("row_count", len(self.rows))
        self.error = self.envelope.get("error")
        self.accuracy_warnings = self.envelope.get("accuracy_warnings") or []
        # ⚠️ `ok` 필드에 의존하지 않는다(§58 _auto_validate 와 동형 버그 수정):
        # 후보는 _agent_call(schema_key="ask_sql_specialist")로 만들어져 **구조화
        # SqlEnvelope** 를 반환하는데, 그 스키마엔 `ok` 가 없다. envelope.get("ok")는
        # 항상 None → 모든 후보 ok=False → "후보 0/k·합의 0%"로 L3 가 사실상 무력화됐었다.
        # 판정 기준: SQL 텍스트가 있고 명시적 에러/파싱실패가 아니면 실행 유효 후보.
        # (자유텍스트 폴백 경로는 ok=True/False 를 줄 수 있으니 명시 False 면 존중.)
        ok_field = self.envelope.get("ok")
        has_sql = bool((sql or "").strip())
        explicit_fail = (ok_field is False) or bool(self.error) or bool(self.envelope.get("parse_error"))
        self.ok = has_sql and not explicit_fail


def select_by_execution(
    candidates: list[Candidate],
    *,
    order_sensitive: bool | None = None,
) -> dict:
    """후보들을 실행 결과셋으로 클러스터링하고 최대 다수파를 선택.

    반환:
      {
        "winner_index": int | None,   # candidates 내 대표 후보 인덱스
        "agreement": float,           # 다수파 크기 / 유효(ok&동률검토) 후보 수
        "n_candidates": int,
        "n_valid": int,               # ok=True 후보 수
        "n_clusters": int,            # 서로 다른 결과셋 수
        "tie": bool,                  # 최대 클러스터가 복수(동률) → judge 필요
        "tie_indices": [int],         # 동률 클러스터들의 대표 인덱스
        "clusters": [{"hash","size","indices","representative"}],
        "rejected_with_warnings": [int],  # accuracy_warnings 있는 후보(참고)
      }

    규칙:
      - ok=False(실행 실패) 후보는 클러스터에서 제외(빈 클러스터/에러 = 자동 탈락).
      - accuracy_warnings 가 있는 후보는 클러스터엔 포함하되, **동률일 때 페널티**
        (경고 없는 클러스터를 우선) — 결정적 가드 신호를 선택에 반영.
      - 단일 후보면 그 후보가 winner(합의 1.0, tie=False) — k=1 폴백.
    """
    n = len(candidates)
    if n == 0:
        return {"winner_index": None, "agreement": 0.0, "n_candidates": 0,
                "n_valid": 0, "n_clusters": 0, "tie": False, "tie_indices": [],
                "clusters": [], "rejected_with_warnings": []}

    valid = [(i, c) for i, c in enumerate(candidates) if c.ok]
    rejected_warn = [i for i, c in enumerate(candidates) if c.accuracy_warnings]

    if not valid:
        return {"winner_index": None, "agreement": 0.0, "n_candidates": n,
                "n_valid": 0, "n_clusters": 0, "tie": False, "tie_indices": [],
                "clusters": [], "rejected_with_warnings": rejected_warn}

    # 결과셋 해시로 클러스터링
    clusters: dict[str, list[int]] = {}
    for i, c in valid:
        h = resultset_hash(c.rows, order_sensitive=order_sensitive)
        clusters.setdefault(h, []).append(i)

    cluster_list = [
        {
            "hash": h,
            "size": len(idxs),
            "indices": idxs,
            "representative": idxs[0],
            "has_warnings": any(candidates[i].accuracy_warnings for i in idxs),
        }
        for h, idxs in clusters.items()
    ]
    # 정렬: 큰 클러스터 우선, 동률이면 경고 없는 쪽 우선
    cluster_list.sort(key=lambda c: (-c["size"], c["has_warnings"]))

    max_size = cluster_list[0]["size"]
    top = [c for c in cluster_list if c["size"] == max_size]
    # 동률 판정: 경고 페널티 적용 후에도 복수면 tie
    no_warn_top = [c for c in top if not c["has_warnings"]]
    effective_top = no_warn_top if no_warn_top else top

    n_valid = len(valid)
    tie = len(effective_top) > 1
    winner = effective_top[0]["representative"]
    agreement = round(max_size / n_valid, 3) if n_valid else 0.0

    return {
        "winner_index": winner,
        "agreement": agreement,
        "n_candidates": n,
        "n_valid": n_valid,
        "n_clusters": len(cluster_list),
        "tie": tie,
        "tie_indices": [c["representative"] for c in effective_top] if tie else [],
        "clusters": cluster_list,
        "rejected_with_warnings": rejected_warn,
    }
