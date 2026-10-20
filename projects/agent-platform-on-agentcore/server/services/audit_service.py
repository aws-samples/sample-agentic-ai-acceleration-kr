"""Who did what on the Organization page.

Only what the UI did: a change made in the Cognito console or in Entra never
passes through here, which is why the page pairs this ledger with the drift
check (services/org_drift.py) rather than pretending to be complete.

Recording never raises. An audit write that failed must not undo or block the
admin's action; it is logged and the action stands.
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.auth import AuthUser
from models.knowledge import now_iso

logger = logging.getLogger(__name__)

ACTIONS = (
    "team.create", "team.update", "team.delete", "team.provision",
    "member.add", "member.remove",
    "user.invite", "user.update_role", "user.update_teams", "user.disable", "user.enable",
    "user.reset_password", "user.delete",
    "slack.integration.create", "slack.integration.delete",
)
# Never persisted even when a caller passes them in before/after.
SECRET_KEYS = {"temporary_password", "password", "secret", "bot_token", "app_token", "secret_arn"}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _months(start_iso: str, end_iso: str) -> List[str]:
    start = datetime.fromisoformat(start_iso[:19])
    end = datetime.fromisoformat(end_iso[:19])
    months: List[str] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return months


class AuditService:
    def __init__(self, repository=None, clock=now_iso):
        self._repository = repository
        self._clock = clock

    @property
    def configured(self) -> bool:
        return self._repository is not None

    def record(self, actor: AuthUser, action: str, target: str, *, before: Any = None, after: Any = None,
               result: str = "ok", message: str = "") -> Optional[Dict[str, Any]]:
        if not self.configured:
            return None
        entry = {
            "id": uuid.uuid4().hex[:12],
            "at": self._clock(),
            "actor_sub": actor.sub or actor.username,
            "actor_email": actor.email or actor.username,
            "action": action,
            "target": target,
            "before": _scrub(before),
            "after": _scrub(after),
            "result": result,
            "message": message,
        }
        try:
            self._repository.append_audit(entry)
        except Exception:
            logger.warning("Audit write failed for %s %s", action, target, exc_info=True)
            return None
        return entry

    def list(self, start_iso: str, end_iso: str, actor: str = "", target: str = "", limit: int = 500) -> List[Dict[str, Any]]:
        if not self.configured:
            return []
        out: List[Dict[str, Any]] = []
        for month in reversed(_months(start_iso, end_iso)):
            try:
                rows = self._repository.list_audit(month, start_iso, end_iso)
            except Exception:
                logger.warning("Audit read failed for %s", month, exc_info=True)
                continue
            for row in rows:
                if actor and row.get("actor_sub") != actor:
                    continue
                if target and row.get("target") != target:
                    continue
                out.append(row)
        out.sort(key=lambda r: str(r.get("at", "")), reverse=True)
        return out[:limit]
