"""Teams and the org audit ledger — one table, two key shapes.

`pk = "TEAM", sk = <name>`: one row per team, platform-made or written from outside.
Listing every team is one Query; there is no Scan here.

`pk = "AUDIT#<yyyy-mm>", sk = "<iso-ts>#<id>"`: what admins did on the
Organization page. Month-sharded so a range read is one Query per month and the
sort key orders by time; the id suffix keeps two writes in the same millisecond
apart.

Membership is NOT stored here (see services/org_directory_service.py).
"""
import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key

from repositories.base import DynamoDBRepository

TEAM_PK = "TEAM"


def _to_dynamo(item: Dict[str, Any]) -> Dict[str, Any]:
    """boto3's resource layer rejects Python floats; numbers go in as Decimal."""
    return json.loads(json.dumps(item), parse_float=Decimal)


class TeamRepository(DynamoDBRepository):
    def list_teams(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(TEAM_PK)}
        while True:
            resp = self.table.query(**kwargs)
            items.extend(self._strip(i) for i in resp.get("Items") or [])
            key = resp.get("LastEvaluatedKey")
            if not key:
                return items
            kwargs["ExclusiveStartKey"] = key

    def get_team(self, name: str) -> Optional[Dict[str, Any]]:
        item = self.table.get_item(Key={"pk": TEAM_PK, "sk": name}).get("Item")
        return self._strip(item) if item else None

    def put_team(self, item: Dict[str, Any]) -> None:
        row = {k: v for k, v in item.items() if v is not None}
        row["pk"], row["sk"] = TEAM_PK, item["name"]
        self.table.put_item(Item=_to_dynamo(row))

    def delete_team(self, name: str) -> None:
        self.table.delete_item(Key={"pk": TEAM_PK, "sk": name})

    def append_audit(self, entry: Dict[str, Any]) -> None:
        at = str(entry["at"])
        row = {k: v for k, v in entry.items() if v is not None}
        row["pk"] = f"AUDIT#{at[:7]}"
        row["sk"] = f"{at}#{entry['id']}"
        self.table.put_item(Item=_to_dynamo(row))

    def list_audit(self, month: str, start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"AUDIT#{month}") & Key("sk").between(start_iso, end_iso + "#~"),
            "ScanIndexForward": False,
        }
        while True:
            resp = self.table.query(**kwargs)
            items.extend(self._strip(i) for i in resp.get("Items") or [])
            key = resp.get("LastEvaluatedKey")
            if not key:
                return items
            kwargs["ExclusiveStartKey"] = key

    @staticmethod
    def _strip(item: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in item.items() if k not in ("pk", "sk")}
