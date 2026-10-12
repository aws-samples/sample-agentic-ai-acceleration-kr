"""Which registry record is the deployment's default agent.

The retired basic-chat path addressed the default runtime by ARN with no record.
Now the default agent is simply the record that points at AGENT_RUNTIME_ARN, and
the web pins it to the top of the picker by this flag. The flag is derived, never
stored: a redeploy that changes the default runtime moves it without a record edit.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.registry import RegistryRecordSummary  # noqa: E402
from services import default_agent  # noqa: E402
from services.default_agent import default_record, is_default_record, mark_default  # noqa: E402
from services.registry_service import RegistryNotConfigured  # noqa: E402

RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_default-AbC"
OTHER = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_other-XyZ"
HARNESS = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"


def _record(record_id, runtime=None, harness=None, status="APPROVED"):
    return RegistryRecordSummary(
        record_id=record_id, name=record_id, agent_runtime_arn=runtime, harness_arn=harness, status=status
    )


def test_the_record_pointing_at_the_default_runtime_is_the_default():
    assert is_default_record(_record("a", runtime=RUNTIME), RUNTIME) is True
    assert is_default_record(_record("b", runtime=OTHER), RUNTIME) is False


def test_a_harness_record_is_never_the_default():
    # A harness carries its companion runtime ARN; even if that equalled the
    # default runtime, the harness is what answers, not the runtime.
    assert is_default_record(_record("h", runtime=RUNTIME, harness=HARNESS), RUNTIME) is False


def test_without_a_default_runtime_nothing_is_the_default():
    assert is_default_record(_record("a", runtime=RUNTIME), "") is False
    assert is_default_record(_record("a", runtime=RUNTIME), None) is False


def test_the_default_runtime_comes_from_the_environment_when_not_given(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", RUNTIME)
    assert is_default_record(_record("a", runtime=RUNTIME)) is True
    monkeypatch.delenv("AGENT_RUNTIME_ARN")
    assert is_default_record(_record("a", runtime=RUNTIME)) is False


def test_mark_default_flags_exactly_one_record_and_returns_the_list():
    records = [_record("a", runtime=OTHER), _record("b", runtime=RUNTIME), _record("c", harness=HARNESS)]
    out = mark_default(records, RUNTIME)
    assert out is not None and [r.is_default for r in out] == [False, True, False]
    assert RegistryRecordSummary(record_id="x", name="x").is_default is False  # model default


class _Registry:
    def __init__(self, records=None, exc=None):
        self._records, self._exc = records or [], exc

    def agent_records(self):
        if self._exc:
            raise self._exc
        return self._records


def test_default_record_prefers_the_registry():
    reg = _Registry([_record("a", runtime=OTHER), _record("b", runtime=RUNTIME)])
    assert default_record(reg, RUNTIME).record_id == "b"


def test_default_record_falls_back_to_the_deployed_listing(monkeypatch):
    # Registry off (RegistryNotConfigured) or without a matching record: the
    # deployed-resource listing the registry-off picker uses answers instead.
    fallback = [_record(f"deployed:{RUNTIME}", runtime=RUNTIME)]
    monkeypatch.setattr(default_agent, "_deployed_records", lambda: fallback)
    assert default_record(_Registry(exc=RegistryNotConfigured("off")), RUNTIME).record_id == f"deployed:{RUNTIME}"
    assert default_record(_Registry([_record("a", runtime=OTHER)]), RUNTIME).record_id == f"deployed:{RUNTIME}"


def test_default_record_is_none_when_nothing_points_at_the_runtime(monkeypatch):
    monkeypatch.setattr(default_agent, "_deployed_records", lambda: [])
    assert default_record(_Registry([]), RUNTIME) is None
    assert default_record(_Registry([_record("b", runtime=RUNTIME)]), "") is None


def test_default_record_survives_a_registry_outage(monkeypatch):
    monkeypatch.setattr(default_agent, "_deployed_records", lambda: [_record(f"deployed:{RUNTIME}", runtime=RUNTIME)])
    assert default_record(_Registry(exc=RuntimeError("boom")), RUNTIME) is not None


def test_a_deprecated_or_unapproved_record_is_not_the_default():
    # A harness delete / re-sync leaves a DEPRECATED record on the same ARN; a
    # fresh DRAFT is not chattable either. Only what chat can bind to is default.
    deprecated = RegistryRecordSummary(record_id="old", name="old", agent_runtime_arn=RUNTIME, status="DEPRECATED")
    draft = RegistryRecordSummary(record_id="new", name="new", agent_runtime_arn=RUNTIME, status="DRAFT")
    edited = RegistryRecordSummary(record_id="ed", name="ed", agent_runtime_arn=RUNTIME, status="DRAFT", discoverable=True)
    approved = RegistryRecordSummary(record_id="ok", name="ok", agent_runtime_arn=RUNTIME, status="APPROVED")
    assert is_default_record(deprecated, RUNTIME) is False
    assert is_default_record(draft, RUNTIME) is False
    assert is_default_record(edited, RUNTIME) is True   # approved revision still served
    assert is_default_record(approved, RUNTIME) is True
    assert default_record(_Registry([deprecated, draft, approved]), RUNTIME).record_id == "ok"
    assert [r.is_default for r in mark_default([deprecated, approved], RUNTIME)] == [False, True]
