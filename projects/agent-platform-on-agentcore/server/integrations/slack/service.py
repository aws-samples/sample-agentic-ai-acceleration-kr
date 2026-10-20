"""The admin side of the Slack integration: check, link, unlink.

Create is: check the tokens again → write the secret → write the integration
(and its app row, in one transaction) → start the connection. A failure after
the secret exists deletes the secret, so a failed save leaves nothing behind.

Delete is ordered so it can always be retried from where it stopped:

1. mark the integration `deleting` and stop receiving; stop its turns
2. revoke the Bot Token (auth.revoke) — the app stays installed, but dead
3. delete the secret, then the integration and app rows

A resource already gone counts as cleaned up. The DM threads, their memory
and the usage ledger are not touched: they are the users' conversations.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

from core.auth import AuthUser
from integrations.slack import manifest as manifest_mod
from integrations.slack.api import (
    REQUIRED_APP_SCOPES,
    REQUIRED_BOT_SCOPES,
    SlackApi,
    SlackApiError,
    socket_app_id,
)
from integrations.slack.repository import (
    STATUS_ACTIVE,
    STATUS_DELETING,
    IntegrationExists,
    SlackRepository,
)

logger = logging.getLogger(__name__)

# Errors from auth.revoke that mean the token is already unusable.
_ALREADY_REVOKED = {"invalid_auth", "token_revoked", "account_inactive", "not_authed", "token_expired", "app_uninstalled"}


class SlackNotConfigured(Exception):
    """The feature is off or its table/secret prefix is missing (503)."""


class IntegrationNotFound(Exception):
    """No integration for this Harness (404)."""


class ValidationFailed(Exception):
    """The tokens or the Harness did not pass the checks (400). Carries the checks."""

    def __init__(self, result: Dict[str, Any]):
        super().__init__("Slack integration checks failed")
        self.result = result


class DeletionIncomplete(Exception):
    """Some cleanup step failed (502). Carries the per-step results for a retry."""

    def __init__(self, steps: List[Dict[str, Any]]):
        super().__init__("Slack integration cleanup incomplete")
        self.steps = steps


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class TokenStore:
    """Secrets Manager, one secret per integration. Never logs a value."""

    def __init__(self, client: Any, prefix: str, platform: str):
        self._client = client
        self._prefix = prefix
        self._platform = platform

    def name_for(self, harness_id: str, integration_id: str) -> str:
        return f"{self._prefix}/{harness_id}/{integration_id}"

    def create(self, harness_id: str, integration_id: str, bot_token: str, app_token: str) -> str:
        resp = self._client.create_secret(
            Name=self.name_for(harness_id, integration_id),
            Description="Slack DM integration tokens (bot, app-level). Managed by the platform server.",
            SecretString=json.dumps({"bot_token": bot_token, "app_token": app_token}),
            Tags=[
                {"Key": "Platform", "Value": self._platform},
                {"Key": "SlackHarness", "Value": harness_id},
            ],
        )
        return resp["ARN"]

    def load(self, secret_arn: str) -> Tuple[str, str]:
        raw = self._client.get_secret_value(SecretId=secret_arn)["SecretString"]
        data = json.loads(raw)
        return str(data["bot_token"]), str(data["app_token"])

    def delete(self, secret_arn: str) -> None:
        """Immediately, no recovery window: the token was revoked first, and a
        recoverable copy of a credential is a liability, not a safety net."""
        try:
            self._client.delete_secret(SecretId=secret_arn, ForceDeleteWithoutRecovery=True)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise


def _check(checks: List[Dict[str, Any]], key: str, ok: bool, message: str) -> bool:
    checks.append({"id": key, "ok": bool(ok), "message": message})
    return bool(ok)


def public_view(integration: Dict[str, Any], connection: Dict[str, Any]) -> Dict[str, Any]:
    """What the routes return. No secret ARN (it carries the account id)."""
    return {
        "harness_id": integration.get("harness_id"),
        "integration_id": integration.get("integration_id"),
        "status": integration.get("status"),
        "app_id": integration.get("app_id"),
        "team_id": integration.get("team_id"),
        "team_name": integration.get("team_name"),
        "team_url": integration.get("team_url"),
        "bot_user_id": integration.get("bot_user_id"),
        "bot_name": integration.get("bot_name"),
        "agent_name": integration.get("agent_name"),
        "created_at": integration.get("created_at"),
        "created_by": integration.get("created_by"),
        "deletion": integration.get("deletion"),
        "connection": connection,
    }


class SlackIntegrationService:
    def __init__(
        self,
        *,
        enabled: bool,
        repository: Optional[SlackRepository],
        tokens: Optional[TokenStore],
        api: SlackApi,
        harness_lookup: Callable[[str], Any],
        record_lookup: Callable[[Any], Tuple[Optional[str], str, Optional[str]]],
        audit: Any = None,
    ):
        self.enabled = enabled and repository is not None and tokens is not None
        self.repository = repository
        self.tokens = tokens
        self.api = api
        # harness_id -> HarnessSummary (raises when it does not exist)
        self._harness_lookup = harness_lookup
        # HarnessSummary -> (record_id, agent_name, problem); problem is a
        # user-facing reason the Harness cannot be chatted with yet, or None.
        self._record_lookup = record_lookup
        self.audit = audit
        # Set by core/dependencies once the manager exists.
        self.manager: Any = None
        self._socket_app_id = socket_app_id

    def _require(self) -> None:
        if not self.enabled:
            raise SlackNotConfigured("Slack 연동이 꺼져 있습니다 (SLACK_INTEGRATION_ENABLED, SLACK_TABLE, SLACK_SECRET_PREFIX).")

    def _connection(self, harness_id: str) -> Dict[str, Any]:
        if self.manager is None:
            return {"state": "disconnected", "connected_at": None, "last_event_at": None, "last_error": None}
        return self.manager.status(harness_id)

    # ── reads ───────────────────────────────────────────────────────────────

    async def list(self) -> List[Dict[str, Any]]:
        self._require()
        rows = await asyncio.to_thread(self.repository.list_integrations)
        return [
            {
                "harness_id": r.get("harness_id"),
                "status": r.get("status"),
                "team_name": r.get("team_name"),
                "connection_state": self._connection(r.get("harness_id", "")).get("state"),
            }
            for r in rows
        ]

    async def get(self, harness_id: str) -> Optional[Dict[str, Any]]:
        self._require()
        row = await asyncio.to_thread(self.repository.get_integration, harness_id)
        return public_view(row, self._connection(harness_id)) if row else None

    async def manifest(self, harness_id: str) -> Dict[str, Any]:
        self._require()
        harness = await asyncio.to_thread(self._harness_lookup, harness_id)
        return manifest_mod.manifest_payload(harness.harness_name)

    # ── checks ──────────────────────────────────────────────────────────────

    async def test(self, harness_id: str, bot_token: str, app_token: str) -> Dict[str, Any]:
        """Every check, without saving anything. Never echoes a token."""
        self._require()
        result, _ = await self._checks(harness_id, bot_token, app_token)
        return result

    async def _checks(self, harness_id: str, bot_token: str, app_token: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        checks: List[Dict[str, Any]] = []
        info: Dict[str, Any] = {}

        bot_ok = _check(checks, "bot_format", bot_token.startswith("xoxb-"), "Bot Token은 xoxb- 로 시작해야 합니다.")
        app_ok = _check(checks, "app_format", app_token.startswith("xapp-"), "App-Level Token은 xapp- 로 시작해야 합니다.")

        bot_app_id = ""
        if bot_ok:
            try:
                auth = await self.api.auth_test(bot_token)
                info.update(
                    team_id=auth.data.get("team_id"),
                    team_name=auth.data.get("team"),
                    team_url=auth.data.get("url"),
                    bot_user_id=auth.data.get("user_id"),
                    bot_name=auth.data.get("user"),
                    bot_id=auth.data.get("bot_id"),
                )
                _check(checks, "bot_valid", True, "Bot Token이 유효합니다.")
                if auth.data.get("enterprise_id") and not auth.data.get("team_id"):
                    _check(checks, "bot_workspace", False, "Enterprise Grid 조직 단위 설치는 지원하지 않습니다.")
                missing = [s for s in REQUIRED_BOT_SCOPES if s not in auth.scopes]
                _check(
                    checks, "bot_scopes", not missing,
                    "필수 scope가 있습니다." if not missing else f"Bot scope가 없습니다: {', '.join(missing)}",
                )
                if auth.data.get("bot_id"):
                    bot_app_id = await self.api.bot_app_id(bot_token, auth.data["bot_id"])
                _check(checks, "bot_app", bool(bot_app_id), "Bot Token의 앱을 확인했습니다." if bot_app_id else "Bot Token의 앱을 확인하지 못했습니다.")
            except SlackApiError as exc:
                _check(checks, "bot_valid", False, f"Bot Token 확인 실패 ({exc.error})")

        socket_app = ""
        if app_ok:
            try:
                opened = await self.api.open_connection(app_token)
                missing = [s for s in REQUIRED_APP_SCOPES if opened.scopes and s not in opened.scopes]
                _check(
                    checks, "app_scopes", not missing,
                    "connections:write scope가 있습니다." if not missing else "App-Level Token에 connections:write scope가 없습니다.",
                )
                try:
                    socket_app = await self._socket_app_id(str(opened.data.get("url") or ""))
                except Exception as exc:  # noqa: BLE001
                    logger.info("Slack socket check failed: %s", type(exc).__name__)
                _check(
                    checks, "app_socket", bool(socket_app),
                    "Socket Mode 연결을 확인했습니다." if socket_app else "Socket Mode 연결에 실패했습니다. 앱 설정에서 Socket Mode를 켜 주세요.",
                )
            except SlackApiError as exc:
                _check(checks, "app_valid", False, f"App-Level Token 확인 실패 ({exc.error})")

        if bot_app_id and socket_app:
            _check(checks, "same_app", bot_app_id == socket_app, "두 토큰이 같은 앱의 것입니다." if bot_app_id == socket_app else "두 토큰이 서로 다른 앱의 것입니다.")
        app_id = bot_app_id or socket_app
        info["app_id"] = app_id or None

        if app_id:
            owner = await asyncio.to_thread(self.repository.get_app, app_id)
            _check(
                checks, "app_unused", owner is None,
                "이 앱은 다른 Harness에 연결되어 있지 않습니다." if owner is None else "이 Slack 앱은 이미 다른 Harness에 연결되어 있습니다.",
            )

        existing = await asyncio.to_thread(self.repository.get_integration, harness_id)
        _check(checks, "harness_unlinked", existing is None, "이 Harness에는 연동이 없습니다." if existing is None else "이 Harness에는 이미 Slack 연동이 있습니다.")

        try:
            harness = await asyncio.to_thread(self._harness_lookup, harness_id)
            ready = (harness.status or "").upper() == "READY"
            _check(checks, "harness_ready", ready, "Harness가 준비되었습니다." if ready else f"Harness가 준비되지 않았습니다 ({harness.status}).")
            record_id, agent_name, problem = await asyncio.to_thread(self._record_lookup, harness)
            _check(checks, "harness_approved", problem is None, problem or "에이전트가 승인되어 있습니다.")
            info.update(record_id=record_id, agent_name=agent_name, harness_arn=harness.harness_arn, harness_name=harness.harness_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Harness check failed for %s: %s", harness_id, exc)
            _check(checks, "harness_ready", False, "Harness를 확인하지 못했습니다.")

        ok = all(c["ok"] for c in checks)
        public = {k: info.get(k) for k in ("team_id", "team_name", "team_url", "bot_user_id", "bot_name", "app_id", "agent_name")}
        return {"ok": ok, "checks": checks, "workspace": public}, info

    # ── create ──────────────────────────────────────────────────────────────

    async def create(self, harness_id: str, bot_token: str, app_token: str, actor: AuthUser) -> Dict[str, Any]:
        self._require()
        result, info = await self._checks(harness_id, bot_token, app_token)
        if not result["ok"]:
            failed = {c["id"] for c in result["checks"] if not c["ok"]}
            if failed & {"harness_unlinked", "app_unused"} and failed <= {"harness_unlinked", "app_unused"}:
                raise IntegrationExists("harness" if "harness_unlinked" in failed else "app")
            raise ValidationFailed(result)

        integration_id = uuid.uuid4().hex[:12]
        secret_arn = await asyncio.to_thread(self.tokens.create, harness_id, integration_id, bot_token, app_token)
        integration = {
            "integration_id": integration_id,
            "harness_id": harness_id,
            "harness_name": info.get("harness_name"),
            "harness_arn": info.get("harness_arn"),
            "record_id": info.get("record_id"),
            "agent_name": info.get("agent_name"),
            "app_id": info.get("app_id"),
            "team_id": info.get("team_id"),
            "team_name": info.get("team_name"),
            "team_url": info.get("team_url"),
            "bot_user_id": info.get("bot_user_id"),
            "bot_name": info.get("bot_name"),
            "bot_id": info.get("bot_id"),
            "secret_arn": secret_arn,
            "status": STATUS_ACTIVE,
            "created_at": _now(),
            "created_by": actor.email or actor.username or actor.sub,
        }
        integration = {k: v for k, v in integration.items() if v is not None}
        try:
            await asyncio.to_thread(self.repository.create_integration, integration)
        except BaseException:
            # The secret is ours alone; nothing references it now.
            try:
                await asyncio.to_thread(self.tokens.delete, secret_arn)
            except Exception:
                logger.error("Could not delete the secret of a failed Slack integration %s/%s", harness_id, integration_id)
            raise
        if self.manager is not None:
            await self.manager.add(integration)
        self._audit(actor, "slack.integration.create", harness_id, after={k: integration.get(k) for k in ("integration_id", "app_id", "team_id", "team_name")})
        return public_view(integration, self._connection(harness_id))

    # ── delete ──────────────────────────────────────────────────────────────

    async def delete(self, harness_id: str, actor: Optional[AuthUser] = None) -> Dict[str, Any]:
        """Run (or resume) the cleanup. Raises IntegrationNotFound / DeletionIncomplete."""
        self._require()
        row = await asyncio.to_thread(self.repository.get_integration, harness_id)
        if row is None:
            raise IntegrationNotFound(harness_id)
        steps: List[Dict[str, Any]] = []

        async def step(name: str, action) -> bool:
            try:
                await action()
                steps.append({"step": name, "ok": True})
                return True
            except Exception as exc:  # noqa: BLE001
                code = exc.error if isinstance(exc, SlackApiError) else type(exc).__name__
                logger.warning("Slack cleanup step %s failed for %s: %s", name, harness_id, code)
                steps.append({"step": name, "ok": False, "error": code})
                return False

        async def stop() -> None:
            if row.get("status") != STATUS_DELETING:
                await asyncio.to_thread(self.repository.update_integration, harness_id, status=STATUS_DELETING)
            if self.manager is not None:
                await self.manager.remove(harness_id)

        async def revoke() -> None:
            try:
                bot_token, _ = await asyncio.to_thread(self.tokens.load, row["secret_arn"])
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                    return  # secret gone: a previous run revoked and deleted it
                raise
            try:
                await self.api.revoke(bot_token)
            except SlackApiError as exc:
                if exc.error not in _ALREADY_REVOKED:
                    raise

        async def forget() -> None:
            await asyncio.to_thread(self.tokens.delete, row["secret_arn"])
            await asyncio.to_thread(self.repository.delete_integration, harness_id, row.get("app_id", ""))

        ok = await step("stop", stop) and await step("revoke_token", revoke) and await step("delete_records", forget)
        if not ok:
            # Keep what a retry needs: the row (and its secret ARN) stays.
            try:
                await asyncio.to_thread(
                    self.repository.update_integration, harness_id, deletion={"at": _now(), "steps": steps}
                )
            except Exception:
                logger.warning("Could not record the Slack cleanup progress for %s", harness_id, exc_info=True)
            self._audit(actor, "slack.integration.delete", harness_id, result="error", message=json.dumps(steps))
            raise DeletionIncomplete(steps)
        self._audit(actor, "slack.integration.delete", harness_id, before={"integration_id": row.get("integration_id"), "app_id": row.get("app_id")})
        return {"deleted": True, "steps": steps}

    async def cleanup_for_harness(self, harness_id: str, actor: Optional[AuthUser] = None) -> None:
        """Harness deletion's hook: nothing to do when there is no integration
        (or the feature is off — see the known limits in the docs)."""
        if not self.enabled:
            return
        try:
            await self.delete(harness_id, actor)
        except IntegrationNotFound:
            return

    def _audit(self, actor: Optional[AuthUser], action: str, target: str, **kwargs: Any) -> None:
        if self.audit is None or actor is None:
            return
        self.audit.record(actor, action, f"harness:{target}", **kwargs)


def harness_record(harness: Any, registry: Any) -> Tuple[Optional[str], str, Optional[str]]:
    """(record_id, agent_name, problem) for the Harness a DM will talk to.

    The record is what every turn is addressed to, so the integration stores
    it: the approval gate and team checks of the web path then apply as-is.
    Without a registry the deployed-resource id stands in, as it does on the
    web.
    """
    from services.registry_service import is_registry_unavailable, registry_enabled

    fallback = (f"deployed:{harness.harness_arn}", harness.harness_name, None)
    if not registry_enabled():
        return fallback
    try:
        record = registry.agent_records_by_arn().get(harness.harness_arn)
    except Exception as exc:  # noqa: BLE001
        if is_registry_unavailable(exc):
            return fallback
        raise
    if record is None:
        return None, harness.harness_name, "레지스트리에 등록되지 않은 Harness입니다. 등록·승인 후 연동해 주세요."
    chattable = registry.chattable_record(record.record_id)
    status = (chattable.status or "").upper()
    name = getattr(record, "name", "") or harness.harness_name
    if status != "APPROVED":
        return record.record_id, name, f"에이전트가 아직 승인되지 않았습니다 ({chattable.status or 'unknown'})."
    return record.record_id, name, None
