"""Slack user -> platform user, judged afresh on every turn.

A DM carries no platform token, so the caller is found by email: the Slack
profile's email must match exactly one account across the Cognito pool and the
OIDC sign-in table. Two accounts (a disabled one included) is a refusal, not a
guess — a DM must never run as the wrong person.

What is cached and what is not: the *email lookup* (which accounts carry this
email) may be cached briefly, because it is a directory search. The *verdict*
is never cached: account state, groups and the provider's rules are re-read on
every turn, so disabling someone takes effect on their next message.

Cognito: the account must be enabled, CONFIRMED and have a verified email;
groups are read from the pool (AdminListGroupsForUser). OIDC (Entra): there is
no directory to ask, so the groups are those stored at the user's last web
sign-in. The `team:` tags in that row are dropped and recomputed from the
current Entra group mapping, and the provider must still be configured and
still admit those groups. That staleness is a known limit (docs).
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.auth import (
    ADMIN_GROUP,
    COGNITO_PROVIDER_ID,
    TEAM_GROUP_PREFIX,
    AuthUser,
    with_entra_teams,
)

logger = logging.getLogger(__name__)

EMAIL_CACHE_SECONDS = 300

# Resolution.refusal values; formatting.REFUSALS has the user-facing text.
UNMAPPED = "unmapped"
AMBIGUOUS = "ambiguous"
INACTIVE = "inactive"
NOT_ALLOWED = "not_allowed"
LOOKUP_FAILED = "lookup_failed"


@dataclass
class Resolution:
    user: Optional[AuthUser] = None
    refusal: Optional[str] = None


# ("cognito", username) or (provider_id, sub)
Candidate = Tuple[str, str]


def _attrs(attributes: Sequence[Dict[str, str]]) -> Dict[str, str]:
    return {a.get("Name", ""): a.get("Value", "") for a in attributes or []}


class IdentityResolver:
    def __init__(
        self,
        *,
        cognito: Any = None,
        user_pool_id: str = "",
        users: Any = None,
        providers: Callable[[], List[Any]] = lambda: [],
        auth_enforced: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = EMAIL_CACHE_SECONDS,
    ):
        self._cognito = cognito
        self._pool = user_pool_id
        self._users = users
        self._providers = providers
        self._auth_enforced = auth_enforced
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._email_cache: Dict[str, Tuple[float, List[Candidate]]] = {}
        self._oidc_index: Optional[Tuple[float, Dict[str, List[Candidate]]]] = None

    def resolve(self, email: str) -> Resolution:
        """Blocking (boto3); call through asyncio.to_thread."""
        if not self._auth_enforced():
            # Local development, matching core.auth.current_user: every web
            # request runs as `local`, so a DM does too and the conversation
            # shows up in the same web list.
            return Resolution(
                user=AuthUser(sub="local", username="local", groups=[ADMIN_GROUP], role="admin", provider="local")
            )
        email = (email or "").strip()
        if not email or '"' in email or "\\" in email:
            return Resolution(refusal=UNMAPPED)
        try:
            candidates = self._candidates(email)
        except Exception:
            logger.warning("Slack identity lookup failed", exc_info=True)
            return Resolution(refusal=LOOKUP_FAILED)
        if not candidates:
            return Resolution(refusal=UNMAPPED)
        if len(candidates) > 1:
            return Resolution(refusal=AMBIGUOUS)
        kind, ident = candidates[0]
        try:
            if kind == COGNITO_PROVIDER_ID:
                return self._judge_cognito(ident, email)
            return self._judge_oidc(kind, ident, email)
        except Exception:
            logger.warning("Slack identity check failed", exc_info=True)
            return Resolution(refusal=LOOKUP_FAILED)

    # ── lookup (cacheable) ──────────────────────────────────────────────────

    def _candidates(self, email: str) -> List[Candidate]:
        key = email.lower()
        now = self._clock()
        with self._lock:
            hit = self._email_cache.get(key)
            if hit and now - hit[0] < self._cache_seconds:
                return list(hit[1])
        found = self._cognito_candidates(email) + self._oidc_candidates(key)
        # Same account found twice (two case spellings) is still one account.
        unique = list(dict.fromkeys(found))
        with self._lock:
            self._email_cache[key] = (now, unique)
        return unique

    def _cognito_candidates(self, email: str) -> List[Candidate]:
        if self._cognito is None or not self._pool:
            return []
        names: List[Candidate] = []
        # The pool's email filter is exact; Slack may report a different case
        # than the one the account was created with.
        for spelling in dict.fromkeys([email, email.lower()]):
            kwargs: Dict[str, Any] = {"UserPoolId": self._pool, "Filter": f'email = "{spelling}"'}
            while True:
                resp = self._cognito.list_users(**kwargs)
                for user in resp.get("Users") or []:
                    names.append((COGNITO_PROVIDER_ID, user.get("Username", "")))
                token = resp.get("PaginationToken")
                if not token:
                    break
                kwargs["PaginationToken"] = token
        return names

    def _oidc_candidates(self, email_lower: str) -> List[Candidate]:
        if self._users is None:
            return []
        now = self._clock()
        with self._lock:
            index = self._oidc_index
        if index is None or now - index[0] >= self._cache_seconds:
            table: Dict[str, List[Candidate]] = {}
            for row in self._users.list_all():
                row_email = str(row.get("email") or "").strip().lower()
                if row_email and row.get("sub") and row.get("provider"):
                    table.setdefault(row_email, []).append((str(row["provider"]), str(row["sub"])))
            index = (now, table)
            with self._lock:
                self._oidc_index = index
        return list(index[1].get(email_lower, []))

    # ── verdict (never cached) ──────────────────────────────────────────────

    def _judge_cognito(self, username: str, email: str) -> Resolution:
        user = self._cognito.admin_get_user(UserPoolId=self._pool, Username=username)
        attrs = _attrs(user.get("UserAttributes") or [])
        if (
            not user.get("Enabled", False)
            or user.get("UserStatus") != "CONFIRMED"
            or str(attrs.get("email_verified", "")).lower() != "true"
        ):
            return Resolution(refusal=INACTIVE)
        groups: List[str] = []
        kwargs: Dict[str, Any] = {"UserPoolId": self._pool, "Username": username}
        while True:
            resp = self._cognito.admin_list_groups_for_user(**kwargs)
            groups.extend(g.get("GroupName", "") for g in resp.get("Groups") or [] if g.get("GroupName"))
            token = resp.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token
        sub = attrs.get("sub") or ""
        if not sub:
            return Resolution(refusal=LOOKUP_FAILED)
        return Resolution(
            user=AuthUser(
                sub=sub,
                username=username,
                email=attrs.get("email") or email,
                groups=groups,
                role="admin" if ADMIN_GROUP in groups else "user",
                provider=COGNITO_PROVIDER_ID,
            )
        )

    def _judge_oidc(self, provider_id: str, sub: str, email: str) -> Resolution:
        provider = next((p for p in self._providers() if p.id == provider_id), None)
        if provider is None:
            # The provider was removed from the configuration: nobody can sign
            # in through it on the web any more, so not through Slack either.
            return Resolution(refusal=NOT_ALLOWED)
        row = self._users.get(provider_id, sub)
        if not row:
            return Resolution(refusal=UNMAPPED)
        stored = [g for g in (row.get("groups") or []) if isinstance(g, str)]
        groups = with_entra_teams([g for g in stored if not g.startswith(TEAM_GROUP_PREFIX)])
        if not provider.allows(groups):
            return Resolution(refusal=NOT_ALLOWED)
        row_email = str(row.get("email") or email)
        return Resolution(
            user=AuthUser(
                sub=sub,
                username=row_email or sub,
                email=row_email,
                name=str(row.get("display_name") or ""),
                groups=groups,
                role=provider.role_for(row_email, groups),
                provider=provider.id,
            )
        )
