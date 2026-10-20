"""Teams from the Organization page: create provisions in the background, edit reapplies, delete refuses with members."""
import inspect
from datetime import datetime, timedelta, timezone
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.org as org  # noqa: E402
from core.auth import AuthUser, require_admin  # noqa: E402
from models.team import TeamConfig, TeamConfigUpdate, TeamCreate  # noqa: E402
from services.audit_service import AuditService  # noqa: E402
from services.org_directory_service import OrgUser  # noqa: E402
from services.team_provisioner import ProvisionError  # noqa: E402
from services.team_service import TeamService  # noqa: E402
from team_table_stub import seed  # noqa: E402

ROLES = {"finance": "arn:aws:iam::1:role/bap-harness-finance"}
TOOLS = ["bap-platform-tools___fetch_url", "bap-platform-tools___calculate", "bap-web-search___WebSearch"]
ADMIN = AuthUser(sub="s-a", username="alice", email="alice@example.com", groups=["admin"])


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
        return list(self.audit)


class Provisioner:
    def __init__(self, fail_step=None):
        self.fail_step = fail_step
        self.calls = []
        self.enabled = True
        self.policies_enabled = True

    def provision(self, team):
        self.calls.append(("provision", team.name))
        if self.fail_step:
            raise ProvisionError(self.fail_step, "AccessDenied")
        return team.model_copy(update={"execution_role_arn": f"arn:aws:iam::1:role/bap/teams/bap-harness-{team.name}",
                                       "policy_ids": {"tools": "p1", "shared": "p2"}})

    def apply_settings(self, team):
        self.calls.append(("apply", team.name, tuple(team.allowed_tools)))
        return team

    def deprovision(self, team):
        self.calls.append(("deprovision", team.name))

    def group_exists(self, name):
        return True

    def role_exists(self, team):
        return True

    def policies_exist(self, team):
        return {k: True for k in team.policy_ids}


class Directory:
    has_pool = True

    def __init__(self, members=None):
        self.members = members or {}

    def group_members(self, team):
        return [OrgUser(sub=s, username=s, email=f"{s}@x", teams=[team]) for s in self.members.get(team, [])]

    def member_subs(self, team):
        return list(self.members.get(team, []))

    def team_sizes(self):
        return {team: len(subs) for team, subs in self.members.items()}

    def team_groups(self):
        return sorted(self.members)


class Usage:
    configured = True

    def begin_read(self):
        pass

    def team_totals(self, start, end):
        return {"finance": {"turns": 7, "model_cost_micros": 1500}}

    daily = {}

    def team_daily_costs(self, start, end):
        return self.daily


class Record:
    def __init__(self, team):
        self.custom_metadata = {"team": team}
        self.record_id = f"r-{team}"
        self.name = f"agent-{team}"
        self.descriptor_type = "A2A"
        self.description = f"{team} 업무 에이전트"


class Registry:
    def __init__(self, teams=()):
        self.records = [Record(t) for t in teams]

    def list_records(self, status=None, **kw):
        return list(self.records)


def setup_function():
    org._run_in_background = lambda fn: fn()  # synchronous for tests
    org._known_tools_override = lambda: list(TOOLS)


def teardown_function():
    for name in ("_teams_override", "_provisioner_override", "_directory_override", "_audit_override",
                 "_usage_override", "_registry_override", "_known_tools_override"):
        setattr(org, name, None)
    org._run_in_background = org._thread_background


def wire(provisioner=None, directory=None, registry=None):
    table = Table()
    seed(table, ROLES)
    org._teams_override = TeamService(table)
    org._provisioner_override = provisioner or Provisioner()
    org._directory_override = directory or Directory()
    org._audit_override = AuditService(table)
    org._usage_override = Usage()
    org._registry_override = registry or Registry()
    return table


def test_admin_only_on_every_write():
    for fn in (org.create_team, org.update_team, org.delete_team, org.provision_team, org.add_member, org.remove_member):
        deps = [p.default.dependency for p in inspect.signature(fn).parameters.values() if hasattr(p.default, "dependency")]
        assert require_admin in deps, fn.__name__


def test_list_joins_members_cost_and_policy_link():
    wire(directory=Directory({"finance": ["s-1", "s-2"]}))
    rows = org.list_teams(user=ADMIN)["teams"]
    fin = next(r for r in rows if r["name"] == "finance")
    assert fin["members"] == 2 and fin["turns_30d"] == 7 and fin["model_cost_micros_30d"] == 1500
    assert fin["managed_by"] == "platform" and fin["policy_linked"] is False


def test_create_provisions_and_marks_ready_and_audits():
    table = wire()
    out = org.create_team(TeamCreate(name="hr", label="인사", allowed_tools=TOOLS[:1]), user=ADMIN)
    assert out.status == "ready" and out.execution_role_arn.endswith("bap-harness-hr") and out.policy_ids["tools"] == "p1"
    assert table.rows["hr"]["status"] == "ready"
    assert [e["action"] for e in table.audit] == ["team.create", "team.provision"]


def test_create_failure_records_step_and_retry_resumes():
    prov = Provisioner(fail_step="iam_role")
    table = wire(provisioner=prov)
    out = org.create_team(TeamCreate(name="hr"), user=ADMIN)
    assert out.status == "error" and out.error["step"] == "iam_role"
    assert table.audit[-1]["result"] == "error"
    prov.fail_step = None
    again = org.provision_team("hr", user=ADMIN)
    assert again.status == "ready"


def test_unknown_tool_is_400_before_any_provisioning():
    prov = Provisioner()
    wire(provisioner=prov)
    with pytest.raises(HTTPException) as caught:
        org.create_team(TeamCreate(name="hr", allowed_tools=["bap-platform-tools___nope"]), user=ADMIN)
    assert caught.value.status_code == 400 and prov.calls == []


def test_duplicate_and_reserved_names_are_400_or_409():
    wire()
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    with pytest.raises(HTTPException) as dup:
        org.create_team(TeamCreate(name="hr"), user=ADMIN)
    assert dup.value.status_code == 409
    with pytest.raises(HTTPException) as bad:
        org.create_team(TeamCreate(name="shared"), user=ADMIN)
    assert bad.value.status_code == 400


def test_update_reapplies_settings_for_platform_teams_only():
    prov = Provisioner()
    table = wire(provisioner=prov)
    table.rows["finance"]["managed_by"] = "workshop"
    org.create_team(TeamCreate(name="hr", allowed_tools=TOOLS[:1]), user=ADMIN)
    out = org.update_team("hr", TeamConfigUpdate(allowed_tools=TOOLS[:2], label="HR"), user=ADMIN)
    assert out.allowed_tools == TOOLS[:2] and ("apply", "hr", tuple(TOOLS[:2])) in prov.calls
    fin = org.update_team("finance", TeamConfigUpdate(allowed_tools=TOOLS[:1]), user=ADMIN)
    assert fin.allowed_tools == TOOLS[:1]
    assert not any(c[0] == "apply" and c[1] == "finance" for c in prov.calls)


def test_delete_refuses_members_and_records_then_succeeds_and_external_rows_go_row_only():
    prov = Provisioner()
    table = wire(provisioner=prov, directory=Directory({"hr": ["s-1"]}), registry=Registry(["hr"]))
    table.rows["finance"]["managed_by"] = "workshop"
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    with pytest.raises(HTTPException) as members:
        org.delete_team("hr", user=ADMIN)
    assert members.value.status_code == 409 and "구성원" in members.value.detail
    org._directory_override = Directory()
    with pytest.raises(HTTPException) as records:
        org.delete_team("hr", user=ADMIN)
    assert records.value.status_code == 409 and "레코드" in records.value.detail
    org._registry_override = Registry()
    org.delete_team("finance", user=ADMIN)
    assert "finance" not in table.rows and not any(c[0] == "deprovision" and c[1] == "finance" for c in prov.calls)
    assert table.audit[-1]["after"] == {"reason": "external_row_only"}
    org.delete_team("hr", user=ADMIN)
    assert "hr" not in table.rows and ("deprovision", "hr") in prov.calls
    assert table.audit[-1]["action"] == "team.delete"


def test_register_unregistered_group_creates_row_and_provisions():
    wire(directory=Directory({"zzz": []}))
    out = org.provision_team("zzz", user=ADMIN)
    assert out.managed_by == "platform" and out.status == "ready"


def test_members_add_remove_use_directory_and_audit():
    class Dir(Directory):
        def __init__(self):
            super().__init__({"hr": ["s-1"]})
            self.calls = []

        def get_user(self, sub):
            return OrgUser(sub=sub, username=sub, email=f"{sub}@x", provider="cognito", teams=["hr"] if sub == "s-1" else [])

        def add_to_team(self, user, team):
            self.calls.append(("add", user.sub, team))

        def remove_from_team(self, user, team):
            self.calls.append(("remove", user.sub, team))

    d = Dir()
    table = wire(directory=d)
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    assert [m["sub"] for m in org.list_members("hr", user=ADMIN)["members"]] == ["s-1"]
    org.add_member("hr", "s-2", user=ADMIN)
    org.remove_member("hr", "s-1", user=ADMIN)
    assert d.calls == [("add", "s-2", "hr"), ("remove", "s-1", "hr")]
    assert [e["action"] for e in table.audit[-2:]] == ["member.add", "member.remove"]
    with pytest.raises(HTTPException) as missing:
        org.add_member("ghost", "s-2", user=ADMIN)
    assert missing.value.status_code == 404


def test_team_records_lists_approved_records_of_that_team():
    wire(registry=Registry(["hr", "finance"]))
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    out = org.team_records("hr", user=ADMIN)
    assert out["records"] == [{"record_id": "r-hr", "name": "agent-hr", "descriptor_type": "A2A", "description": "hr 업무 에이전트"}]


def test_501_without_table():
    org._teams_override = TeamService(None)
    with pytest.raises(HTTPException) as caught:
        org.create_team(TeamCreate(name="hr"), user=ADMIN)
    assert caught.value.status_code == 501


def test_conditioned_tool_is_400(monkeypatch):
    tool = "bap-platform-tools___approve_expense"
    prov = Provisioner()
    wire(provisioner=prov)
    monkeypatch.setattr(org, "TEAM_CONDITIONED_ACTIONS", [tool])
    org._known_tools_override = lambda: TOOLS + [tool]
    with pytest.raises(HTTPException) as caught:
        org.create_team(TeamCreate(name="hr", allowed_tools=[tool]), user=ADMIN)
    assert caught.value.status_code == 400 and prov.calls == []


def test_delete_fails_closed_when_registry_errors():
    class Broken:
        def list_records(self, **kw):
            raise RuntimeError("throttled")

    prov = Provisioner()
    wire(provisioner=prov, registry=Broken())
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    with pytest.raises(HTTPException) as caught:
        org.delete_team("hr", user=ADMIN)
    assert caught.value.status_code == 503 and not any(c[0] == "deprovision" for c in prov.calls)


def test_unconfigured_registry_counts_as_no_records():
    from services.registry_service import RegistryNotConfigured

    class Off:
        def list_records(self, **kw):
            raise RegistryNotConfigured("no id")

    wire(registry=Off())
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    assert org.team_records("hr", user=ADMIN) == {"records": []}


def test_update_during_provisioning_is_409():
    table = wire()
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    org._teams_override.set_status("hr", "provisioning")
    with pytest.raises(HTTPException) as caught:
        org.update_team("hr", TeamConfigUpdate(label="x"), user=ADMIN)
    assert caught.value.status_code == 409


def test_provision_result_merges_onto_fresh_row():
    table = Table()

    class Racing(Provisioner):
        def provision(self, team):
            table.rows["hr"]["label"] = "edited meanwhile"
            return super().provision(team)

    org._teams_override = TeamService(seed(table, ROLES))
    org._provisioner_override = Racing()
    org._directory_override = Directory()
    org._audit_override = AuditService(table)
    org._usage_override = Usage()
    org._registry_override = Registry()
    out = org.create_team(TeamCreate(name="hr", label="orig"), user=ADMIN)
    assert out.label == "edited meanwhile" and out.execution_role_arn.endswith("bap-harness-hr")
    assert out.policy_ids["tools"] == "p1" and out.status == "ready"


def test_double_provision_is_409_while_in_flight():
    wire()
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    org._teams_override.set_status("hr", "provisioning")
    with pytest.raises(HTTPException) as caught:
        org.provision_team("hr", user=ADMIN)
    assert caught.value.status_code == 409


def test_stale_in_flight_provision_can_be_retried():
    table = wire()
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    org._teams_override.set_status("hr", "provisioning")
    table.rows["hr"]["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert org.provision_team("hr", user=ADMIN).status == "ready"


def test_delete_during_provisioning_is_409():
    wire(provisioner=type("P", (Provisioner,), {"provision": lambda self, team: team})())
    org._run_in_background = lambda fn: None  # row stays `provisioning`
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    assert org._teams_override.get("hr").status == "provisioning"
    with pytest.raises(HTTPException) as caught:
        org.delete_team("hr", user=ADMIN)
    assert caught.value.status_code == 409


# ── final review fix wave ─────────────────────────────────────────────────
def _age(table, name, seconds):
    table.rows[name]["updated_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_pattern_entries_pass_the_tool_check_and_gateway_names_are_still_checked(monkeypatch):
    # C1: `@builtin` / `@<server>/<tool>` / `*` are harness patterns, not gateway names.
    table = wire()
    table.rows["finance"] = {"name": "finance", "managed_by": "workshop", "status": "ready",
                             "allowed_tools": ["@builtin", "@bap-platform-tools/approve_expense"]}
    out = org.update_team("finance", TeamConfigUpdate(label="재무", allowed_tools=["@builtin", "@bap-platform-tools/approve_expense"]),
                          user=ADMIN)
    assert out.label == "재무"
    org.create_team(TeamCreate(name="hr", allowed_tools=["@builtin", "*", TOOLS[0]]), user=ADMIN)
    with pytest.raises(HTTPException) as unknown:
        org.update_team("hr", TeamConfigUpdate(allowed_tools=["@builtin", "bap-platform-tools___nope"]), user=ADMIN)
    assert unknown.value.status_code == 400 and "nope" in unknown.value.detail and "@builtin" not in unknown.value.detail
    tool = "bap-platform-tools___approve_expense"
    monkeypatch.setattr(org, "TEAM_CONDITIONED_ACTIONS", [tool])
    org._known_tools_override = lambda: TOOLS + [tool]
    with pytest.raises(HTTPException) as conditioned:
        org.update_team("hr", TeamConfigUpdate(allowed_tools=[tool]), user=ADMIN)
    assert conditioned.value.status_code == 400


def test_external_row_without_group_can_be_cleaned_up_and_not_provisioned():
    prov = Provisioner()
    table = wire(provisioner=prov)
    table.rows["old"] = {"name": "old", "managed_by": "workshop", "status": "ready"}
    with pytest.raises(HTTPException) as external:
        org.provision_team("old", user=ADMIN)
    assert external.value.status_code == 409
    org.delete_team("old", user=ADMIN)
    assert "old" not in table.rows and not any(c[0] == "deprovision" for c in prov.calls)
    entry = table.audit[-1]
    assert entry["action"] == "team.delete" and entry["after"] == {"reason": "external_row_only"}


def test_stale_deleting_row_can_be_deleted_again():
    # I2: a row left in `deleting` (crash, restart) is retried from the drift band.
    prov = Provisioner()
    table = wire(provisioner=prov)
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    org._teams_override.set_status("hr", "deleting")
    with pytest.raises(HTTPException) as fresh:
        org.delete_team("hr", user=ADMIN)
    assert fresh.value.status_code == 409
    _age(table, "hr", org.PROVISION_STALE_SECONDS + 1)
    org.delete_team("hr", user=ADMIN)
    assert "hr" not in table.rows and ("deprovision", "hr") in prov.calls


def test_delete_wraps_unexpected_errors_into_error_status():
    class Crashing(Provisioner):
        def deprovision(self, team):
            raise RuntimeError("EndpointConnectionError")

    table = wire(provisioner=Crashing())
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    with pytest.raises(HTTPException) as caught:
        org.delete_team("hr", user=ADMIN)
    assert caught.value.status_code == 502
    team = org._teams_override.get("hr")
    assert team.status == "error" and team.error["step"] == "unknown"
    assert table.audit[-1]["result"] == "error"


def test_delete_counts_raw_group_members_and_fails_closed():
    # I3: the guard reads ListUsersInGroup directly, not the snapshot-filtered list.
    class Undecorated(Directory):
        def group_members(self, team):
            return []

    class Down(Directory):
        def member_subs(self, team):
            raise RuntimeError("Throttling")

    prov = Provisioner()
    wire(provisioner=prov, directory=Undecorated({"hr": ["s-new"]}))
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    with pytest.raises(HTTPException) as members:
        org.delete_team("hr", user=ADMIN)
    assert members.value.status_code == 409 and "1명" in members.value.detail
    org._directory_override = Down()
    with pytest.raises(HTTPException) as down:
        org.delete_team("hr", user=ADMIN)
    assert down.value.status_code == 503 and "구성원을 확인하지 못해" in down.value.detail
    assert not any(c[0] == "deprovision" for c in prov.calls)


def test_patch_with_null_alert_clears_it():
    # I5: an explicit null is "clear", an omitted field is "unchanged".
    wire()
    org.create_team(TeamCreate(name="hr", daily_cost_alert_usd=5.0), user=ADMIN)
    org.update_team("hr", TeamConfigUpdate(label="HR"), user=ADMIN)
    assert org._teams_override.get("hr").daily_cost_alert_usd == 5.0
    org.update_team("hr", TeamConfigUpdate.model_validate({"daily_cost_alert_usd": None}), user=ADMIN)
    assert org._teams_override.get("hr").daily_cost_alert_usd is None


def test_provision_is_501_without_the_provisioner():
    # I7: otherwise the row flips to provisioning and fails on an empty boundary.
    prov = Provisioner()
    table = wire(provisioner=prov)
    org.create_team(TeamCreate(name="hr"), user=ADMIN)
    org._teams_override.set_status("hr", "error", {"step": "iam_role", "message": "x"})
    prov.enabled = False
    with pytest.raises(HTTPException) as caught:
        org.provision_team("hr", user=ADMIN)
    assert caught.value.status_code == 501 and table.rows["hr"]["status"] == "error"


def test_every_org_route_is_501_without_table():
    # I4: not only create/provision — a PATCH used to answer 200 and persist nothing.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import core.auth as auth

    org._teams_override = TeamService(None, {})
    app = FastAPI()
    app.include_router(org.router)
    paths = [(r.path, m) for r in org.router.routes for m in r.methods]
    assert len(paths) >= 19
    bodies = {"POST": {}, "PATCH": {}, "PUT": {}}
    for who, expected in ((ADMIN, 501), (AuthUser(sub="u", username="bob", groups=["user"]), 403)):
        app.dependency_overrides[auth.current_user] = lambda who=who: who
        client = TestClient(app)
        for path, method in paths:
            url = path.replace("{name}", "hr").replace("{sub}", "s-1")
            resp = client.request(method, url, json=bodies.get(method))
            assert resp.status_code == expected, (who.username, method, url, resp.status_code)


def test_list_and_get_carry_todays_cost_against_the_alert(monkeypatch):
    wire(directory=Directory({"finance": ["s-1"]}))
    monkeypatch.setattr(org.clock, "today", lambda: "2026-10-10")
    org._usage_override.daily = {"finance": {"2026-10-09": 9_000_000, "2026-10-10": 1_250_000}}
    fin = next(r for r in org.list_teams(user=ADMIN)["teams"] if r["name"] == "finance")
    assert fin["today_cost_micros"] == 1_250_000  # yesterday's 9.0 is not today's
    assert org.get_team("finance", user=ADMIN)["today_cost_micros"] == 1_250_000
    # A team with no item today spent nothing today: 0, not None (the ledger was read).
    org._usage_override.daily = {}
    assert org.get_team("finance", user=ADMIN)["today_cost_micros"] == 0


def test_todays_cost_is_null_when_the_ledger_is_unreadable():
    wire()
    org._usage_override.configured = False
    fin = next(r for r in org.list_teams(user=ADMIN)["teams"] if r["name"] == "finance")
    assert fin["today_cost_micros"] is None
