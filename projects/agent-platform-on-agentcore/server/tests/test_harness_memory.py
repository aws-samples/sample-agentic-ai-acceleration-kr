"""
Managed memory on composed harnesses.

The harness loads conversation history from AgentCore Memory itself, so memory
must be enabled and its event expiry pinned: the AWS default is 30 days, which
would silently expire conversations we intend to keep.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.harness import ComposeHarnessRequest  # noqa: E402
from services.harness_service import HarnessService  # noqa: E402


class StubControl:
    def __init__(self):
        self.params = None

    def create_harness(self, **params):
        self.params = params
        return {
            "harness": {
                "harnessId": "h-1",
                "arn": "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1",
                "harnessName": params["harnessName"],
                "status": "CREATING",
            }
        }


def create_params(**request_kwargs):
    service = HarnessService(
        registry=None,
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    control = StubControl()
    service._control = control
    service.create_harness(ComposeHarnessRequest(name="test_agent", **request_kwargs))
    return control.params


def test_managed_memory_is_enabled():
    memory = create_params()["memory"]

    assert "disabled" not in memory
    # SUMMARIZATION only. Its namespace is session-scoped
    # (/strategy/{id}/actor/{actor}/session/{session}/), so recall stays inside
    # one chat. SEMANTIC is deliberately absent: its namespace is actor-scoped,
    # not session-scoped, so facts extracted in one chat leak into the same
    # user's other chats. The runtime path made the same call (see
    # docs runtime-longterm-memory-design: "SEMANTIC 등은 YAGNI 로 제외").
    strategies = memory["managedMemoryConfiguration"]["strategies"]
    assert strategies == ["SUMMARIZATION"]
    assert "SEMANTIC" not in strategies


def test_event_expiry_is_pinned_to_the_maximum():
    """The AWS default is 30 days; conversations would expire out from under us."""
    memory = create_params()["memory"]

    assert memory["managedMemoryConfiguration"]["eventExpiryDuration"] == 365


def test_truncation_strategy_is_explicit():
    """The harness now assembles context, so its truncation governs the model."""
    assert create_params()["truncation"] == {"strategy": "sliding_window"}
