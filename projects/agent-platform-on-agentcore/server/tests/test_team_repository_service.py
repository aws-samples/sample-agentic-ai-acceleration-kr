"""Teams live in their own table; externally written rows are read-only in lifecycle."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import AuthUser  # noqa: E402
from models.team import TeamConfigUpdate, TeamCreate, validate_team_name  # noqa: E402
from services.team_service import TeamService  # noqa: E402

ROLES = {"finance": "arn:aws:iam::1:role/bap-harness-finance"}


class Table:
    """Just enough of TeamRepository: a dict of rows and an audit list."""

    def __init__(self, fail=False):
        self.rows = {}
        self.audit = []
        self.fail = fail

    def _check(self):
        if self.fail:
            raise RuntimeError("table down")

    def list_teams(self):
        self._check()
        return [dict(r) for r in self.rows.values()]

    def get_team(self, name):
        self._check()
        return dict(self.rows[name]) if name in self.rows else None

    def put_team(self, item):
        self._check()
        self.rows[item["name"]] = dict(item)

    def delete_team(self, name):
        self._check()
        self.rows.pop(name, None)

    def append_audit(self, entry):
        self.audit.append(entry)

    def list_audit(self, month, start_iso, end_iso):
        return [e for e in self.audit if start_iso <= e["at"] <= end_iso]


def test_name_validation_rejects_pattern_and_reserved_words():
    validate_team_name("fin-ops2")
    for bad in ("F", "a", "shared", "admin", "user", "x" * 33, "fin ops", "한글"):
        with pytest.raises(ValueError):
            validate_team_name(bad)


def finance_table():
    table = Table()
    table.rows["finance"] = {"name": "finance", "label": "재무", "execution_role_arn": ROLES["finance"],
                             "managed_by": "platform", "status": "ready"}
    return table


def test_external_rows_keep_their_role_and_label():
    table = Table()
    table.rows["finance"] = {"name": "finance", "label": "재무", "execution_role_arn": "arn:aws:iam::1:role/ws-finance",
                             "managed_by": "workshop", "status": "ready"}
    svc = TeamService(table)
    teams = {t.name: t for t in svc.list()}
    assert teams["finance"].label == "재무"
    assert teams["finance"].execution_role_arn == "arn:aws:iam::1:role/ws-finance"
    assert teams["finance"].managed_by == "workshop"


def test_create_writes_provisioning_row_and_rejects_duplicate_and_reserved():
    table = finance_table()
    svc = TeamService(table)
    team = svc.create(TeamCreate(name="hr", label="인사", allowed_tools=["bap-platform-tools___fetch_url"]), by="alice")
    assert team.status == "provisioning" and team.managed_by == "platform"
    assert team.created_by == "alice" and team.created_at
    assert table.rows["hr"]["allowed_tools"] == ["bap-platform-tools___fetch_url"]
    with pytest.raises(ValueError):
        svc.create(TeamCreate(name="hr"), by="alice")
    with pytest.raises(ValueError):
        svc.create(TeamCreate(name="finance"), by="alice")  # existing row
    with pytest.raises(ValueError):
        svc.create(TeamCreate(name="shared"), by="alice")


def test_save_and_set_status_round_trip():
    table = Table()
    svc = TeamService(table)
    team = svc.create(TeamCreate(name="hr"), by="alice")
    team = team.model_copy(update={"execution_role_arn": "arn:aws:iam::1:role/bap/teams/bap-harness-hr",
                                   "policy_ids": {"tools": "p1"}})
    svc.save(team, by="alice")
    assert svc.get("hr").execution_role_arn.endswith("bap-harness-hr")
    assert svc.role_for("hr").endswith("bap-harness-hr")
    svc.set_status("hr", "error", {"step": "iam_role", "message": "AccessDenied"})
    assert svc.get("hr").status == "error" and svc.get("hr").error["step"] == "iam_role"
    stamped = svc.get("hr").updated_at
    svc.set_status("hr", "ready")
    assert svc.get("hr").status == "ready" and svc.get("hr").error is None
    assert svc.get("hr").updated_at >= stamped != ""


def test_put_keeps_the_old_contract_for_editable_fields():
    svc = TeamService(finance_table())
    out = svc.put("finance", TeamConfigUpdate(label="재무팀", allowed_models=["m1"], description="d"), by="a")
    assert out.label == "재무팀" and out.allowed_models == ["m1"] and out.description == "d"
    assert out.execution_role_arn == ROLES["finance"]
    with pytest.raises(ValueError):
        svc.put("ghost", TeamConfigUpdate(label="x"))


def test_delete_removes_the_row():
    table = finance_table()
    svc = TeamService(table)
    svc.create(TeamCreate(name="hr"), by="a")
    svc.delete("hr")
    assert svc.get("hr") is None
    svc.delete("finance")
    assert svc.list() == []


def test_for_user_and_allowed_models_union_still_work():
    svc = TeamService(finance_table())
    svc.create(TeamCreate(name="hr", allowed_models=["m2"]), by="a")
    svc.put("finance", TeamConfigUpdate(allowed_models=["m1"]))
    user = AuthUser(username="u", groups=["team:finance", "team:hr", "team:ghost"])
    assert [t.name for t in svc.for_user(user)] == ["finance", "hr"]
    assert svc.allowed_models_for(["finance", "hr"]) == {"m1", "m2"}
    assert svc.allowed_models_for(["ghost"]) is None


def test_entra_group_mapping_resolves_and_caches():
    now = [100.0]
    table = Table()
    svc = TeamService(table, clock=lambda: now[0])
    svc.create(TeamCreate(name="hr", entra_group_ids=["g-hr"]), by="a")
    assert svc.teams_for_entra_groups(["g-hr", "g-other"]) == ["hr"]
    # A write through the service shows at once (it drops the cache) …
    svc.create(TeamCreate(name="ops", entra_group_ids=["g-ops"]), by="a")
    assert svc.teams_for_entra_groups(["g-ops"]) == ["ops"]
    # … a change made behind its back waits for the TTL.
    table.rows["ops"]["entra_group_ids"] = ["g-new"]
    assert svc.teams_for_entra_groups(["g-new"]) == []
    now[0] += 61
    assert svc.teams_for_entra_groups(["g-new"]) == ["ops"]


def test_without_table_there_are_no_teams_and_enabled_tracks_rows():
    svc = TeamService(None)
    assert svc.configured is False and svc.enabled is False and svc.list() == []
    table = Table()
    empty = TeamService(table)
    assert empty.enabled is False
    empty.create(TeamCreate(name="hr"), by="a")
    assert empty.enabled is True


def test_table_failure_reads_as_no_teams_and_write_raises():
    from services.team_service import TeamStorageUnavailable
    table = Table(fail=True)
    svc = TeamService(table)
    assert svc.list() == []
    with pytest.raises(TeamStorageUnavailable):
        svc.put("finance", TeamConfigUpdate(label="x"))


def test_name_validation_rejects_a_trailing_newline():
    for bad in ("ab\n", "admin\n", "fin\nops"):
        with pytest.raises(ValueError):
            validate_team_name(bad)


def test_name_validation_keeps_the_policy_name_within_48_chars(monkeypatch):
    import core.config as cfg
    monkeypatch.setattr(cfg, "PLATFORM", "ws-apbac")
    # ws_apbac_team_<name>_shared: 8 + 6 + n + 7 <= 48 → n <= 27
    validate_team_name("a" * 27)
    with pytest.raises(ValueError, match="27자"):
        validate_team_name("a" * 28)
    monkeypatch.setattr(cfg, "PLATFORM", "bap")
    validate_team_name("a" * 32)
