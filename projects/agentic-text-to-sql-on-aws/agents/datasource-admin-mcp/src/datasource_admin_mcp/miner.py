"""semantic 후보 채굴기 (개선 파이프라인 Track B).

orchestrator 가 실행 종료 시 남기는 구조화 로그(``t2sql_query_record {JSON}``)를 CloudWatch
Logs 에서 읽어 semantic 후보를 만든다.

- **fewshot 후보**: 성공(status="ok") 질의의 (질문, SQL) 쌍 → 검색 시 few-shot 예시로 사용.
- **term 후보**: 실패(status="error"|"clarification") 질문에서 반복 등장하는 토큰 →
  용어 정의가 비어 있어 실패했을 가능성이 큰 후보(Manager 가 정의를 채워 승인).

모든 적재는 **candidate** 로만 한다(승인 게이트 유지 — crawler 와 동일 원칙). 쓰기는
``SemanticRepository`` 한 곳만 경유한다(dual-write 금지 / 단일 쓰기 지점).

중복 채굴 방지
-------------
entity_id 는 질문(또는 토큰) 해시라서 재실행 시 같은 값이 나온다. put 전에 ``get_entity`` 로
존재를 확인해 **status 무관(rejected 포함)** 이면 skip 한다 — 반려한 후보가 배치 재실행으로
되살아나지 않게 하는 장치다.

boto3 logs 클라이언트와 repository 는 생성자로 주입 가능하다(단위 테스트용 fake).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any

logger = logging.getLogger("datasource_admin_mcp.miner")

# orchestrator 구조화 로그 마커 — 이 문자열 뒤의 JSON 이 레코드 본문이다.
RECORD_MARKER = "t2sql_query_record"

# AgentCore Runtime 로그 그룹 접두어(env 로 재정의 가능).
DEFAULT_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/"

# 로그 그룹 이름에 이 문자열이 포함된 그룹만 스캔한다(orchestrator 런타임).
LOG_GROUP_FILTER = "orchestrator"

# 응답에 실어 보낼 후보 목록의 최대 길이(카운트는 전체값 유지 — 응답 경량화).
MAX_REPORTED_CANDIDATES = 50

# 한 번의 채굴에서 스캔할 최대 이벤트 수(비용·응답시간 상한). 초과분은 경고 로그로 알린다.
MAX_EVENTS = 5000

# term 후보로 승격하려면 실패 질문 레코드 중 최소 몇 건에 등장해야 하는지.
TERM_MIN_OCCURRENCES = 2

# 실패 질문에서 term 후보를 뽑을 때 제외하는 토큰(조사 제거 같은 형태소 처리는 과설계 —
# 공백 분리 + 불용어 + 최소 길이 수준으로 충분하다. Manager 가 승인 게이트에서 걸러낸다).
STOPWORDS = frozenset(
    {
        # 한국어 일반어·의문어
        "그리고",
        "그런데",
        "누가",
        "무엇",
        "무엇이",
        "보여줘",
        "알려줘",
        "어떻게",
        "언제",
        "얼마",
        "얼마나",
        "왜",
        "우리",
        "이거",
        "저거",
        "전체",
        "정말",
        "제일",
        "조회",
        "좀",
        "지금",
        "총",
        "하는",
        "하고",
        "해줘",
        # 영문 일반어
        "a",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "by",
        "for",
        "from",
        "give",
        "how",
        "in",
        "is",
        "many",
        "me",
        "much",
        "of",
        "on",
        "or",
        "show",
        "the",
        "to",
        "top",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)

# 실패 질문에서 term 후보를 만들 대상 status.
FAILED_STATUSES = frozenset({"error", "clarification"})

# 토큰 정리용 — 앞뒤 구두점/기호 제거(한글·영문·숫자만 남긴다).
_TOKEN_STRIP_RE = re.compile(r"^[^0-9A-Za-z가-힣]+|[^0-9A-Za-z가-힣]+$")
_WHITESPACE_RE = re.compile(r"\s+")


def parse_record(message: str) -> dict[str, Any] | None:
    """로그 메시지에서 ``t2sql_query_record {JSON}`` 의 JSON 부분을 파싱한다.

    마커 뒤 첫 ``{`` 부터 ``json.JSONDecoder.raw_decode`` 로 읽어 뒤에 붙은 잡음
    (타임스탬프·개행·추가 텍스트)에 견디게 한다. 마커/JSON 이 없으면 None.
    """
    if not message:
        return None
    marker_at = message.find(RECORD_MARKER)
    if marker_at < 0:
        return None
    brace_at = message.find("{", marker_at + len(RECORD_MARKER))
    if brace_at < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(message[brace_at:])
    except ValueError:
        logger.warning("t2sql_query_record JSON 파싱 실패(무시): %s", message[:200])
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def normalize_question(question: str) -> str:
    """질문 정규화 — 해시 안정화용(대소문자·공백 차이를 같은 후보로 묶는다)."""
    return _WHITESPACE_RE.sub(" ", (question or "").strip()).lower()


def _hash12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def fewshot_entity_id(question: str) -> str:
    """fewshot 후보 entity_id — ``mined-<sha256(정규화 질문)[:12]>``."""
    return f"mined-{_hash12(normalize_question(question))}"


def term_entity_id(token: str) -> str:
    """term 후보 entity_id — ``mined-term-<sha256(토큰)[:12]>``."""
    return f"mined-term-{_hash12(token)}"


def tokenize(question: str) -> list[str]:
    """공백 분리 + 구두점 제거 + 2자 이상 + 불용어 제외 토큰 목록(소문자)."""
    tokens: list[str] = []
    for raw in normalize_question(question).split():
        token = _TOKEN_STRIP_RE.sub("", raw)
        if len(token) < 2 or token in STOPWORDS or token.isdigit():
            continue
        tokens.append(token)
    return tokens


class CandidateMiner:
    """CloudWatch Logs 의 orchestrator 레코드에서 semantic 후보를 채굴한다."""

    def __init__(
        self,
        repository: Any,
        logs_client: Any | None = None,
        log_group_prefix: str | None = None,
        region: str | None = None,
        clock: Any = time.time,
    ) -> None:
        self._repository = repository
        self._logs_client = logs_client
        self._log_group_prefix = (
            log_group_prefix
            if log_group_prefix is not None
            else os.environ.get("ORCHESTRATOR_LOG_GROUP_PREFIX", DEFAULT_LOG_GROUP_PREFIX)
        )
        self._region = region or os.environ.get("AWS_REGION", "us-west-2")
        self._clock = clock

    @property
    def logs_client(self) -> Any:
        """boto3 logs client(지연 생성)."""
        if self._logs_client is None:
            import boto3

            self._logs_client = boto3.client("logs", region_name=self._region)
        return self._logs_client

    # --- 로그 수집 -----------------------------------------------------------
    def discover_log_groups(self) -> list[str]:
        """접두어 하위에서 orchestrator 로그 그룹 이름 목록을 반환(페이지네이션 처리)."""
        names: list[str] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"logGroupNamePrefix": self._log_group_prefix}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self.logs_client.describe_log_groups(**kwargs)
            for group in response.get("logGroups", []):
                name = group.get("logGroupName") or ""
                if LOG_GROUP_FILTER in name:
                    names.append(name)
            next_token = response.get("nextToken")
            if not next_token:
                break
        return names

    def fetch_records(self, hours: int = 24) -> list[dict[str, Any]]:
        """최근 ``hours`` 시간의 ``t2sql_query_record`` 레코드 목록을 반환."""
        start_time = int((self._clock() - hours * 3600) * 1000)
        records: list[dict[str, Any]] = []
        for group in self.discover_log_groups():
            next_token: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "logGroupName": group,
                    "startTime": start_time,
                    "filterPattern": f'"{RECORD_MARKER}"',
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                try:
                    response = self.logs_client.filter_log_events(**kwargs)
                except Exception as exc:  # noqa: BLE001 — 한 그룹 실패가 전체를 막지 않게
                    logger.warning("filter_log_events 실패(그룹 건너뜀) %s: %s", group, exc)
                    break
                for event in response.get("events", []):
                    parsed = parse_record(event.get("message", ""))
                    if parsed is not None:
                        records.append(parsed)
                if len(records) >= MAX_EVENTS:
                    logger.warning(
                        "채굴 스캔 상한(%d) 도달 — 이후 이벤트는 스캔하지 않았다(그룹=%s).",
                        MAX_EVENTS,
                        group,
                    )
                    return records[:MAX_EVENTS]
                next_token = response.get("nextToken")
                if not next_token:
                    break
        return records

    # --- 채굴 ---------------------------------------------------------------
    def mine(self, hours: int = 24, actor: str = "mining-batch") -> dict[str, Any]:
        """레코드를 수집해 fewshot/term 후보를 candidate 로 적재하고 요약을 반환.

        Returns:
            {"scanned":N,"mined":N,"skipped_existing":N,
             "candidates":[{"entity_type":...,"entity_id":...}]}
        """
        if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
            raise ValueError(f"hours 는 1 이상의 정수여야 합니다: {hours!r}")

        records = self.fetch_records(hours)
        mined: list[dict[str, str]] = []
        skipped = 0

        for entity_type, entity_id, payload in self._build_candidates(records):
            if self._repository.get_entity(entity_type, entity_id) is not None:
                # status 무관(rejected 포함) — 반려한 후보를 되살리지 않는다.
                skipped += 1
                continue
            self._repository.put_entity(
                entity_type, entity_id, payload, status="candidate", actor=actor
            )
            mined.append({"entity_type": entity_type, "entity_id": entity_id})

        return {
            "scanned": len(records),
            "mined": len(mined),
            "skipped_existing": skipped,
            "candidates": mined,
        }

    def _build_candidates(
        self, records: list[dict[str, Any]]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """레코드 목록 → (entity_type, entity_id, payload) 후보 목록(중복 제거)."""
        candidates: list[tuple[str, str, dict[str, Any]]] = []
        seen: set[tuple[str, str]] = set()

        for record in records:
            candidate = self._fewshot_candidate(record)
            if candidate is None:
                continue
            key = (candidate[0], candidate[1])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

        for candidate in self._term_candidates(records):
            key = (candidate[0], candidate[1])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

        return candidates

    @staticmethod
    def _fewshot_candidate(record: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        """성공 레코드 → fewshot 후보. 질문/SQL 이 비면 None."""
        if record.get("status") != "ok":
            return None
        question = str(record.get("question") or "").strip()
        sql = str(record.get("sql") or "").strip()
        if not question or not sql:
            return None
        payload: dict[str, Any] = {
            "question": question,
            "sql": sql,
            "source": "mined",
            "mined_from_session": record.get("session_id") or "",
        }
        return ("fewshot", fewshot_entity_id(question), payload)

    @staticmethod
    def _term_candidates(
        records: list[dict[str, Any]],
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """실패 레코드 질문에서 반복 등장 토큰을 term 후보로."""
        counts: Counter[str] = Counter()
        for record in records:
            if record.get("status") not in FAILED_STATUSES:
                continue
            question = str(record.get("question") or "")
            # 레코드 단위로 중복 제거 — 한 질문에서 같은 토큰이 여러 번 나와도 1회.
            for token in set(tokenize(question)):
                counts[token] += 1

        results: list[tuple[str, str, dict[str, Any]]] = []
        for token, count in sorted(counts.items()):
            if count < TERM_MIN_OCCURRENCES:
                continue
            payload: dict[str, Any] = {
                "term": token,
                "definition": "채굴 후보 — 정의 필요 (실패 질의에서 반복 등장)",
                "synonyms": [],
                "source": "mined",
            }
            results.append(("term", term_entity_id(token), payload))
        return results
