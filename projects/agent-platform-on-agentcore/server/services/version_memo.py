"""Remember per-item Get results for as long as the List says the item is unchanged.

Several AgentCore List APIs omit the fields a listing needs — ListHarnesses has
no companion runtime, model or tools; ListAgentRuntimes no environment;
ListGateways no ARN or URL; ListRegistryRecords no descriptors — so every
listing fanned out one Get per item, on every request. Measured against the
live stack, `GET /api/registry/deployed` made 54 AWS calls, 46 of them Gets,
each time it was opened.

What a Get returns is fixed for a given revision of the item, and each of
those List APIs reports the revision: a version, `updatedAt` and status. So the
List still runs on every request — a record registered a second ago is listed a
second later, with no TTL to wait out — and only the Get is skipped, when the
List-reported key matches the one the cached result was read under. A status
change (CREATING → READY, DRAFT → APPROVED) is part of the key, so a Get made
mid-transition is never served after it.

Only use this for control-plane Gets, which read the revision the List just
reported. The registry *data* plane is indexed asynchronously, 10-30s behind an
update; a result memoised in that window would stay stale until the next edit.
"""
import threading
from typing import Any, Dict, Generic, Hashable, Optional, Tuple, TypeVar

_V = TypeVar("_V")


class VersionMemo(Generic[_V]):
    """`item id -> (revision key, value)`; a lookup hits only on an equal key.

    One entry per id, overwritten when the revision moves, so the memo holds at
    most one value per item ever listed. Callers store successful reads only: a
    failed Get must be retried on the next listing, not remembered as empty.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, Tuple[Hashable, _V]] = {}

    def get(self, item_id: str, key: Optional[Hashable]) -> Optional[_V]:
        if key is None:
            return None
        with self._lock:
            hit = self._items.get(item_id)
        if hit is None or hit[0] != key:
            return None
        return hit[1]

    def put(self, item_id: str, key: Optional[Hashable], value: _V) -> None:
        if key is None:
            return
        with self._lock:
            self._items[item_id] = (key, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def revision_key(*parts: Any) -> Optional[Tuple[Any, ...]]:
    """The List-reported fields that identify one revision of an item.

    None when the List gave none of them: an all-None key would compare equal
    forever and pin the first Get result for the life of the process, so such an
    item is simply not memoised. A partly missing key is fine — the fields that
    are present still invalidate it.
    """
    if all(part is None for part in parts):
        return None
    return tuple(parts)
