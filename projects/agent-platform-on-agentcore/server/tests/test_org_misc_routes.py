"""Drift, audit and the tool list the team dialog picks from."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.org as org  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from models.team import TeamCreate  # noqa: E402
from services.audit_service import AuditService  # noqa: E402
from services.team_service import TeamService  # noqa: E402

ADMIN = AuthUser(sub="s-a", username="alice", groups=["admin"])


class Table:
    def __init__(self):
        self.rows, self.audit = {}, []

    def list_teams(self):
        return [dict(r) for r in self.rows.values()]

    def get_team(self, name):
        return dict(self.rows[name]) if name in self.rows else None

    def put_team(self, item):
        self.rows[item["name"]] = dict(item)

    def delete_team(self, name):
        self.rows.pop(name, None)

    def append_audit(self, e):
        self.audit.append(e)

    def list_audit(self, month, s, e):
        return [x for x in self.audit if x["at"][:7] == month]


class Provisioner:
    enabled = True
    policies_enabled = True

    def __init__(self):
        self.probed = []

    def role_exists(self, team):
        self.probed.append(team.name)
        return team.name != "broken"

    def policies_exist(self, team):
        return {k: True for k in team.policy_ids}


class Directory:
    has_pool = True

    def team_groups(self):
        return ["finance", "hr", "zzz"]


def setup_function():
    table = Table()
    org._teams_override = TeamService(table)
    org._provisioner_override = Provisioner()
    org._directory_override = Directory()
    org._audit_override = AuditService(table, clock=lambda: "2026-10-10T12:00:00+00:00")
    org._known_tools_override = lambda: ["bap-platform-tools___fetch_url", "bap-web-search___WebSearch"]
    org._table = table


def teardown_function():
    for name in ("_teams_override", "_provisioner_override", "_directory_override", "_audit_override", "_known_tools_override"):
        setattr(org, name, None)


def test_drift_probes_only_ready_platform_teams():
    svc = org._teams_override
    svc.create(TeamCreate(name="hr"), by="a")
    svc.set_status("hr", "ready")
    svc.create(TeamCreate(name="broken"), by="a")
    svc.set_status("broken", "ready")
    svc.create(TeamCreate(name="pending"), by="a")
    # "finance" and "zzz" are groups in the Directory fixture without a row.
    out = org.drift(user=ADMIN)
    kinds = sorted((i["kind"], i["team"]) for i in out["items"])
    # broken has missing_role and missing_group; finance/zzz are unregistered groups
    assert kinds == [("missing_group", "broken"), ("missing_role", "broken"), ("unregistered_group", "finance"), ("unregistered_group", "zzz")]
    assert sorted(org._provisioner_override.probed) == ["broken", "hr"]
    assert out["checked_at"]


def test_audit_defaults_to_last_30_days_and_filters():
    org._audit_override.record(ADMIN, "team.create", "hr")
    out = org.audit(from_="", to="", actor="", target="", user=ADMIN)
    assert [e["action"] for e in out["entries"]] == ["team.create"]
    assert org.audit(from_="", to="", actor="nobody", target="", user=ADMIN)["entries"] == []
    assert out["from"] < out["to"]


def test_tools_lists_known_and_marks_shared(monkeypatch):
    monkeypatch.setattr(org, "TEAM_SHARED_ACTIONS", ["bap-web-search___WebSearch"])
    out = org.tools(user=ADMIN)
    assert out["tools"] == ["bap-platform-tools___fetch_url", "bap-web-search___WebSearch"]
    assert out["shared"] == ["bap-web-search___WebSearch"]


def test_tools_lists_conditioned_actions(monkeypatch):
    monkeypatch.setattr(org, "TEAM_SHARED_ACTIONS", ["bap-web-search___WebSearch"])
    monkeypatch.setattr(org, "TEAM_CONDITIONED_ACTIONS", [])
    out = org.tools(user=ADMIN)
    assert out["conditioned"] == []


def test_drift_without_a_pool_skips_group_comparison():
    # I7: an Entra-only stack has no `team:` groups; every ready team used to show missing_group.
    class NoPool(Directory):
        has_pool = False

        def team_groups(self):
            raise AssertionError("must not be called without a pool")

    svc = org._teams_override
    svc.create(TeamCreate(name="hr"), by="a")
    svc.set_status("hr", "ready")
    org._directory_override = NoPool()
    kinds = sorted(i["kind"] for i in org.drift(user=ADMIN)["items"])
    assert "missing_group" not in kinds and "unregistered_group" not in kinds


def test_drift_reports_a_stale_provisioning_row():
    svc = org._teams_override
    svc.create(TeamCreate(name="hr"), by="a")
    org._table.rows["hr"]["updated_at"] = "2026-01-01T00:00:00+00:00"
    items = [i for i in org.drift(user=ADMIN)["items"] if i["team"] == "hr"]
    assert [(i["kind"], i["actions"]) for i in items] == [("stale_provisioning", ["retry"])]
