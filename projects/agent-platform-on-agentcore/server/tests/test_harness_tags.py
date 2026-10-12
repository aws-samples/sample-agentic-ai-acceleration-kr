"""Harness tags are the only way to split *billed* cost per agent.

They propagate to the managed Runtime, endpoint and Memory. `TagResource` does
accept an existing harness and its companion runtime, so a missed tag is
recoverable — but cost data is not retroactive, so every hour billed before the
tag landed stays in the untagged bucket. That is why they go on at creation
rather than being reconciled later.

No owner tag: `routes/harness.py` says harnesses carry no owner, and per-user
attribution comes from this platform's own usage counters instead.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import PLATFORM  # noqa: E402
from models.harness import ComposeHarnessRequest  # noqa: E402
from services.harness_service import HarnessService  # noqa: E402

HARNESS_ID = "writer-abc123"
HARNESS_ARN = f"arn:aws:bedrock-agentcore:us-east-1:1:harness/{HARNESS_ID}"


class RecordingControl:
    """Captures the CreateHarness params without calling AWS."""

    def __init__(self):
        self.params = None

    def create_harness(self, **params):
        self.params = params
        return {
            "harness": {
                "harnessId": HARNESS_ID,
                "arn": HARNESS_ARN,
                "harnessName": params["harnessName"],
                "status": "CREATING",
            }
        }


def created_with(**kwargs) -> dict:
    control = RecordingControl()
    service = HarnessService(
        registry=object(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    service._control = control
    service._resolve_mcp_tools = lambda ids: []
    service._resolve_skills = lambda record_ids, paths: []
    # Use the provided name if given, otherwise default to "academic_writer"
    name = kwargs.pop("name", "academic_writer")
    service.create_harness(ComposeHarnessRequest(name=name, **kwargs))
    return control.params


def test_a_composed_harness_is_tagged_for_cost_attribution():
    tags = created_with().get("tags")

    assert tags, (
        "no tags on CreateHarness — the managed Runtime and Memory it creates "
        "cannot be attributed to this agent in Cost Explorer, and tagging them "
        "later does not backfill the cost data"
    )
    assert tags["Platform"] == PLATFORM


def test_the_tag_set_identifies_which_agent_the_cost_belongs_to():
    """`Platform` alone groups the whole stack into one line item."""
    # Use "my-writer-agent" which sanitizes to "my_writer_agent" (hyphens become underscores).
    # This ensures the test actually exercises the sanitization path, not just passing
    # through an already-valid name.
    params = created_with(name="my-writer-agent")
    assert params["tags"]["AgentName"] == "my_writer_agent"


def test_no_owner_tag_is_invented():
    """Harnesses carry no owner in this system; per-user usage lives in the
    usage table, not in a billing tag."""
    assert "OwnerSub" not in created_with()["tags"]


class TagControl:
    def __init__(self, tags=None, fail=False):
        self.tags, self.fail, self.calls = tags or {}, fail, 0

    def list_tags_for_resource(self, resourceArn):
        self.calls += 1
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"tags": self.tags}


def _tag_service(control):
    service = HarnessService(registry=object(), region="us-east-1", execution_role_arn="arn:aws:iam::1:role/h")
    service._control = control
    return service


def test_team_tag_is_read_back_normalised_and_cached():
    # The deployed-record fallback reads the team from this tag (final review C1).
    control = TagControl({"Platform": PLATFORM, "Team": "Finance"})
    service = _tag_service(control)
    assert service.team_tag_of(HARNESS_ARN) == "finance"
    assert service.team_tag_of(HARNESS_ARN) == "finance"
    assert control.calls == 1
    assert _tag_service(TagControl({"Platform": PLATFORM})).team_tag_of(HARNESS_ARN) is None


def test_unreadable_tags_raise_and_are_not_cached():
    control = TagControl(fail=True)
    service = _tag_service(control)
    for _ in range(2):
        try:
            service.team_tag_of(HARNESS_ARN)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the read failure to surface")
    assert control.calls == 2
