# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""worker CostStreamEntry TTFT 역직렬화 검증.

worker는 엔트리를 생산하지 않고 gateway가 XADD 한 payload를 역직렬화만 한다
(gateway가 유일한 producer). schema_version 2 엔트리는 ttft_ms 를 실어 오고,
구버전(schema_version 1) 엔트리는 ttft_ms 부재 → None 으로 파싱돼야 한다.
"""
import json
from decimal import Decimal

from worker.schemas.cost_stream import CostStreamEntry


def test_worker_entry_parses_ttft_ms():
    entry = CostStreamEntry(
        request_id="r1", user_id="u1", team_id="t1", dept_id="d1",
        model_alias="claude", provider="BEDROCK",
        input_tokens=1, output_tokens=2, cache_creation_tokens=0, cache_read_tokens=0,
        cost_usd=Decimal("0.001"), latency_ms=4200, ttft_ms=800, is_streaming=True,
        estimated_usage=False, downgraded_from=None,
        requested_at="2026-07-08T00:00:00+00:00",
        completed_at="2026-07-08T00:00:00+00:00",
        period="2026-07", date="2026-07-08",
    )
    assert entry.ttft_ms == 800
    assert entry.schema_version == 2


def test_worker_entry_ttft_absent_is_none():
    # 구버전(schema_version 1) gateway가 보낸, ttft_ms 없는 payload 역직렬화
    raw = {
        "request_id": "r2", "user_id": "u1", "team_id": "t1", "dept_id": "d1",
        "model_alias": "claude", "provider": "BEDROCK",
        "input_tokens": 1, "output_tokens": 2, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "cost_usd": "0.001", "latency_ms": 4200,
        "is_streaming": True, "estimated_usage": False,
        "requested_at": "2026-07-08T00:00:00+00:00",
        "completed_at": "2026-07-08T00:00:00+00:00",
        "period": "2026-07", "date": "2026-07-08", "schema_version": 1,
    }
    entry = CostStreamEntry.model_validate_json(json.dumps(raw))
    assert entry.ttft_ms is None
