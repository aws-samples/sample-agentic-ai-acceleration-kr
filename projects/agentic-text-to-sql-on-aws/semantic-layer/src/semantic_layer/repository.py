"""semantic layer system-of-record CRUD (DynamoDB, 단일 쓰기 지점).

semantic 지식(term/fewshot/table/column/join)을 DynamoDB 한 테이블에 저장하고
항목 단위로 버전을 관리한다(ARCHITECTURE §4.4 / §5.3 ② 축). dual-write 금지 —
DynamoDB 만 쓰고, 파생 저장소(OpenSearch/Neptune)는 Streams 로 단방향 동기화된다.

키 설계
-------
- ``pk`` = ``{entity_type}#{entity_id}``  (entity_type ∈ term|fewshot|table|column|join)
- ``sk`` = ``v0``(최신본) | ``v{n}``(n≥1, 버전 이력)

쓰기 규칙
--------
``put_entity`` 는 항상 최신본 ``v0`` 에 조건부 쓰기(version 증가)를 하고, 직전 본을
``v{n}`` 으로 복사해 이력을 남긴다. 최초 생성은 ``attribute_not_exists(pk)`` 로,
갱신은 ``version = <직전값>`` 낙관적 잠금으로 동시성 충돌을 방어한다.

임베딩
------
term/fewshot 은 hybrid 검색을 위해 임베딩이 필요하다. 생성자에 주입된 embedder
콜러블(``str -> list[float]``)로 쓰기 시점에 계산해 페이로드에 포함한다.
이미 ``embedding`` 이 페이로드에 있으면 재계산하지 않는다(status 전환 시 보존).

boto3 클라이언트/embedder 는 생성자로 주입 가능하다(단위 테스트용 fake 주입).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

# 최신본을 가리키는 정렬 키.
LATEST_SK = "v0"

# 쓰기 시점에 임베딩을 계산해야 하는 entity_type.
EMBED_ENTITIES = frozenset({"term", "fewshot"})

# 페이로드와 구분되는 공통 메타 속성(재기록 시 payload 에서 제외).
META_FIELDS = frozenset(
    {"pk", "sk", "entity_type", "entity_id", "status", "version", "updated_at", "updated_by"}
)

# M4 additive: "datasource" — admin panel 이 등록한 데이터소스 연결 메타(자격증명 제외).
# graph_sync 는 미지원 타입에 빈 statement 를 반환하므로 그래프 동기화에 영향이 없다.
VALID_ENTITY_TYPES = frozenset({"term", "fewshot", "table", "column", "join", "datasource"})

# M5 additive: "rejected" — 승인 큐에서 반려된 항목(§9.1). published 가 아니므로
# 파생 저장소(OpenSearch/Neptune)에는 노출되지 않는다(graph_sync 는 삭제 경로로 처리).
# candidate 와 구분해 두면 채굴기가 반려된 후보를 재적재하지 않고, 반려 이력도 조회 가능하다.
VALID_STATUSES = frozenset({"candidate", "published", "rejected"})


def _utcnow_iso() -> str:
    """현재 UTC 시각의 ISO8601 문자열."""
    return datetime.now(UTC).isoformat()


# --- DynamoDB AttributeValue 직렬화 ------------------------------------------
# 저수준 client 를 쓰므로 값을 AttributeValue({"S": ...}) 형식으로 변환한다.
# graph_sync 의 Streams 레코드 파싱과 동일한 표현을 공유한다.


def to_attribute_value(value: Any) -> dict:
    """파이썬 값 → DynamoDB AttributeValue."""
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float, Decimal)):
        return {"N": _num_str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, dict):
        return {"M": {k: to_attribute_value(v) for k, v in value.items()}}
    if isinstance(value, Iterable):
        return {"L": [to_attribute_value(v) for v in value]}
    raise TypeError(f"지원하지 않는 값 타입: {type(value)!r}")


def from_attribute_value(av: dict) -> Any:
    """DynamoDB AttributeValue → 파이썬 값."""
    if "NULL" in av:
        return None
    if "BOOL" in av:
        return av["BOOL"]
    if "S" in av:
        return av["S"]
    if "N" in av:
        return _parse_num(av["N"])
    if "M" in av:
        return {k: from_attribute_value(v) for k, v in av["M"].items()}
    if "L" in av:
        return [from_attribute_value(v) for v in av["L"]]
    raise TypeError(f"지원하지 않는 AttributeValue: {av!r}")


def _num_str(value: int | float | Decimal) -> str:
    """숫자를 DynamoDB N 문자열로. float 는 Decimal 경유로 표기 안정화."""
    if isinstance(value, bool):  # bool 은 int 하위타입이므로 방어.
        raise TypeError("bool 은 N 으로 변환할 수 없다")
    if isinstance(value, float):
        return str(Decimal(str(value)))
    return str(value)


def _parse_num(text: str) -> int | float:
    """DynamoDB N 문자열 → int(정수) 또는 float."""
    if "." in text or "e" in text or "E" in text:
        return float(text)
    return int(text)


def item_to_dict(item: dict) -> dict:
    """AttributeValue map(item) → 평범한 파이썬 dict."""
    return {k: from_attribute_value(v) for k, v in item.items()}


def dict_to_item(data: dict) -> dict:
    """평범한 dict → AttributeValue map(item)."""
    return {k: to_attribute_value(v) for k, v in data.items()}


class SemanticRepository:
    """DynamoDB 기반 semantic system-of-record 저장소."""

    def __init__(
        self,
        table_name: str,
        *,
        region: str = "us-west-2",
        client: Any | None = None,
        embedder: Callable[[str], list[float]] | None = None,
        clock: Callable[[], str] = _utcnow_iso,
    ):
        self._table_name = table_name
        self._embedder = embedder
        self._clock = clock
        self._client = client
        self._region = region

    @property
    def client(self):
        """boto3 dynamodb client(지연 생성)."""
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self._region)
        return self._client

    # --- 키 헬퍼 -------------------------------------------------------------
    @staticmethod
    def make_pk(entity_type: str, entity_id: str) -> str:
        return f"{entity_type}#{entity_id}"

    # --- 읽기 ---------------------------------------------------------------
    def _get_raw(self, pk: str, sk: str) -> dict | None:
        resp = self.client.get_item(
            TableName=self._table_name,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        return item or None

    def get_entity(self, entity_type: str, entity_id: str) -> dict | None:
        """최신본(v0) 을 평범한 dict 로 반환. 없으면 None."""
        raw = self._get_raw(self.make_pk(entity_type, entity_id), LATEST_SK)
        return item_to_dict(raw) if raw is not None else None

    def list_entities(
        self, entity_type: str | None = None, status: str | None = None
    ) -> list[dict]:
        """최신본(v0) 목록을 반환(scan + python 필터, 데모 규모 충분).

        entity_type/status 로 필터 가능. 버전 이력(v{n≥1})은 제외한다.
        """
        results: list[dict] = []
        start_key: dict | None = None
        while True:
            kwargs: dict[str, Any] = {"TableName": self._table_name}
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            resp = self.client.scan(**kwargs)
            for raw in resp.get("Items", []):
                data = item_to_dict(raw)
                if data.get("sk") != LATEST_SK:
                    continue
                if entity_type is not None and data.get("entity_type") != entity_type:
                    continue
                if status is not None and data.get("status") != status:
                    continue
                results.append(data)
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
        results.sort(key=lambda d: (d.get("entity_type", ""), d.get("entity_id", "")))
        return results

    # --- 쓰기 ---------------------------------------------------------------
    def put_entity(
        self,
        entity_type: str,
        entity_id: str,
        payload: dict,
        status: str = "candidate",
        actor: str = "system",
    ) -> dict:
        """최신본을 조건부로 갱신하고 직전 본을 이력으로 복사. 새 v0 를 반환.

        - 최초: ``attribute_not_exists(pk)`` 로 v0(version=1) 생성.
        - 갱신: 직전 v0 를 ``v{version}`` 으로 복사 후, ``version = <직전값>`` 낙관적
          잠금으로 새 v0(version+1) 를 기록.
        term/fewshot 은 payload 에 ``embedding`` 이 없으면 embedder 로 계산해 포함한다.
        """
        if entity_type not in VALID_ENTITY_TYPES:
            raise ValueError(f"알 수 없는 entity_type: {entity_type!r}")
        if status not in VALID_STATUSES:
            raise ValueError(f"알 수 없는 status: {status!r}")

        payload = dict(payload)
        if entity_type in EMBED_ENTITIES and payload.get("embedding") is None:
            payload["embedding"] = self._compute_embedding(entity_type, payload)

        pk = self.make_pk(entity_type, entity_id)
        current = self._get_raw(pk, LATEST_SK)

        if current is None:
            version = 1
            item = self._build_item(pk, entity_type, entity_id, payload, status, version, actor)
            self.client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(pk)",
            )
            return item_to_dict(item)

        old_version = int(from_attribute_value(current["version"]))
        # 직전 본을 이력(v{old_version}) 으로 복사.
        history = dict(current)
        history["sk"] = {"S": f"v{old_version}"}
        self.client.put_item(
            TableName=self._table_name,
            Item=history,
            ConditionExpression="attribute_not_exists(sk)",
        )
        # 새 최신본을 낙관적 잠금으로 기록.
        new_version = old_version + 1
        item = self._build_item(pk, entity_type, entity_id, payload, status, new_version, actor)
        self.client.put_item(
            TableName=self._table_name,
            Item=item,
            ConditionExpression="version = :expected",
            ExpressionAttributeValues={":expected": {"N": str(old_version)}},
        )
        return item_to_dict(item)

    def publish(self, entity_type: str, entity_id: str, actor: str = "system") -> dict:
        """status 를 published 로 전환(put_entity 경유 → 버전 증가)."""
        return self._set_status(entity_type, entity_id, "published", actor)

    def unpublish(self, entity_type: str, entity_id: str, actor: str = "system") -> dict:
        """status 를 candidate 로 전환(put_entity 경유 → 버전 증가).

        rejected 항목에도 적용 가능하다(반려 → 재검토 큐 복귀 경로).
        """
        return self._set_status(entity_type, entity_id, "candidate", actor)

    def reject(
        self,
        entity_type: str,
        entity_id: str,
        reason: str = "",
        actor: str = "system",
    ) -> dict:
        """status 를 rejected 로 전환하고 반려 사유를 payload 에 기록(M5 §9.1).

        ``reason`` 이 비어 있지 않으면 payload 에 ``rejection_reason`` 으로 남긴다
        (빈 문자열이면 키를 추가하지 않는다 — 불필요한 필드 오염 방지).
        rejected 는 published 가 아니므로 파생 저장소에서는 제거된다.
        ``publish``/``unpublish`` 로 반려 후 재승인·재검토가 가능하다.
        """
        extra = {"rejection_reason": reason} if reason else None
        return self._set_status(entity_type, entity_id, "rejected", actor, extra=extra)

    def _set_status(
        self,
        entity_type: str,
        entity_id: str,
        status: str,
        actor: str,
        extra: dict | None = None,
    ) -> dict:
        current = self.get_entity(entity_type, entity_id)
        if current is None:
            raise KeyError(f"엔티티 없음: {entity_type}#{entity_id}")
        payload = {k: v for k, v in current.items() if k not in META_FIELDS}
        if extra:
            payload.update(extra)
        # embedding 은 payload 에 남아 재계산되지 않는다(status 전환은 내용 불변).
        return self.put_entity(entity_type, entity_id, payload, status=status, actor=actor)

    # --- 내부 ---------------------------------------------------------------
    def _build_item(
        self,
        pk: str,
        entity_type: str,
        entity_id: str,
        payload: dict,
        status: str,
        version: int,
        actor: str,
    ) -> dict:
        data: dict[str, Any] = {
            "pk": pk,
            "sk": LATEST_SK,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "status": status,
            "version": version,
            "updated_at": self._clock(),
            "updated_by": actor,
        }
        for key, value in payload.items():
            if key in ("pk", "sk"):
                continue
            data[key] = value
        return dict_to_item(data)

    def _compute_embedding(self, entity_type: str, payload: dict) -> list[float]:
        if self._embedder is None:
            raise ValueError(
                f"{entity_type} 쓰기에는 embedder 가 필요하다(생성자에 주입)."
            )
        return self._embedder(self._embedding_text(entity_type, payload))

    @staticmethod
    def _embedding_text(entity_type: str, payload: dict) -> str:
        """임베딩 대상 텍스트 구성(hybrid 검색 매칭 품질용)."""
        if entity_type == "term":
            synonyms = payload.get("synonyms") or []
            syn_text = f" 동의어: {', '.join(synonyms)}." if synonyms else ""
            return f"{payload.get('term', '')}. {payload.get('definition', '')}{syn_text}".strip()
        if entity_type == "fewshot":
            return str(payload.get("question", "")).strip()
        return ""
