"""Per-user preferences repository.

Deliberately a separate table from usage. The usage repository is ADD-only —
every mutation is an `ADD` so concurrent turns cannot lose each other's
increments — and a layout is a `put_item` of a whole document. Mixing the two
would put a whole-value overwrite next to counters that must never be
overwritten, violating the usage table's invariant that the collection stage
depends on.

Keys are `pk = f"PREFS#{sub}"` and `sk = name` (e.g. `"insights-layout"`).
The value is nested under a single `value` attribute rather than being spread
over the item, so a stored key can never collide with `pk`, `sk`, or an
attribute added later.
"""
from typing import Any, Dict, Optional

from repositories.base import DynamoDBRepository


class PrefsRepository(DynamoDBRepository):
    """Per-user preference storage."""

    def get(self, sub: str, name: str) -> Optional[Dict[str, Any]]:
        """Retrieve a preference. Returns None if absent."""
        response = self.table.get_item(
            Key={"pk": f"PREFS#{sub}", "sk": name}
        )
        item = response.get("Item")
        if item is None:
            return None
        return item.get("value")

    def put(self, sub: str, name: str, value: Dict[str, Any]) -> None:
        """Store a preference, overwriting any previous value."""
        self.table.put_item(
            Item={
                "pk": f"PREFS#{sub}",
                "sk": name,
                "value": value,
            }
        )
