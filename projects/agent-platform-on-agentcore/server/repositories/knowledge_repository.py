"""Knowledge-base repository — one DynamoDB item per user-created knowledge base."""
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from boto3.dynamodb.conditions import Attr, Key

from models.knowledge import SOURCE_UPLOAD, KnowledgeBaseRecord, now_iso
from repositories.base import DynamoDBRepository

OWNER_INDEX = "owner_id-created_at-index"

# Attributes the caller may set through `update`. Anything else is either the key
# or set once at creation.
_MUTABLE_FIELDS = {
    "shared",
    "name",
    "description",
    "status",
    "failure_reason",
    "kb_id",
    "data_source_id",
    "gateway_id",
    "gateway_arn",
    "target_id",
}


def _plain(value: Any) -> Any:
    """DynamoDB's Decimal back to int/float, recursively.

    `source_config` is handed to boto3 as connector parameters, and a Decimal there
    is not JSON-serialisable.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _plain(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_plain(inner) for inner in value]
    return value


def _to_model(item: Dict[str, Any]) -> KnowledgeBaseRecord:
    return KnowledgeBaseRecord(
        kb_key=item["kb_key"],
        owner_id=item.get("owner_id", ""),
        owner_name=item.get("owner_name", ""),
        shared=bool(item.get("shared", False)),
        name=item.get("name", item["kb_key"]),
        description=item.get("description") or None,
        # Absent on records written before source types existed, which is what
        # those records are.
        source_type=item.get("source_type") or SOURCE_UPLOAD,
        source_config=_plain(item.get("source_config")) or {},
        status=item.get("status", ""),
        failure_reason=item.get("failure_reason") or None,
        kb_id=item.get("kb_id") or None,
        data_source_id=item.get("data_source_id") or None,
        gateway_id=item.get("gateway_id") or None,
        gateway_arn=item.get("gateway_arn") or None,
        target_id=item.get("target_id") or None,
        created_at=item.get("created_at", ""),
        updated_at=item.get("updated_at", ""),
    )


class KnowledgeRepository(DynamoDBRepository):
    """Ownership and provisioning progress for knowledge bases."""

    def put(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        item: Dict[str, Any] = {
            "kb_key": record.kb_key,
            "owner_id": record.owner_id,
            "owner_name": record.owner_name,
            "shared": record.shared,
            "name": record.name,
            "source_type": record.source_type,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        # Empty values are left out rather than written as null, so a record only
        # carries the ids and settings it actually has. `source_type` is above
        # instead: it always has a value, and omitting it would make an S3
        # knowledge base read back as an upload one.
        for field in ("description", "kb_id", "data_source_id", "gateway_id",
                      "gateway_arn", "target_id", "failure_reason",
                      "source_config"):
            value = getattr(record, field)
            if value:
                item[field] = value
        self.table.put_item(Item=item)
        return record

    def get(self, kb_key: str) -> Optional[KnowledgeBaseRecord]:
        item = self.table.get_item(Key={"kb_key": kb_key}).get("Item")
        return _to_model(item) if item else None

    def update(self, kb_key: str, **fields: Any) -> Optional[KnowledgeBaseRecord]:
        """Set the given fields and stamp `updated_at`.

        An UpdateExpression rather than a put_item: the provisioner records one
        resource id per step, and rewriting the whole item from an in-memory copy
        would erase whatever a concurrent driver wrote in the meantime.
        """
        updates = {k: v for k, v in fields.items() if k in _MUTABLE_FIELDS}
        updates["updated_at"] = now_iso()

        # Placeholders named after the fields, which reads far better in a
        # CloudWatch line than `#f0 = :f0`. Safe because every key has already
        # passed the _MUTABLE_FIELDS whitelist above — a caller cannot inject an
        # expression fragment through a field name.
        names = {f"#{field}": field for field in updates}
        values = {f":{field}": value for field, value in updates.items()}
        assignments = [f"#{field} = :{field}" for field in updates]

        response = self.table.update_item(
            Key={"kb_key": kb_key},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        item = response.get("Attributes")
        return _to_model(item) if item else None

    def list_for_owner(self, owner_id: str) -> List[KnowledgeBaseRecord]:
        response = self.table.query(
            IndexName=OWNER_INDEX,
            KeyConditionExpression=Key("owner_id").eq(owner_id),
            ScanIndexForward=False,
        )
        return [_to_model(item) for item in response.get("Items", [])]

    def list_shared(self) -> List[KnowledgeBaseRecord]:
        """Every knowledge base an admin has published.

        A scan rather than an index: `shared` has two values, so a GSI on it
        would be one hot partition. Revisit if this grows past a few hundred
        knowledge bases.
        """
        records: List[KnowledgeBaseRecord] = []
        params: Dict[str, Any] = {"FilterExpression": Attr("shared").eq(True)}
        while True:
            response = self.table.scan(**params)
            records.extend(_to_model(item) for item in response.get("Items", []))
            token = response.get("LastEvaluatedKey")
            if not token:
                break
            params["ExclusiveStartKey"] = token
        return records

    def visible_to(self, owner_id: str) -> List[KnowledgeBaseRecord]:
        """The caller's own knowledge bases plus every shared one, newest first."""
        by_key = {r.kb_key: r for r in self.list_for_owner(owner_id)}
        for record in self.list_shared():
            by_key.setdefault(record.kb_key, record)
        return sorted(by_key.values(), key=lambda r: r.created_at, reverse=True)

    def all_gateway_arns(self) -> Set[str]:
        """Gateway ARNs owned by knowledge bases, for the registry-sync exclusion."""
        arns: Set[str] = set()
        params: Dict[str, Any] = {"ProjectionExpression": "gateway_arn"}
        while True:
            response = self.table.scan(**params)
            for item in response.get("Items", []):
                arn = item.get("gateway_arn")
                if arn:
                    arns.add(arn)
            token = response.get("LastEvaluatedKey")
            if not token:
                break
            params["ExclusiveStartKey"] = token
        return arns

    def delete(self, kb_key: str) -> None:
        self.table.delete_item(Key={"kb_key": kb_key})
