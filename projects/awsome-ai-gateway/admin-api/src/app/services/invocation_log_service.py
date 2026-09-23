# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Bedrock model-invocation log ↔ `usage.usage_logs` 대조(reconcile) 서비스.

**왜 필요한가.** GPT-5.6 을 표준 runtime plane(`bedrock-runtime`, SigV4 + CRIS)으로 호출하면
Bedrock 이 요청/응답 **본문까지** CloudWatch Logs 에 기록한다. Mantle plane 에는 이 기능이
없다. 즉 같은 모델이라도 어느 plane 으로 호출했는지에 따라 "본문 감사가 가능한가" 가
갈린다. 이 서비스는 그 로그를 우리 `usage_logs` 와 맞춰보고, **우리가 과금한 호출 중 감사
근거가 없는 것** 을 찾아낸다.

**live 로 증명된 캡처 표** (`gateway-proxy/tests/integration/test_invocation_logging_live.py`):

    plane    wire       stream   records
    runtime  responses  no          1
    runtime  responses  YES         1     ← 스트리밍도 기록된다(실측)
    runtime  chat       YES         1
    Mantle   responses  no          0     ← plane 음성대조군
    Mantle   responses  YES         0

이 표가 이 서비스의 판정 규칙 전부의 근거다. 특히 **Mantle 이 0 이라는 사실** 때문에
Mantle plane 행의 `bedrock_request_id IS NULL` 은 결함이 아니라 문서화된 속성이며,
`missing` 으로 세면 codex 호출마다 영원히 늑대를 부르게 된다 —
`skipped_null` 로 이유코드와 함께 분리한다.

**설계 원칙**

- boto3 `logs` 클라이언트를 **주입** 받는다 — AWS 없이 단위테스트 가능.
- 판정 로직(`classify_null_request_id`, `reconcile`)은 **순수 함수** 로 두고 AWS I/O 와 분리한다.
- **조용한 truncation 금지.** Logs Insights 는 한 쿼리 상한이 10 000 건이다. 잘렸으면
  `truncated=True` 와 실제 매칭 건수를 보고한다(부분결과를 전수처럼 보이게 하지 않는다).
- `orphan`(로그엔 있는데 usage 행이 없음)은 **결함 단정 금지**. invocation logging 은
  Region 단위·계정 전체로 켜지므로 게이트웨이가 아닌 주체(운영자 CLI, admin-chat-agent 등)의
  호출도 같은 log group 에 섞인다. 패딩 구간 경계 효과까지 있어, orphan 은 core 창 안쪽만
  세고 "조사 대상" 으로만 보고한다.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import structlog

logger = structlog.get_logger()


class InvocationLogNotConfigured(RuntimeError):
    """log group 미설정 또는 해당 Region 에 그 group 이 없음 → 503 으로 알린다."""


class InvocationLogQueryError(RuntimeError):
    """Logs Insights 쿼리 실패/타임아웃 → 502. 부분결과를 성공으로 위장하지 않는다."""


# ── NULL bedrock_request_id 이유코드 ──────────────────────────────────────────
# 값은 API 응답에 그대로 나가므로 문자열을 바꾸면 대시보드/알람이 깨진다.
REASON_MANTLE_PLANE = "mantle_plane"
REASON_NOT_BEDROCK = "not_bedrock"
REASON_WEB_SEARCH = "web_search_multi_turn"
REASON_LEGACY = "legacy"

# Mantle plane provider 값(model.provider enum). 이 plane 은 invocation log 를 아예 남기지
# 않는다(위 캡처 표) — 그래서 NULL 이 정상이다.
_MANTLE_PROVIDERS = frozenset({"BEDROCK_MANTLE", "BEDROCK_MANTLE_OPENAI"})
# Bedrock 이 아닌 provider(자체 호스팅 vLLM 등)는 Bedrock 로그가 존재할 수 없다.
_NON_BEDROCK_PROVIDERS = frozenset({"OPENMODEL"})

# `filter requestId = '...'` 로 들어가는 값 — 쿼리 문자열 주입을 막기 위해 화이트리스트 검증.
# AWS request id 는 UUID 형태지만, Mantle 의 `req_…` 나 향후 형식 변화를 흡수하려고
# 문자군만 제한한다.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class UsageRowRef:
    """reconcile 에 필요한 `usage_logs` 열만 담은 읽기 전용 사영(projection)."""

    request_id: str  # 게이트웨이 request id (usage_logs.request_id)
    bedrock_request_id: str | None
    model_alias: str
    provider: str
    requested_at: datetime
    web_search_count: int = 0
    is_streaming: bool = False


@dataclass
class LogWindow:
    """한 번의 Logs Insights 조회 결과."""

    request_ids: set[str] = field(default_factory=set)
    # request_id → 그 레코드의 modelId (orphan 을 사람이 판단할 때 필요)
    model_by_id: dict[str, str] = field(default_factory=dict)
    # request_id → 레코드 timestamp(ms). core 창 판정에 쓴다.
    timestamp_by_id: dict[str, int] = field(default_factory=dict)
    records_matched: int = 0
    records_returned: int = 0
    query_id: str = ""

    @property
    def truncated(self) -> bool:
        """limit 에서 잘렸는가.

        저장 필드가 아니라 파생값이다 — 필드로 두면 `records_matched` 만 바꾸고 플래그를
        안 바꾼 창이 만들어져 "전수" 로 오보할 수 있다. 같은 requestId 가 여러 레코드로
        나올 수 있으니 set 크기가 아니라 **반환 행 수** 와 비교한다.
        """
        return self.records_matched > self.records_returned


@dataclass
class ReconcileReport:
    window_start: datetime
    window_end: datetime
    log_group: str
    region: str
    usage_rows: int = 0
    matched: int = 0
    # 과금은 했는데 감사 근거가 없는 행 — 이 리포트의 존재 이유.
    # `missing` 은 표본(최대 max_samples), `missing_count` 가 진짜 건수다.
    missing: list[dict[str, Any]] = field(default_factory=list)
    missing_count: int = 0
    # NULL 은 이유코드별 집계로만 보고한다(missing 아님).
    skipped_null: dict[str, int] = field(default_factory=dict)
    # 로그엔 있는데 usage 행이 없음 — 결함 단정 금지(모듈 docstring 참조).
    orphan_log_ids: list[str] = field(default_factory=list)
    orphan_count: int = 0
    log_records_matched: int = 0
    log_truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "log_group": self.log_group,
            "region": self.region,
            "usage_rows": self.usage_rows,
            "matched": self.matched,
            "missing_count": self.missing_count,
            "missing": self.missing,
            "missing_sample_truncated": self.missing_count > len(self.missing),
            "skipped_null": dict(self.skipped_null),
            "skipped_null_total": sum(self.skipped_null.values()),
            "orphan_count": self.orphan_count,
            "orphan_log_ids": self.orphan_log_ids,
            "orphan_sample_truncated": self.orphan_count > len(self.orphan_log_ids),
            "log_records_matched": self.log_records_matched,
            "log_truncated": self.log_truncated,
            "notes": list(self.notes),
        }


def classify_null_request_id(
    *,
    provider: str,
    model_alias: str,
    web_search_count: int,
) -> str:
    """`bedrock_request_id IS NULL` 인 행의 **이유** 를 돌려준다. 절대 missing 이 아니다.

    우선순위가 의미를 만든다: Mantle plane 은 웹서치를 했든 안 했든 로그 자체가 없으므로
    plane 판정이 먼저다. 그 다음이 웹서치(1 usage 행 = N invocation 이라 단일 id 로는
    1:N 을 1:1 처럼 오보하게 된다), 마지막이 legacy(id 수집 이전 행).
    """
    normalized = (provider or "").strip().upper()
    if normalized in _MANTLE_PROVIDERS:
        return REASON_MANTLE_PLANE
    if normalized in _NON_BEDROCK_PROVIDERS:
        return REASON_NOT_BEDROCK
    # alias 기반 보조 판정 — provider 가 비어있는 legacy 행에서도 Mantle 을 놓치지 않는다.
    if (model_alias or "").startswith("codex-gpt"):
        return REASON_MANTLE_PLANE
    if web_search_count and web_search_count > 0:
        return REASON_WEB_SEARCH
    return REASON_LEGACY


def reconcile(
    rows: Iterable[UsageRowRef],
    window: LogWindow,
    *,
    window_start: datetime,
    window_end: datetime,
    log_group: str,
    region: str,
    core_start: datetime | None = None,
    core_end: datetime | None = None,
    max_samples: int = 200,
) -> ReconcileReport:
    """순수 함수: usage 행 목록과 로그 창을 교집합해 리포트를 만든다(AWS 접근 없음).

    `core_start/core_end` 는 패딩을 제외한 실제 대상 구간이다. orphan 은 이 안쪽 레코드만
    센다 — 패딩 구간의 레코드는 창 밖 usage 행의 것일 수 있어 orphan 으로 세면 거짓양성이다.
    """
    report = ReconcileReport(
        window_start=window_start,
        window_end=window_end,
        log_group=log_group,
        region=region,
        log_records_matched=window.records_matched,
        log_truncated=window.truncated,
    )
    core_start_ms = int((core_start or window_start).timestamp() * 1000)
    core_end_ms = int((core_end or window_end).timestamp() * 1000)

    seen_bedrock_ids: set[str] = set()
    for row in rows:
        report.usage_rows += 1
        if not row.bedrock_request_id:
            reason = classify_null_request_id(
                provider=row.provider,
                model_alias=row.model_alias,
                web_search_count=row.web_search_count,
            )
            report.skipped_null[reason] = report.skipped_null.get(reason, 0) + 1
            continue
        seen_bedrock_ids.add(row.bedrock_request_id)
        if row.bedrock_request_id in window.request_ids:
            report.matched += 1
            continue
        # 카운트는 항상 올리고 표본만 상한을 둔다 — 표본 길이를 건수로 쓰면 상한에 걸린
        # 순간부터 "정확히 max_samples 건" 으로 조용히 축소 보고된다.
        report.missing_count += 1
        if len(report.missing) < max_samples:
            report.missing.append(
                {
                    "request_id": row.request_id,
                    "bedrock_request_id": row.bedrock_request_id,
                    "model_alias": row.model_alias,
                    "provider": row.provider,
                    "requested_at": row.requested_at.isoformat() if row.requested_at else None,
                    "is_streaming": row.is_streaming,
                }
            )

    if report.missing_count > len(report.missing):
        report.notes.append(
            f"missing 표본이 {max_samples}건에서 잘렸습니다 — 실제 {report.missing_count}건. "
            "구간을 좁혀 재조회하세요."
        )

    for log_id in window.request_ids:
        if log_id in seen_bedrock_ids:
            continue
        ts = window.timestamp_by_id.get(log_id)
        if ts is not None and not (core_start_ms <= ts <= core_end_ms):
            continue  # 패딩 구간 — 창 밖 usage 행의 레코드일 수 있다
        report.orphan_count += 1
        if len(report.orphan_log_ids) < max_samples:
            report.orphan_log_ids.append(log_id)

    if report.orphan_count:
        report.notes.append(
            "orphan 은 결함 단정이 아닙니다: invocation logging 은 Region 단위·계정 전체로 "
            "켜지므로 게이트웨이 외 주체(운영자 CLI, admin-chat-agent 등)의 호출도 같은 "
            "log group 에 기록됩니다. identity.arn 으로 주체를 확인하세요."
        )
    if report.log_truncated:
        report.notes.append(
            f"Logs Insights 결과가 잘렸습니다(matched={report.log_records_matched}, "
            f"returned={window.records_returned}). matched 가 아닌 행이 실제로는 로그에 "
            "있을 수 있으므로 missing 을 신뢰하지 말고 구간을 좁혀 재조회하세요."
        )
    return report


class InvocationLogService:
    """CloudWatch Logs Insights 로 Bedrock invocation log 를 읽는다(읽기 전용).

    boto3 `logs` 클라이언트를 주입받으며, 이 서비스는 **어떤 쓰기 API 도 호출하지 않는다**
    (`stop_query` 는 우리 쿼리 취소로, 로그 데이터에 영향 없음).
    """

    def __init__(
        self,
        logs_client,
        *,
        log_group: str,
        region: str = "",
        query_timeout_s: int = 30,
        max_records: int = 10_000,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._client = logs_client
        self._log_group = log_group
        self._region = region
        self._timeout_s = query_timeout_s
        # Logs Insights 한 쿼리 상한은 10 000. 그보다 크게 요청하면 API 가 거부한다.
        self._max_records = max(1, min(int(max_records), 10_000))
        self._poll_interval_s = poll_interval_s

    @property
    def log_group(self) -> str:
        return self._log_group

    @property
    def region(self) -> str:
        return self._region

    async def fetch_request_ids(self, *, start: datetime, end: datetime) -> LogWindow:
        """구간 내 모든 invocation 레코드의 `requestId` 집합을 가져온다."""
        query = (
            "fields @timestamp, requestId, modelId, operation, errorCode\n"
            "| filter ispresent(requestId)\n"
            "| sort @timestamp asc\n"
            f"| limit {self._max_records}"
        )
        rows, stats, query_id = await self._run_query(query, start=start, end=end)
        window = LogWindow(records_returned=len(rows), query_id=query_id)
        window.records_matched = int(stats.get("recordsMatched") or 0)
        for row in rows:
            rid = row.get("requestId")
            if not rid:
                continue
            window.request_ids.add(rid)
            if row.get("modelId"):
                window.model_by_id[rid] = row["modelId"]
            ts = _parse_insights_timestamp(row.get("@timestamp"))
            if ts is not None:
                window.timestamp_by_id[rid] = ts
        return window

    async def fetch_record(
        self,
        bedrock_request_id: str,
        *,
        start: datetime,
        end: datetime,
        include_bodies: bool = False,
    ) -> dict[str, Any] | None:
        """단일 `requestId` 의 레코드를 가져온다.

        `include_bodies=False` 면 메타데이터만 남기고 요청/응답 본문을 제거한다 — 프롬프트
        원문이 기본으로 흘러나가지 않게 하는 마지막 방어선(설정 게이트와 이중화).
        """
        if not _REQUEST_ID_RE.match(bedrock_request_id or ""):
            raise ValueError("bedrock_request_id 형식이 올바르지 않습니다")
        query = (
            "fields @timestamp, @message\n"
            f"| filter requestId = '{bedrock_request_id}'\n"
            "| sort @timestamp asc\n"
            "| limit 5"
        )
        rows, _stats, _qid = await self._run_query(query, start=start, end=end)
        if not rows:
            return None
        parsed: list[dict[str, Any]] = []
        for row in rows:
            raw = row.get("@message") or "{}"
            try:
                record = json.loads(raw)
            except (TypeError, ValueError):
                # 파싱 실패를 조용히 버리면 "레코드 없음" 과 구분이 안 된다.
                record = {"_unparsed": raw[:2000]}
            parsed.append(record if include_bodies else redact_bodies(record))
        s3_pointers = sorted({p for r in parsed for p in s3_body_pointers(r)})
        return {
            "bedrock_request_id": bedrock_request_id,
            "log_group": self._log_group,
            "region": self._region,
            "bodies_included": include_bodies,
            "record_count": len(parsed),
            "records": parsed,
            # 큰 본문은 CloudWatch 레코드에 실리지 않고 sidecar S3 로 간다. 포인터를 그대로
            # 보여준다 — 조용히 본문 없는 레코드를 주면 "본문이 없다" 로 오해한다.
            # admin-api 는 S3 를 읽지 않는다(권한도 없다).
            "s3_body_pointers": s3_pointers,
        }

    async def _run_query(
        self,
        query: str,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[list[dict[str, str]], dict[str, Any], str]:
        if not self._log_group:
            raise InvocationLogNotConfigured(
                "BEDROCK_INVOCATION_LOG_GROUP 이 설정되지 않았습니다"
            )
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        try:
            started = await asyncio.to_thread(
                self._client.start_query,
                logGroupName=self._log_group,
                startTime=start_ts,
                endTime=end_ts,
                queryString=query,
                limit=self._max_records,
            )
        except Exception as exc:  # noqa: BLE001 — 예외 타입은 botocore 동적 클래스
            if _is_resource_not_found(exc):
                raise InvocationLogNotConfigured(
                    f"log group '{self._log_group}' 을 {self._region or 'region'} 에서 찾을 수 "
                    "없습니다. invocation logging 이 그 Region 에 켜져 있는지 확인하세요"
                ) from exc
            raise InvocationLogQueryError(f"start_query 실패: {exc}") from exc

        query_id = started.get("queryId", "")
        deadline = self._timeout_s
        waited = 0.0
        while True:
            result = await asyncio.to_thread(self._client.get_query_results, queryId=query_id)
            status = result.get("status", "")
            if status == "Complete":
                return _rows_to_dicts(result.get("results", [])), result.get("statistics", {}), query_id
            if status in ("Failed", "Cancelled", "Timeout"):
                raise InvocationLogQueryError(f"쿼리가 {status} 상태로 끝났습니다 (queryId={query_id})")
            if waited >= deadline:
                # 부분결과를 반환하지 않는다 — 잘린 결과로 missing 을 판정하면 거짓양성이다.
                try:
                    await asyncio.to_thread(self._client.stop_query, queryId=query_id)
                except Exception:  # noqa: BLE001 — 취소 실패는 결과에 영향 없음
                    logger.warning("invocation_log.stop_query_failed", query_id=query_id)
                raise InvocationLogQueryError(
                    f"쿼리가 {self._timeout_s}s 안에 끝나지 않았습니다 (queryId={query_id}). "
                    "구간을 좁혀 재시도하세요"
                )
            await asyncio.sleep(self._poll_interval_s)
            waited += self._poll_interval_s


def redact_bodies(record: dict[str, Any]) -> dict[str, Any]:
    """invocation 레코드에서 요청/응답 **본문** 만 제거하고 메타데이터는 유지한다.

    본문 키는 Bedrock 스키마 그대로다(`input.inputBodyJson`, `output.outputBodyJson`).
    dict 를 새로 만들어 돌려주므로 입력을 변형하지 않는다.
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key in ("input", "output") and isinstance(value, dict):
            kept = {k: v for k, v in value.items() if not k.endswith("BodyJson")}
            kept["_body_redacted"] = any(k.endswith("BodyJson") for k in value)
            out[key] = kept
        elif key in ("inputBodyJson", "outputBodyJson", "messages", "prompt"):
            out[f"{key}_redacted"] = True
        else:
            out[key] = value
    return out


def s3_body_pointers(record: dict[str, Any]) -> list[str]:
    """레코드 안의 sidecar S3 본문 포인터(`*BodyS3Path`)를 모아 돌려준다.

    Bedrock 은 본문이 CloudWatch 레코드 크기 상한을 넘으면 본문을 S3 로 보내고 로그에는
    경로만 남긴다. 포인터를 무시하면 "이 호출은 본문이 없다" 로 잘못 읽힌다.
    """
    out: list[str] = []
    for value in record.values():
        if isinstance(value, dict):
            for k, v in value.items():
                if k.endswith("BodyS3Path") and isinstance(v, str) and v:
                    out.append(v)
    for k, v in record.items():
        if k.endswith("BodyS3Path") and isinstance(v, str) and v:
            out.append(v)
    return out


def padded_window(
    start: datetime,
    end: datetime,
    *,
    pad_minutes: int = 10,
) -> tuple[datetime, datetime]:
    """로그 조회용 패딩 구간.

    필요한 이유: `usage_logs.requested_at` 은 게이트웨이가 요청을 받은 시각이고 로그
    레코드의 timestamp 는 Bedrock 이 호출을 처리한 시각이라 정확히 같지 않다. 스트리밍이면
    수십 초 벌어질 수 있고, 배달 자체도 비동기다. 패딩 없이 딱 맞춰 조회하면 경계의
    정상 호출이 `missing` 으로 오보된다.
    """
    pad = timedelta(minutes=max(0, pad_minutes))
    return start - pad, end + pad


def _rows_to_dicts(results: list[list[dict[str, str]]]) -> list[dict[str, str]]:
    """Insights 의 `[[{field,value},…],…]` 형태를 dict 리스트로 변환."""
    out: list[dict[str, str]] = []
    for row in results:
        record: dict[str, str] = {}
        for cell in row:
            name = cell.get("field")
            if name is None:
                continue
            record[name] = cell.get("value", "")
        out.append(record)
    return out


def _parse_insights_timestamp(value: str | None) -> int | None:
    """Insights 의 `@timestamp`("YYYY-MM-DD HH:MM:SS.mmm", UTC)를 epoch ms 로."""
    if not value:
        return None
    text = value.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _is_resource_not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
    return code == "ResourceNotFoundException" or exc.__class__.__name__ == "ResourceNotFoundException"
