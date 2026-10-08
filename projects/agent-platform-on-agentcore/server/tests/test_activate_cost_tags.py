"""Cost allocation tag activation.

The three states matter more than the happy path, because the interesting one is
`not_registered`: the tags exist on resources and still cannot be activated,
because the billing tag registry is a separate system that lags up to 24h. A
script that reported that as a generic failure would send someone hunting for a
permissions problem that is not there.
"""
import scripts.activate_cost_tags as mod


class StubCostExplorer:
    def __init__(self, listed=(), error=None, update_errors=()):
        self.listed = list(listed)
        self.error = error
        # `Errors` is the ONLY member of the real UpdateCostAllocationTagsStatus
        # response, a list of {TagKey, Code, Message}: this API reports per-key
        # refusal inside a 200. A stub that always returned an empty list would
        # make "we activated it" unfalsifiable.
        self.update_errors = list(update_errors)
        self.updates = []

    def list_cost_allocation_tags(self, **params):
        keys = params.get("TagKeys")
        tags = [t for t in self.listed if not keys or t["TagKey"] in keys]
        return {"CostAllocationTags": tags}

    def update_cost_allocation_tags_status(self, **params):
        self.updates.append(params)
        if self.error:
            raise self.error
        return {"Errors": list(self.update_errors)}


def test_a_key_the_billing_registry_has_never_seen_is_reported_as_such():
    stub = StubCostExplorer(listed=[])
    result = mod.activate(stub, ["Platform", "AgentName"])

    assert result["status"] == "not_registered"
    assert result["keys"] == ["Platform", "AgentName"]
    # No write attempted: there is nothing to write to yet.
    assert stub.updates == []
    assert "24" in result["detail"]


def test_already_active_keys_are_left_alone():
    stub = StubCostExplorer(listed=[
        {"TagKey": "Platform", "Status": "Active"},
        {"TagKey": "AgentName", "Status": "Active"},
    ])
    result = mod.activate(stub, ["Platform", "AgentName"])

    assert result["status"] == "already_active"
    assert stub.updates == []


def test_registered_but_inactive_keys_are_activated():
    stub = StubCostExplorer(listed=[
        {"TagKey": "Platform", "Status": "Inactive"},
        {"TagKey": "AgentName", "Status": "Inactive"},
    ])
    result = mod.activate(stub, ["Platform", "AgentName"])

    assert result["status"] == "activated"
    assert stub.updates[0]["CostAllocationTagsStatus"] == [
        {"TagKey": "Platform", "Status": "Active"},
        {"TagKey": "AgentName", "Status": "Active"},
    ]


def test_a_partially_registered_set_activates_what_it_can_and_says_what_it_could_not():
    stub = StubCostExplorer(listed=[{"TagKey": "Platform", "Status": "Inactive"}])
    result = mod.activate(stub, ["Platform", "AgentName"])

    assert result["status"] == "activated"
    assert result["keys"] == ["Platform"]
    assert "AgentName" in result["detail"]


def test_a_key_refused_inside_a_200_is_not_reported_as_activated():
    """UpdateCostAllocationTagsStatus reports per-key failure in `Errors`, not by
    raising. Reporting "activated" for a refused key is the one thing this script
    exists not to do."""
    stub = StubCostExplorer(
        listed=[
            {"TagKey": "Platform", "Status": "Inactive"},
            {"TagKey": "AgentName", "Status": "Inactive"},
        ],
        update_errors=[
            {"TagKey": "AgentName", "Code": "InternalFailure", "Message": "try later"}
        ],
    )
    result = mod.activate(stub, ["Platform", "AgentName"])

    assert result["status"] == "partial"
    assert result["keys"] == ["Platform"]
    assert "AgentName" in result["detail"]
    assert "InternalFailure" in result["detail"]


def test_every_key_refused_is_reported_as_failed():
    stub = StubCostExplorer(
        listed=[{"TagKey": "Platform", "Status": "Inactive"}],
        update_errors=[
            {"TagKey": "Platform", "Code": "AccessDeniedException", "Message": "nope"}
        ],
    )
    result = mod.activate(stub, ["Platform"])

    assert result["status"] == "failed"
    assert result["keys"] == []
    assert "AccessDeniedException" in result["detail"]
