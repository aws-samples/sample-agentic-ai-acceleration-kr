"""Threads the retired basic-chat path left behind.

Basic chat pinned a synthetic record id (`__basic_chat__`) and a model onto its
threads. The path is gone; those threads move onto the default agent's record on
their next turn, carrying the model into the thread's override so the conversation
keeps answering the way it was built. The model is a *decision made by the
binding* (services/agent_access.py): a pinned model that is no longer allowed is
not carried, so the thread falls back to the agent's default instead of 403-ing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config as config  # noqa: E402
from core.config import LEGACY_BASIC_CHAT_RECORD_ID, OVERRIDES_METADATA_KEY  # noqa: E402
from models.thread import Thread  # noqa: E402
from repositories.thread_repository import ThreadRepository  # noqa: E402
from services.thread_service import ThreadAgentMismatch, ThreadService  # noqa: E402

OWNER = "sub-owner"
SONNET = "global.anthropic.claude-sonnet-5-5"
OLD = "global.anthropic.claude-sonnet-5"
DEFAULT_RECORD = "rec-default"
TURN = [{"id": "m1", "type": "human", "content": "hi"}]


class _CapturingTable:
    def __init__(self):
        self.item = None

    def put_item(self, Item):  # noqa: N803
        self.item = Item


def _real_item(method, thread_id, thread):
    repository = ThreadRepository.__new__(ThreadRepository)
    repository.table = _CapturingTable()
    if method == "create":
        ThreadRepository.create(repository, thread)
    else:
        ThreadRepository.update(repository, thread_id, thread)
    return repository.table.item


class StubRepository:
    def __init__(self):
        self.items = {}

    def seed(self, thread):
        self.items[thread.thread_id] = _real_item("create", thread.thread_id, thread)

    def get(self, thread_id):
        item = self.items.get(thread_id)
        return ThreadRepository._to_thread(item) if item else None

    def create(self, thread):
        self.items[thread.thread_id] = _real_item("create", thread.thread_id, thread)
        return thread

    def update(self, thread_id, thread):
        self.items[thread_id] = _real_item("update", thread_id, thread)
        return thread


def _legacy_thread(thread_id, *, model=OLD, metadata=None):
    return Thread(
        thread_id=thread_id,
        created_at="2026-10-01T00:00:00",
        updated_at="2026-10-01T00:00:00",
        values={"messages": TURN},
        metadata=metadata or {},
        owner_sub=OWNER,
        agent_record_id=LEGACY_BASIC_CHAT_RECORD_ID,
        agent_name="기본 채팅",
        basic_chat_model_id=model,
    )


@pytest.fixture
def repo():
    return StubRepository()


@pytest.fixture
def service(repo):
    return ThreadService(repo)


def _adopt(service, thread_id, adopted_model_id):
    return service.get_or_create_thread(
        thread_id=thread_id,
        owner_sub=OWNER,
        agent_record_id=DEFAULT_RECORD,
        agent_name="bap_default",
        adopted_model_id=adopted_model_id,
    )


# --- adoption ----------------------------------------------------------------


def test_a_legacy_thread_moves_onto_the_default_record_and_keeps_its_model_as_an_override(service, repo):
    repo.seed(_legacy_thread("t"))
    thread = _adopt(service, "t", SONNET)
    assert (thread.agent_record_id, thread.agent_name) == (DEFAULT_RECORD, "bap_default")
    assert thread.metadata[OVERRIDES_METADATA_KEY] == {"modelId": SONNET}
    assert thread.basic_chat_model_id is None
    # The whole-item write dropped the retired attribute and kept the override.
    item = repo.items["t"]
    assert "basic_chat_model_id" not in item
    assert ThreadRepository._to_thread(item).metadata[OVERRIDES_METADATA_KEY]["modelId"] == SONNET


def test_adoption_without_a_model_leaves_overrides_alone(service, repo):
    # The binding decided the pinned model may not be carried (not allowed any
    # more). The thread still moves; it answers with the agent's default model.
    repo.seed(_legacy_thread("t", model=OLD))
    thread = _adopt(service, "t", None)
    assert thread.agent_record_id == DEFAULT_RECORD
    assert OVERRIDES_METADATA_KEY not in (thread.metadata or {})
    assert thread.basic_chat_model_id is None


def test_adoption_does_not_overwrite_an_existing_override(service, repo):
    repo.seed(_legacy_thread("t", metadata={OVERRIDES_METADATA_KEY: {"modelId": SONNET, "systemPrompt": "be brief"}}))
    thread = _adopt(service, "t", OLD)
    assert thread.metadata[OVERRIDES_METADATA_KEY] == {"modelId": SONNET, "systemPrompt": "be brief"}


def test_an_adopted_thread_is_an_ordinary_pinned_thread_afterwards(service, repo):
    repo.seed(_legacy_thread("t"))
    _adopt(service, "t", SONNET)
    # Same record continues; another record is refused like on any pinned thread.
    assert service.get_or_create_thread("t", OWNER, agent_record_id=DEFAULT_RECORD, agent_name="x").agent_record_id == DEFAULT_RECORD
    with pytest.raises(ThreadAgentMismatch):
        service.get_or_create_thread("t", OWNER, agent_record_id="rec-other", agent_name="other")


def test_a_turn_that_names_no_agent_leaves_a_legacy_thread_untouched(service, repo):
    repo.seed(_legacy_thread("t"))
    thread = service.get_or_create_thread("t", OWNER)
    assert thread.agent_record_id == LEGACY_BASIC_CHAT_RECORD_ID


# --- the model is no longer pinned on new threads -----------------------------


def test_a_new_thread_records_no_model(service, repo):
    thread = service.get_or_create_thread("t-new", OWNER, agent_record_id="rec-a", agent_name="A")
    assert thread.basic_chat_model_id is None
    assert "basic_chat_model_id" not in repo.items["t-new"]


def test_create_thread_has_no_model_parameter(service):
    with pytest.raises(TypeError):
        service.create_thread(OWNER, agent_record_id="rec-a", agent_name="A", basic_chat_model_id=SONNET)


# --- configuration -------------------------------------------------------------


def test_allowed_models_are_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("ALLOWED_MODELS", f" {SONNET}, {OLD} ,")
    assert config._csv("ALLOWED_MODELS") == [SONNET, OLD]


def test_the_basic_chat_names_are_gone():
    for name in ("BASIC_CHAT_ALLOWED_MODELS", "BASIC_CHAT_RUNTIME_ARN", "BASIC_CHAT_RECORD_ID", "BASIC_CHAT_AGENT_NAME"):
        assert not hasattr(config, name), name
    assert config.LEGACY_BASIC_CHAT_RECORD_ID == "__basic_chat__"
    assert config.OVERRIDES_METADATA_KEY == "harness_overrides"
