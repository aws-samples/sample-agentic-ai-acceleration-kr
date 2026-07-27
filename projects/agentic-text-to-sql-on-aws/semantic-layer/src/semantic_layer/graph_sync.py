"""DynamoDB Streams 레코드 → Neptune openCypher 동기화.

순수 변환 로직(``record_to_cypher``)과 실행기(``NeptuneGraphClient``)를 분리한다.
변환은 AWS 호출 없이 단위 테스트 가능하고, 실행기는 boto3 ``neptunedata`` 클라이언트를
지연 생성한다. dual-write 금지 규율에 따라 이 경로는 DynamoDB → Neptune 단방향이다.

동기화 규칙 (ARCHITECTURE §4.4)
------------------------------
- ``sk != "v0"`` (버전 이력) 레코드는 무시 → 빈 리스트.
- ``INSERT``/``MODIFY`` 의 NewImage 가 ``published`` → MERGE 멱등 upsert.
- NewImage 가 ``published`` 이외(``candidate``/``rejected`` — published 에서의 강등 포함)
  또는 ``REMOVE`` → 노드/엣지 삭제. candidate/rejected 는 에이전트에 노출되지 않아야
  하므로 그래프에서 제거한다(`rejected` 상태도 이 경로로 처리된다).

그래프 모델
----------
- 노드: ``(:Table {name})``, ``(:Column {name, table, key})``, ``(:Term {name})``
- 엣지: ``(Table)-[:HAS_COLUMN]->(Column)``, ``(Table)-[:JOINS {on}]->(Table)``,
  ``(Term)-[:MAPS_TO]->(Table|Column)``
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .repository import LATEST_SK, from_attribute_value


@dataclass(frozen=True)
class CypherStatement:
    """실행할 openCypher 문 하나. parameters 는 평범한 dict(실행기가 json 직렬화)."""

    query: str
    parameters: dict[str, Any] = field(default_factory=dict)


def _image_to_dict(image: dict | None) -> dict:
    """DynamoDB Streams 이미지(AttributeValue map) → 평범한 dict."""
    if not image:
        return {}
    return {k: from_attribute_value(v) for k, v in image.items()}


def _sk_of(record: dict) -> str | None:
    """레코드의 sk 를 Keys → NewImage → OldImage 순으로 추출."""
    ddb = record.get("dynamodb", {})
    for src in ("Keys", "NewImage", "OldImage"):
        image = ddb.get(src)
        if image and "sk" in image:
            return from_attribute_value(image["sk"])
    return None


def _upsert_statements(entity_type: str, data: dict) -> list[CypherStatement]:
    """published 엔티티의 멱등 MERGE 문 목록."""
    if entity_type == "table":
        name = data.get("table") or data.get("entity_id")
        return [CypherStatement("MERGE (t:Table {name: $name})", {"name": name})]

    if entity_type == "column":
        table = data.get("table")
        column = data.get("column")
        key = f"{table}.{column}"
        return [
            CypherStatement(
                "MERGE (t:Table {name: $table}) "
                "MERGE (c:Column {key: $key}) "
                "SET c.name = $column, c.table = $table "
                "MERGE (t)-[:HAS_COLUMN]->(c)",
                {"table": table, "column": column, "key": key},
            )
        ]

    if entity_type == "join":
        left = data.get("left_table")
        right = data.get("right_table")
        on = data.get("join_on")
        return [
            CypherStatement(
                "MERGE (l:Table {name: $left}) "
                "MERGE (r:Table {name: $right}) "
                "MERGE (l)-[j:JOINS]->(r) "
                "SET j.on = $on",
                {"left": left, "right": right, "on": on},
            )
        ]

    if entity_type == "term":
        name = data.get("term") or data.get("entity_id")
        stmts = [CypherStatement("MERGE (t:Term {name: $name})", {"name": name})]
        for target in data.get("maps_to") or []:
            table = target.get("table")
            column = target.get("column")
            if column:
                stmts.append(
                    CypherStatement(
                        "MATCH (t:Term {name: $name}) "
                        "MATCH (c:Column {key: $key}) "
                        "MERGE (t)-[:MAPS_TO]->(c)",
                        {"name": name, "key": f"{table}.{column}"},
                    )
                )
            elif table:
                stmts.append(
                    CypherStatement(
                        "MATCH (t:Term {name: $name}) "
                        "MATCH (tb:Table {name: $table}) "
                        "MERGE (t)-[:MAPS_TO]->(tb)",
                        {"name": name, "table": table},
                    )
                )
        return stmts

    return []


def _delete_statements(entity_type: str, data: dict) -> list[CypherStatement]:
    """candidate/rejected 강등 또는 REMOVE 시 노드·엣지 삭제 문 목록."""
    if entity_type == "table":
        name = data.get("table") or data.get("entity_id")
        return [CypherStatement("MATCH (t:Table {name: $name}) DETACH DELETE t", {"name": name})]

    if entity_type == "column":
        key = f"{data.get('table')}.{data.get('column')}"
        return [CypherStatement("MATCH (c:Column {key: $key}) DETACH DELETE c", {"key": key})]

    if entity_type == "term":
        name = data.get("term") or data.get("entity_id")
        return [CypherStatement("MATCH (t:Term {name: $name}) DETACH DELETE t", {"name": name})]

    if entity_type == "join":
        left = data.get("left_table")
        right = data.get("right_table")
        return [
            CypherStatement(
                "MATCH (:Table {name: $left})-[j:JOINS]->(:Table {name: $right}) DELETE j",
                {"left": left, "right": right},
            )
        ]

    return []


def record_to_cypher(record: dict) -> list[CypherStatement]:
    """단일 Streams 레코드 → openCypher 문 목록(순수 함수).

    최신본(v0)이 아니거나 매핑 불가한 레코드는 빈 리스트를 반환한다.
    """
    if _sk_of(record) != LATEST_SK:
        return []

    event = record.get("eventName")
    ddb = record.get("dynamodb", {})

    if event == "REMOVE":
        data = _image_to_dict(ddb.get("OldImage"))
        entity_type = data.get("entity_type")
        return _delete_statements(entity_type, data) if entity_type else []

    if event in ("INSERT", "MODIFY"):
        data = _image_to_dict(ddb.get("NewImage"))
        entity_type = data.get("entity_type")
        if not entity_type:
            return []
        if data.get("status") == "published":
            return _upsert_statements(entity_type, data)
        # published 이외(candidate/rejected — 신규 또는 published 에서의 강등)는 그래프에서 제거.
        return _delete_statements(entity_type, data)

    return []


class NeptuneGraphClient:
    """Neptune(openCypher) 실행기. neptunedata 클라이언트를 지연 생성한다."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        region: str = "us-west-2",
        client: Any | None = None,
    ):
        self._endpoint = endpoint
        self._region = region
        self._client = client

    @property
    def client(self):
        """boto3 neptunedata client(지연 생성)."""
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "neptunedata", endpoint_url=self._endpoint, region_name=self._region
            )
        return self._client

    def execute(self, statements: list[CypherStatement]) -> int:
        """openCypher 문들을 순차 실행하고 실행 건수를 반환."""
        count = 0
        for stmt in statements:
            self.client.execute_open_cypher_query(
                openCypherQuery=stmt.query,
                parameters=json.dumps(stmt.parameters),
            )
            count += 1
        return count
