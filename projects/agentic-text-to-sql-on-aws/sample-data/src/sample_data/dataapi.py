"""RDS Data API 헬퍼.

boto3 rds-data 클라이언트를 감싸 파라미터화된 statement 실행과
배치 insert 를 단순화한다. 값 → RDS Data API 파라미터 변환 로직을
한 곳에 모아 seed 스크립트와 테스트가 공유한다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# RDS Data API typeHint 가 필요한 컬럼(문자열로 전달 후 캐스팅).
# 값은 문자열로 넘기되 typeHint 로 서버가 캐스팅한다.
TIMESTAMP_HINT = "TIMESTAMP"
DECIMAL_HINT = "DECIMAL"


def to_param(name: str, value: Any, *, type_hint: str | None = None) -> dict:
    """단일 값을 RDS Data API SqlParameter dict 로 변환.

    None -> isNull, bool -> booleanValue, int -> longValue,
    float/Decimal -> doubleValue(또는 DECIMAL typeHint 시 stringValue),
    그 외 -> stringValue.
    """
    param: dict[str, Any] = {"name": name}
    if value is None:
        param["value"] = {"isNull": True}
        return param

    if type_hint is not None:
        # typeHint 사용 시 문자열 값으로 전달(TIMESTAMP/DECIMAL/DATE 등).
        param["value"] = {"stringValue": str(value)}
        param["typeHint"] = type_hint
        return param

    if isinstance(value, bool):
        param["value"] = {"booleanValue": value}
    elif isinstance(value, int):
        param["value"] = {"longValue": value}
    elif isinstance(value, (float, Decimal)):
        param["value"] = {"doubleValue": float(value)}
    else:
        param["value"] = {"stringValue": str(value)}
    return param


def row_to_parameters(row: dict, hints: dict[str, str] | None = None) -> list[dict]:
    """행 dict 를 SqlParameter 목록으로 변환. hints 는 컬럼별 typeHint."""
    hints = hints or {}
    return [to_param(k, v, type_hint=hints.get(k)) for k, v in row.items()]


class DataApiClient:
    """rds-data 클라이언트 래퍼. 트랜잭션/배치 실행 편의 메서드 제공."""

    def __init__(self, client, cluster_arn: str, secret_arn: str, database: str):
        self._client = client
        self._cluster_arn = cluster_arn
        self._secret_arn = secret_arn
        self._database = database

    @property
    def database(self) -> str:
        return self._database

    def _base_kwargs(self, database: str | None = None) -> dict:
        return {
            "resourceArn": self._cluster_arn,
            "secretArn": self._secret_arn,
            "database": database if database is not None else self._database,
        }

    def execute(
        self,
        sql: str,
        parameters: list[dict] | None = None,
        *,
        database: str | None = None,
        transaction_id: str | None = None,
    ) -> dict:
        kwargs = self._base_kwargs(database)
        kwargs["sql"] = sql
        if parameters:
            kwargs["parameters"] = parameters
        if transaction_id:
            kwargs["transactionId"] = transaction_id
        return self._client.execute_statement(**kwargs)

    def batch_execute(
        self,
        sql: str,
        parameter_sets: list[list[dict]],
        *,
        transaction_id: str | None = None,
    ) -> dict:
        kwargs = self._base_kwargs()
        kwargs["sql"] = sql
        kwargs["parameterSets"] = parameter_sets
        if transaction_id:
            kwargs["transactionId"] = transaction_id
        return self._client.batch_execute_statement(**kwargs)

    def begin_transaction(self) -> str:
        resp = self._client.begin_transaction(**self._base_kwargs())
        return resp["transactionId"]

    def commit_transaction(self, transaction_id: str) -> None:
        self._client.commit_transaction(
            resourceArn=self._cluster_arn,
            secretArn=self._secret_arn,
            transactionId=transaction_id,
        )

    def rollback_transaction(self, transaction_id: str) -> None:
        self._client.rollback_transaction(
            resourceArn=self._cluster_arn,
            secretArn=self._secret_arn,
            transactionId=transaction_id,
        )
