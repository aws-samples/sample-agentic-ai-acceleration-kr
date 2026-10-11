"""What admins did on the Organization page, month-sharded and never blocking the action."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import AuthUser  # noqa: E402
from services.audit_service import ACTIONS, AuditService  # noqa: E402


class Table:
    def __init__(self, fail=False):
        self.audit = []
        self.fail = fail

    def append_audit(self, entry):
        if self.fail:
            raise RuntimeError("table down")
        self.audit.append(entry)

    def list_audit(self, month, start_iso, end_iso):
        return [e for e in self.audit if e["at"][:7] == month and start_iso <= e["at"] <= end_iso]


ALICE = AuthUser(sub="s-a", username="alice", email="alice@example.com", groups=["admin"])


def test_record_writes_a_complete_entry_and_strips_secrets():
    table = Table()
    clock = iter(["2026-10-10T09:00:00+00:00"])
    svc = AuditService(table, clock=lambda: next(clock))
    entry = svc.record(ALICE, "user.invite", "erin@example.com",
                       after={"teams": ["hr"], "temporary_password": "Secret123!"})
    assert entry["id"] and entry["at"] == "2026-10-10T09:00:00+00:00"
    assert entry["actor_sub"] == "s-a" and entry["actor_email"] == "alice@example.com"
    assert entry["action"] == "user.invite" and entry["target"] == "erin@example.com" and entry["result"] == "ok"
    assert "temporary_password" not in entry["after"] and entry["after"]["teams"] == ["hr"]
    assert table.audit == [entry]


def test_record_never_raises_and_unknown_action_is_still_logged():
    svc = AuditService(Table(fail=True))
    assert svc.record(ALICE, "team.create", "hr") is None
    assert "team.create" in ACTIONS
    svc = AuditService(Table())
    assert svc.record(ALICE, "something.new", "x")["action"] == "something.new"


def test_record_scrubs_secrets_nested_in_lists():
    table = Table()
    svc = AuditService(table)
    entry = svc.record(ALICE, "user.invite", "team",
                       after=[{"temporary_password": "secret123", "email": "alice@example.com"}])
    assert len(entry["after"]) == 1
    assert entry["after"][0] == {"email": "alice@example.com"}
    assert "temporary_password" not in entry["after"][0]


def test_list_spans_months_and_filters_by_actor_and_target():
    table = Table()
    times = iter(["2026-09-30T23:00:00+00:00", "2026-10-01T01:00:00+00:00", "2026-10-02T01:00:00+00:00"])
    svc = AuditService(table, clock=lambda: next(times))
    svc.record(ALICE, "team.create", "hr")
    svc.record(AuthUser(sub="s-b", username="bob", groups=["admin"]), "member.add", "hr")
    svc.record(ALICE, "user.disable", "carol@example.com")
    out = svc.list("2026-09-30T00:00:00+00:00", "2026-10-03T00:00:00+00:00")
    assert [e["action"] for e in out] == ["user.disable", "member.add", "team.create"]  # newest first
    assert [e["action"] for e in svc.list("2026-09-30T00:00:00+00:00", "2026-10-03T00:00:00+00:00", actor="s-b")] == ["member.add"]
    assert [e["action"] for e in svc.list("2026-09-30T00:00:00+00:00", "2026-10-03T00:00:00+00:00", target="hr")] == ["member.add", "team.create"]


def test_unconfigured_lists_nothing_and_records_nothing():
    svc = AuditService(None)
    assert svc.configured is False
    assert svc.record(ALICE, "team.create", "hr") is None
    assert svc.list("2026-10-01T00:00:00+00:00", "2026-10-31T00:00:00+00:00") == []
