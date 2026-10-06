"""Basic chat: the default runtime answering with an operator-allowed model.

No registry record is involved, so two things the registry normally supplies
have to come from the server itself: *which target answers* (always the default
runtime, never an ARN the client sent) and *which model* (one from the
allow-list, pinned onto the thread like an agent is). The model is pinned for the
same reason the agent is — AgentCore Memory is scoped by the thread alone, so a
conversation built under one model must not be silently continued under another.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config as config  # noqa: E402
from core.config import BASIC_CHAT_RECORD_ID  # noqa: E402
from models.thread import Thread  # noqa: E402
from repositories.thread_repository import ThreadRepository  # noqa: E402
from services.thread_service import ThreadAgentMismatch, ThreadService  # noqa: E402

OWNER = "sub-owner"
SONNET = "global.anthropic.claude-sonnet-5"
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
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


def _thread(thread_id, *, record_id="", model=None, messages=None):
    return Thread(
        thread_id=thread_id,
        created_at="2026-10-01T00:00:00",
        updated_at="2026-10-01T00:00:00",
        values={"messages": messages if messages is not None else []},
        metadata={},
        owner_sub=OWNER,
        agent_record_id=record_id,
        agent_name="기본 채팅" if record_id == BASIC_CHAT_RECORD_ID else "",
        basic_chat_model_id=model,
    )


@pytest.fixture
def repo():
    return StubRepository()


@pytest.fixture
def service(repo):
    return ThreadService(repo)


def _turn(service, thread_id, model):
    return service.get_or_create_thread(
        thread_id=thread_id,
        owner_sub=OWNER,
        agent_record_id=BASIC_CHAT_RECORD_ID,
        agent_name="기본 채팅",
        basic_chat_model_id=model,
    )


# --- storage -----------------------------------------------------------------


def test_the_model_survives_both_whole_item_writes():
    thread = _thread("t", record_id=BASIC_CHAT_RECORD_ID, model=SONNET)
    assert _real_item("create", "t", thread)["basic_chat_model_id"] == SONNET
    assert _real_item("update", "t", thread)["basic_chat_model_id"] == SONNET


def test_a_registry_thread_stores_no_model_attribute():
    item = _real_item("create", "t", _thread("t", record_id="rec-a"))
    assert "basic_chat_model_id" not in item
    assert ThreadRepository._to_thread(item).basic_chat_model_id is None


# --- pinning -----------------------------------------------------------------


def test_the_first_turn_pins_the_model(service, repo):
    thread = _turn(service, "t-new", SONNET)
    assert thread.agent_record_id == BASIC_CHAT_RECORD_ID
    assert thread.basic_chat_model_id == SONNET
    assert repo.items["t-new"]["basic_chat_model_id"] == SONNET


def test_the_same_model_may_continue(service, repo):
    repo.seed(_thread("t", record_id=BASIC_CHAT_RECORD_ID, model=SONNET, messages=TURN))
    assert _turn(service, "t", SONNET).basic_chat_model_id == SONNET


def test_another_model_is_refused_and_names_the_pinned_one(service, repo):
    repo.seed(_thread("t", record_id=BASIC_CHAT_RECORD_ID, model=SONNET, messages=TURN))
    with pytest.raises(ThreadAgentMismatch) as exc:
        _turn(service, "t", HAIKU)
    assert SONNET in str(exc.value)
    # A refused turn must not have moved the pin.
    assert repo.items["t"]["basic_chat_model_id"] == SONNET


def test_a_basic_thread_without_a_recorded_model_cannot_be_continued(service, repo):
    repo.seed(_thread("t", record_id=BASIC_CHAT_RECORD_ID, messages=TURN))
    with pytest.raises(ThreadAgentMismatch):
        _turn(service, "t", SONNET)


def test_a_registry_agent_cannot_take_over_a_basic_thread(service, repo):
    repo.seed(_thread("t", record_id=BASIC_CHAT_RECORD_ID, model=SONNET, messages=TURN))
    with pytest.raises(ThreadAgentMismatch):
        service.get_or_create_thread(
            thread_id="t", owner_sub=OWNER, agent_record_id="rec-a", agent_name="A"
        )


def test_an_empty_unpinned_thread_adopts_basic_chat_with_its_model(service, repo):
    repo.seed(_thread("t"))
    thread = _turn(service, "t", HAIKU)
    assert (thread.agent_record_id, thread.basic_chat_model_id) == (BASIC_CHAT_RECORD_ID, HAIKU)


def test_a_registry_pin_never_carries_a_model(service, repo):
    thread = service.get_or_create_thread(
        thread_id="t", owner_sub=OWNER, agent_record_id="rec-a", agent_name="A",
        basic_chat_model_id=SONNET,
    )
    assert thread.basic_chat_model_id is None


# --- capability flag ---------------------------------------------------------


def test_config_route_exposes_the_allow_list(monkeypatch):
    import routes.config as config_route

    monkeypatch.setattr(config_route, "BASIC_CHAT_ALLOWED_MODELS", [SONNET, HAIKU])
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/bap_default-AbCdE12345")
    body = config_route.get_config()
    assert body["basicChat"] == {"configured": True, "models": [SONNET, HAIKU]}

    monkeypatch.setattr(config_route, "BASIC_CHAT_ALLOWED_MODELS", [])
    assert config_route.get_config()["basicChat"]["configured"] is False


def test_allow_list_is_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("BASIC_CHAT_ALLOWED_MODELS", f" {SONNET}, {HAIKU} ,")
    assert config._csv("BASIC_CHAT_ALLOWED_MODELS") == [SONNET, HAIKU]
