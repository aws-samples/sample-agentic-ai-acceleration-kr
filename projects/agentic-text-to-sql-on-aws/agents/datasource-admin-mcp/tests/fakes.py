"""단위 테스트용 fake (AWS 호출 없이 도구 로직 검증)."""

from __future__ import annotations

import json
from typing import Any


class ResourceExistsException(Exception):
    """Secrets Manager ResourceExistsException 대역."""


class ResourceNotFoundException(Exception):
    """Secrets Manager ResourceNotFoundException 대역."""


class FakeSecretsClient:
    """최소 in-memory secretsmanager client 대역."""

    class exceptions:  # noqa: N801 - boto3 client.exceptions 관례 모방
        ResourceExistsException = ResourceExistsException
        ResourceNotFoundException = ResourceNotFoundException

    def __init__(self) -> None:
        # name -> SecretString
        self.store: dict[str, str] = {}
        self.calls: list[str] = []

    @staticmethod
    def _arn(name: str) -> str:
        return f"arn:aws:secretsmanager:us-west-2:000000000000:secret:{name}-AbCdEf"

    def create_secret(self, Name: str, SecretString: str, Description: str = "") -> dict:  # noqa: N803
        self.calls.append("create_secret")
        if Name in self.store:
            raise ResourceExistsException(f"이미 존재: {Name}")
        self.store[Name] = SecretString
        return {"ARN": self._arn(Name), "Name": Name}

    def put_secret_value(self, SecretId: str, SecretString: str) -> dict:  # noqa: N803
        self.calls.append("put_secret_value")
        if SecretId not in self.store:
            raise ResourceNotFoundException(f"없음: {SecretId}")
        self.store[SecretId] = SecretString
        return {"ARN": self._arn(SecretId), "Name": SecretId}

    def describe_secret(self, SecretId: str) -> dict:  # noqa: N803
        self.calls.append("describe_secret")
        if SecretId not in self.store:
            raise ResourceNotFoundException(f"없음: {SecretId}")
        return {"ARN": self._arn(SecretId), "Name": SecretId}

    def get_secret_value(self, SecretId: str) -> dict:  # noqa: N803
        self.calls.append("get_secret_value")
        if SecretId not in self.store:
            raise ResourceNotFoundException(f"없음: {SecretId}")
        return {"ARN": self._arn(SecretId), "SecretString": self.store[SecretId]}


class FakeRepository:
    """SemanticRepository 대역 — put/get/list/publish/unpublish 만 지원."""

    def __init__(self) -> None:
        # (entity_type, entity_id) -> entity dict
        self.store: dict[tuple[str, str], dict[str, Any]] = {}
        self.puts: list[dict[str, Any]] = []

    def put_entity(
        self,
        entity_type: str,
        entity_id: str,
        payload: dict,
        status: str = "candidate",
        actor: str = "system",
    ) -> dict[str, Any]:
        key = (entity_type, entity_id)
        version = int(self.store.get(key, {}).get("version", 0)) + 1
        entity = {
            "pk": f"{entity_type}#{entity_id}",
            "sk": "v0",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "status": status,
            "version": version,
            "updated_at": "2026-07-26T00:00:00+00:00",
            "updated_by": actor,
            **payload,
        }
        self.store[key] = entity
        self.puts.append({"entity_type": entity_type, "entity_id": entity_id, "status": status})
        return entity

    def get_entity(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        return self.store.get((entity_type, entity_id))

    def list_entities(
        self, entity_type: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        results = [
            entity
            for entity in self.store.values()
            if (entity_type is None or entity["entity_type"] == entity_type)
            and (status is None or entity["status"] == status)
        ]
        return sorted(results, key=lambda e: (e["entity_type"], e["entity_id"]))

    def publish(self, entity_type: str, entity_id: str, actor: str = "system") -> dict[str, Any]:
        return self._set_status(entity_type, entity_id, "published", actor)

    def unpublish(self, entity_type: str, entity_id: str, actor: str = "system") -> dict[str, Any]:
        return self._set_status(entity_type, entity_id, "candidate", actor)

    def _set_status(
        self, entity_type: str, entity_id: str, status: str, actor: str
    ) -> dict[str, Any]:
        current = self.store.get((entity_type, entity_id))
        if current is None:
            raise KeyError(f"엔티티 없음: {entity_type}#{entity_id}")
        meta = {
            "pk",
            "sk",
            "entity_type",
            "entity_id",
            "status",
            "version",
            "updated_at",
            "updated_by",
        }
        payload = {k: v for k, v in current.items() if k not in meta}
        return self.put_entity(entity_type, entity_id, payload, status=status, actor=actor)


class FakeRdsDataClient:
    """rds-data client 대역 — SQL 별 응답을 미리 등록한다."""

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.responses = responses or {}
        self.sqls: list[str] = []

    def execute_statement(self, **kwargs: Any) -> dict:
        sql = kwargs["sql"]
        self.sqls.append(sql)
        for marker, response in self.responses.items():
            if marker in sql:
                if isinstance(response, Exception):
                    raise response
                return response
        return {"columnMetadata": [], "records": []}


class FakeRedshiftDataClient:
    """redshift-data client 대역 — execute → describe(FINISHED) → get_result 흐름."""

    def __init__(self, responses: dict[str, dict] | None = None, status: str = "FINISHED") -> None:
        self.responses = responses or {}
        self.status = status
        self.kwargs: list[dict[str, Any]] = []
        self._sql_by_id: dict[str, str] = {}
        self.cancelled: list[str] = []

    def execute_statement(self, **kwargs: Any) -> dict:
        self.kwargs.append(kwargs)
        statement_id = f"stmt-{len(self.kwargs)}"
        self._sql_by_id[statement_id] = kwargs["Sql"]
        return {"Id": statement_id}

    def describe_statement(self, Id: str) -> dict:  # noqa: N803
        return {"Status": self.status, "Error": "의도된 실패"}

    def get_statement_result(self, Id: str) -> dict:  # noqa: N803
        sql = self._sql_by_id[Id]
        for marker, response in self.responses.items():
            if marker in sql:
                return response
        return {"ColumnMetadata": [], "Records": []}

    def cancel_statement(self, Id: str) -> dict:  # noqa: N803
        self.cancelled.append(Id)
        return {}


def rds_rows(columns: list[str], rows: list[list[Any]]) -> dict:
    """rds-data ExecuteStatement 응답 형태로 변환."""
    return {
        "columnMetadata": [{"label": name} for name in columns],
        "records": [[_rds_field(value) for value in row] for row in rows],
    }


def redshift_rows(columns: list[str], rows: list[list[Any]]) -> dict:
    """redshift-data GetStatementResult 응답 형태로 변환."""
    return {
        "ColumnMetadata": [{"name": name} for name in columns],
        "Records": [[_rds_field(value) for value in row] for row in rows],
    }


def _rds_field(value: Any) -> dict:
    if value is None:
        return {"isNull": True}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"longValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def fake_embedder(text: str) -> list[float]:
    """결정적 1024차원 임베딩 대역(실제 값 무의미)."""
    return [float(len(text) % 7)] * 1024


class FakeBedrockClient:
    """bedrock-runtime InvokeModel 대역."""

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions
        self.calls: list[dict[str, Any]] = []

    def invoke_model(self, modelId: str, body: str) -> dict:  # noqa: N803
        parsed = json.loads(body)
        self.calls.append({"modelId": modelId, **parsed})
        return {"body": json.dumps({"embedding": [0.1] * self.dimensions}).encode()}
