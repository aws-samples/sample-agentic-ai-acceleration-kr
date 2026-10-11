"""Where the console and the table disagree, and what fixes each."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.team import TeamConfig  # noqa: E402
from services.org_drift import compute_drift  # noqa: E402


def team(name, **kw):
    base = dict(name=name, managed_by="platform", status="ready", execution_role_arn="arn:r", policy_ids={"tools": "p1"})
    base.update(kw)
    return TeamConfig(**base)


def kinds(items):
    return sorted((i["kind"], i["team"]) for i in items)


def test_group_without_row_is_unregistered():
    out = compute_drift(["finance", "zzz"], [team("finance", managed_by="workshop")], {})
    assert kinds(out) == [("unregistered_group", "zzz")]
    assert out[0]["actions"] == ["register"]


def test_row_without_group_is_missing_group_for_ready_platform_and_external_teams():
    out = compute_drift([], [team("hr"), team("finance", managed_by="workshop")],
                        {"hr": {"role": True, "policies": {"tools": True}}})
    assert kinds(out) == [("missing_group", "finance"), ("missing_group", "hr")]
    assert set(next(i for i in out if i["team"] == "hr")["actions"]) == {"cleanup", "recreate_group"}
    finance = next(i for i in out if i["team"] == "finance")
    # The group is the external stack's: the server will not recreate it, the row can only be dropped.
    assert finance["actions"] == ["cleanup"] and "외부" in finance["detail"]


def test_probe_failures_name_role_and_policy():
    out = compute_drift(["hr"], [team("hr")], {"hr": {"role": False, "policies": {"tools": False}}})
    assert kinds(out) == [("missing_policy", "hr"), ("missing_role", "hr")]
    assert all(i["actions"] == ["retry"] for i in out)


def test_provisioning_error_and_in_progress_rows_are_not_probed():
    out = compute_drift(["hr", "ops"], [team("hr", status="error", error={"step": "iam_role", "message": "AccessDenied"}),
                                        team("ops", status="provisioning")], {})
    assert kinds(out) == [("provisioning_error", "hr")]
    assert "iam_role" in out[0]["detail"] and out[0]["actions"] == ["retry"]


def test_external_rows_are_not_probed():
    # No probe result for an external team is not drift: its role and policies are not ours.
    out = compute_drift(["ops"], [team("ops", managed_by="workshop", policy_ids={})], {})
    assert out == []


def test_consistent_state_has_no_drift():
    out = compute_drift(["hr", "finance"], [team("hr"), team("finance", managed_by="workshop")],
                        {"hr": {"role": True, "policies": {"tools": True}}})
    assert out == []


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def stamp(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def test_stale_in_flight_rows_offer_retry_and_fresh_ones_do_not():
    rows = [team("p-old", status="provisioning", updated_at=stamp(301)),
            team("p-new", status="provisioning", updated_at=stamp(10)),
            team("d-old", status="deleting", updated_at=stamp(900)),
            team("d-new", status="deleting", updated_at=stamp(299))]
    out = compute_drift(["p-old", "p-new", "d-old", "d-new"], rows, {}, now=NOW)
    assert kinds(out) == [("stale_deleting", "d-old"), ("stale_provisioning", "p-old")]
    assert next(i for i in out if i["team"] == "p-old")["actions"] == ["retry"]
    assert next(i for i in out if i["team"] == "d-old")["actions"] == ["retry_delete"]


def test_without_a_pool_group_kinds_are_skipped():
    out = compute_drift(None, [team("hr"), team("finance", managed_by="workshop")],
                        {"hr": {"role": True, "policies": {"tools": True}}})
    assert out == []
