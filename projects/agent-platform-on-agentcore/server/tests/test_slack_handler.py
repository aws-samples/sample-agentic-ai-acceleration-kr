"""A DM becomes one turn through the real StreamingService (integrations/slack/handler.py).

The streaming service, thread service and run broker are the production ones;
only the edges are replaced — the agent, Slack, the Slack table and the
identity lookup. So these tests also pin that a DM thread is an ordinary
thread: owned by the mapped user, stored with its transcript, and continued
from the web like any other.
"""
import asyncio
import copy
import os
import sys
import time
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfg  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from integrations.slack import formatting as fmt  # noqa: E402
from integrations.slack.api import SlackApiError  # noqa: E402
from integrations.slack.handler import DmHandler, thread_key  # noqa: E402
from integrations.slack.identity import Resolution  # noqa: E402
from models.common import StreamRequest  # noqa: E402
from models.thread import THREAD_SOURCE_SLACK_DM  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402
from services.thread_service import ThreadService  # noqa: E402

USER = AuthUser(sub="sub-kim", username="kim", email="kim@example.com", groups=["team:sales"], role="user", provider="cognito")
INTEGRATION = {
    "integration_id": "i1",
    "harness_id": "h1",
    "app_id": "A1",
    "team_id": "T1",
    "bot_user_id": "UBOT",
    "record_id": None,
    "agent_name": "",
    "status": "active",
}


# ── edges ────────────────────────────────────────────────────────────────────

class MemThreadRepo:
    def __init__(self):
        self.rows = {}

    def create(self, thread):
        self.rows[thread.thread_id] = copy.deepcopy(thread)
        return thread

    def get(self, thread_id):
        row = self.rows.get(thread_id)
        return copy.deepcopy(row) if row else None

    def update(self, thread_id, thread):
        self.rows[thread_id] = copy.deepcopy(thread)
        return thread

    def search(self, owner_sub, **_):
        return [copy.deepcopy(t) for t in self.rows.values() if owner_sub is None or t.owner_sub == owner_sub]


class SlackTable:
    def __init__(self):
        self.integrations = {"h1": dict(INTEGRATION)}
        self.events = set()
        self.locks: Dict[str, Dict[str, Any]] = {}
        self.fail_mark = False
        self.renew_ok = True
        self.released = []

    def get_integration(self, harness_id):
        row = self.integrations.get(harness_id)
        return dict(row) if row else None

    def mark_event(self, integration_id, channel, ts):
        if self.fail_mark:
            raise RuntimeError("table down")
        key = (integration_id, channel, ts)
        if key in self.events:
            return False
        self.events.add(key)
        return True

    def acquire_lock(self, thread_id, holder, ttl):
        lock = self.locks.get(thread_id)
        if lock and lock["expires"] > time.time() and lock["holder"] != holder:
            return False
        self.locks[thread_id] = {"holder": holder, "expires": time.time() + ttl}
        return True

    def renew_lock(self, thread_id, holder, ttl):
        lock = self.locks.get(thread_id)
        if not self.renew_ok or not lock or lock["holder"] != holder:
            return False
        lock["expires"] = time.time() + ttl
        return True

    def release_lock(self, thread_id, holder):
        lock = self.locks.get(thread_id)
        if lock and lock["holder"] == holder:
            del self.locks[thread_id]
            self.released.append(thread_id)


class Slack:
    def __init__(self):
        self.posts: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []
        self.profile = {"id": "U1", "team_id": "T1", "profile": {"email": "kim@example.com"}}
        self.refuse_markdown = False
        self._ts = 0

    async def user_info(self, token, user_id):
        return dict(self.profile)

    async def post_message(self, token, channel, *, thread_ts=None, text=None, markdown_text=None):
        if markdown_text is not None and self.refuse_markdown:
            raise SlackApiError("chat.postMessage", "invalid_arguments")
        self._ts += 1
        ts = f"900.{self._ts}"
        self.posts.append({"channel": channel, "thread_ts": thread_ts, "text": text, "markdown_text": markdown_text, "ts": ts})
        return ts

    async def update_message(self, token, channel, ts, *, text=None, markdown_text=None):
        if markdown_text is not None and self.refuse_markdown:
            raise SlackApiError("chat.update", "invalid_arguments")
        self.updates.append({"channel": channel, "ts": ts, "text": text, "markdown_text": markdown_text})

    def final(self) -> str:
        """The answer as the user sees it: the replaced placeholder plus any continuation."""
        update = self.updates[-1]
        first = update["markdown_text"] if update["markdown_text"] is not None else update["text"]
        rest = [p["markdown_text"] or p["text"] for p in self.posts[1:]]
        return "\n".join([first] + rest)


class Identity:
    def __init__(self, user=USER, refusal=None):
        self.result = Resolution(user=user, refusal=refusal)
        self.emails = []

    def resolve(self, email):
        self.emails.append(email)
        return self.result


def text_turn(answer="안녕하세요", *extra):
    return [
        {"event": {"messageStart": {"role": "assistant"}}},
        {"event": {"contentBlockDelta": {"delta": {"text": answer}}}},
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
        *extra,
    ]


class Agent:
    """Plays a scripted turn. `hang` keeps the stream open until cancelled."""

    def __init__(self, events=None, hang=False, raise_after=None):
        self.events = events if events is not None else text_turn()
        self.hang = hang
        self.raise_after = raise_after
        self.calls = []

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        self.calls.append({"thread_id": thread_id, "values": copy.deepcopy(values), "config": dict(config or {}), "actor_id": actor_id})
        for event in self.events:
            yield copy.deepcopy(event)
        if self.raise_after:
            raise RuntimeError(self.raise_after)
        if self.hang:
            await asyncio.sleep(3600)


class Registry:
    def __init__(self, status="APPROVED"):
        self.status = status

    def chattable_record(self, record_id):
        return type("R", (), {"status": self.status, "record_id": record_id, "harness_arn": None, "agent_runtime_arn": None, "qualifier": None, "custom_metadata": None, "name": "Agent"})()


class World:
    def __init__(self, agent=None, identity=None, registry=None):
        self.thread_repo = MemThreadRepo()
        self.threads = ThreadService(self.thread_repo)
        self.agent = agent or Agent()
        self.streaming = StreamingService(thread_service=self.threads, agentcore_client=self.agent, registry_service=registry or Registry())
        self.streaming._get_agent_client = lambda config=None: self.agent
        self.table = SlackTable()
        self.slack = Slack()
        self.identity = identity or Identity()
        self.handler = DmHandler(
            repository=self.table, api=self.slack, identity=self.identity,
            streaming_service=self.streaming, thread_service=self.threads,
            web_origin="https://agents.example",
        )
        self.handler.lock_renew = 0.05
        self.handler.stop_wait = 0.5

    def dm(self, text="hi", ts="100.1", thread_ts=None, **event):
        body = {"type": "message", "channel_type": "im", "channel": "D1", "user": "U1", "text": text, "ts": ts}
        if thread_ts:
            body["thread_ts"] = thread_ts
        body.update(event)
        return {"api_app_id": "A1", "team_id": "T1", "event": body}

    def send(self, payload, integration=None):
        asyncio.run(self.handler.handle(integration or dict(INTEGRATION), "xoxb-test", payload))


# ── the turn ────────────────────────────────────────────────────────────────

def test_a_dm_runs_one_turn_as_the_mapped_user_and_replaces_the_placeholder():
    w = World()
    w.send(w.dm("hello"))

    assert w.slack.posts[0]["text"] == fmt.PROCESSING
    assert w.slack.posts[0]["thread_ts"] == "100.1"
    assert w.slack.updates[0]["ts"] == w.slack.posts[0]["ts"]
    assert w.slack.final().startswith("안녕하세요")
    assert "웹에서 이어 보기" in w.slack.final()

    call = w.agent.calls[0]
    assert call["actor_id"] == "sub-kim"
    tid = thread_key("i1", "D1", "100.1")
    stored = w.thread_repo.rows[tid]
    assert stored.owner_sub == "sub-kim"
    assert stored.source == THREAD_SOURCE_SLACK_DM
    assert [m["type"] for m in stored.values["messages"]] == ["human", "ai"]
    assert w.table.released == [tid]
    assert w.identity.emails == ["kim@example.com"]


def test_a_reply_in_the_dm_thread_continues_the_conversation():
    w = World()
    w.send(w.dm("first", ts="100.1"))
    w.send(w.dm("second", ts="100.5", thread_ts="100.1"))

    assert len(w.thread_repo.rows) == 1
    sent = w.agent.calls[1]["values"]["messages"]
    assert [m["content"] for m in sent if m["type"] == "human"] == ["first", "second"]
    assert any(m["type"] == "ai" for m in sent)
    # The second answer is posted into the same Slack thread.
    assert w.slack.posts[-1]["thread_ts"] == "100.1"


def test_a_new_top_level_message_is_a_new_conversation():
    w = World()
    w.send(w.dm("first", ts="100.1"))
    w.send(w.dm("other", ts="200.1"))
    assert len(w.thread_repo.rows) == 2
    assert [m["content"] for m in w.agent.calls[1]["values"]["messages"]] == ["other"]


def test_the_dm_thread_is_the_users_web_thread_and_keeps_its_origin():
    w = World()
    w.send(w.dm("from slack"))
    tid = thread_key("i1", "D1", "100.1")
    assert [t.thread_id for t in w.threads.search_threads(owner_sub="sub-kim")] == [tid]

    async def web_turn():
        response = await w.streaming.stream_thread_execution(
            tid,
            StreamRequest(values={"messages": [{"id": "web-1", "type": "human", "content": "from web"}]}),
            actor_id="sub-kim", owner_sub="sub-kim", caller=USER,
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(web_turn())
    stored = w.thread_repo.rows[tid]
    assert stored.source == THREAD_SOURCE_SLACK_DM
    assert [m["content"] for m in stored.values["messages"] if m["type"] == "human"] == ["from slack", "from web"]


def test_slack_escaping_is_undone_before_the_agent_sees_it():
    w = World()
    w.send(w.dm("a &lt;b&gt; &amp; c"))
    assert w.agent.calls[0]["values"]["messages"][-1]["content"] == "a <b> & c"


def test_thread_overrides_set_on_the_web_ride_along(monkeypatch):
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", ["model-x"])
    w = World()
    w.send(w.dm("first"))
    tid = thread_key("i1", "D1", "100.1")
    w.thread_repo.rows[tid].metadata = {cfg.OVERRIDES_METADATA_KEY: {"modelId": "model-x", "systemPrompt": "be brief"}}
    w.send(w.dm("second", ts="100.2", thread_ts="100.1"))
    config = w.agent.calls[1]["config"]
    assert config["model_id"] == "model-x"
    assert config["system_prompt"] == "be brief"


# ── what is not a turn ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "change",
    [
        {"channel_type": "channel"},
        {"channel_type": "mpim"},
        {"bot_id": "B1"},
        {"user": "UBOT"},
        {"subtype": "message_changed"},
        {"subtype": "message_deleted"},
        {"text": "   "},
    ],
)
def test_events_other_than_a_persons_dm_are_ignored(change):
    w = World()
    w.send(w.dm(**change) if "text" not in change else w.dm(text=change["text"]))
    assert w.agent.calls == [] and w.slack.posts == [] and w.table.events == set()


def test_another_apps_or_workspaces_event_is_ignored():
    w = World()
    for payload in ({**w.dm(), "api_app_id": "A2"}, {**w.dm(), "team_id": "T2"}):
        w.send(payload)
    assert w.agent.calls == [] and w.slack.posts == []


def test_a_deleted_or_deleting_integration_runs_nothing():
    w = World()
    w.table.integrations["h1"]["status"] = "deleting"
    w.send(w.dm())
    w.table.integrations.pop("h1")
    w.send(w.dm(ts="100.9"))
    assert w.agent.calls == [] and w.slack.posts == []


def test_a_redelivered_event_runs_once():
    w = World()
    w.send(w.dm())
    w.send(w.dm())
    assert len(w.agent.calls) == 1


def test_no_turn_when_the_event_cannot_be_recorded():
    w = World()
    w.table.fail_mark = True
    w.send(w.dm())
    assert w.agent.calls == [] and w.slack.posts == []


def test_an_attachment_only_dm_is_answered_not_run():
    w = World()
    w.send(w.dm(text="", subtype="file_share", files=[{"id": "F1"}]))
    assert w.agent.calls == []
    assert w.slack.posts[0]["text"] == fmt.FILE_SHARE_UNSUPPORTED


def test_an_external_user_is_refused():
    w = World()
    w.slack.profile = {"id": "U1", "team_id": "T9", "is_stranger": True, "profile": {"email": "x@other.example"}}
    w.send(w.dm())
    assert w.agent.calls == [] and w.identity.emails == []
    assert w.slack.posts[0]["text"] == fmt.EXTERNAL_USER


@pytest.mark.parametrize("refusal", ["unmapped", "ambiguous", "inactive", "not_allowed", "lookup_failed"])
def test_an_unmapped_or_refused_user_is_told_why_before_anything_runs(refusal):
    w = World(identity=Identity(user=None, refusal=refusal))
    w.send(w.dm())
    assert w.agent.calls == []
    assert w.slack.posts == [{"channel": "D1", "thread_ts": "100.1", "text": fmt.REFUSALS[refusal], "markdown_text": None, "ts": "900.1"}]


def test_a_busy_thread_is_refused_with_a_resend_hint():
    w = World()
    tid = thread_key("i1", "D1", "100.1")
    w.table.locks[tid] = {"holder": "other-process", "expires": time.time() + 60}
    w.send(w.dm(ts="100.3", thread_ts="100.1"))
    assert w.agent.calls == []
    assert w.slack.posts[0]["text"] == fmt.BUSY
    assert w.table.locks[tid]["holder"] == "other-process"


def test_an_expired_lock_does_not_block():
    w = World()
    tid = thread_key("i1", "D1", "100.1")
    w.table.locks[tid] = {"holder": "dead-process", "expires": time.time() - 1}
    w.send(w.dm())
    assert len(w.agent.calls) == 1


def test_an_unapproved_agent_is_refused_without_internal_detail():
    w = World(registry=Registry(status="DRAFT"))
    w.send(w.dm(), integration={**INTEGRATION, "record_id": "rec-1"})
    assert w.agent.calls == []
    assert w.slack.updates[-1]["text"] == fmt.AGENT_UNAVAILABLE
    assert w.table.released  # the lock does not outlive a refused turn


def test_someone_elses_thread_is_refused():
    w = World()
    w.send(w.dm())
    tid = thread_key("i1", "D1", "100.1")
    w.thread_repo.rows[tid].owner_sub = "sub-other"
    w.send(w.dm(ts="100.2", thread_ts="100.1"))
    assert len(w.agent.calls) == 1
    assert w.slack.updates[-1]["text"] == fmt.FOREIGN_THREAD


# ── how the stream ends ─────────────────────────────────────────────────────

def test_artifacts_after_message_stop_are_still_collected():
    events = text_turn("보고서를 만들었습니다.", {"event": {"artifact": {"id": "art-1", "title": "report.docx", "kind": "document"}}})
    w = World(agent=Agent(events=events))
    w.send(w.dm())
    final = w.slack.final()
    assert final.startswith("보고서를 만들었습니다.")
    assert fmt.artifacts_note(1) in final
    assert fmt.FAILED_NOTE not in final and fmt.INTERRUPTED_NOTE not in final


def test_a_failed_run_keeps_the_partial_answer_and_says_it_failed():
    w = World(agent=Agent(events=text_turn("일부")[:2], raise_after="boom"))
    w.send(w.dm())
    final = w.slack.final()
    assert "일부" in final and fmt.FAILED_NOTE in final
    assert "boom" not in final


def test_a_stream_that_ends_without_end_is_interrupted():
    from integrations.slack.handler import TurnOutcome

    class Cut:
        async def body():
            yield 'data: {"event": "thread_created", "thread_id": "t"}\n\n'
            yield 'data: {"event": {"contentBlockDelta": {"delta": {"text": "부분"}}}}\n\n'

        body_iterator = body()

    w = World()
    outcome = asyncio.run(w.handler.collect(Cut(), "t-none", "h", "sub-kim"))
    assert isinstance(outcome, TurnOutcome)
    assert outcome.status == "interrupted" and outcome.text == "부분"


def test_a_turn_over_the_time_limit_is_stopped_and_then_unlocked():
    w = World(agent=Agent(events=text_turn("시작")[:2], hang=True))
    w.handler.turn_timeout = 0.3
    w.send(w.dm())
    final = w.slack.final()
    assert "시작" in final and fmt.TIMEOUT_NOTE in final
    tid = thread_key("i1", "D1", "100.1")
    assert w.table.released == [tid]
    assert w.thread_repo.rows[tid].status.value == "interrupted"


def test_losing_the_lock_stops_the_turn():
    w = World(agent=Agent(events=text_turn("시작")[:2], hang=True))
    w.table.renew_ok = False
    w.send(w.dm())
    assert fmt.LOCK_LOST_NOTE in w.slack.final()


def test_one_failed_renewal_within_the_lease_does_not_stop_the_turn():
    w = World(agent=Agent(events=text_turn("시작")[:2], hang=True))
    w.handler.lock_renew = 0.05
    w.handler.lock_ttl = 10
    w.handler.turn_timeout = 0.4
    calls = []
    original = w.table.renew_lock

    def flaky(thread_id, holder, ttl):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("throttled")
        return original(thread_id, holder, ttl)

    w.table.renew_lock = flaky
    w.send(w.dm())
    # It ran to the time limit rather than stopping on the first blip.
    assert fmt.TIMEOUT_NOTE in w.slack.final()
    assert len(calls) > 1


def test_renewal_failing_past_the_lease_stops_the_turn():
    w = World(agent=Agent(events=text_turn("시작")[:2], hang=True))
    w.handler.lock_renew = 0.05
    w.handler.lock_ttl = 0.12

    def down(*args):
        raise RuntimeError("table down")

    w.table.renew_lock = down
    w.send(w.dm())
    assert fmt.LOCK_LOST_NOTE in w.slack.final()


def test_a_run_that_will_not_stop_keeps_the_lock_until_it_expires():
    w = World(agent=Agent(events=text_turn("시작")[:2], hang=True))
    w.handler.turn_timeout = 0.2
    w.handler.stop_wait = 0.2
    # The stop request goes nowhere, as when a run is wedged past cancellation.
    w.streaming.run_broker.cancel = lambda *args, **kwargs: True

    async def scenario():
        await w.handler.handle(dict(INTEGRATION), "xoxb-test", w.dm())
        tid = thread_key("i1", "D1", "100.1")
        held = tid in w.table.locks
        # Tidy up the stubborn run before the loop closes.
        run = w.streaming.run_broker.get(tid)
        if run is not None and run.task is not None:
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
        return held

    assert asyncio.run(scenario()) is True
    assert w.table.released == []


# ── posting ─────────────────────────────────────────────────────────────────

def test_a_long_answer_continues_in_more_messages():
    answer = "\n\n".join(("문단%d " % i) * 400 for i in range(12))
    w = World(agent=Agent(events=text_turn(answer)))
    w.send(w.dm())
    chunks = [w.slack.updates[-1]["markdown_text"]] + [p["markdown_text"] for p in w.slack.posts[1:]]
    assert len(chunks) > 1
    assert all(len(c) <= fmt.MARKDOWN_CHUNK for c in chunks)
    assert all(p["thread_ts"] == "100.1" for p in w.slack.posts[1:])


def test_markdown_refused_falls_back_to_mrkdwn_text():
    w = World(agent=Agent(events=text_turn("**굵게**")))
    w.slack.refuse_markdown = True
    w.send(w.dm())
    assert w.slack.updates[-1]["markdown_text"] is None
    assert w.slack.updates[-1]["text"].startswith("*굵게*")
