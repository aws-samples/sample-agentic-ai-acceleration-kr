"""Who has signed in through an OIDC provider, written on each login.

Cognito users need no such record: the pool itself is the directory
(services/directory_service.py reads it with ListUsers). An Entra user has no
pool here, and the ledger keys people by `sub` on purpose, so the only place
the admin Insights views can get a name for them is a record this writes on
login. Roles are not read back from it — they come from the token every
request.

Keys: `pk = f"{provider}#{sub}"`. One table, PAY_PER_REQUEST, no index.
"""
from typing import Any, Dict, List, Optional

from models.knowledge import now_iso
from repositories.base import DynamoDBRepository


class UserRepository(DynamoDBRepository):
    @staticmethod
    def _pk(provider: str, sub: str) -> str:
        return f"{provider}#{sub}"

    def upsert_on_login(
        self,
        *,
        sub: str,
        provider: str,
        email: str,
        name: str,
        groups: List[str],
        role: str,
    ) -> None:
        now = now_iso()
        self.table.update_item(
            Key={"pk": self._pk(provider, sub)},
            UpdateExpression=(
                "SET #sub = :sub, #provider = :provider, #email = :email, "
                "#display_name = :display_name, #groups = :groups, #role = :role, "
                "#last_login = :now, #updated_at = :now, "
                "#created_at = if_not_exists(#created_at, :now)"
            ),
            ExpressionAttributeNames={
                "#sub": "sub",
                "#provider": "provider",
                "#email": "email",
                "#display_name": "display_name",
                "#groups": "groups",
                "#role": "role",
                "#last_login": "last_login",
                "#updated_at": "updated_at",
                "#created_at": "created_at",
            },
            ExpressionAttributeValues={
                ":sub": sub,
                ":provider": provider,
                ":email": email,
                ":display_name": name,
                ":groups": groups,
                ":role": role,
                ":now": now,
            },
        )

    def get(self, provider: str, sub: str) -> Optional[Dict[str, Any]]:
        item = self.table.get_item(Key={"pk": self._pk(provider, sub)}).get("Item")
        return item if item else None

    def labels(self) -> Dict[str, str]:
        """sub -> email (or display name) for everyone recorded. A full scan: the
        table holds one row per person who ever signed in, read once per
        directory cache window (services/directory_service.py)."""
        out: Dict[str, str] = {}
        kwargs: Dict[str, Any] = {"ProjectionExpression": "#sub, #email, #display_name",
                                  "ExpressionAttributeNames": {"#sub": "sub", "#email": "email", "#display_name": "display_name"}}
        while True:
            resp = self.table.scan(**kwargs)
            for item in resp.get("Items") or []:
                sub = item.get("sub")
                label = item.get("email") or item.get("display_name")
                if sub and label:
                    out[str(sub)] = str(label)
            key = resp.get("LastEvaluatedKey")
            if not key:
                return out
            kwargs["ExclusiveStartKey"] = key
