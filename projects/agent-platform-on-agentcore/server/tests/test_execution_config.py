"""The config a turn executes with is the bound config, nothing more.

`bind_execution` receives the request's config as a dict, may *remove* keys (a
retired basic-chat thread's withdrawn model is dropped so the agent's default
runs), and returns the result. The stream path then used to merge that result
back over the raw request — which revived every key the binding had removed.
Live 2026-10-10 (server v46): a legacy thread pinned to claude-sonnet-5 was
"dropped" on the thread row but the runtime payload and the ledger still carried
claude-sonnet-5, because the request's model_id came back through the merge.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamConfig  # noqa: E402
from services.streaming_service import execution_config  # noqa: E402


def test_keys_the_binding_removed_do_not_come_back():
    request = StreamConfig(model_id="global.anthropic.claude-sonnet-5", basic_chat=True,
                           basic_chat_model_id="global.anthropic.claude-sonnet-5")
    bound = {"registry_record_id": "rec-default", "agent_runtime_arn": "arn:runtime", "adopted_model_id": None}
    out = execution_config(request, bound)
    assert "model_id" not in out
    assert "basic_chat" not in out and "basic_chat_model_id" not in out
    assert out["registry_record_id"] == "rec-default" and out["agent_runtime_arn"] == "arn:runtime"


def test_bound_values_win_over_the_request():
    request = StreamConfig(agent_runtime_arn="arn:client-sent", model_id="m")
    out = execution_config(request, {"agent_runtime_arn": "arn:bound", "model_id": "m"})
    assert out["agent_runtime_arn"] == "arn:bound" and out["model_id"] == "m"


def test_extra_request_fields_survive_through_the_binding_not_the_merge():
    # bind_execution gets the request as a dict (extras included) and returns a
    # copy, so an extra field reaches execution only if the binding kept it.
    request = StreamConfig(model_id="m", custom_flag=True)  # extra="allow"
    bound = {"model_id": "m", "custom_flag": True}
    assert execution_config(request, bound) == bound


def test_no_request_config_is_just_the_bound_config():
    assert execution_config(None, {"a": 1}) == {"a": 1}
