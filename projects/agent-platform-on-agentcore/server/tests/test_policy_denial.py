"""Telling a policy denial apart from an ordinary tool failure, and whose team a turn is."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.policy_denial import is_policy_denial, resolve_turn_team  # noqa: E402


def test_denials_are_recognised_in_text_and_structured_results():
    assert is_policy_denial("Error: tool call denied by policy (authorization_decision=DENY)")
    assert is_policy_denial({"error": "AccessDeniedException: not authorized by gateway policy"})
    assert is_policy_denial([{"text": "Policy engine returned DENY for bap-platform-tools___lookup_salary"}])
    # Live, 2026-10-10 (bap, ENFORCE): the harness tool result for approve_expense
    # amount=25000, and the gateway's own JSON-RPC error for a call with no permit.
    assert is_policy_denial("Tool execution failed: Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default).]")
    assert is_policy_denial({"code": -32002, "message": "Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default).]"})


def test_ordinary_failures_are_not_denials():
    assert not is_policy_denial({"error": "Cannot divide by zero."})
    assert not is_policy_denial("Unknown tool: foo")
    assert not is_policy_denial("The policy document was summarised")   # 'policy' alone is not enough
    assert not is_policy_denial(None)
    # "unauthorized" is a denial word; without a policy word it is an authn
    # failure, not a policy denial (the old "authoriz" marker matched it twice).
    assert not is_policy_denial("401 Unauthorized")
    assert not is_policy_denial({"error": "HTTP 401 Unauthorized: missing authorization header"})
    assert not is_policy_denial("Request forbidden: AccessDenied for s3:GetObject")


def test_turn_team_prefers_the_record_team_the_caller_belongs_to():
    assert resolve_turn_team(["hr", "finance"], "finance") == "finance"
    assert resolve_turn_team(["hr"], "finance") == "hr"        # not a member: their own team
    assert resolve_turn_team(["hr"], None) == "hr"
    assert resolve_turn_team([], "finance") is None             # admin / teamless: unattributed
    assert resolve_turn_team([], None) is None


DENIAL = "Tool execution failed: Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default).]"


def test_undeclared_team_groups_are_not_attributed():
    # Final review M5: a `team:` group terraform never declared got a TEAMS#
    # row that no panel renders, and the turn vanished from "unattributed" too.
    assert resolve_turn_team(["legacy", "hr"], None, {"hr", "finance"}) == "hr"
    assert resolve_turn_team(["legacy"], "legacy", {"hr"}) is None
    assert resolve_turn_team(["legacy"], None) == "legacy"   # no declared set: unchanged


def test_a_denial_streamed_over_several_deltas_is_recorded_once():
    # Final review I7: the harness adapter re-emits a tool result per delta with
    # the accumulated text and status="error", so once the prefix matched every
    # later delta of the same result counted the denial again.
    import asyncio

    from models.common import StreamRequest
    from services.streaming_service import StreamingService
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_failed_tool_calls_are_marked import STOP, TOOL_TURN, VALUES, ScriptedClient, StubThreadService

    class Usage:
        configured = True

        def __init__(self):
            self.denials = []

        def record_turn(self, **kwargs):
            pass

        def record_policy_denial(self, **kwargs):
            self.denials.append(kwargs)

    deltas = [DENIAL[:60] + DENIAL[60:i] for i in (100, 130, len(DENIAL))]
    events = TOOL_TURN + [
        {"event": {"toolResult": {"toolUseId": "tu-1", "result": text, "status": "error"}}}
        for text in deltas
    ] + [STOP]
    usage = Usage()
    client = ScriptedClient(events)
    service = StreamingService(thread_service=StubThreadService(), agentcore_client=client, usage_service=usage)
    service._get_agent_client = lambda config=None: client

    async def scenario():
        response = await service.stream_thread_execution("t-1", StreamRequest(values=VALUES))
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())
    assert [is_policy_denial(t) for t in deltas] == [True, True, True]
    assert len(usage.denials) == 1 and usage.denials[0]["tool_name"] == "calculate"
