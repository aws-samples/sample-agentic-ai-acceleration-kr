# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import inspect
from decimal import Decimal

from app.services.cost_recorder import CostRecorder


def test_finalize_accepts_availability_fallback_from():
    sig = inspect.signature(CostRecorder.finalize)
    assert "availability_fallback_from" in sig.parameters
    # default must be None so existing callers are unaffected
    assert sig.parameters["availability_fallback_from"].default is None


def test_cost_stream_entry_carries_availability_fallback_from():
    import json
    from app.schemas.cost_stream import CostStreamEntry

    entry = CostStreamEntry.make(
        request_id="req-test-001",
        user_id="user-test-001",
        team_id="00000000-0000-4000-a000-000000000003",
        dept_id="00000000-0000-4000-a000-000000000002",
        model_alias="claude-opus-4",
        provider="bedrock",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cost_usd=Decimal("0.000123"),
        latency_ms=250,
        is_streaming=False,
        estimated_usage=False,
        downgraded_from=None,
        availability_fallback_from="claude-opus-4-8",
    )
    dumped = json.loads(entry.model_dump_json())
    assert dumped["availability_fallback_from"] == "claude-opus-4-8"
