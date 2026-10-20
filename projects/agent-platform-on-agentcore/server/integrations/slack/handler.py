"""One DM -> one turn, through the same StreamingService the web uses.

So the approval gate, the team/model checks, usage attribution and thread
storage are the web's own rather than a Slack copy of them. What this adds is
only what Slack needs: filtering, de-duplication, a caller found by email, a
lock shared across processes, and posting the finished answer back.

Order (each step refuses before the next one costs anything):

1. 1:1 DM from a person, for this app and workspace; everything else is ignored.
2. The integration is still active (a delete elsewhere stops it at once).
3. The event is new: (integration, DM, ts) is written conditionally. A storage
   error runs nothing — a retry would otherwise run it twice.
4. The sender maps to exactly one platform user who may use the agent.
5. The thread's shared lock is free.
6. "처리 중…" goes up, the turn runs, the stream is read to its end.
7. The placeholder becomes the answer; a long answer continues in more
   messages. The lock is released only once the run has really ended.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.config import OVERRIDES_METADATA_KEY
from integrations.slack import formatting as fmt
from integrations.slack.api import SlackApi, SlackApiError
from integrations.slack.identity import IdentityResolver
from integrations.slack.repository import STATUS_ACTIVE, SlackRepository
from models.common import StreamRequest
from models.thread import THREAD_SOURCE_SLACK_DM

logger = logging.getLogger(__name__)

# Internal defaults, not environment variables until operation shows a need.
LOCK_TTL_SECONDS = 90
LOCK_RENEW_SECONDS = 30
TURN_TIMEOUT_SECONDS = 20 * 60
# After a stop is requested: how long to wait for the run to actually end. Past
# this the lock is left to expire rather than released under a live run.
STOP_WAIT_SECONDS = 30

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agent-platform/slack-dm")


def thread_key(integration_id: str, channel: str, root_ts: str) -> str:
    """The platform thread of a Slack thread. Same DM thread, same id."""
    return str(uuid.uuid5(_NAMESPACE, f"thread:{integration_id}:{channel}:{root_ts}"))


def message_key(integration_id: str, channel: str, ts: str) -> str:
    """The human message id. Stable, so a replayed event cannot add a second copy."""
    return str(uuid.uuid5(_NAMESPACE, f"message:{integration_id}:{channel}:{ts}"))


@dataclass
class TurnOutcome:
    # "completed" | "failed" | "interrupted" | "timeout" | "lock_lost"
    status: str = "interrupted"
    segments: List[str] = field(default_factory=list)
    artifacts: int = 0
    # False only when the run could not be confirmed stopped: then the lock is
    # left to expire instead of being released under it.
    run_finished: bool = True

    @property
    def text(self) -> str:
        return "\n\n".join(s.strip() for s in self.segments if s.strip())


def _frames(chunk: Any) -> List[Dict[str, Any]]:
    """The `data:` payloads in one SSE chunk. Heartbeats and junk are skipped."""
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", "replace")
    out: List[Dict[str, Any]] = []
    for line in str(chunk).splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except ValueError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


class DmHandler:
    def __init__(
        self,
        *,
        repository: SlackRepository,
        api: SlackApi,
        identity: IdentityResolver,
        streaming_service: Any,
        thread_service: Any,
        web_origin: str = "",
        instance_id: Optional[str] = None,
    ):
        self.repository = repository
        self.api = api
        self.identity = identity
        self.streaming = streaming_service
        self.threads = thread_service
        self.web_origin = web_origin
        self.instance_id = instance_id or uuid.uuid4().hex[:12]
        # thread_id -> harness_id of the turns running now, so removing an
        # integration can stop exactly its own runs.
        self.active_threads: Dict[str, str] = {}
        # Tests shorten these.
        self.lock_ttl = LOCK_TTL_SECONDS
        self.lock_renew = LOCK_RENEW_SECONDS
        self.turn_timeout = TURN_TIMEOUT_SECONDS
        self.stop_wait = STOP_WAIT_SECONDS

    # ── filtering ───────────────────────────────────────────────────────────

    @staticmethod
    def accepts(integration: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        """A 1:1 DM a person sent to this app in this workspace."""
        event = payload.get("event") or {}
        if payload.get("api_app_id") and payload.get("api_app_id") != integration.get("app_id"):
            return False
        if payload.get("team_id") and integration.get("team_id") and payload.get("team_id") != integration.get("team_id"):
            return False
        if event.get("type") != "message" or event.get("channel_type") != "im":
            return False
        if event.get("bot_id") or event.get("app_id"):
            return False
        user = event.get("user")
        if not user or user == integration.get("bot_user_id"):
            return False
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            # Edits, deletions, joins, bot messages, thread broadcasts.
            return False
        if not subtype and not (event.get("text") or "").strip():
            return False
        return bool(event.get("channel") and event.get("ts"))

    # ── entry point ─────────────────────────────────────────────────────────

    async def handle(self, integration: Dict[str, Any], bot_token: str, payload: Dict[str, Any]) -> None:
        if not self.accepts(integration, payload):
            return
        event = payload["event"]
        channel, ts = event["channel"], event["ts"]
        root_ts = event.get("thread_ts") or ts
        integration_id = integration["integration_id"]

        current = await asyncio.to_thread(self.repository.get_integration, integration["harness_id"])
        if (
            not current
            or current.get("integration_id") != integration_id
            or current.get("status") != STATUS_ACTIVE
        ):
            return

        try:
            fresh = await asyncio.to_thread(self.repository.mark_event, integration_id, channel, ts)
        except Exception:
            logger.warning("Slack event marker write failed; not running the turn", exc_info=True)
            return
        if not fresh:
            return

        if event.get("subtype") == "file_share":
            await self._reply(bot_token, channel, root_ts, fmt.FILE_SHARE_UNSUPPORTED)
            return

        user = await self._caller(integration, bot_token, event, channel, root_ts)
        if user is None:
            return

        thread_id = thread_key(integration_id, channel, root_ts)
        holder = f"{self.instance_id}:{uuid.uuid4().hex}"
        try:
            acquired = await asyncio.to_thread(self.repository.acquire_lock, thread_id, holder, self.lock_ttl)
        except Exception:
            logger.warning("Slack thread lock unavailable", exc_info=True)
            await self._reply(bot_token, channel, root_ts, fmt.NOT_STARTED)
            return
        if not acquired:
            await self._reply(bot_token, channel, root_ts, fmt.BUSY)
            return

        release = True
        self.active_threads[thread_id] = integration["harness_id"]
        try:
            release = await self._run(integration, bot_token, user, event, channel, root_ts, thread_id, holder)
        finally:
            self.active_threads.pop(thread_id, None)
            if release:
                try:
                    await asyncio.to_thread(self.repository.release_lock, thread_id, holder)
                except Exception:
                    logger.warning("Could not release Slack thread lock; it will expire", exc_info=True)

    async def _caller(self, integration, bot_token, event, channel, root_ts):
        try:
            profile = await self.api.user_info(bot_token, event["user"])
        except SlackApiError as exc:
            logger.warning("Slack users.info failed: %s", exc.error)
            await self._reply(bot_token, channel, root_ts, fmt.REFUSALS["lookup_failed"])
            return None
        if profile.get("is_bot") or profile.get("deleted"):
            return None
        # No single "external" flag: a Slack Connect stranger is marked as one,
        # and an external member carries their own workspace's team id.
        if profile.get("is_stranger") or (
            profile.get("team_id") and integration.get("team_id") and profile["team_id"] != integration["team_id"]
        ):
            await self._reply(bot_token, channel, root_ts, fmt.EXTERNAL_USER)
            return None
        email = ((profile.get("profile") or {}).get("email")) or ""
        resolution = await asyncio.to_thread(self.identity.resolve, email)
        if resolution.user is None:
            await self._reply(
                bot_token, channel, root_ts, fmt.REFUSALS.get(resolution.refusal or "", fmt.NOT_STARTED)
            )
            return None
        return resolution.user

    # ── the turn ────────────────────────────────────────────────────────────

    async def _run(self, integration, bot_token, user, event, channel, root_ts, thread_id, holder) -> bool:
        """Run the turn and post its answer. Returns whether to release the lock."""
        # Lazy: the streaming module pulls in the agent clients.
        from services.agent_access import AgentTargetMismatch
        from services.run_broker import RunAlreadyActive
        from services.streaming_service import AgentNotApproved
        from services.thread_service import ThreadAgentMismatch, ThreadForbidden

        try:
            placeholder = await self.api.post_message(bot_token, channel, thread_ts=root_ts, text=fmt.PROCESSING)
        except SlackApiError as exc:
            logger.warning("Slack placeholder post failed: %s", exc.error)
            return True

        existing = await asyncio.to_thread(self.threads.get_thread, thread_id)
        if existing is not None and existing.owner_sub and existing.owner_sub != user.sub:
            await self._replace(bot_token, channel, placeholder, root_ts, fmt.FOREIGN_THREAD)
            return True

        request = self._request(integration, existing, event, channel)
        try:
            response = await self.streaming.stream_thread_execution(
                thread_id,
                request,
                actor_id=user.sub or None,
                owner_sub=user.sub,
                caller=user,
                source=THREAD_SOURCE_SLACK_DM,
            )
        except ThreadForbidden:
            text = fmt.FOREIGN_THREAD
        except (AgentNotApproved, AgentTargetMismatch):
            text = fmt.AGENT_UNAVAILABLE
        except ThreadAgentMismatch:
            text = fmt.NEW_THREAD_NEEDED
        except RunAlreadyActive:
            text = fmt.BUSY
        except Exception:
            logger.exception("Slack turn on thread %s could not start", thread_id)
            text = fmt.NOT_STARTED
        else:
            outcome = await self.collect(response, thread_id, holder, user.sub)
            await self._post_answer(bot_token, channel, placeholder, root_ts, thread_id, outcome)
            return outcome.run_finished
        await self._replace(bot_token, channel, placeholder, root_ts, text)
        return True

    def _request(self, integration, existing, event, channel) -> StreamRequest:
        """The stored conversation plus this message, addressed to the integration's record.

        The thread's web-set overrides (model, system prompt) ride along exactly
        as the web resends them, so a DM thread continued from either side
        answers the same way.
        """
        messages: List[Dict[str, Any]] = []
        metadata: Dict[str, Any] = {}
        if existing is not None:
            messages = list((existing.values or {}).get("messages") or [])
            metadata = existing.metadata or {}
        messages.append(
            {
                "id": message_key(integration["integration_id"], channel, event["ts"]),
                "type": "human",
                "content": fmt.unescape(event.get("text") or ""),
            }
        )
        config: Dict[str, Any] = {
            "registry_record_id": integration.get("record_id") or None,
            "registry_agent_name": integration.get("agent_name") or None,
        }
        overrides = metadata.get(OVERRIDES_METADATA_KEY) or {}
        if isinstance(overrides, dict):
            if (overrides.get("modelId") or "").strip():
                config["model_id"] = overrides["modelId"].strip()
            if (overrides.get("systemPrompt") or "").strip():
                config["system_prompt"] = overrides["systemPrompt"].strip()
        return StreamRequest(values={"messages": messages}, config=config)

    async def collect(self, response: Any, thread_id: str, holder: str, owner_sub: str) -> TurnOutcome:
        """Read the run's stream to its end, renewing the lock meanwhile.

        Not done at `messageStop`: artifacts and the harness sweep arrive after
        it. `end` is success, a top-level `error` is failure, and a stream that
        stops with neither was cut off.
        """
        outcome = TurnOutcome()
        run = self.streaming.run_broker.get(thread_id)
        lock_lost = asyncio.Event()

        async def consume() -> None:
            current: Optional[int] = None
            async for chunk in response.body_iterator:
                for frame in _frames(chunk):
                    kind = frame.get("event")
                    if kind == "end":
                        outcome.status = "completed"
                    elif kind == "error":
                        outcome.status = "failed"
                    elif isinstance(kind, dict):
                        if "messageStart" in kind:
                            outcome.segments.append("")
                            current = len(outcome.segments) - 1
                        delta = ((kind.get("contentBlockDelta") or {}).get("delta") or {}).get("text")
                        if isinstance(delta, str):
                            if current is None:
                                outcome.segments.append("")
                                current = len(outcome.segments) - 1
                            outcome.segments[current] += delta
                        if "artifact" in kind:
                            outcome.artifacts += 1

        async def renew() -> None:
            loop = asyncio.get_running_loop()
            renewed_at = loop.time()
            while True:
                await asyncio.sleep(self.lock_renew)
                try:
                    ours = await asyncio.to_thread(self.repository.renew_lock, thread_id, holder, self.lock_ttl)
                except Exception:
                    # A storage blip is not a lost lock while the last lease
                    # still outlives the next attempt; past that, it might be.
                    logger.warning("Slack lock renewal failed for %s", thread_id, exc_info=True)
                    if loop.time() - renewed_at + self.lock_renew < self.lock_ttl:
                        continue
                    ours = False
                if not ours:
                    lock_lost.set()
                    return
                renewed_at = loop.time()

        consumer = asyncio.create_task(consume())
        renewer = asyncio.create_task(renew())
        lost = asyncio.create_task(lock_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {consumer, lost}, timeout=self.turn_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if consumer in done:
                if consumer.exception() is not None:
                    logger.warning("Reading the Slack turn failed", exc_info=consumer.exception())
                    outcome.status = "interrupted" if outcome.status != "failed" else "failed"
            else:
                outcome.status = "lock_lost" if lost in done else "timeout"
                consumer.cancel()
                self.streaming.run_broker.cancel(thread_id, owner_sub=owner_sub)
        finally:
            renewer.cancel()
            lost.cancel()
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, renewer, lost, return_exceptions=True)

        # The run may outlive the stream (a stop, a cut subscription): the lock
        # is only released once its task has really finished.
        if run is not None and run.task is not None and not run.task.done():
            await asyncio.wait({run.task}, timeout=self.stop_wait)
            outcome.run_finished = run.task.done()
            if not outcome.run_finished:
                logger.warning("Slack turn on %s did not stop in time; lock left to expire", thread_id)
        return outcome

    # ── posting ─────────────────────────────────────────────────────────────

    def _answer_text(self, thread_id: str, outcome: TurnOutcome) -> str:
        parts: List[str] = []
        body = outcome.text
        if body:
            parts.append(body)
        note = {
            "failed": fmt.FAILED_NOTE,
            "interrupted": fmt.INTERRUPTED_NOTE,
            "timeout": fmt.TIMEOUT_NOTE,
            "lock_lost": fmt.LOCK_LOST_NOTE,
        }.get(outcome.status)
        if note:
            parts.append(note)
        elif not body:
            parts.append(fmt.EMPTY_ANSWER)
        if outcome.artifacts:
            parts.append(fmt.artifacts_note(outcome.artifacts))
        if self.web_origin:
            parts.append(fmt.web_link(self.web_origin, thread_id))
        return "\n\n".join(parts)

    async def _post_answer(self, token, channel, placeholder, root_ts, thread_id, outcome: TurnOutcome) -> None:
        text = self._answer_text(thread_id, outcome)
        try:
            await self._send_chunks(token, channel, placeholder, root_ts, fmt.split_markdown(text), markdown=True)
            return
        except SlackApiError as exc:
            logger.info("Slack refused markdown_text (%s); sending mrkdwn instead", exc.error)
        try:
            await self._send_chunks(
                token, channel, placeholder, root_ts,
                fmt.split_markdown(fmt.to_mrkdwn(text), fmt.MRKDWN_CHUNK), markdown=False,
            )
        except SlackApiError as exc:
            logger.warning("Slack answer could not be posted: %s", exc.error)

    async def _send_chunks(self, token, channel, placeholder, root_ts, chunks: List[str], *, markdown: bool) -> None:
        key = "markdown_text" if markdown else "text"
        first, rest = (chunks[0] if chunks else fmt.EMPTY_ANSWER), chunks[1:]
        await self.api.update_message(token, channel, placeholder, **{key: first})
        for chunk in rest:
            await self.api.post_message(token, channel, thread_ts=root_ts, **{key: chunk})

    async def _replace(self, token, channel, placeholder, root_ts, text: str) -> None:
        try:
            await self.api.update_message(token, channel, placeholder, text=text)
        except SlackApiError as exc:
            logger.warning("Slack placeholder update failed: %s", exc.error)
            await self._reply(token, channel, root_ts, text)

    async def _reply(self, token, channel, root_ts, text: str) -> None:
        try:
            await self.api.post_message(token, channel, thread_ts=root_ts, text=text)
        except SlackApiError as exc:
            logger.warning("Slack reply failed: %s", exc.error)
