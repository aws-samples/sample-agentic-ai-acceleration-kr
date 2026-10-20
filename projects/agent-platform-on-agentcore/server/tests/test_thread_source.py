"""Thread.source: where a conversation started, kept by the server.

A whole-item put (ThreadRepository.update) drops any attribute it does not
write, so `source` must be carried on every write path or a DM thread would
quietly turn into a web thread on its first PATCH.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.thread import THREAD_SOURCE_SLACK_DM, THREAD_SOURCE_WEB, Thread, ThreadStateUpdate  # noqa: E402
from repositories.thread_repository import ThreadRepository  # noqa: E402
from services.thread_service import ThreadService  # noqa: E402


class Table:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["thread_id"]] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key["thread_id"])
        return {"Item": dict(item)} if item else {}


def repo():
    r = ThreadRepository.__new__(ThreadRepository)
    r.table = Table()
    r.table_name = "threads"
    return r


def test_rows_written_before_the_field_read_as_web():
    thread = ThreadRepository._to_thread(
        {"thread_id": "t", "created_at": "c", "updated_at": "u", "values": "{}", "metadata": "{}"}
    )
    assert thread.source == THREAD_SOURCE_WEB


def test_source_survives_create_update_and_read():
    r = repo()
    r.create(Thread(thread_id="t", created_at="c", updated_at="u", owner_sub="s", source=THREAD_SOURCE_SLACK_DM))
    assert r.table.items["t"]["source"] == THREAD_SOURCE_SLACK_DM
    thread = r.get("t")
    thread.values = {"messages": [{"id": "m", "type": "human", "content": "x"}]}
    r.update("t", thread)
    assert r.get("t").source == THREAD_SOURCE_SLACK_DM


def test_get_or_create_records_the_origin_once():
    service = ThreadService(repo())
    created = service.get_or_create_thread("t", owner_sub="s", source=THREAD_SOURCE_SLACK_DM)
    assert created.source == THREAD_SOURCE_SLACK_DM
    # A later web turn on the same thread does not rewrite where it started.
    again = service.get_or_create_thread("t", owner_sub="s")
    assert again.source == THREAD_SOURCE_SLACK_DM
    assert service.get_or_create_thread("w", owner_sub="s").source == THREAD_SOURCE_WEB


def test_patching_metadata_cannot_change_the_origin():
    service = ThreadService(repo())
    service.get_or_create_thread("t", owner_sub="s", source=THREAD_SOURCE_SLACK_DM)
    service.update_thread_state("t", ThreadStateUpdate(metadata={"source": "web"}, values={"x": 1}))
    thread = service.get_thread("t")
    assert thread.source == THREAD_SOURCE_SLACK_DM
    assert thread.metadata["source"] == "web"  # just a user key, not the origin
