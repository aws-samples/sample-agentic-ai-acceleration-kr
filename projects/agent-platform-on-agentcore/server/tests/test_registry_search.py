"""
Search-path tests for RegistryService.

The registry's hybrid search is the discovery surface, so what matters here is
the request we send AWS (bounded maxResults, no server-side filters) and that
we hand back its relevance order untouched. Type narrowing happens client-side
because SearchDiscoverableRegistryRecords takes no type filter.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.registry_service import RegistryService  # noqa: E402


class StubData:
    """Stands in for the agent-registry data-plane client."""

    def __init__(self, records=None):
        self.calls = []
        self._records = records if records is not None else []

    def search_discoverable_registry_records(self, **params):
        self.calls.append(params)
        return {"registryRecords": self._records}


def _record(name, record_type="AGENT"):
    return {
        "recordId": f"rec-{name}",
        "name": name,
        "recordType": record_type,
        "status": "APPROVED",
    }


def _service(records=None):
    service = RegistryService(registry_id="reg-1", region="us-east-1")
    stub = StubData(records)
    # The service memoises its boto3 clients on these attributes.
    service._registry_data = stub
    return service, stub


def test_max_results_stays_within_the_api_limit():
    service, stub = _service()
    service.search_records(query="weather")
    assert stub.calls[0]["maxResults"] <= 20


def test_no_server_side_filters_are_sent():
    service, stub = _service()
    service.search_records(query="weather", descriptor_types=["A2A", "MCP"])
    assert "filters" not in stub.calls[0]
    assert stub.calls[0]["registryIds"] == ["reg-1"]


def test_search_filters_by_record_type_client_side():
    records = [_record("agent", "AGENT"), _record("tools", "MCP")]
    service, _ = _service(records)
    result = service.search_records(query="x", descriptor_types=["A2A"])
    assert [r.name for r in result] == ["agent"]


def test_relevance_order_is_preserved():
    # AWS merges semantic and keyword ranking; re-sorting would throw that away.
    records = [_record("zebra"), _record("alpha"), _record("middle")]
    service, _ = _service(records)
    result = service.search_records(query="anything")
    assert [r.name for r in result] == ["zebra", "alpha", "middle"]


def test_results_are_not_filtered_client_side_when_no_types_requested():
    records = [_record("agent", "AGENT"), _record("tools", "MCP")]
    service, _ = _service(records)
    result = service.search_records(query="x")
    assert len(result) == 2


class StubControl:
    """Stands in for the agent-registry-control client."""

    def __init__(self, records=None):
        self.calls = []
        self._records = records if records is not None else []

    def list_registry_records(self, **params):
        self.calls.append(params)
        return {"registryRecords": self._records}


def _list_service(records=None):
    service = RegistryService(registry_id="reg-1", region="us-east-1")
    stub = StubControl(records)
    service._registry_control = stub
    return service, stub


def test_list_passes_name_through_as_a_filter():
    service, stub = _list_service()
    service.list_records(name="strands_agent")
    assert {"name": "name", "values": ["strands_agent"]} in stub.calls[0]["filters"]


def test_list_omits_filters_when_no_narrowing_given():
    service, stub = _list_service()
    service.list_records()
    assert "filters" not in stub.calls[0]
