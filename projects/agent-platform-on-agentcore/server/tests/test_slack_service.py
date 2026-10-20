"""Check, link and unlink (integrations/slack/service.py)."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import AuthUser  # noqa: E402
from integrations.slack.api import REQUIRED_BOT_SCOPES, SlackApiError, SlackResponse  # noqa: E402
from integrations.slack.repository import IntegrationExists  # noqa: E402
from integrations.slack.service import (  # noqa: E402
    DeletionIncomplete,
    IntegrationNotFound,
    SlackIntegrationService,
    SlackNotConfigured,
    TokenStore,
    ValidationFailed,
    harness_record,
)

BOT = "xoxb-1-bot-secret"
APP = "xapp-1-app-secret"
ADMIN = AuthUser(sub="admin-sub", username="admin", email="admin@example.com", groups=["admin"])


class Api:
    def __init__(self, scopes=REQUIRED_BOT_SCOPES, app_id="A1", revoke_error=None):
        self.scopes = frozenset(scopes)
        self.app_id = app_id
        self.revoke_error = revoke_error
        self.revoked = []

    async def auth_test(self, token):
        if token != BOT:
            raise SlackApiError("auth.test", "invalid_auth")
        return SlackResponse(
            data={"ok": True, "team_id": "T1", "team": "Acme", "url": "https://acme.slack.com/", "user_id": "UBOT", "user": "helper", "bot_id": "B1"},
            scopes=self.scopes,
        )

    async def bot_app_id(self, token, bot_id):
        return self.app_id

    async def open_connection(self, token):
        if token != APP:
            raise SlackApiError("apps.connections.open", "invalid_auth")
        return SlackResponse(data={"ok": True, "url": "wss://x"}, scopes=frozenset({"connections:write"}))

    async def revoke(self, token):
        self.revoked.append(token)
        if self.revoke_error:
            error, self.revoke_error = self.revoke_error, None
            raise SlackApiError("auth.revoke", error)


class Repo:
    def __init__(self):
        self.integrations = {}
        self.apps = {}
        self.fail_create = None
        self.fail_delete = False
        self.log = []

    def get_integration(self, harness_id):
        row = self.integrations.get(harness_id)
        return json.loads(json.dumps(row)) if row else None

    def list_integrations(self):
        return [dict(r) for r in self.integrations.values()]

    def get_app(self, app_id):
        return self.apps.get(app_id)

    def create_integration(self, integration):
        self.log.append("create_integration")
        if self.fail_create:
            raise self.fail_create
        if integration["harness_id"] in self.integrations:
            raise IntegrationExists("harness")
        self.integrations[integration["harness_id"]] = dict(integration)
        self.apps[integration["app_id"]] = {"harness_id": integration["harness_id"]}

    def update_integration(self, harness_id, **fields):
        self.log.append(("update", tuple(sorted(fields))))
        self.integrations[harness_id].update(fields)

    def delete_integration(self, harness_id, app_id):
        self.log.append("delete_integration")
        if self.fail_delete:
            raise RuntimeError("table down")
        self.integrations.pop(harness_id, None)
        self.apps.pop(app_id, None)


class Secrets:
    def __init__(self):
        self.store = {}
        self.deleted = []
        self.tags = None

    def create_secret(self, Name, Description, SecretString, Tags):
        arn = f"arn:aws:secretsmanager:r:1:secret:{Name}"
        self.store[arn] = SecretString
        self.tags = Tags
        return {"ARN": arn}

    def get_secret_value(self, SecretId):
        if SecretId not in self.store:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetSecretValue")
        return {"SecretString": self.store[SecretId]}

    def delete_secret(self, SecretId, ForceDeleteWithoutRecovery):
        assert ForceDeleteWithoutRecovery is True
        self.deleted.append(SecretId)
        if SecretId not in self.store:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DeleteSecret")
        del self.store[SecretId]


class Manager:
    def __init__(self):
        self.log = []

    async def add(self, integration):
        self.log.append(("add", integration["harness_id"]))

    async def remove(self, harness_id):
        self.log.append(("remove", harness_id))

    def status(self, harness_id):
        return {"state": "connected"}


class Audit:
    def __init__(self):
        self.entries = []

    def record(self, actor, action, target, **kwargs):
        self.entries.append((action, target, kwargs))


HARNESS = SimpleNamespace(harness_id="h1", harness_name="helper", harness_arn="arn:harness/h1", status="READY")


def build(api=None, record=("rec-1", "Helper", None), harness=HARNESS, enabled=True):
    repo, secrets, manager, audit = Repo(), Secrets(), Manager(), Audit()
    service = SlackIntegrationService(
        enabled=enabled,
        repository=repo,
        tokens=TokenStore(secrets, "bap/slack", "bap"),
        api=api or Api(),
        harness_lookup=lambda harness_id: harness,
        record_lookup=lambda h: record,
        audit=audit,
    )
    service.manager = manager

    async def socket_app(url):
        return "A1"

    service._socket_app_id = socket_app
    return service, repo, secrets, manager, audit


def run(coro):
    return asyncio.run(coro)


def failed(result):
    return {c["id"] for c in result["checks"] if not c["ok"]}


# ── test ────────────────────────────────────────────────────────────────────

def test_good_tokens_pass_every_check_and_nothing_is_saved():
    service, repo, secrets, *_ = build()
    result = run(service.test("h1", BOT, APP))
    assert result["ok"] is True, result
    assert result["workspace"]["team_name"] == "Acme"
    assert result["workspace"]["app_id"] == "A1"
    assert repo.integrations == {} and secrets.store == {}
    assert BOT not in json.dumps(result) and APP not in json.dumps(result)


def test_swapped_or_malformed_tokens_fail_format():
    service, *_ = build()
    result = run(service.test("h1", APP, BOT))
    assert {"bot_format", "app_format"} <= failed(result)


def test_missing_bot_scopes_are_named():
    service, *_ = build(api=Api(scopes=["chat:write"]))
    result = run(service.test("h1", BOT, APP))
    check = next(c for c in result["checks"] if c["id"] == "bot_scopes")
    assert not check["ok"] and "users:read.email" in check["message"]


def test_tokens_from_two_different_apps_are_refused():
    service, *_ = build(api=Api(app_id="A-other"))
    assert "same_app" in failed(run(service.test("h1", BOT, APP)))


def test_an_app_linked_to_another_harness_is_refused():
    service, repo, *_ = build()
    repo.apps["A1"] = {"harness_id": "h-other"}
    assert "app_unused" in failed(run(service.test("h1", BOT, APP)))


def test_a_harness_that_is_not_ready_or_not_approved_is_refused():
    service, *_ = build(harness=SimpleNamespace(**{**HARNESS.__dict__, "status": "CREATING"}))
    assert "harness_ready" in failed(run(service.test("h1", BOT, APP)))
    service, *_ = build(record=("rec-1", "Helper", "에이전트가 아직 승인되지 않았습니다 (DRAFT)."))
    assert "harness_approved" in failed(run(service.test("h1", BOT, APP)))


def test_disabled_feature_is_503():
    service, *_ = build(enabled=False)
    with pytest.raises(SlackNotConfigured):
        run(service.test("h1", BOT, APP))


# ── create ──────────────────────────────────────────────────────────────────

def test_create_stores_the_tokens_only_in_the_secret_and_connects():
    service, repo, secrets, manager, audit = build()
    view = run(service.create("h1", BOT, APP, ADMIN))
    row = repo.integrations["h1"]
    assert row["record_id"] == "rec-1" and row["status"] == "active"
    assert BOT not in json.dumps(row) and APP not in json.dumps(row)
    assert json.loads(secrets.store[row["secret_arn"]]) == {"bot_token": BOT, "app_token": APP}
    assert row["secret_arn"].endswith(f"bap/slack/h1/{row['integration_id']}")
    assert {"Key": "SlackHarness", "Value": "h1"} in secrets.tags
    assert manager.log == [("add", "h1")]
    assert "secret_arn" not in view and view["connection"] == {"state": "connected"}
    assert audit.entries[0][0] == "slack.integration.create"
    assert BOT not in json.dumps(audit.entries) and "secret_arn" not in json.dumps(audit.entries)


def test_a_failed_save_deletes_the_secret_it_made():
    service, repo, secrets, manager, _ = build()
    repo.fail_create = ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
    with pytest.raises(ClientError):
        run(service.create("h1", BOT, APP, ADMIN))
    assert secrets.store == {} and len(secrets.deleted) == 1
    assert manager.log == []


def test_create_on_a_linked_harness_is_a_conflict_not_a_validation_error():
    service, repo, *_ = build()
    run(service.create("h1", BOT, APP, ADMIN))
    repo.apps.clear()
    with pytest.raises(IntegrationExists):
        run(service.create("h1", BOT, APP, ADMIN))


def test_create_with_failing_checks_raises_validation_failed():
    service, *_ = build(api=Api(scopes=[]))
    with pytest.raises(ValidationFailed) as caught:
        run(service.create("h1", BOT, APP, ADMIN))
    assert "bot_scopes" in failed(caught.value.result)


# ── delete ──────────────────────────────────────────────────────────────────

def test_delete_stops_revokes_then_forgets_and_keeps_conversations():
    api = Api()
    service, repo, secrets, manager, audit = build(api=api)
    run(service.create("h1", BOT, APP, ADMIN))
    result = run(service.delete("h1", ADMIN))
    assert result["deleted"] is True
    assert [s["step"] for s in result["steps"]] == ["stop", "revoke_token", "delete_records"]
    assert manager.log[-1] == ("remove", "h1")
    assert api.revoked == [BOT]
    assert secrets.store == {} and repo.integrations == {} and repo.apps == {}
    # Marked `deleting` before anything else, so every process stops serving it.
    assert repo.log.index(("update", ("status",))) < repo.log.index("delete_integration")
    assert audit.entries[-1][0] == "slack.integration.delete"


def test_a_failed_revoke_keeps_everything_needed_for_a_retry():
    api = Api(revoke_error="network_error")
    service, repo, secrets, *_ = build(api=api)
    run(service.create("h1", BOT, APP, ADMIN))
    with pytest.raises(DeletionIncomplete) as caught:
        run(service.delete("h1", ADMIN))
    assert caught.value.steps[-1] == {"step": "revoke_token", "ok": False, "error": "network_error"}
    row = repo.integrations["h1"]
    assert row["status"] == "deleting" and row["deletion"]["steps"] == caught.value.steps
    assert secrets.store  # still there, so the retry can revoke

    result = run(service.delete("h1", ADMIN))
    assert result["deleted"] is True and api.revoked == [BOT, BOT]
    assert repo.integrations == {} and secrets.store == {}


def test_a_token_already_revoked_or_a_secret_already_gone_counts_as_done():
    service, repo, secrets, *_ = build(api=Api(revoke_error="token_revoked"))
    run(service.create("h1", BOT, APP, ADMIN))
    assert run(service.delete("h1", ADMIN))["deleted"] is True

    service, repo, secrets, *_ = build()
    run(service.create("h1", BOT, APP, ADMIN))
    secrets.store.clear()
    assert run(service.delete("h1", ADMIN))["deleted"] is True
    assert repo.integrations == {}


def test_a_failed_row_delete_is_retried_after_the_secret_is_gone():
    service, repo, secrets, *_ = build()
    run(service.create("h1", BOT, APP, ADMIN))
    repo.fail_delete = True
    with pytest.raises(DeletionIncomplete):
        run(service.delete("h1", ADMIN))
    repo.fail_delete = False
    assert run(service.delete("h1", ADMIN))["deleted"] is True


def test_delete_without_an_integration_is_not_found_but_harness_cleanup_is_fine():
    service, *_ = build()
    with pytest.raises(IntegrationNotFound):
        run(service.delete("h1", ADMIN))
    run(service.cleanup_for_harness("h1", ADMIN))
    disabled, *_ = build(enabled=False)
    run(disabled.cleanup_for_harness("h1", ADMIN))


def test_cleanup_for_harness_surfaces_an_incomplete_cleanup():
    service, *_ = build(api=Api(revoke_error="network_error"))
    run(service.create("h1", BOT, APP, ADMIN))
    with pytest.raises(DeletionIncomplete):
        run(service.cleanup_for_harness("h1", ADMIN))


# ── the record a DM talks to ────────────────────────────────────────────────

class Registry:
    def __init__(self, records, status="APPROVED"):
        self.records = records
        self.status = status

    def agent_records_by_arn(self):
        return self.records

    def chattable_record(self, record_id):
        return SimpleNamespace(status=self.status)


def test_harness_record_requires_a_registered_approved_record(monkeypatch):
    import services.registry_service as rs

    monkeypatch.setattr(rs, "registry_enabled", lambda: True)
    rec = SimpleNamespace(record_id="rec-1", name="Helper")
    assert harness_record(HARNESS, Registry({"arn:harness/h1": rec})) == ("rec-1", "Helper", None)
    assert harness_record(HARNESS, Registry({}))[2] is not None
    assert "DRAFT" in harness_record(HARNESS, Registry({"arn:harness/h1": rec}, status="DRAFT"))[2]


def test_without_a_registry_the_deployed_id_stands_in(monkeypatch):
    import services.registry_service as rs

    monkeypatch.setattr(rs, "registry_enabled", lambda: False)
    assert harness_record(HARNESS, None) == ("deployed:arn:harness/h1", "helper", None)
