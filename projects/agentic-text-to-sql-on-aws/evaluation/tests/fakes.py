"""단위 테스트용 fake (AWS 호출 없이 평가 로직 검증)."""

from __future__ import annotations

from typing import Any

from evaluation.dataapi import AuroraReadOnlyRunner, DataApiError, QueryResult


class FakeRunner(AuroraReadOnlyRunner):
    """SQL 문자열 → 결과(또는 예외) 매핑을 갖는 Data API 러너 대역."""

    def __init__(self, results: dict[str, Any]) -> None:
        super().__init__(
            cluster_arn="arn:aws:rds:us-west-2:123456789012:cluster:fake",
            secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:fake",
            db_name="ecommerce",
            client=object(),
        )
        self._results = results
        self.executed: list[str] = []

    def run(self, sql: str) -> QueryResult:
        key = sql.strip()
        self.executed.append(key)
        if key not in self._results:
            raise DataApiError(f"fake 러너에 등록되지 않은 SQL: {key!r}")
        value = self._results[key]
        if isinstance(value, Exception):
            raise value
        return QueryResult(columns=value[0], rows=value[1])


class FakeDataApiClient:
    """boto3 rds-data client 최소 대역."""

    def __init__(self, response: dict | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    def execute_statement(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def query_record_span(record_json: str) -> dict:
    """CloudWatch 로그 이벤트를 품은 스팬 형태(방어적 파싱 대상)."""
    return {
        "spanId": "span-1",
        "name": "orchestrator.invoke",
        "logs": [{"body": f"INFO orchestrator {record_json}"}],
    }
