"""The organisation's people, for the admin Organization page.

Two sources, one list. Cognito users are read live from the pool — `ListUsers`
for the people, then `ListUsersInGroup` for `admin` and every `team:*` group,
so role and teams are what the pool says *now* (a change made in the console
shows on the next read). Entra users are whatever `users` table rows their
logins wrote; their teams come from the token's group ids through the team
mapping, so the page can only show them "as of last login" and says so.

Writes exist for Cognito only. An Entra person's role and groups belong to
Entra; calling a write on one raises NotCognitoUser before any API call.

Group reads are (1 + teams) calls of ≤60 users per page instead of one
AdminListGroupsForUser per person — the pool is dozens of people and a handful
of teams. Cached for `cache_seconds`; every write invalidates the cache so the
next read shows the change.
"""
import logging
import secrets
import string
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError
from pydantic import BaseModel

logger = logging.getLogger(__name__)

PAGE_SIZE = 60
ADMIN_GROUP, USER_GROUP, TEAM_PREFIX = "admin", "user", "team:"
COGNITO = "cognito"


class NotCognitoUser(ValueError):
    """A write was attempted on a person the pool does not hold (Entra)."""


class OrgUser(BaseModel):
    sub: str
    username: str = ""
    email: str = ""
    name: str = ""
    provider: str = COGNITO
    role: str = "user"
    teams: List[str] = []
    status: str = ""  # Cognito UserStatus; "" for Entra
    enabled: bool = True
    created_at: str = ""
    last_login: str = ""
    groups: List[str] = []  # raw groups (Cognito group names / Entra group ids)


def _attrs(user: Dict[str, Any]) -> Dict[str, str]:
    return {str(a.get("Name")): str(a.get("Value")) for a in user.get("Attributes") or []}


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else "")


def temporary_password(length: int = 16) -> str:
    """Satisfies Cognito's default policy: upper, lower, digit, symbol."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*" for c in pw)):
            return pw


class OrgDirectoryService:
    def __init__(self, cognito, user_pool_id: str, users=None,
                 entra_teams: Optional[Callable[[List[str]], List[str]]] = None,
                 cache_seconds: int = 60, clock: Callable[[], float] = time.monotonic):
        self._cognito = cognito
        self._pool = user_pool_id or ""
        self._users = users
        self._entra_teams = entra_teams or (lambda gids: [])
        self._cache_seconds, self._clock = cache_seconds, clock
        self._snapshot: Optional[List[OrgUser]] = None
        self._snapshot_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._pool) or self._users is not None

    @property
    def has_pool(self) -> bool:
        """False on an Entra-only stack: there are no `team:` groups to compare."""
        return bool(self._pool)

    def invalidate(self) -> None:
        self._snapshot = None

    # ── reads ──────────────────────────────────────────────────────────────
    def list_users(self, refresh: bool = False) -> List[OrgUser]:
        if not self.configured:
            return []
        now = self._clock()
        if not refresh and self._snapshot is not None and now - self._snapshot_at < self._cache_seconds:
            return list(self._snapshot)
        try:
            fresh = self._read()
        except Exception:
            logger.warning("Org directory read failed; serving the previous snapshot", exc_info=True)
            return list(self._snapshot or [])
        self._snapshot, self._snapshot_at = fresh, now
        return list(fresh)

    def get_user(self, sub: str) -> Optional[OrgUser]:
        return next((u for u in self.list_users() if u.sub == sub), None)

    def team_groups(self) -> List[str]:
        """Team names the pool has groups for (console-made ones included)."""
        if not self._pool:
            return []
        names: List[str] = []
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"UserPoolId": self._pool, "Limit": PAGE_SIZE}
            if token:
                kwargs["NextToken"] = token
            resp = self._cognito.list_groups(**kwargs)
            names.extend(g["GroupName"][len(TEAM_PREFIX):] for g in resp.get("Groups") or []
                         if str(g.get("GroupName", "")).startswith(TEAM_PREFIX))
            token = resp.get("NextToken")
            if not token:
                return sorted(names)

    def group_members(self, team: str) -> List[OrgUser]:
        """Live `ListUsersInGroup team:<name>`, decorated from the snapshot."""
        if not self._pool:
            return []
        subs = [u for u in self._members_of(f"{TEAM_PREFIX}{team}")]
        known = {u.sub: u for u in self.list_users()}
        return [known[s] for s in subs if s in known]

    def team_sizes(self) -> Dict[str, int]:
        """Head count per team, from the cached snapshot.

        The snapshot already resolved every `team:*` group when it was read, so
        counting from it costs no call. Asking `group_members` per team instead
        made the teams list one sequential ListUsersInGroup per team on every
        load (live: 1.06s against 0.23s for the people list off the same
        snapshot). It also counts Entra members, as the members tab lists them.
        """
        sizes: Dict[str, int] = {}
        for person in self.list_users():
            for team in person.teams:
                sizes[team] = sizes.get(team, 0) + 1
        return sizes

    def member_subs(self, team: str) -> List[str]:
        """Raw live `ListUsersInGroup team:<name>` subs, not filtered by the
        snapshot — a guard (delete refuses a team with people in it) must see a
        member the up-to-60-s-old snapshot has not caught up with. Raises when
        Cognito does; callers must not read a failure as "no members"."""
        if not self._pool:
            return []
        return self._members_of(f"{TEAM_PREFIX}{team}")

    def _members_of(self, group: str) -> List[str]:
        subs: List[str] = []
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"UserPoolId": self._pool, "GroupName": group, "Limit": PAGE_SIZE}
            if token:
                kwargs["NextToken"] = token
            try:
                resp = self._cognito.list_users_in_group(**kwargs)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                    return subs
                raise
            subs.extend(_attrs(u).get("sub") or str(u.get("Username")) for u in resp.get("Users") or [])
            token = resp.get("NextToken")
            if not token:
                return subs

    def _read(self) -> List[OrgUser]:
        out: List[OrgUser] = []
        if self._pool:
            people: Dict[str, OrgUser] = {}
            token: Optional[str] = None
            while True:
                kwargs: Dict[str, Any] = {"UserPoolId": self._pool, "Limit": PAGE_SIZE}
                if token:
                    kwargs["PaginationToken"] = token
                resp = self._cognito.list_users(**kwargs)
                for u in resp.get("Users") or []:
                    a = _attrs(u)
                    sub = a.get("sub") or str(u.get("Username"))
                    people[sub] = OrgUser(
                        sub=sub, username=str(u.get("Username")), email=a.get("email", ""),
                        name=a.get("name", ""), provider=COGNITO, status=str(u.get("UserStatus") or ""),
                        enabled=bool(u.get("Enabled", True)), created_at=_iso(u.get("UserCreateDate")),
                    )
                token = resp.get("PaginationToken")
                if not token:
                    break
            admins = set(self._members_of(ADMIN_GROUP))
            memberships: Dict[str, List[str]] = {}
            for team in self.team_groups():
                for sub in self._members_of(f"{TEAM_PREFIX}{team}"):
                    memberships.setdefault(sub, []).append(team)
            for sub, person in people.items():
                teams = sorted(memberships.get(sub, []))
                groups = ([ADMIN_GROUP] if sub in admins else []) + [f"{TEAM_PREFIX}{t}" for t in teams]
                out.append(person.model_copy(update={"role": "admin" if sub in admins else "user",
                                                     "teams": teams, "groups": groups}))
        if self._users is not None:
            for row in self._users.list_all():
                provider = str(row.get("provider") or "")
                if not provider or provider == COGNITO:
                    continue
                groups = [g for g in (row.get("groups") or []) if isinstance(g, str)]
                out.append(OrgUser(
                    sub=str(row.get("sub") or ""), username=str(row.get("email") or ""),
                    email=str(row.get("email") or ""), name=str(row.get("display_name") or ""),
                    provider=provider, role=str(row.get("role") or "user"),
                    teams=sorted(self._entra_teams(groups)), status="", enabled=True,
                    created_at=str(row.get("created_at") or ""), last_login=str(row.get("last_login") or ""),
                    groups=groups,
                ))
        return out

    # ── writes (Cognito only) ──────────────────────────────────────────────
    def _cognito_only(self, user: OrgUser) -> None:
        if user.provider != COGNITO:
            raise NotCognitoUser("Entra 사용자의 역할·팀·상태는 Entra ID 에서 바꾸세요.")

    def invite(self, email: str, teams: List[str], role: str, send_email: bool = True) -> Tuple[OrgUser, Optional[str]]:
        """AdminCreateUser; Cognito mails the temporary password unless
        `send_email` is False, in which case we mint one and hand it back once."""
        if not self._pool:
            raise ValueError("Cognito 풀이 설정되지 않았습니다.")
        for t in teams:
            try:
                self._cognito.get_group(UserPoolId=self._pool, GroupName=f"{TEAM_PREFIX}{t}")
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                    raise ValueError(f"팀 그룹 team:{t} 이 없습니다.") from exc
                raise
        temp = None if send_email else temporary_password()
        kwargs: Dict[str, Any] = {
            "UserPoolId": self._pool, "Username": email,
            "UserAttributes": [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
        }
        if send_email:
            kwargs["DesiredDeliveryMediums"] = ["EMAIL"]
        else:
            kwargs["MessageAction"] = "SUPPRESS"
            kwargs["TemporaryPassword"] = temp
        try:
            created = self._cognito.admin_create_user(**kwargs)["User"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "UsernameExistsException":
                raise ValueError(f"{email} 은 이미 등록된 사용자입니다.") from exc
            raise
        username = str(created.get("Username"))
        sub = _attrs(created).get("sub") or username
        groups = [USER_GROUP] + ([ADMIN_GROUP] if role == "admin" else []) + [f"{TEAM_PREFIX}{t}" for t in teams]
        try:
            for g in groups:
                try:
                    self._cognito.admin_add_user_to_group(UserPoolId=self._pool, Username=username, GroupName=g)
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    raise RuntimeError(f"{email} 은 만들어졌지만 그룹 {g} 추가에 실패했습니다: {code}") from exc
        finally:
            self.invalidate()  # the user exists now, whether or not the groups landed
        user = OrgUser(sub=sub, username=username, email=email, provider=COGNITO, role=role,
                       teams=sorted(teams), status=str(created.get("UserStatus") or ""), groups=groups)
        return user, temp

    def _fresh(self, user: OrgUser) -> OrgUser:
        """The caller's object may predate an earlier write (or a console edit);
        diff against what the pool says now, falling back to the object given."""
        return self.get_user(user.sub) or user

    def set_role(self, user: OrgUser, role: str) -> None:
        self._cognito_only(user)
        user = self._fresh(user)
        if role == "admin" and user.role != "admin":
            self._cognito.admin_add_user_to_group(UserPoolId=self._pool, Username=user.username, GroupName=ADMIN_GROUP)
        elif role != "admin" and user.role == "admin":
            self._cognito.admin_remove_user_from_group(UserPoolId=self._pool, Username=user.username, GroupName=ADMIN_GROUP)
        self.invalidate()

    def set_teams(self, user: OrgUser, teams: List[str]) -> None:
        self._cognito_only(user)
        user = self._fresh(user)
        wanted, current = set(teams), set(user.teams)
        for t in sorted(current - wanted):
            self._cognito.admin_remove_user_from_group(UserPoolId=self._pool, Username=user.username, GroupName=f"{TEAM_PREFIX}{t}")
        for t in sorted(wanted - current):
            self._cognito.admin_add_user_to_group(UserPoolId=self._pool, Username=user.username, GroupName=f"{TEAM_PREFIX}{t}")
        self.invalidate()

    def add_to_team(self, user: OrgUser, team: str) -> None:
        self._cognito_only(user)
        self.set_teams(user, sorted(set(self._fresh(user).teams) | {team}))

    def remove_from_team(self, user: OrgUser, team: str) -> None:
        self._cognito_only(user)
        self.set_teams(user, sorted(set(self._fresh(user).teams) - {team}))

    def set_enabled(self, user: OrgUser, enabled: bool) -> None:
        self._cognito_only(user)
        if enabled:
            self._cognito.admin_enable_user(UserPoolId=self._pool, Username=user.username)
        else:
            self._cognito.admin_disable_user(UserPoolId=self._pool, Username=user.username)
        self.invalidate()

    def reset_password(self, user: OrgUser) -> None:
        self._cognito_only(user)
        self._cognito.admin_reset_user_password(UserPoolId=self._pool, Username=user.username)

    def delete(self, user: OrgUser) -> None:
        self._cognito_only(user)
        self._cognito.admin_delete_user(UserPoolId=self._pool, Username=user.username)
        self.invalidate()
