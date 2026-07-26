"""DynamoDB Streams → Neptune 동기화 Lambda 핸들러.

각 Streams 레코드를 ``record_to_cypher`` 로 openCypher 문으로 변환하고
``NeptuneGraphClient`` 로 실행한다. 실패한 레코드는 파샬 배치 응답
(``batchItemFailures``, ReportBatchItemFailures 규격)으로 반환해 해당 레코드만
재처리되게 한다(성공 레코드 중복 처리 방지 — MERGE 라 재처리 자체는 멱등).

의존성은 boto3 표준 런타임만 사용한다(외부 패키지 금지, 상대 임포트만).
환경변수: ``GRAPH_ENDPOINT``(https://<host>:8182), ``AWS_REGION``(기본 us-west-2).
"""

from __future__ import annotations

import os
from typing import Any

from .graph_sync import NeptuneGraphClient, record_to_cypher

# 콜드스타트 간 재사용을 위한 모듈 레벨 캐시(핸들러 외부).
_GRAPH_CLIENT: NeptuneGraphClient | None = None


def _get_client() -> NeptuneGraphClient:
    global _GRAPH_CLIENT
    if _GRAPH_CLIENT is None:
        _GRAPH_CLIENT = NeptuneGraphClient(
            endpoint=os.environ.get("GRAPH_ENDPOINT"),
            region=os.environ.get("AWS_REGION", "us-west-2"),
        )
    return _GRAPH_CLIENT


def handler(event: dict, context: Any = None, *, graph_client: NeptuneGraphClient | None = None):
    """Streams 이벤트 핸들러. 파샬 배치 응답 dict 를 반환.

    graph_client 는 단위 테스트용으로 주입 가능(미주입 시 env 로 생성).
    """
    client = graph_client or _get_client()
    failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        seq = _sequence_number(record)
        try:
            statements = record_to_cypher(record)
            if statements:
                client.execute(statements)
        except Exception:
            # 개별 레코드 실패는 파샬 배치 응답으로 격리(전체 배치 실패 방지).
            if seq is not None:
                failures.append({"itemIdentifier": seq})

    return {"batchItemFailures": failures}


def _sequence_number(record: dict) -> str | None:
    """레코드의 SequenceNumber(파샬 배치 응답 식별자)를 추출."""
    return record.get("dynamodb", {}).get("SequenceNumber")
