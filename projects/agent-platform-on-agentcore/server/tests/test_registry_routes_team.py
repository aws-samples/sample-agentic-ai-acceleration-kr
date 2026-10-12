"""The registry routes hide other teams' records from non-admins."""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.registry as registry_routes  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from models.registry import RegistryRecordDetail, RegistryRecordSummary  # noqa: E402

_original_service = registry_routes._service
_original_enabled = registry_routes.registry_enabled


def _meta(team):
    return {"team": team} if team else None


RECORDS = [
    RegistryRecordSummary(record_id="s", name="shared", custom_metadata=None),
    RegistryRecordSummary(record_id="h", name="hr", custom_metadata=_meta("hr")),
    RegistryRecordSummary(record_id="f", name="fin", custom_metadata=_meta("finance")),
]


class StubRegistry:
    def list_records(self, **kwargs):
        return list(RECORDS)

    def search_records(self, **kwargs):
        return list(RECORDS)

    def get_record(self, record_id):
        team = {"s": None, "h": "hr", "f": "finance"}[record_id]
        return RegistryRecordDetail(record_id=record_id, name=record_id, custom_metadata=_meta(team))


class Teams:
    def __init__(self, enabled=True):
        self.enabled = enabled


_original_sync = registry_routes._sync


def setup_function(_):
    registry_routes._service = StubRegistry()
    registry_routes.registry_enabled = lambda: True
    registry_routes._teams_override = Teams()


def teardown_function(_):
    registry_routes._service = _original_service
    registry_routes.registry_enabled = _original_enabled
    registry_routes._teams_override = None
    registry_routes._sync = _original_sync


hr = AuthUser(username="m", groups=["team:hr"])
admin = AuthUser(username="a", groups=["admin"])


def ids(result):
    return [r.record_id for r in result["records"]]


def test_list_shows_shared_and_own_team_only():
    assert ids(registry_routes.list_records(user=hr, type=None, status=None, name=None)) == ["s", "h"]


def test_search_filters_the_same_way():
    assert ids(registry_routes.search_records(q="x", type=None, meta=None, user=hr)) == ["s", "h"]


def test_admin_sees_everything():
    assert ids(registry_routes.list_records(user=admin, type=None, status=None, name=None)) == ["s", "h", "f"]
    assert ids(registry_routes.search_records(q="x", type=None, meta=None, user=admin)) == ["s", "h", "f"]


def test_detail_of_another_teams_record_is_404_for_members_only():
    with pytest.raises(HTTPException) as exc:
        registry_routes.get_record("f", user=hr)
    assert exc.value.status_code == 404
    assert registry_routes.get_record("f", user=admin).record_id == "f"
    assert registry_routes.get_record("h", user=hr).record_id == "h"


def test_without_declared_teams_every_record_is_visible():
    # Final review I1: a free-string team label must not hide records when the
    # deployment has no teams.
    registry_routes._teams_override = Teams(enabled=False)
    assert ids(registry_routes.list_records(user=hr, type=None, status=None, name=None)) == ["s", "h", "f"]
    assert registry_routes.get_record("f", user=hr).record_id == "f"


HARNESS = "arn:aws:bedrock-agentcore:us-east-1:1:harness/fin_bot-abc"


class DeployedSync:
    def __init__(self, team="finance", known=True):
        self.team, self.known = team, known

    def deployed_agent_records(self):
        return [
            RegistryRecordSummary(record_id=f"deployed:{HARNESS}", name="fin_bot", harness_arn=HARNESS,
                                  custom_metadata={"team": self.team} if self.team else None,
                                  visibility_known=self.known, source="deployed"),
            RegistryRecordSummary(record_id="deployed:rt", name="rt", source="deployed"),
        ]


def test_deployed_detail_of_another_teams_harness_is_404():
    registry_routes._sync = lambda: DeployedSync()
    with pytest.raises(HTTPException) as exc:
        registry_routes.get_record(f"deployed:{HARNESS}", user=hr)
    assert exc.value.status_code == 404
    fin = AuthUser(username="f", groups=["team:finance"])
    assert registry_routes.get_record(f"deployed:{HARNESS}", user=fin).name == "fin_bot"


def test_registry_off_listing_hides_unknown_and_foreign_deployed_harnesses():
    registry_routes.registry_enabled = lambda: False
    registry_routes._sync = lambda: DeployedSync()
    assert ids(registry_routes.list_records(user=hr, type=None, status=None, name=None)) == ["deployed:rt"]
    registry_routes._sync = lambda: DeployedSync(team=None, known=False)
    fin = AuthUser(username="f", groups=["team:finance"])
    assert ids(registry_routes.list_records(user=fin, type=None, status=None, name=None)) == ["deployed:rt"]
    assert len(registry_routes.list_records(user=admin, type=None, status=None, name=None)["records"]) == 2


def test_listing_with_failed_batch_get_hides_unknown_records_from_members():
    unknown = [r.model_copy(update={"custom_metadata": None, "visibility_known": False}) for r in RECORDS]
    registry_routes._service.list_records = lambda **kw: unknown
    assert ids(registry_routes.list_records(user=hr, type=None, status=None, name=None)) == []
    assert len(registry_routes.list_records(user=admin, type=None, status=None, name=None)["records"]) == 3
