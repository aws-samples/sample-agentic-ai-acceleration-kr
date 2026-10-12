"""Cognito `sub` -> email, for the admin views that name people.

The pool signs people in by email (`username_attributes = ["email"]`). That makes
Cognito mint the username as a UUID, so the access token's `username` claim is the
sub again and no request the server handles carries an email. The ledger keys
people by sub on purpose (a username can be reassigned after deletion; a sub
cannot), so the name has to be looked up at read time. Read-time resolution is
also why old rows get names: nothing was recorded, so nothing needs backfilling.

The lookup is one snapshot of the whole pool, read in pages, not a filtered
`ListUsers` per sub. Measured on the live pool, `sub = "…"` filtered to one user
and an unfiltered page of sixty both take ~300 ms — so per-person lookups cost
N × 300 ms on the first poll after the cache lapses and scale with the leaderboard,
while the snapshot costs ceil(pool / 60) × 300 ms and scales with the pool. At the
size where that stops being cheap (thousands of accounts) the next step is a
background sync into the ledger table, not a smarter request path.

Every failure degrades to "no label". The client falls back to the shortened sub
it always showed; an admin's usage page must never 5xx because a name was
unavailable — the task role lacked `cognito-idp:ListUsers` for the whole life of
the feature before this shipped. A failed *refresh* keeps serving the previous
snapshot: names that were right a moment ago are still right enough.

OIDC users (Microsoft Entra ID) are not in the pool at all. For them the
server records email/name on login (repositories/user_repository.py, the
USERS_TABLE) and this merges that table into the same snapshot — so a
leaderboard that mixes Cognito and Entra people names both. Either source may
be absent; the other still resolves.
"""
import logging
import time
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# `/summary` polls once a minute; ten minutes is one pool read per ten polls and
# still picks up a changed email within a session.
CACHE_SECONDS = 600
# Cognito's maximum page.
PAGE_SIZE = 60


class DirectoryService:
    def __init__(self, cognito, user_pool_id: str, cache_seconds: int = CACHE_SECONDS, users=None):
        self._cognito = cognito
        self._user_pool_id = user_pool_id or ""
        # Anything with `.labels() -> {sub: label}`; the OIDC users table.
        self._users = users
        self._cache_seconds = cache_seconds
        self._snapshot: Optional[Dict[str, str]] = None
        self._snapshot_at = 0.0

    def emails(self, subs: Iterable[Optional[str]]) -> Dict[str, str]:
        """Emails for the subs that resolve. Unknown, blank and failed lookups are
        simply absent — never an empty string, which a client would render as
        nothing rather than fall back."""
        if not self._user_pool_id and self._users is None:
            return {}
        snapshot = self._current()
        if not snapshot:
            return {}
        return {
            sub: snapshot[sub]
            for sub in dict.fromkeys(s for s in subs if isinstance(s, str) and s)
            if sub in snapshot
        }

    def _current(self) -> Dict[str, str]:
        now = time.monotonic()
        if self._snapshot is not None and now - self._snapshot_at < self._cache_seconds:
            return self._snapshot
        try:
            fresh = self._read_pool()
        except Exception:
            # Serve the stale snapshot if there is one; an empty dict otherwise.
            # Not cached as a miss: the next request retries.
            logger.info("Directory read failed; serving the previous directory snapshot", exc_info=True)
            return self._snapshot or {}
        self._snapshot, self._snapshot_at = fresh, now
        return fresh

    def _read_pool(self) -> Dict[str, str]:
        """Both sources, pool first so a sub recorded in both keeps the pool's
        email. A failure in either raises so the caller keeps the old snapshot:
        a half-read directory would silently drop one IdP's people."""
        out: Dict[str, str] = {}
        if self._user_pool_id:
            out.update(self._read_cognito())
        if self._users is not None:
            for sub, label in self._users.labels().items():
                out.setdefault(sub, label)
        return out

    def _read_cognito(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        token: Optional[str] = None
        while True:
            kwargs = {"UserPoolId": self._user_pool_id, "Limit": PAGE_SIZE}
            if token:
                kwargs["PaginationToken"] = token
            resp = self._cognito.list_users(**kwargs)
            for user in resp.get("Users") or []:
                attrs = {a.get("Name"): a.get("Value") for a in user.get("Attributes") or []}
                sub, email = attrs.get("sub"), attrs.get("email")
                if sub and email:
                    out[str(sub)] = str(email)
            token = resp.get("PaginationToken")
            if not token:
                return out
