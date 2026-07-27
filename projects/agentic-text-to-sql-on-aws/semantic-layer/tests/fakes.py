"""단위 테스트용 fake (AWS 호출 없이 repository/graph 로직 검증)."""

from __future__ import annotations

from typing import Any


class ConditionalCheckFailed(Exception):
    """DynamoDB ConditionalCheckFailedException 대역."""


class FakeDynamoClient:
    """조건부 쓰기를 지원하는 최소 in-memory DynamoDB client 대역.

    ConditionExpression 중 이 코드가 쓰는 세 형태만 해석한다.
    - ``attribute_not_exists(pk)``
    - ``attribute_not_exists(sk)``
    - ``version = :expected``
    """

    class exceptions:  # noqa: N801 - boto3 client.exceptions 관례 모방
        ConditionalCheckFailedException = ConditionalCheckFailed

    def __init__(self):
        # (pk, sk) -> item(AttributeValue map)
        self.store: dict[tuple[str, str], dict] = {}

    # --- 내부 헬퍼 ---
    @staticmethod
    def _key(item_or_key: dict) -> tuple[str, str]:
        return (item_or_key["pk"]["S"], item_or_key["sk"]["S"])

    def _check_condition(
        self, key: tuple[str, str], expr: str | None, values: dict | None
    ) -> None:
        if not expr:
            return
        existing = self.store.get(key)
        if expr == "attribute_not_exists(pk)":
            if existing is not None:
                raise ConditionalCheckFailed(f"pk 이미 존재: {key}")
        elif expr == "attribute_not_exists(sk)":
            if existing is not None:
                raise ConditionalCheckFailed(f"sk 이미 존재: {key}")
        elif expr == "version = :expected":
            if existing is None:
                raise ConditionalCheckFailed("version 조건 대상 없음")
            expected = values[":expected"]["N"]
            if existing["version"]["N"] != expected:
                raise ConditionalCheckFailed("version 불일치")
        else:  # pragma: no cover - 예상 밖 조건식
            raise AssertionError(f"미지원 ConditionExpression: {expr}")

    # --- boto3 dynamodb client 인터페이스 ---
    def put_item(
        self,
        TableName: str,  # noqa: N803 - boto3 케이싱
        Item: dict,  # noqa: N803
        ConditionExpression: str | None = None,  # noqa: N803
        ExpressionAttributeValues: dict | None = None,  # noqa: N803
    ) -> dict:
        key = self._key(Item)
        self._check_condition(key, ConditionExpression, ExpressionAttributeValues)
        self.store[key] = Item
        return {}

    def get_item(
        self,
        TableName: str,  # noqa: N803
        Key: dict,  # noqa: N803
        ConsistentRead: bool = False,  # noqa: N803
    ) -> dict:
        item = self.store.get(self._key(Key))
        return {"Item": item} if item is not None else {}

    def scan(
        self,
        TableName: str,  # noqa: N803
        ExclusiveStartKey: dict | None = None,  # noqa: N803
    ) -> dict:
        return {"Items": list(self.store.values())}


class FakeNeptuneClient:
    """execute_open_cypher_query 호출을 기록하는 neptunedata 대역."""

    def __init__(self, *, fail_on: str | None = None):
        self.calls: list[dict[str, Any]] = []
        self._fail_on = fail_on

    def execute_open_cypher_query(self, *, openCypherQuery: str, parameters: str) -> dict:  # noqa: N803
        if self._fail_on and (self._fail_on in openCypherQuery or self._fail_on in parameters):
            raise RuntimeError(f"의도된 실패: {self._fail_on}")
        self.calls.append({"query": openCypherQuery, "parameters": parameters})
        return {"results": []}


def fake_embedder(text: str) -> list[float]:
    """결정적 1024차원 임베딩 대역(내용 길이 기반, 실제 값 무의미)."""
    return [float(len(text) % 7)] * 1024
