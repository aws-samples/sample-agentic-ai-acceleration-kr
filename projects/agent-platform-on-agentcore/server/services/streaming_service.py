"""
Streaming service for thread execution
"""
import json
import asyncio
import uuid
import functools
import logging
import time
from datetime import datetime
from typing import AsyncIterator, Dict, Any, Optional
from fastapi.responses import StreamingResponse

from services.artifact_service import ArtifactService
from services.mcp_apps_service import McpAppsRelay
from mcp_core.model_context import inject_app_model_context
from services.registry_service import (
    RegistryNotConfigured,
    RegistryService,
    is_registry_unavailable,
)
from services.thread_service import ThreadService, ThreadForbidden
from services.run_broker import HEARTBEAT_FRAME, RunAlreadyActive, RunBroker
from services.agent_access import bind_execution, is_record_verdict
from services.policy_denial import is_policy_denial, resolve_turn_team
from core.auth import AuthUser
from agents.agentcore_client import AgentCoreClient
from agents.harness_client import HarnessClient
from agents.base import AgentClient
from models.common import StreamRequest, StreamEvent, ThreadStatus as ThreadStatusEnum
from models.artifact import normalize_kind

from data.model_rates import LONG_COUNTERS, long_context_threshold

logger = logging.getLogger(__name__)

# How long the stream may carry no bytes at all.
#
# A harness executes its tools inside AWS and emits nothing while one runs — a
# `.docx` build through `execute_command` is minutes of silence. Nothing on the
# path tolerates that: the Next.js proxy in front of this server (see
# `web/src/middleware.ts`) defaults `proxyTimeout` to 30s and applies it as an
# *idle* timeout on the upstream socket, and the ALB in the deployed stack has its
# own 60s. Measured against Next's own `http-proxy`: a 25s gap survives, a 40s gap
# aborts the upstream request and the origin's next write fails with EPIPE.
#
# A severed connection is indistinguishable from a client disconnect here, so a
# turn that was going fine ends as `interrupted`. Well under the tighter of the two
# limits, since the clock restarts from the last byte, not from the request.
HEARTBEAT_INTERVAL_SECONDS = 10.0

# Sent during a gap. An SSE comment, not an event: `:` lines are ignored by every
# SSE parser, and useStream only reads lines beginning with `data: `, so a
# heartbeat can never be mistaken for content or reach the stored transcript.
# Imported from run_broker, which keeps it out of the replay buffer.

# Shared by the start and the attach route, which return the same kind of body.
SSE_HEADERS = {
    # `no-transform` keeps intermediate proxies from gzipping the stream. A
    # compressor withholds each token until its buffer fills, which delivers the
    # whole answer as one burst.
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# Distinguishes "nothing arrived in time" from a real event, which may be any dict.
_HEARTBEAT = object()


_ARTIFACT_TOOL_NAMES = ("create_artifact", "update_artifact")


def _artifact_event_from_tool_use(
    tool_use_id: str,
    tool_name: Optional[str],
    tool_input_str: Optional[str],
    message_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Synthesize an `artifact` event from a completed artifact tool call.

    The runtime builds this in `agent-runtime/main.py`; a harness never runs
    that code (AWS executes its tools and the server only sees Converse events),
    so the harness path builds it here instead. The tool returns only a
    confirmation string, so the document rides in the accumulated tool *input*.

    Matches the bare name after any gateway target prefix, so both
    `create_artifact` and `platform-tools___create_artifact` are recognised.
    Returns None for non-artifact tools, unparsable input, or empty content.
    """
    if (tool_name or "").split("___")[-1] not in _ARTIFACT_TOOL_NAMES:
        return None
    try:
        tool_input = json.loads(tool_input_str) if tool_input_str else {}
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(tool_input, dict):
        return None
    content = tool_input.get("content")
    if not content:
        return None

    artifact: Dict[str, Any] = {
        "toolCallId": tool_use_id,
        "artifactId": tool_input.get("artifact_id") or None,
        "title": tool_input.get("title") or "Untitled",
        "kind": normalize_kind(tool_input.get("kind")),
        "content": content,
    }
    if message_id:
        artifact["messageId"] = message_id
    language = tool_input.get("language")
    if language:
        artifact["language"] = str(language).strip().lower()
    return {"event": {"artifact": artifact}}


def _artifact_event(record: Any) -> Dict[str, Any]:
    """The `artifact` event shape the panel already consumes.

    A swept file carries no inline content, which is exactly what keeps the
    panel's auto-open rule dormant: only entries with inline content pop the
    panel, so a download card appears in the list without hijacking the view.
    """
    return {
        "artifactId": record.artifact_id,
        "version": record.version,
        "threadId": record.thread_id,
        "title": record.title,
        "kind": record.kind,
        "filename": record.filename,
        "s3Key": record.s3_key,
        "sizeBytes": record.size_bytes,
        "createdAt": record.created_at,
        "messageId": record.message_id,
        "stored": True,
    }


# Where each governance policy reports its verdicts inside one assessment. Keys
# and list names are Bedrock's (`GuardrailAssessment` in the Converse API); the
# earlier hand-written `contentPolicyAssessment` / `wordPolicyAssessment` names
# never existed, which is why the Insights guardrail panel stayed at zero.
#
# The third element names the field that identifies *which* filter fired, or
# None for a label the item cannot safely carry. Custom words only have `match`
# — the user's own text — so they are reported under one fixed label; every
# other list has a type or a name that is configuration, not conversation.
_GUARDRAIL_POLICY_LISTS = (
    ("content", "contentPolicy", (("filters", "type"),)),
    ("pii", "sensitiveInformationPolicy", (("piiEntities", "type"), ("regexes", "name"))),
    ("topic", "topicPolicy", (("topics", "name"),)),
    ("word", "wordPolicy", (("customWords", None), ("managedWordLists", "type"))),
)
_CUSTOM_WORD_LABEL = "CUSTOM_WORD"


def _guardrail_event_from_assessment(assessment: Any, stage: str) -> Optional[Dict[str, Any]]:
    """Fold one `GuardrailAssessment` into an intervention event, or None if the
    guardrail scanned but did not act. Bedrock lists every evaluated filter with
    an `action` *and* a `detected` flag; only detected ones intervened, so a
    BLOCKED action on an undetected filter is not counted.

    Besides the policy, the event names the filter (`INSULTS`, `PROMPT_ATTACK`,
    a PII type, a topic name) and, for content filters, the confidence Bedrock
    reported. Both are configuration-side labels; the matched text never leaves
    the trace."""
    if not isinstance(assessment, dict):
        return None
    has_blocked = False
    has_anonymized = False
    triggered: set[str] = set()
    filter_types: set[str] = set()
    confidences: list[str] = []
    for policy, policy_key, lists in _GUARDRAIL_POLICY_LISTS:
        section = assessment.get(policy_key)
        if not isinstance(section, dict):
            continue
        for list_name, label_field in lists:
            items = section.get(list_name)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("detected") is not True:
                    continue
                action = str(item.get("action") or "").upper()
                if action == "BLOCKED":
                    has_blocked = True
                elif action == "ANONYMIZED":
                    has_anonymized = True
                else:
                    continue
                triggered.add(policy)
                label = item.get(label_field) if label_field else _CUSTOM_WORD_LABEL
                if isinstance(label, str) and label:
                    filter_types.add(label)
                confidence = item.get("confidence")
                if isinstance(confidence, str) and confidence:
                    confidences.append(confidence.upper())
    if not triggered:
        return None
    return {
        # BLOCKED takes precedence: the turn was stopped, masking is moot.
        "action": "BLOCKED" if has_blocked else "ANONYMIZED",
        "stage": stage,
        "policies": sorted(triggered),
        "filter_types": sorted(filter_types),
        "confidences": confidences,
    }


def _guardrail_assessment_root(
    metadata: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Locate the guardrail assessment object inside a Converse metadata dict.

    Two shapes appear in the wild — prefer the flat one if both exist:

      1) metadata["guardrail"]              — harness / some adapters flatten here
      2) metadata["trace"]["guardrail"]     — Bedrock ConverseStream (canonical)

    Insights used to stay at 0 after a visible chat BLOCK because we only read (1).
    Bedrock puts inputAssessment / outputAssessment under (2), so the parser
    returned [] and never called record_guardrail_event → no GUARDRAIL# rows.
    """
    if not isinstance(metadata, dict):
        return None
    flat = metadata.get("guardrail")
    if isinstance(flat, dict):
        return flat
    trace = metadata.get("trace")
    if isinstance(trace, dict):
        nested = trace.get("guardrail")
        if isinstance(nested, dict):
            return nested
    return None


def is_guardrail_intervened_stop(stop_reason: Optional[str]) -> bool:
    """True when Strands/Bedrock ended the turn because a Guardrail blocked.

    Measured path (2026-09-23): AgentCore → Strands stream often does **not**
    forward ``metadata.trace.guardrail`` to the platform. The chat still shows
    the blocked message via redactContent, and the turn ends with
    ``messageStop.stopReason = "guardrail_intervened"``. Insights must count
    that stop reason when the assessment metadata never arrives.
    """
    if not stop_reason:
        return False
    normalized = str(stop_reason).strip().lower().replace("-", "_")
    return normalized in ("guardrail_intervened", "guardrailintervened")


# Fixed blockedMessaging strings from ks-agent-platform-guardrail (Terraform).
# Prod 실측: 채팅에는 이 문구가 보이는데 messageStop.stopReason / metadata.trace
# 가 서버에 안 오는 경우가 있음 → 문구 일치로도 집계 (내용·PII는 저장 안 함).
_GUARDRAIL_BLOCKED_INPUT_MSG = (
    "요청을 처리할 수 없습니다. 안전 정책에 위배되는 내용이 감지되었습니다."
)
_GUARDRAIL_BLOCKED_OUTPUT_MSG = (
    "응답을 표시할 수 없습니다. 안전 정책에 위배되는 내용이 감지되었습니다."
)


def guardrail_stage_from_blocked_text(text: Optional[str]) -> Optional[str]:
    """Return 'input' / 'output' if text is exactly a Guardrail blocked message."""
    if not text or not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped == _GUARDRAIL_BLOCKED_INPUT_MSG:
        return "input"
    if stripped == _GUARDRAIL_BLOCKED_OUTPUT_MSG:
        return "output"
    return None


def guardrail_scanned(metadata: Any) -> bool:
    """True when this metadata event carries a guardrail trace at all.

    Present on every model call made with a `guardrailConfig`, intervention or
    not — so its absence across a turn is the fact that the agent ran without a
    guardrail, which is a governance gap the intervention counters alone can
    never show (zero interventions looks the same either way)."""
    trace = metadata.get("trace") if isinstance(metadata, dict) else None
    return isinstance(trace, dict) and isinstance(trace.get("guardrail"), dict)


def guardrail_events_from_metadata(metadata: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Extract guardrail interventions from a Bedrock Converse `metadata` event.

    Accepts either ``metadata.guardrail`` or ``metadata.trace.guardrail``
    (see ``_guardrail_assessment_root``). Its two stages are shaped differently:

        inputAssessment:   {guardrailId: GuardrailAssessment}
        outputAssessments: {guardrailId: [GuardrailAssessment, ...]}

    The output side is a list because async stream processing scans the reply
    in windows and reports each one. Every assessment that acted becomes one
    event: {"action": BLOCKED|ANONYMIZED, "stage": input|output, "policies": [...]}.

    Returns [] when there is no trace or the shape is not what Bedrock sends.
    Never includes content or PII values — counts only, for governance auditing.
    """
    events: list[Dict[str, Any]] = []

    guardrail_trace = _guardrail_assessment_root(metadata)
    if not guardrail_trace:
        return events

    input_assessments = guardrail_trace.get("inputAssessment")
    if isinstance(input_assessments, dict):
        for assessment in input_assessments.values():
            event = _guardrail_event_from_assessment(assessment, "input")
            if event:
                events.append(event)

    output_assessments = guardrail_trace.get("outputAssessments")
    if isinstance(output_assessments, dict):
        for per_chunk in output_assessments.values():
            if not isinstance(per_chunk, list):
                continue
            for assessment in per_chunk:
                event = _guardrail_event_from_assessment(assessment, "output")
                if event:
                    events.append(event)

    return events


async def _with_heartbeats(
    source: AsyncIterator[Dict[str, Any]], interval: float
) -> AsyncIterator[Any]:
    """Yield `source`'s events, plus `_HEARTBEAT` for every `interval` of silence.

    The wait is done on a task rather than with `asyncio.wait_for`, which cancels
    its awaitable on timeout — that would abort the very stream being kept alive.
    The same pending `__anext__` is waited on again after each heartbeat, so the
    padding repeats for as long as the gap lasts instead of firing once.

    Exceptions from `source` surface out of `task.result()` on the caller's side,
    where the existing handlers turn them into error events.
    """
    iterator = source.__aiter__()
    pending: Optional[asyncio.Future] = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield _HEARTBEAT
                continue
            task, pending = pending, None
            try:
                item = task.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        # An abandoned stream (GeneratorExit at a yield) would otherwise leave the
        # read task running with nobody to collect it.
        if pending is not None:
            pending.cancel()


class AgentNotApproved(Exception):
    """The turn would pin an agent whose registry record is not APPROVED.

    Raised only when a thread is being bound to its agent — a record that loses
    approval later does not brick the threads it already owns; the pin (409)
    already keeps other agents out of them. Distinct from ThreadForbidden so the
    route can say *why*: the caller's credentials are fine, the agent isn't.
    """


def execution_config(request_config: Any, bound: Dict[str, Any]) -> Dict[str, Any]:
    """The config a turn runs with: exactly what `bind_execution` returned.

    The binding is given the request's config as a dict (extras included) and
    hands back a copy with the target filled in — and, sometimes, with keys
    *removed*: a retired basic-chat thread pinned to a withdrawn model has its
    `model_id` dropped so the agent's default runs. Merging that result back
    over the raw request, as this used to, revived every removed key: live
    2026-10-10 the "dropped" claude-sonnet-5 still reached the runtime payload
    and the ledger. `request_config` is accepted only to document that it is
    deliberately not consulted here.
    """
    return dict(bound)


class StreamingService:
    """Service for handling thread streaming"""

    def __init__(
        self,
        thread_service: ThreadService,
        agentcore_client: AgentCoreClient,
        artifact_service: Optional[ArtifactService] = None,
        mcp_apps_relay: Optional[McpAppsRelay] = None,
        registry_service: Optional[RegistryService] = None,
        harness_output_service: Optional[Any] = None,
        usage_service: Optional[Any] = None,
        run_broker: Optional[RunBroker] = None,
    ):
        self.thread_service = thread_service
        # Owns the background task that drains each turn. Injected so one broker
        # is shared with the routes that attach to and cancel runs; the default
        # keeps the many tests that build this service bare working.
        self.run_broker = run_broker or RunBroker()
        self.agentcore_client = agentcore_client
        self.artifact_service = artifact_service
        self.mcp_apps_relay = mcp_apps_relay
        # Same default as the sibling services: an unconfigured registry raises
        # RegistryNotConfigured at call time, which the approval check treats as
        # "records cannot exist here" rather than as a refusal.
        self.registry_service = registry_service or RegistryService()
        self.harness_output_service = harness_output_service
        # Tokens already flow through this service as `metadata.usage` events and
        # were discarded. None means the usage table is unconfigured, which the
        # stream must not care about.
        self.usage_service = usage_service

    def _resolve_app_record(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """앱 마운트 신호에 그 앱을 제공하는 MCP 레코드 id를 채운다.

        런타임은 레지스트리를 모르므로 `resourceUri`까지만 보낸다. 브라우저는
        채팅 상대인 에이전트(A2A) 레코드 id밖에 없고, 릴레이는 MCP 레코드에서만
        엔드포인트를 얻으므로 그 값으로는 400이 된다 — 배포 환경에서 앱이 항상 오류
        카드로 떴던 원인이다. 그래서 서버가 여기서 해석한다.

        런타임이 이미 채워 보냈다면 그쪽이 이긴다. 탐색은 폴백이다.
        """
        app = event["event"]["mcpApp"]
        if app.get("recordId") or not self.mcp_apps_relay:
            return event

        uri = app.get("resourceUri")
        if not isinstance(uri, str) or not uri:
            return event

        try:
            record_id = self.mcp_apps_relay.record_for_resource(uri)
        except Exception as exc:
            # 탐색은 레지스트리와 MCP 세션을 탄다. 그것이 대화 전체를 죽이면 안 된다.
            logger.warning("Could not resolve the MCP record serving %s: %s", uri, exc)
            return event

        if record_id:
            app["recordId"] = record_id
        else:
            # 이벤트를 삼키지 않는다: 삼키면 UI에는 아무 일도 없었던 것처럼 보인다.
            # 내보내면 McpAppView가 릴레이 오류를 카드에 표시한다.
            logger.warning("No registered MCP record serves %s", uri)
        return event

    async def _app_signal_for(
        self,
        event: Dict[str, Any],
        message_id: Optional[str],
        already_signalled: set,
        is_harness_stream: bool,
    ) -> Optional[Dict[str, Any]]:
        """툴 호출에 앱이 붙어 있으면 마운트 신호를 만든다 (harness 전용).

        harness 스트림에만 적용한다. 런타임 에이전트는 스스로 `mcpApp` 을 내므로 양쪽에서
        발행하면 같은 앱이 두 번 마운트된다. 클라이언트 종류로 가르는 것이 타이밍에
        의존하지 않는 유일한 방법이다.

        블록이 **시작될 때** 만든다 (`contentBlockStart` 의 toolUse). 규격은 호스트가
        앱을 미리 띄워 `ui/notifications/tool-input-partial` 로 인자를 스트리밍할 수 있게
        하는데, 그러려면 인자가 다 모이기 전에 앱이 떠 있어야 한다. 이름과 id 는 시작
        이벤트에 이미 실려 있으므로 그것으로 충분하다. UI 는 같은 이벤트로 툴 호출을 먼저
        만들고 나서 이 신호를 받으므로 tool_call_id 짝 맞추기도 유지된다. 한 호출에 한 번만
        내도록 `already_signalled` 로 막는다 — harness 는 같은 블록에 delta 를 여러 번 보낸다.
        """
        if not self.mcp_apps_relay or not is_harness_stream:
            return None

        start = event.get("event", {}).get("contentBlockStart")
        tool_use = (start or {}).get("start", {}).get("toolUse") if isinstance(start, dict) else None
        if not isinstance(tool_use, dict):
            return None

        tool_use_id = tool_use.get("toolUseId")
        tool_name = tool_use.get("name")
        if not tool_use_id or not tool_name or tool_use_id in already_signalled:
            return None

        try:
            found = await asyncio.to_thread(
                self.mcp_apps_relay.app_for_tool, tool_name
            )
        except Exception as exc:
            # 탐색은 레지스트리와 MCP 세션을 탄다. 대화를 죽이면 안 된다.
            logger.warning("Could not resolve an app for tool %s: %s", tool_name, exc)
            return None

        if not found:
            return None

        record_id, resource_uri = found
        return {
            "event": {
                "mcpApp": {
                    "toolCallId": tool_use_id,
                    "toolName": tool_name,
                    "resourceUri": resource_uri,
                    "recordId": record_id,
                    "messageId": message_id,
                }
            }
        }

    def _artifact_signal_for(
        self,
        event: Dict[str, Any],
        message_id: Optional[str],
        already_signalled: set,
        tool_uses: Dict[str, Dict[str, Any]],
        is_harness_stream: bool,
    ) -> Optional[Dict[str, Any]]:
        """artifact 툴 호출을 `artifact` 이벤트로 합성한다 (harness 전용).

        런타임 에이전트는 `agent-runtime/main.py` 에서 스스로 `artifact` 를 내고 그
        시점이 `contentBlockStop` 과 같아서(`mcpApp` 과 같은 이유), 양쪽에서 내면
        artifact 가 두 번 저장된다. harness 는 그 코드를 지나지 않으므로 여기가
        harness 의 create_artifact/update_artifact 가 패널에 닿는 유일한 자리다.

        블록이 **끝날 때** 만든다. harness 는 같은 블록에 입력 delta 를 여러 번 보내므로
        그 사이에 만들면 문서가 아직 다 안 모였고, 툴 결과 턴에서 stop 이 한 번 더 온다.
        """
        if not is_harness_stream:
            return None
        if "contentBlockStop" not in event.get("event", {}):
            return None

        # 이 블록에서 시작된 툴 호출. `current_tool_use_id` 는 호출자가 이미 비웠을 수
        # 있어, `_app_signal_for` 와 같이 추적 중인 마지막 것을 쓴다.
        tool_use_id = next(reversed(tool_uses), None) if tool_uses else None
        if not tool_use_id or tool_use_id in already_signalled:
            return None

        tracked = tool_uses.get(tool_use_id) or {}
        return _artifact_event_from_tool_use(
            tool_use_id, tracked.get("name"), tracked.get("input"), message_id
        )

    def _get_agent_client(
        self,
        config: Optional[Dict[str, Any]] = None,
    ) -> AgentClient:
        """Build a client bound to the requested harness or runtime."""
        harness_arn = config.get("harness_arn") if config else None
        agent_runtime_arn = config.get("agent_runtime_arn") if config else None
        qualifier = config.get("qualifier") if config else None

        default_region = self.agentcore_client.region_name

        def _region_of(arn: Optional[str]) -> str:
            # arn:aws:bedrock-agentcore:{region}:{account}:... — the invoke has to
            # target the runtime's own region, not this platform's, or a runtime
            # registered from another region (AGENT_RUNTIME_DISCOVERY_REGIONS) is
            # unreachable. A same-region or malformed ARN falls back to ours.
            parts = (arn or "").split(":")
            return parts[3] if len(parts) > 4 and parts[3] else default_region

        # A harness owns its runtime and rejects InvokeAgentRuntime, so it has to be
        # checked first — a harness-backed record also carries a runtime ARN.
        if harness_arn and ":harness/" in harness_arn:
            return HarnessClient(
                harness_arn=harness_arn,
                qualifier=qualifier,
                region_name=_region_of(harness_arn),
            )

        if agent_runtime_arn or qualifier:
            arn = agent_runtime_arn or self.agentcore_client.agent_runtime_arn
            return AgentCoreClient(
                agent_runtime_arn=arn,
                qualifier=qualifier or self.agentcore_client.qualifier,
                region_name=_region_of(arn),
            )
        return self.agentcore_client

    @staticmethod
    def _utf16_length(text: str) -> int:
        """Length of `text` as JavaScript would count it.

        The offset is consumed by `String.prototype.slice`, which indexes UTF-16
        code units, while `len()` counts code points. They agree until the text
        contains an astral character — an emoji, which agents produce readily —
        and each one shifts every later offset by one, cutting the answer a
        character early at the tool call.
        """
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def _tool_calls_for_storage(
        tool_uses: Dict[str, Dict[str, Any]]
    ) -> list:
        """Convert tracked tool uses into the `tool_calls` the frontend reads.

        Every call is written settled — `completed` unless the tool reported a
        failure. The UI cannot tell a stored transcript from a live one — it reads
        `tool_calls` the same way in both — and treats a call with no status as
        still running, so omitting it leaves a spinner turning forever in a thread
        whose run ended long ago. This is only reached once the turn is over,
        which is the same reasoning useStream applies when it settles pending
        calls on `messageStop`.
        """
        tool_calls = []
        for tool_use_id, tool_use_data in tool_uses.items():
            tool_input = tool_use_data.get("input", "")

            # Input arrives as streamed JSON fragments; a truncated turn can
            # leave it unparseable, which is worth keeping verbatim rather than
            # dropping.
            try:
                if isinstance(tool_input, str):
                    parsed_input = json.loads(tool_input) if tool_input else {}
                else:
                    parsed_input = tool_input
            except (json.JSONDecodeError, TypeError):
                parsed_input = {"raw": tool_input}

            call: Dict[str, Any] = {
                "id": tool_use_id,
                "name": tool_use_data.get("name", ""),
                "args": parsed_input,
                "status": tool_use_data.get("status") or "completed",
            }
            # Lets a reopened thread interleave the call with the answer text the
            # way the live view did, instead of dropping every call to the bottom.
            if "content_offset" in tool_use_data:
                call["contentOffset"] = tool_use_data["content_offset"]
            # Runtimes that execute tools server-side send a toolResult; the ones
            # that do not simply have nothing to store here.
            if "result" in tool_use_data:
                call["result"] = tool_use_data["result"]
            tool_calls.append(call)
        return tool_calls

    def _attach_turn_extras(
        self,
        thread_id: str,
        messages: list,
        *,
        charts: list,
        verifications: list,
    ) -> None:
        """Amend the already-saved assistant message with charts it produced later.

        A runtime flushes its stashed charts and verification notes *after*
        `messageStop`, so by the time they arrive the message has been written and
        the per-message tracking state reset. Without this they reach the browser
        and are then lost: the live view renders the chart and a reopened thread
        does not.

        The last assistant message is the one being amended, because a turn is one
        message — the runtime keeps a single messageId across its whole tool loop.
        If the turn produced no message at all (no text, no tool call), one is
        created: the chart is work that really happened and would otherwise have
        nowhere to live.
        """
        index = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if isinstance(messages[i], dict) and messages[i].get("type") == "ai"
            ),
            None,
        )
        if index is None:
            messages.append(
                {"id": f"msg-{int(time.time() * 1000)}", "type": "ai", "content": ""}
            )
            index = len(messages) - 1

        message = dict(messages[index])
        if charts:
            message["charts"] = list(charts)
        if verifications:
            message["verifications"] = list(verifications)
        messages[index] = message

        thread = self.thread_service.get_thread(thread_id)
        if thread:
            thread.values["messages"] = messages
            thread.updated_at = datetime.utcnow().isoformat()
            self.thread_service.repository.update(thread_id, thread)

    def _persist_turn(
        self,
        thread_id: str,
        messages: list,
        *,
        message_id: Optional[str],
        content: str,
        reasoning: Optional[str],
        tool_uses: Dict[str, Dict[str, Any]],
        charts: Optional[list] = None,
        verifications: Optional[list] = None,
    ) -> bool:
        """Write the turn's assistant message into the stored transcript.

        The one writer for all three endings a turn can have — `messageStop`, a
        stream that stopped without one, and a severed connection. They used to
        carry near-copies of this, and the cancellation path carried none at all:
        the whole turn was discarded, so a thread cut mid-answer came back from
        DynamoDB as the question alone. Deliberately synchronous, because the
        cancellation path cannot await.

        `messages` is amended in place. Returns whether anything was written, which
        is false for a turn that produced no content, no reasoning and no tool call
        — there is nothing to store and no message worth inventing.
        """
        if not message_id or not (content or reasoning or tool_uses):
            return False

        ai_message: Dict[str, Any] = {
            "id": message_id,
            "type": "ai",
            # Ensure content is always a string.
            "content": content or "",
        }
        if reasoning:
            ai_message["reasoning"] = reasoning
        if tool_uses:
            tool_calls = self._tool_calls_for_storage(tool_uses)
            if tool_calls:
                ai_message["tool_calls"] = tool_calls
        # Only when present: an empty list on every message would be noise in
        # every stored record.
        if charts:
            ai_message["charts"] = list(charts)
        if verifications:
            ai_message["verifications"] = list(verifications)

        # A turn is one message, so a second write for the same id replaces the
        # first rather than appending beside it.
        for i, msg in enumerate(messages):
            if (
                isinstance(msg, dict)
                and msg.get("id") == message_id
                and msg.get("type") == "ai"
            ):
                messages[i] = ai_message
                break
        else:
            messages.append(ai_message)

        thread = self.thread_service.get_thread(thread_id)
        if not thread:
            return False
        thread.values["messages"] = messages
        thread.updated_at = datetime.utcnow().isoformat()
        self.thread_service.repository.update(thread_id, thread)
        logger.info(
            "Saved AI message to thread %s: id=%s, content length=%d, "
            "tool_calls count=%d",
            thread_id,
            message_id,
            len(ai_message.get("content", "")),
            len(ai_message.get("tool_calls", [])),
        )
        return True

    def _flush_usage(
        self,
        usage: Dict[str, int],
        tool_names: Dict[str, int],
        *,
        agent_record_id: str,
        owner_sub: str,
        interrupted: bool,
        failed: bool = False,
        turns: int = 0,
        thread_started: bool = False,
        thread_id: str = "",
        turn_id: str = "",
        model_id: Optional[str] = None,
        started_at: Optional[str] = None,
        model_calls: int = 0,
        team: Optional[str] = None,
    ) -> None:
        """Record what the turn consumed, then zero the accumulator.

        `turn_id` is the same string on every flush of one request, which is what
        lets the ledger count a turn once no matter how many times it flushes:
        the event is written on the first call and the later ones fold their
        tokens onto it. `model_id` was resolved before the turn ran, so the event
        is priced against what the agent actually executed with.

        Called from all four of `_persist_turn`'s sites. Zeroing is the
        idempotency: a runtime that emits several `messageStop` events gets one
        flush each, and the post-loop, cancellation and failure calls then find
        nothing left to write. A separate call rather than a line inside
        `_persist_turn` because that method returns early for a turn with no
        message — and such a turn can still have spent tokens. Each metadata event
        reports one model call's usage, not a running total — outputTokens can
        decrease across events in a multi-call turn, which a cumulative figure
        cannot do. Summing every call is the only way to bill accurately.

        `interrupted` and `failed` are distinct endings and both are recorded as
        turns. The failure path used to reach neither this method nor
        `_persist_turn`, so a turn that died on a model error or a runtime 5xx left
        no message, no tokens and no turn — the tokens it had already burned simply
        vanished, the leaderboard undercounted by however many turns failed, and the
        platform had no failure count of its own at all. "중단된 턴" counted only
        client disconnects, so the one number an operator asks for first was
        answerable exclusively from the metered CloudWatch tier.
        """
        spent = any(
            usage[tier] for tier in ("input", "output", "cache_read", "cache_write")
        )
        pending = bool(usage.get("pending"))
        if not self.usage_service or not (spent or tool_names or turns or thread_started or pending):
            return
        failed = failed or bool(usage.get("failed"))
        self.usage_service.record_turn(
            agent_record_id=agent_record_id,
            owner_sub=owner_sub,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
            cache_write_tokens=usage["cache_write"],
            tool_calls=dict(tool_names),
            turns=turns,
            interrupted=interrupted,
            failed=failed,
            thread_started=thread_started,
            thread_id=thread_id,
            turn_id=turn_id,
            model_id=model_id,
            started_at=started_at,
            ended_at=datetime.utcnow().isoformat() + "Z",
            model_calls=model_calls,
            # A metadata event arrived — with tokens, or with only a guardrail
            # trace because the block happened before the model ran. Either way
            # the runtime reported this turn, so zero tokens is a figure.
            usage_reported=bool(model_calls or usage.get("guardrail_scanned")),
            blocked=bool(usage.get("blocked")),
            team=team,
            long_tokens={name: int(usage.get(name) or 0) for name in LONG_COUNTERS},
        )
        for name in LONG_COUNTERS:
            usage[name] = 0
        usage["input"] = 0
        usage["output"] = 0
        usage["cache_read"] = 0
        usage["cache_write"] = 0
        usage["model_calls"] = 0
        usage["pending"] = 0
        tool_names.clear()

    def _require_approved(self, record_id: str, name: str) -> None:
        """Refuse to bind a thread to a registry record that is not APPROVED.

        The registry's search UI only surfaces approved records, but the request
        names the agent directly, so only the server can actually hold the line
        — otherwise any caller could chat with a DRAFT or REJECTED agent by id.

        Fails closed: a record that cannot be fetched cannot be shown to be
        approved. The exception is a server with no registry configured, where
        records cannot exist at all — local runtimes are addressed by ARN alone
        there, and that path must keep working.
        """
        # A deployed-resource fallback agent has no registry record to check; its
        # ARN binding is the authorisation. Recognised by the synthetic id prefix.
        if record_id.startswith("deployed:"):
            return
        try:
            # The approved revision when one exists, even if the latest revision
            # is a DRAFT opened by an edit — AWS keeps serving the approved one
            # to consumers, and so does chat.
            record = self.registry_service.chattable_record(record_id)
        except RegistryNotConfigured:
            return
        except Exception as exc:
            # Registry disabled or unavailable on the AWS side: cannot verify,
            # but the same fallback that produced this binding treats the agent
            # as usable. A verdict about the id itself (malformed, deleted) is
            # not an outage — that record cannot be approved. Anything else
            # stays fail-closed.
            if is_registry_unavailable(exc) and not is_record_verdict(exc):
                logger.warning("Registry unavailable; skipping approval check: %s", exc)
                return
            raise AgentNotApproved(
                f"Agent record {record_id} could not be verified: {exc}"
            ) from exc
        status = (record.status or "").upper()
        if status != "APPROVED":
            raise AgentNotApproved(
                f"Agent '{name or record_id}' is not approved for chat "
                f"(status: {record.status or 'unknown'})."
            )

    def _set_status_quietly(self, thread_id: str, status: ThreadStatusEnum) -> None:
        """Record a terminal status without letting the write mask what caused it.

        Reached from failure paths, where the thread may already be gone: raising
        here would replace the real error (or the cancellation) with a lookup
        failure about it.
        """
        try:
            self.thread_service.update_thread_status(thread_id, status)
        except Exception:
            logger.warning(
                "Could not set thread %s to %s", thread_id, status, exc_info=True
            )

    async def _prepare_run(
        self,
        thread_id: str,
        request: StreamRequest,
        existing: Any,
        owner_sub: str,
        t_request: float,
        caller: Optional[AuthUser] = None,
    ):
        """The pre-stream setup: approval gate, target binding, thread row.

        Runs under a reservation (see stream_thread_execution) and raises the
        same exceptions it always did; the caller releases the reservation.
        Returns (prepared_thread, requested_config, learned_target).
        """
        # Only a turn that would *bind* the thread to this agent needs the
        # approval gate: a thread already pinned to the record was approved when
        # it was pinned, and re-checking every turn would let a later revocation
        # brick existing conversations mid-history. Checked before the target is
        # derived so an unapproved record is refused as such, not for its ARNs.
        requested_record_id = (
            (getattr(request.config, "registry_record_id", None) or "") if request.config else ""
        ) or (getattr(existing, "agent_record_id", "") or "")
        if requested_record_id and (
            getattr(existing, "agent_record_id", "") != requested_record_id
        ):
            await asyncio.to_thread(
                self._require_approved,
                requested_record_id,
                (getattr(request.config, "registry_agent_name", None) or "") if request.config else "",
            )

        # Decide the execution target server-side. The record (or the default
        # runtime) says which ARNs this turn may run with; a model override is
        # checked against the allow-lists; client-sent values that disagree are
        # refused. See services/agent_access.py. Raises AgentTargetMismatch (403
        # at the route) before anything is written.
        requested_config, learned_target = await asyncio.to_thread(
            functools.partial(
                bind_execution,
                request.config.dict(exclude_none=False) if request.config else {},
                existing_thread=existing,
                registry_service=self.registry_service,
                default_client=self.agentcore_client,
                caller=caller,
                team_service=getattr(self, "team_service", None),
            )
        )

        # The agent this turn is addressed to. Pinned onto a new thread and
        # enforced on an existing one, so a thread cannot accumulate turns from
        # two agents — their AgentCore Memory scopes would collide or diverge.
        agent_record_id = requested_config.get("registry_record_id") or ""
        agent_name = requested_config.get("registry_agent_name") or ""
        # Set only when this turn moved a retired basic-chat thread onto the
        # default record: the model ThreadService writes into the thread's
        # override (None = the pinned model is no longer allowed, carry nothing).
        adopting = "adopted_model_id" in requested_config
        adopted_model_id = requested_config.get("adopted_model_id")

        # Everything up to the StreamingResponse goes through to_thread. These are
        # blocking DynamoDB and GetRegistryRecord calls, and this handler is
        # `async def` (it genuinely awaits the stream), so run directly they would
        # own the event loop and stall every other request in the process for their
        # duration — GetRegistryRecord alone was measured at 35s on a cold client.
        # The sync routes next door get this for free from FastAPI's threadpool;
        # see the comment above the handlers in routes/threads.py.
        #
        # to_thread re-raises in the awaiting coroutine, so ThreadForbidden /
        # ThreadAgentMismatch / AgentNotApproved still reach the route and still
        # become 403/409/403 rather than error events on an already-200 stream.

        # Get or create thread (without initial values - will be merged later).
        # Deliberately outside event_generator: a ThreadForbidden raised here
        # propagates to the route and becomes a 403, whereas the same failure
        # inside the generator would arrive as an error event on a 200 response,
        # after the client had already been told the stream had started.
        # ThreadAgentMismatch (409) rides the same path for the same reason.
        # AgentNotApproved (403) likewise fires above, before any stream starts.
        prepared_thread = await asyncio.to_thread(
            functools.partial(
                self.thread_service.get_or_create_thread,
                thread_id=thread_id,
                owner_sub=owner_sub,
                initial_values=None,
                agent_record_id=agent_record_id,
                agent_name=agent_name,
                # Only what this turn actually learned: a target freshly read
                # from the registry, or the model carried off a legacy thread.
                **({"adopted_model_id": adopted_model_id} if adopting else {}),
                **({"agent_target": learned_target} if learned_target else {}),
            )
        )

        logger.info(
            "[timing] thread=%s prestream (approval+thread setup)=%.3fs",
            thread_id,
            time.perf_counter() - t_request,
        )

        return prepared_thread, requested_config, learned_target

    async def stream_thread_execution(
        self,
        thread_id: str,
        request: StreamRequest,
        actor_id: Optional[str] = None,
        owner_sub: str = "",
        caller: Optional[AuthUser] = None,
    ) -> StreamingResponse:
        """Stream thread execution (Server-Sent Events)"""
        # Wall-clock origin for the startup-latency breakdown. Logged with the
        # `[timing]` tag at each phase boundary so a single grep reconstructs
        # where the pre-first-token seconds go (approval+thread setup vs. the
        # runtime invoke). perf_counter, not the event loop clock: the pre-stream
        # part runs before the generator and its loop.
        t_request = time.perf_counter()

        # The thread as it is now: supplies the pinned agent when the request
        # omits one, and decides whether this turn needs the approval gate.
        existing = await asyncio.to_thread(self.thread_service.get_thread, thread_id)

        # Ownership before the run check, so a guessed id on someone else's
        # busy thread is refused as foreign (403) and never learns it is busy
        # (409). get_or_create_thread enforces the full rule below; this only
        # settles the order for threads that already have an owner.
        existing_owner = getattr(existing, "owner_sub", None) if existing is not None else None
        if existing_owner and owner_sub and existing_owner != owner_sub:
            raise ThreadForbidden(f"Thread {thread_id} belongs to another user")

        # Claim the thread for the whole setup, not just from the first frame.
        # Two requests used to be able to pass an "is a run active" check
        # together, and the second then ran get_or_create_thread — a whole-row
        # put — after the first run had started writing, clobbering its human
        # message. Reserved, a second request is refused (409) before it writes
        # anything, and a Stop pressed during setup has a run to land on.
        reservation = self.run_broker.reserve(thread_id, owner_sub=owner_sub or None)
        try:
            prepared_thread, requested_config, learned_target = await self._prepare_run(
                thread_id, request, existing, owner_sub, t_request, caller
            )
        except BaseException:
            # No run will follow; free the thread for the next attempt.
            self.run_broker.release(reservation)
            raise

        agent_record_id = requested_config.get("registry_record_id") or ""
        agent_name = requested_config.get("registry_agent_name") or ""
        # The team this turn's spend and denials are attributed to. Decided once
        # here, from the caller's groups and the record's team tag; see
        # services/policy_denial.resolve_turn_team.
        team_names = getattr(getattr(self, "team_service", None), "names", None)
        turn_team = resolve_turn_team(
            list(caller.teams) if caller is not None else [],
            requested_config.get("record_team"),
            team_names() if callable(team_names) else None,
        )

        # What a reattaching client keeps from the stored record: the messages
        # from before this run, plus this run's own question — the generator
        # saves that below, so it is not in the row yet, and the replay never
        # re-sends it. Everything else in the row is this run's partial work,
        # which the replay rebuilds.
        baseline_message_ids = [
            m.get("id")
            for m in ((prepared_thread.values or {}).get("messages") or [])
            if isinstance(m, dict) and m.get("id")
        ]
        if request.values and isinstance(request.values.get("messages"), list):
            for msg in reversed(request.values["messages"]):
                if isinstance(msg, dict) and msg.get("type") == "human":
                    if msg.get("id") and msg["id"] not in baseline_message_ids:
                        baseline_message_ids.append(msg["id"])
                    break

        async def event_generator() -> AsyncIterator[str]:
            """Generate SSE events using Bedrock"""

            # Declared before the `try` so the cancellation handler can persist
            # whatever the turn had produced. Inside it, a disconnect during the
            # first `yield` would reach the handler with these still unbound.
            #
            # Track current message state for saving to thread
            current_message_id: Optional[str] = None
            current_message_content: str = ""
            current_message_reasoning: Optional[str] = None

            # Survives the `messageStop` reset. Whatever runs after the turn still
            # has to name the message it belongs to, and `current_message_id` is
            # None by then: the harness sweep stored every file with
            # `message_id=None`, and the chat filters cards by message, so the
            # files were downloadable through the API and attached to nothing.
            last_message_id: Optional[str] = None

            # Set once the client is resolved. The cancellation handler runs
            # outside the scope where config_dict exists, and it needs the ARN.
            sweep_harness_arn: Optional[str] = None

            # Track tool use for Strands Agent SDK
            current_tool_uses: Dict[str, Dict[str, Any]] = {}  # toolUseId -> {name, input}
            # A harness re-emits a tool result once per delta, each restamped
            # status="error"; record a denial once per toolUseId, not per delta.
            denials_recorded: set = set()
            current_tool_use_id: Optional[str] = None
            # Charts and verification notes the turn produced. Kept for the whole
            # turn rather than per-message: the runtime flushes them after
            # messageStop, when the per-message state has already been reset.
            current_charts: list = []
            current_verifications: list = []
            # Tool calls that already carry an app, from either path: the runtime's
            # own signal or the harness fallback. Without this a runtime agent whose
            # tool the fallback can also resolve mounts the same app twice.
            signalled_tool_calls: set = set()
            # Tool calls already turned into an `artifact` event (harness path).
            # A harness resends the block stop on its tool-result turn; without
            # this the same document would be stored on every stop.
            artifact_tool_calls: set = set()
            # Replaced by the stored transcript once it has been read.
            current_messages: list = []

            # Survives the `messageStop` reset for the same reason the charts do,
            # and is zeroed by `_flush_usage` rather than here.
            turn_usage: Dict[str, int] = {
                "input": 0,
                "output": 0,
                # Their own accumulators, not folded into `input`: Bedrock bills a
                # cache read at a tenth of an uncached input token, so a cost built
                # on one combined figure is off by that factor.
                "cache_read": 0,
                "cache_write": 0,
                # The part of each tier above spent in calls whose prompt crossed
                # the model's long-context threshold — a subset, not a fifth tier.
                **{name: 0 for name in LONG_COUNTERS},
                # How many model calls reported usage this turn. Not a token, but
                # it rides in the same accumulator because it is zeroed with them.
                "model_calls": 0,
                # 1 once any model call this turn carried a guardrail trace. Recorded
                # to the ledger on the flip, so a tool round-trip (two guarded calls)
                # is one scanned turn, and a turn with no trace at all leaves 0 —
                # which is how an unguarded agent becomes visible.
                "guardrail_scanned": 0,
                # 1 once the guardrail BLOCKED this turn. The turn then ends with
                # zero tokens, which is exact rather than unmeasured.
                "blocked": 0,
                # 1 once the runtime emitted an `error` event this turn.
                "failed": 0,
                # 1 while something recorded since the last flush is unwritten:
                # a metadata event (usage or a guardrail trace) or an error. The
                # flush gate used to be "tokens or tools or a turn to count", and
                # a zero-token event after messageStop met none of those.
                "pending": 0,
            }
            turn_tool_names: Dict[str, int] = {}
            # True once this turn already wrote GUARDRAIL# (metadata / redact /
            # stopReason / blocked-text). Prevents double-counting.
            turn_guardrail_recorded: bool = False
            # Best-effort stage when we only have stopReason / redactContent /
            # blocked messaging (no assessment). Input redaction is the common path.
            turn_guardrail_stage: str = "input"

            # The turn's identity for the ledger. The human message id names the
            # request, so every flush of this request presents the same turn id
            # and the ledger counts it once. Resolved (with the model) once the
            # request's config is known, below.
            turn_started_at = datetime.utcnow().isoformat() + "Z"
            turn_id = ""
            turn_model_id: Optional[str] = None
            long_threshold: Optional[int] = None

            # Gate for recording turns once per request, not once per flush.
            # A request may flush at messageStop, post-loop, and on cancellation,
            # but turns measure requests. Zeroed after first flush so later calls
            # pass turns=0 and write nothing.
            record_turns_on_next_flush: bool = True

            # Gate for recording thread start exactly once: on the first flush
            # of a thread's first turn. Evaluated after retrieving the thread.
            thread_has_prior_ai_message: bool = False

            t_gen = time.perf_counter()
            try:
                # Send initial event
                yield f"data: {json.dumps({'event': 'thread_created', 'thread_id': thread_id})}\n\n"

                # Reuse the thread the pre-stream block already fetched (and, for
                # a new thread, created). It settled both the ownership and the
                # agent question a moment ago on this same request; re-fetching
                # here only added a second read of a row nothing else has touched.
                thread = prepared_thread

                if thread.values is None:
                    thread.values = {}

                # Evaluate whether this is the thread's first turn (before streaming starts)
                thread_has_prior_ai_message = any(
                    m.get("type") == "ai"
                    for m in thread.values.get("messages", [])
                    if isinstance(m, dict)
                )
                
                # Always append new user message to DDB (works for both single-turn and multi-turn)
                # Extract the last user message from frontend messages and append to existing messages
                if request.values and "messages" in request.values and isinstance(request.values["messages"], list):
                    frontend_messages = request.values["messages"]
                    
                    # Find the last user message (new message to append)
                    last_user_message = None
                    for msg in reversed(frontend_messages):
                        if isinstance(msg, dict) and msg.get("type") == "human":
                            last_user_message = msg
                            break
                    
                    # Append last user message to existing messages if it's new
                    if last_user_message and last_user_message.get("id"):
                        existing_messages = thread.values.get("messages", []) if thread.values else []
                        existing_ids = {msg.get("id") for msg in existing_messages if isinstance(msg, dict) and msg.get("id")}
                        
                        if last_user_message.get("id") not in existing_ids:
                            existing_messages.append(last_user_message)
                            if thread.values is None:
                                thread.values = {}
                            thread.values["messages"] = existing_messages
                            thread.updated_at = datetime.utcnow().isoformat()
                            self.thread_service.repository.update(thread_id, thread)
                            # No reload: the in-memory `thread` already holds the
                            # list we just wrote, so re-reading it from DDB only
                            # spends a round-trip to get back what we have.
                else:
                    # No messages from frontend - ensure messages exist (from DDB)
                    if "messages" not in thread.values:
                        thread.values["messages"] = []

                # Execute with the config the binding returned — see execution_config.
                config_dict = execution_config(request.config, requested_config)
                last_update_time = asyncio.get_event_loop().time()
                UPDATE_INTERVAL = 1.0  # Update DynamoDB at most once per second during streaming
                
                # The turn's ledger identity and the model it will run on. The
                # model is read *now*, from the ARN this request names, because
                # pricing at read time re-priced history at whatever the agent runs
                # today. A lookup failure leaves the turn recorded but unpriced.
                human_message_id = None
                if request.values and isinstance(request.values.get("messages"), list):
                    for msg in reversed(request.values["messages"]):
                        if isinstance(msg, dict) and msg.get("type") == "human":
                            human_message_id = msg.get("id")
                            break
                turn_id = f"{thread_id}:{human_message_id or uuid.uuid4()}"
                # A thread-level override (`config.model_id`) is what InvokeHarness
                # actually runs — the harness client sends it as-is — so it is what
                # the turn is priced at. Only without one is the agent's default read.
                turn_model_id = config_dict.get("model_id") or None
                if self.usage_service and not turn_model_id:
                    try:
                        turn_model_id = self.usage_service.resolve_model_for(
                            harness_arn=config_dict.get("harness_arn"),
                            agent_runtime_arn=config_dict.get("agent_runtime_arn"),
                        )
                    except Exception:
                        logger.warning("Could not resolve the turn's model", exc_info=True)
                long_threshold = long_context_threshold(turn_model_id)

                # Log configuration for debugging
                logger.info(f"Streaming request config: agent_runtime_arn={config_dict.get('agent_runtime_arn')}, "
                          f"qualifier={config_dict.get('qualifier')}, "
                          f"harness_arn={config_dict.get('harness_arn')}")
                
                # Prepare values for agent execution. The client sends the whole
                # conversation, which wins over the stored copy: it is current,
                # while the thread record may lag a still-streaming turn.
                if request.values and "messages" in request.values and isinstance(request.values["messages"], list):
                    stream_values = {**(thread.values or {}), "messages": request.values["messages"]}
                else:
                    # No frontend messages - use thread messages
                    stream_values = thread.values.copy() if thread.values else {}

                # An MCP App's `ui/update-model-context` rides in config and is added
                # to the copy the model sees, never to the stored transcript: it is
                # not something the user said (spec SHOULD: provide it to the model
                # in future turns). The browser keeps only the last one per turn.
                app_model_context = config_dict.get("app_model_context")
                if app_model_context:
                    stream_values["messages"] = inject_app_model_context(
                        stream_values.get("messages", []), app_model_context
                    )

                # For DDB storage: always use thread's complete message list (preserves history)
                # User message was already appended above, AI message will be appended during streaming
                thread_messages = thread.values.get("messages", []) if thread.values else []
                
                agent_client = self._get_agent_client(config_dict)

                if isinstance(agent_client, HarnessClient):
                    # Only a harness leaves files behind that this server cannot
                    # see: a runtime agent emits its own `artifact` events, and
                    # sweeping as well would register each file twice.
                    sweep_harness_arn = config_dict.get("harness_arn")

                # Get raw stream from client (uses stream_values, not thread_messages)
                t_invoke = time.perf_counter()
                logger.info(
                    "[timing] thread=%s gen setup (read+append+client)=%.3fs",
                    thread_id,
                    t_invoke - t_gen,
                )
                # First turn of this thread: the runtime's session has no prior
                # events, so recall would read Memory twice only to restore
                # nothing. Ride the flag in config so the client signature is
                # untouched; AgentCoreClient forwards it into the runtime payload.
                config_dict["skip_recall"] = not thread_has_prior_ai_message
                raw_stream = agent_client.execute_stream(
                    thread_id=thread_id,
                    values=stream_values,
                    config=config_dict,
                    actor_id=actor_id,
                )
                # Flips false once the runtime puts the first real (non-heartbeat)
                # event on the wire — that gap is the InvokeAgentRuntime/InvokeHarness
                # latency (Memory retrieval + guardrail + model TTFT + any container
                # warm-up), the term the pre-stream cleanup cannot touch.
                timing_first_event = True
                
                # Use thread_messages for DDB storage (preserves all conversation history)
                current_messages = thread_messages.copy()
                
                # AgentCoreClient already normalizes runtime output to Strands format.
                #
                # Wrapped so a silent tool run still puts bytes on the wire: the
                # proxy in front of this server aborts an idle upstream socket, and
                # the server can only read that as the client hanging up. See
                # HEARTBEAT_INTERVAL_SECONDS.
                async for event in _with_heartbeats(
                    raw_stream, HEARTBEAT_INTERVAL_SECONDS
                ):
                    if event is _HEARTBEAT:
                        yield HEARTBEAT_FRAME
                        continue
                    if timing_first_event:
                        timing_first_event = False
                        logger.info(
                            "[timing] thread=%s invoke->first event=%.3fs "
                            "total request->first event=%.3fs",
                            thread_id,
                            time.perf_counter() - t_invoke,
                            time.perf_counter() - t_request,
                        )
                    # Parse metadata first, before any yields, so GeneratorExit won't
                    # prevent accumulation of tokens spent on this event. Each metadata
                    # event reports one model call's usage, which may be a small fraction
                    # of the turn's total; accumulate all of them.
                    event_for_parsing = event.get("event", {})
                    # A runtime that gives up mid-turn says so with an `error`
                    # event and then closes the stream normally, so the loop ends
                    # on the completed path. Remember it here: the turn is a
                    # failure whatever the stream's shape afterwards.
                    if isinstance(event_for_parsing, dict) and "error" in event_for_parsing:
                        turn_usage["failed"] = 1
                        turn_usage["pending"] = 1
                    if "metadata" in event_for_parsing:
                        # Something arrived to record. The metadata for a turn the
                        # guardrail blocks comes *after* messageStop, when the
                        # messageStop flush has already run with nothing to write —
                        # `pending` is what makes the post-loop flush write it.
                        turn_usage["pending"] = 1
                        reported = (event_for_parsing.get("metadata") or {}).get("usage") or {}
                        if reported:
                            turn_usage["model_calls"] += 1
                        turn_usage["input"] += int(reported.get("inputTokens") or 0)
                        turn_usage["output"] += int(reported.get("outputTokens") or 0)
                        # The cache tiers are their own fields and `inputTokens`
                        # does not include them: Converse's `TokenUsage` documents
                        # `inputTokens` as "the number of tokens sent in the request
                        # to the model" and lists `cacheReadInputTokens` separately.
                        # Reading only the first two therefore *undercounts the
                        # prompt* on any path where caching is on, silently — the
                        # day still carries a token attribute, so nothing marks the
                        # total as a floor. It also makes a model cost impossible to
                        # compute, because Bedrock bills a cache read at a tenth of
                        # an uncached input token and the two were being added into
                        # one counter.
                        turn_usage["cache_read"] += int(
                            reported.get("cacheReadInputTokens") or 0
                        )
                        turn_usage["cache_write"] += int(
                            reported.get("cacheWriteInputTokens") or 0
                        )
                        # A long-context card prices a *call* by that call's own
                        # prompt, which is why this is decided here, per metadata
                        # event: the turn's sums cannot tell one 150K-token call
                        # from three 50K ones, and only the first is billed long.
                        if reported and long_threshold is not None:
                            call = {
                                "input_tokens": int(reported.get("inputTokens") or 0),
                                "output_tokens": int(reported.get("outputTokens") or 0),
                                "cache_read_tokens": int(reported.get("cacheReadInputTokens") or 0),
                                "cache_write_tokens": int(reported.get("cacheWriteInputTokens") or 0),
                            }
                            prompt = call["input_tokens"] + call["cache_read_tokens"] + call["cache_write_tokens"]
                            if prompt > long_threshold:
                                for name, tokens in call.items():
                                    turn_usage[f"long_{name}"] += tokens

                        # Record guardrail interventions. Extract events from the metadata
                        # trace and record each independently. Silent on failure: a stream
                        # must never break because a counter write failed.
                        if self.usage_service:
                            metadata_dict = event_for_parsing.get("metadata") or {}
                            if not turn_usage["guardrail_scanned"] and guardrail_scanned(metadata_dict):
                                turn_usage["guardrail_scanned"] = 1
                                try:
                                    self.usage_service.record_guardrail_scan(
                                        agent_record_id=agent_record_id, owner_sub=owner_sub
                                    )
                                except Exception:
                                    logger.warning(
                                        "Failed to record guardrail scan for agent %s",
                                        agent_record_id, exc_info=True,
                                    )
                            for guardrail_event in guardrail_events_from_metadata(metadata_dict):
                                if guardrail_event.get("action") == "BLOCKED":
                                    turn_usage["blocked"] = 1
                                try:
                                    self.usage_service.record_guardrail_event(
                                        agent_record_id=agent_record_id,
                                        owner_sub=owner_sub,
                                        action=guardrail_event.get("action"),
                                        stage=guardrail_event.get("stage"),
                                        policies=guardrail_event.get("policies", []),
                                        filter_types=guardrail_event.get("filter_types", []),
                                        confidences=guardrail_event.get("confidences", []),
                                        thread_id=thread_id,
                                        turn_id=turn_id,
                                    )
                                    turn_guardrail_recorded = True
                                except Exception:
                                    logger.warning(
                                        "Failed to record guardrail event for agent %s, user %s",
                                        agent_record_id,
                                        owner_sub,
                                        exc_info=True,
                                    )

                            # Strands yields redactContent on BLOCK (often without metadata.trace).
                            # Count here when assessment never arrives — that is the measured
                            # AgentHub path (chat shows blocked text, Insights stayed at 0).
                            redact = event_for_parsing.get("redactContent")
                            if isinstance(redact, dict):
                                if redact.get("redactAssistantContentMessage"):
                                    turn_guardrail_stage = "output"
                                elif redact.get("redactUserContentMessage"):
                                    turn_guardrail_stage = "input"
                                if (
                                    self.usage_service
                                    and not turn_guardrail_recorded
                                    and (
                                        redact.get("redactUserContentMessage")
                                        or redact.get("redactAssistantContentMessage")
                                    )
                                ):
                                    try:
                                        self.usage_service.record_guardrail_event(
                                            agent_record_id=agent_record_id,
                                            owner_sub=owner_sub,
                                            action="BLOCKED",
                                            stage=turn_guardrail_stage,
                                            policies=[],
                                        )
                                        turn_guardrail_recorded = True
                                        logger.info(
                                            "[guardrail-stop-diag] recorded from redactContent "
                                            "stage=%s agent=%s keys=%s",
                                            turn_guardrail_stage,
                                            agent_record_id,
                                            sorted(redact.keys()),
                                        )
                                    except Exception:
                                        logger.warning(
                                            "Failed to record guardrail redactContent for agent %s",
                                            agent_record_id,
                                            exc_info=True,
                                        )

                    # Artifacts must be stored before they go out: the frontend needs
                    # the resolved id/version to refetch or share the artifact later.
                    # This delays only the artifact event, not the token stream.
                    if self.artifact_service and "artifact" in event.get("event", {}):
                        if current_message_id and not event["event"]["artifact"].get("messageId"):
                            event["event"]["artifact"]["messageId"] = current_message_id
                        event = await self.artifact_service.persist_and_enrich(thread_id, event)

                    # The browser cannot name the MCP server that serves an app, and
                    # the runtime does not know its registry record. Fill it in here,
                    # before the event goes out, or the relay rejects the lookup.
                    if "mcpApp" in event.get("event", {}):
                        event = await asyncio.to_thread(self._resolve_app_record, event)
                        signalled_tool_calls.add(
                            event["event"]["mcpApp"].get("toolCallId")
                        )

                    # Strands Agent format: {"event": {"messageStart": {...}, ...}}
                    # Yield event as SSE immediately (non-blocking) - don't delay streaming!
                    # Pass through Strands Agent format directly
                    yield f"data: {json.dumps(event, default=str)}\n\n"

                    # A harness runs its tools inside AWS, so the server only ever sees
                    # Converse events — no tool definitions and no `_meta`. The runtime's
                    # own `_mcp_app_event` never runs for it, which left apps unrendered
                    # for every harness-composed agent. The tool name is the only clue
                    # left in the stream, so derive the signal from it here.
                    #
                    # Emitted on `contentBlockStart`, right after the tool-use event
                    # itself went out, so the app is mounted before its arguments
                    # finish streaming (spec: hosts may preload the view and stream
                    # `tool-input-partial`). Harness-only: a runtime agent sends its
                    # own `mcpApp`, and two sources would mount the same app twice.
                    app_signal = await self._app_signal_for(
                        event,
                        current_message_id,
                        signalled_tool_calls,
                        isinstance(agent_client, HarnessClient),
                    )
                    if app_signal:
                        signalled_tool_calls.add(
                            app_signal["event"]["mcpApp"]["toolCallId"]
                        )
                        yield f"data: {json.dumps(app_signal, default=str)}\n\n"

                    # Same fallback for artifacts: a harness that calls the
                    # gateway's create_artifact/update_artifact never runs the
                    # runtime interception that opens the panel, so synthesize the
                    # `artifact` event from the tool input here. Persisted before
                    # it goes out for the same reason the runtime's own events are
                    # (see below): the frontend needs the resolved id/version.
                    artifact_signal = self._artifact_signal_for(
                        event,
                        current_message_id,
                        artifact_tool_calls,
                        current_tool_uses,
                        isinstance(agent_client, HarnessClient),
                    )
                    if artifact_signal:
                        artifact_tool_calls.add(
                            artifact_signal["event"]["artifact"]["toolCallId"]
                        )
                        if self.artifact_service:
                            artifact_signal = await self.artifact_service.persist_and_enrich(
                                thread_id, artifact_signal
                            )
                        yield f"data: {json.dumps(artifact_signal, default=str)}\n\n"


                    # Update thread values with new messages (after yielding - background update)
                    # Parse Strands Agent format
                    event_dict = event.get("event", {})
                    
                    # Track message state for Strands Agent SDK
                    if "messageStart" in event_dict:
                        message_start = event_dict.get("messageStart", {})
                        # Strands Agent SDK may not provide id, generate one if missing
                        new_message_id = message_start.get("id")
                        if not new_message_id:
                            new_message_id = f"msg-{int(time.time() * 1000)}"
                        
                        # Only reset if this is a different message (new message ID)
                        # This allows multiple tool calls in the same message
                        if new_message_id != current_message_id:
                            current_message_id = new_message_id
                            current_message_content = ""
                            current_message_reasoning = None
                            # Reset tool use tracking for new message
                            current_tool_uses = {}
                            current_tool_use_id = None
                        # If same message ID, keep accumulating tool calls
                    
                    elif "contentBlockStart" in event_dict:
                        # Handle tool use start for Strands Agent SDK
                        start_info = event_dict.get("contentBlockStart", {})
                        start = start_info.get("start", {})
                        tool_use = start.get("toolUse")
                        
                        if tool_use:
                            tool_use_id = tool_use.get("toolUseId")
                            tool_name = tool_use.get("name")
                            if tool_use_id and tool_name:
                                # Ensure message ID exists
                                if not current_message_id:
                                    current_message_id = f"msg-{int(time.time() * 1000)}"
                                
                                current_tool_use_id = tool_use_id
                                # Add or update tool use (allows multiple tool calls in same message)
                                if tool_use_id not in current_tool_uses:
                                    turn_tool_names[tool_name] = (
                                        turn_tool_names.get(tool_name, 0) + 1
                                    )
                                    current_tool_uses[tool_use_id] = {
                                        "name": tool_name,
                                        "input": "",
                                        # Where this call sits relative to the answer
                                        # text. One turn is one message, so without
                                        # this the stored transcript cannot say
                                        # whether the call came before or after the
                                        # text it is stored beside — and the UI
                                        # renders every call after all the text.
                                        "content_offset": self._utf16_length(
                                            current_message_content
                                        ),
                                    }
                                    logger.debug(f"Started tool use: {tool_name} (id: {tool_use_id}), total tools: {len(current_tool_uses)}")
                                else:
                                    logger.debug(f"Tool use already exists: {tool_name} (id: {tool_use_id})")
                    
                    elif "contentBlockDelta" in event_dict:
                        # If messageStart hasn't come yet, generate an ID
                        if not current_message_id:
                            current_message_id = f"msg-{int(time.time() * 1000)}"
                        
                        delta_info = event_dict.get("contentBlockDelta", {})
                        delta = delta_info.get("delta", {})
                        text = delta.get("text", "")
                        reasoning_content = delta.get("reasoningContent", {})
                        tool_use_delta = delta.get("toolUse", {})
                        
                        # Handle tool use input streaming for Strands Agent SDK
                        if tool_use_delta:
                            tool_input = tool_use_delta.get("input", "")
                            if tool_input:
                                # Try to find the tool use ID from the delta or use current
                                tool_use_id_to_update = current_tool_use_id
                                
                                # If we have tool input, update the current tool use
                                if tool_use_id_to_update and tool_use_id_to_update in current_tool_uses:
                                    current_tool_uses[tool_use_id_to_update]["input"] += tool_input
                                elif current_tool_uses:
                                    # If no current tool use ID but we have input, try to find the last tool use
                                    # This handles the case where contentBlockStop reset current_tool_use_id
                                    last_tool_use_id = list(current_tool_uses.keys())[-1]
                                    current_tool_uses[last_tool_use_id]["input"] += tool_input
                                    logger.debug(f"Updated tool use input for {last_tool_use_id} (no current ID set, using last tool)")
                                else:
                                    logger.warning(f"Received tool use delta but no tool uses tracked: {tool_input[:50]}")
                        
                        # Handle reasoning content
                        if isinstance(reasoning_content, dict):
                            reasoning_text = reasoning_content.get("text", "")
                            if reasoning_text:
                                if current_message_reasoning is None:
                                    current_message_reasoning = ""
                                current_message_reasoning += reasoning_text
                        
                        # Handle regular text content (can coexist with reasoning)
                        if text:
                            current_message_content += text
                    
                    elif "contentBlockStop" in event_dict:
                        # Tool use block completed - input is already accumulated
                        # The tool use is already in current_tool_uses, no action needed
                        current_tool_use_id = None
                    
                    elif "toolResult" in event_dict:
                        # Already yielded above; recorded here so a reopened thread
                        # shows the result the live view showed instead of an empty
                        # tool box.
                        result_info = event_dict.get("toolResult", {})
                        result_tool_use_id = result_info.get("toolUseId")
                        if result_tool_use_id in current_tool_uses:
                            current_tool_uses[result_tool_use_id]["result"] = (
                                result_info.get("result")
                            )
                            # A tool that failed reported an error as its result,
                            # and storing that beside a success mark would leave a
                            # reopened thread claiming the call worked. Only a
                            # failure is recorded: "success" is the default the
                            # writer already applies, and a runtime that reports
                            # nothing must keep landing on it.
                            if result_info.get("status") == "error":
                                current_tool_uses[result_tool_use_id]["status"] = "error"
                                if (
                                    self.usage_service
                                    and result_tool_use_id not in denials_recorded
                                    and is_policy_denial(result_info.get("result"))
                                ):
                                    denials_recorded.add(result_tool_use_id)
                                    try:
                                        self.usage_service.record_policy_denial(
                                            agent_record_id=agent_record_id,
                                            owner_sub=owner_sub,
                                            team=turn_team,
                                            tool_name=str(current_tool_uses[result_tool_use_id].get("name") or ""),
                                            thread_id=thread_id,
                                            turn_id=turn_id,
                                        )
                                    except Exception:
                                        logger.warning("Policy denial not recorded", exc_info=True)

                    elif "chart" in event_dict or "verification" in event_dict:
                        # Both normally arrive *after* messageStop: the runtime ends
                        # its turn and then flushes what it stashed. So recording
                        # them alongside the tool results is not enough — by then the
                        # message has been written and the tracking state reset, and
                        # anything later was yielded to the browser and forgotten.
                        # The live view had the chart; a reopened thread did not.
                        #
                        # The markdown image in the answer is not a fallback: it is
                        # presigned for five minutes while the object lives for days,
                        # so the stored copy needs the spec or it has nothing.
                        is_chart = "chart" in event_dict
                        bucket = current_charts if is_chart else current_verifications
                        bucket.append(
                            event_dict.get("chart" if is_chart else "verification")
                        )
                        # Before the stop the accumulated lists are picked up by the
                        # write below; after it, the saved record is amended in place.
                        if current_message_id is None:
                            self._attach_turn_extras(
                                thread_id,
                                current_messages,
                                charts=current_charts,
                                verifications=current_verifications,
                            )

                    # Handle messageStop event - save final AI message to thread
                    if "messageStop" in event_dict:
                        stop_info = event_dict.get("messageStop", {})
                        message_id = stop_info.get("messageId") or current_message_id
                        last_message_id = message_id or last_message_id
                        full_text = stop_info.get("fullText") or current_message_content
                        full_reasoning = stop_info.get("fullReasoning") or current_message_reasoning

                        # P2 fallbacks when metadata.trace.guardrail never arrives:
                        # 1) stopReason=guardrail_intervened
                        # 2) assistant text == Terraform blockedMessaging (실측: 채팅 OK)
                        # Always log stopReason so CloudWatch shows what we got.
                        stop_reason = stop_info.get("stopReason")
                        blocked_stage = guardrail_stage_from_blocked_text(full_text)
                        if blocked_stage:
                            turn_guardrail_stage = blocked_stage
                        logger.info(
                            "[guardrail-stop-diag] messageStop stopReason=%r "
                            "blocked_text_stage=%s already_recorded=%s agent=%s",
                            stop_reason,
                            blocked_stage,
                            turn_guardrail_recorded,
                            agent_record_id,
                        )
                        if (
                            self.usage_service
                            and not turn_guardrail_recorded
                            and (
                                is_guardrail_intervened_stop(stop_reason)
                                or blocked_stage is not None
                            )
                        ):
                            if not agent_record_id:
                                logger.warning(
                                    "[guardrail-stop-diag] skip record: empty "
                                    "agent_record_id (stopReason=%r)",
                                    stop_reason,
                                )
                            else:
                                try:
                                    self.usage_service.record_guardrail_event(
                                        agent_record_id=agent_record_id,
                                        owner_sub=owner_sub,
                                        action="BLOCKED",
                                        stage=turn_guardrail_stage,
                                        # Policy unknown without assessment — count
                                        # the intervention only (policies optional).
                                        policies=[],
                                    )
                                    turn_guardrail_recorded = True
                                    logger.info(
                                        "[guardrail-stop-diag] recorded from messageStop "
                                        "stopReason=%r stage=%s via=%s agent=%s",
                                        stop_reason,
                                        turn_guardrail_stage,
                                        "stopReason"
                                        if is_guardrail_intervened_stop(stop_reason)
                                        else "blocked_text",
                                        agent_record_id,
                                    )
                                except Exception:
                                    logger.warning(
                                        "Failed to record guardrail stopReason for agent %s",
                                        agent_record_id,
                                        exc_info=True,
                                    )
                        # Reset per-turn guardrail flags after this stop (a
                        # runtime may emit more than one messageStop).
                        turn_guardrail_recorded = False
                        turn_guardrail_stage = "input"

                        # Content may be empty when the turn only produced reasoning or tool calls.
                        if self._persist_turn(
                            thread_id,
                            current_messages,
                            message_id=message_id,
                            content=full_text,
                            reasoning=full_reasoning,
                            tool_uses=current_tool_uses,
                            charts=current_charts,
                            verifications=current_verifications,
                        ):
                            # Reset tracking state
                            current_message_id = None
                            current_message_content = ""
                            current_message_reasoning = None
                            current_tool_uses = {}
                            current_tool_use_id = None
                        else:
                            logger.warning(f"Failed to save message: message_id={message_id}, "
                                         f"full_text={bool(full_text)}, full_reasoning={bool(full_reasoning)}, "
                                         f"tool_uses={bool(current_tool_uses)}")

                        try:
                            turns_to_record = 1 if record_turns_on_next_flush else 0
                            thread_started_to_record = (
                                record_turns_on_next_flush and not thread_has_prior_ai_message
                            )
                            if record_turns_on_next_flush:
                                record_turns_on_next_flush = False
                            self._flush_usage(
                                turn_usage,
                                turn_tool_names,
                                agent_record_id=agent_record_id,
                                owner_sub=owner_sub,
                                team=turn_team,
                                thread_id=thread_id,
                                turn_id=turn_id,
                                model_id=turn_model_id,
                                started_at=turn_started_at,
                                model_calls=turn_usage["model_calls"],
                                interrupted=False,
                                turns=turns_to_record,
                                thread_started=thread_started_to_record,
                            )
                        except Exception:
                            logger.warning(
                                "Could not flush usage for turn on thread %s",
                                thread_id,
                                exc_info=True,
                            )

                    # Handle messages event (for backward compatibility)
                    if "messages" in event:
                        new_messages = event.get("messages", [])
                        current_messages = new_messages
                        # Save complete message list to DDB
                        thread = self.thread_service.get_thread(thread_id)
                        if thread:
                            thread.values["messages"] = current_messages
                            thread.updated_at = datetime.utcnow().isoformat()
                            self.thread_service.repository.update(thread_id, thread)
                        
                        # Throttle DynamoDB updates - only update every UPDATE_INTERVAL seconds
                        current_time = asyncio.get_event_loop().time()
                        if current_time - last_update_time >= UPDATE_INTERVAL:
                            last_update_time = current_time

                # Final update to ensure all messages are saved
                # If there's a pending message (stream ended without messageStop), save it
                self._persist_turn(
                    thread_id,
                    current_messages,
                    message_id=current_message_id,
                    content=current_message_content,
                    reasoning=current_message_reasoning,
                    tool_uses=current_tool_uses,
                    charts=current_charts,
                    verifications=current_verifications,
                )

                # Same blocked-text fallback if the stream ended without a
                # messageStop that carried stopReason (some AgentCore paths).
                post_blocked = guardrail_stage_from_blocked_text(current_message_content)
                if (
                    self.usage_service
                    and not turn_guardrail_recorded
                    and post_blocked
                    and agent_record_id
                ):
                    try:
                        self.usage_service.record_guardrail_event(
                            agent_record_id=agent_record_id,
                            owner_sub=owner_sub,
                            action="BLOCKED",
                            stage=post_blocked,
                            policies=[],
                        )
                        turn_guardrail_recorded = True
                        logger.info(
                            "[guardrail-stop-diag] recorded from post-loop blocked text "
                            "stage=%s agent=%s",
                            post_blocked,
                            agent_record_id,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to record post-loop guardrail for agent %s",
                            agent_record_id,
                            exc_info=True,
                        )

                try:
                    turns_to_record = 1 if record_turns_on_next_flush else 0
                    thread_started_to_record = (
                        record_turns_on_next_flush and not thread_has_prior_ai_message
                    )
                    if record_turns_on_next_flush:
                        record_turns_on_next_flush = False
                    self._flush_usage(
                        turn_usage,
                        turn_tool_names,
                        agent_record_id=agent_record_id,
                        owner_sub=owner_sub,
                        team=turn_team,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        model_id=turn_model_id,
                        started_at=turn_started_at,
                        model_calls=turn_usage["model_calls"],
                        interrupted=False,
                        turns=turns_to_record,
                        thread_started=thread_started_to_record,
                    )
                except Exception:
                    logger.warning(
                        "Could not flush usage for post-loop turn on thread %s",
                        thread_id,
                        exc_info=True,
                    )

                # A harness runs its tools inside AWS, so a file it produced is
                # still sitting in its sandbox. Collect it now, while the session
                # is alive, and emit the cards before the turn closes.
                #
                # After the loop there are no heartbeats — `_with_heartbeats`
                # wraps only the stream iteration — so this must stay short. It
                # is: two to four commands at 0.4-0.8s each. Preview extraction is
                # deliberately not here.
                if sweep_harness_arn and self.harness_output_service:
                    try:
                        collected = await self.harness_output_service.sweep(
                            thread_id,
                            sweep_harness_arn,
                            current_message_id or last_message_id,
                        )
                    except Exception:
                        # Best-effort: a finished turn must not fail because its
                        # files could not be collected.
                        logger.warning(
                            "Could not collect harness output for %s",
                            thread_id,
                            exc_info=True,
                        )
                        collected = []
                    for record in collected:
                        yield f"data: {json.dumps({'event': {'artifact': _artifact_event(record)}}, default=str)}\n\n"

                self.thread_service.update_thread_status(thread_id, ThreadStatusEnum.IDLE)

                event = StreamEvent(
                    event="end",
                    data={"status": "completed"},
                )
                yield f"data: {json.dumps(event.model_dump(), default=str)}\n\n"

            except (asyncio.CancelledError, GeneratorExit):
                # Stopping a run, closing the tab, or navigating away abandons this
                # generator, and the thread would keep the `busy` it was given at
                # the start of the run — forever. Every Stop click left a ghost for
                # the busy filter to accumulate.
                #
                # Both exceptions are needed, and neither is an `Exception`:
                # cancelling the consuming task raises CancelledError, while
                # closing a generator that is suspended at a `yield` raises
                # GeneratorExit. A streaming generator sits at a `yield` almost all
                # the time, so GeneratorExit is the common case, not the exotic one.
                #
                # Keep the work the turn had already done. Persisting only on
                # `messageStop` meant a severed connection discarded the whole turn,
                # and a harness sends exactly one `messageStop` — at the very end of
                # a tool loop that runs for minutes. The browser rebuilds a thread
                # from the stored record, so dropping it is what made every tool call
                # and every token already on screen disappear, leaving the question
                # alone. Synchronous by necessity: a closing generator cannot await.
                try:
                    self._persist_turn(
                        thread_id,
                        current_messages,
                        message_id=current_message_id,
                        content=current_message_content,
                        reasoning=current_message_reasoning,
                        tool_uses=current_tool_uses,
                        charts=current_charts,
                        verifications=current_verifications,
                    )
                except Exception:
                    # Same reasoning as _set_status_quietly: this runs while the
                    # generator is being torn down, and a failure to save must not
                    # replace the shutdown being propagated below.
                    logger.warning(
                        "Could not save the interrupted turn on thread %s",
                        thread_id,
                        exc_info=True,
                    )

                try:
                    turns_to_record = 1 if record_turns_on_next_flush else 0
                    thread_started_to_record = (
                        record_turns_on_next_flush and not thread_has_prior_ai_message
                    )
                    if record_turns_on_next_flush:
                        record_turns_on_next_flush = False
                    self._flush_usage(
                        turn_usage,
                        turn_tool_names,
                        agent_record_id=agent_record_id,
                        owner_sub=owner_sub,
                        team=turn_team,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        model_id=turn_model_id,
                        started_at=turn_started_at,
                        model_calls=turn_usage["model_calls"],
                        interrupted=True,
                        turns=turns_to_record,
                        thread_started=thread_started_to_record,
                    )
                except Exception:
                    logger.warning(
                        "Could not flush usage for interrupted turn on thread %s",
                        thread_id,
                        exc_info=True,
                    )

                # The turn's files are still in the sandbox, and the agent is
                # probably still writing them: on 2026-08-14 the client
                # disconnected at 04:58:38 while the harness kept running tools
                # until 05:00:06. Detached and delayed for that reason, and
                # because a closing generator cannot await anything.
                if sweep_harness_arn and self.harness_output_service:
                    try:
                        self.harness_output_service.spawn_delayed_sweep(
                            thread_id,
                            sweep_harness_arn,
                            current_message_id or last_message_id,
                        )
                    except Exception:
                        logger.warning(
                            "Could not schedule harness output recovery for %s",
                            thread_id,
                            exc_info=True,
                        )

                self._set_status_quietly(thread_id, ThreadStatusEnum.INTERRUPTED)
                # Never swallow either: the caller and the interpreter are both
                # entitled to see the shutdown they asked for.
                raise

            except Exception as e:
                self._set_status_quietly(thread_id, ThreadStatusEnum.ERROR)

                # A failed turn is still a turn, and it still spent what it spent.
                # This path used to save neither, so a model error or a runtime 5xx
                # discarded the partial answer *and* the tokens behind it, and the
                # turn never appeared in any count. Same writer and same ordering as
                # the cancellation path above, for the same reason: whatever the
                # agent had produced is worth keeping, and the flush has to happen
                # even when there was no message to keep.
                try:
                    self._persist_turn(
                        thread_id,
                        current_messages,
                        message_id=current_message_id,
                        content=current_message_content,
                        reasoning=current_message_reasoning,
                        tool_uses=current_tool_uses,
                        charts=current_charts,
                        verifications=current_verifications,
                    )
                except Exception:
                    # Never let the recovery replace the error being reported: the
                    # client is owed the original failure, not a failure about it.
                    logger.warning(
                        "Could not save the failed turn on thread %s",
                        thread_id,
                        exc_info=True,
                    )

                try:
                    turns_to_record = 1 if record_turns_on_next_flush else 0
                    thread_started_to_record = (
                        record_turns_on_next_flush and not thread_has_prior_ai_message
                    )
                    if record_turns_on_next_flush:
                        record_turns_on_next_flush = False
                    self._flush_usage(
                        turn_usage,
                        turn_tool_names,
                        agent_record_id=agent_record_id,
                        owner_sub=owner_sub,
                        team=turn_team,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        model_id=turn_model_id,
                        started_at=turn_started_at,
                        model_calls=turn_usage["model_calls"],
                        interrupted=False,
                        failed=True,
                        turns=turns_to_record,
                        thread_started=thread_started_to_record,
                    )
                except Exception:
                    logger.warning(
                        "Could not flush usage for failed turn on thread %s",
                        thread_id,
                        exc_info=True,
                    )

                # Same recovery as the interrupted path: a failed turn can still
                # have produced a finished file.
                if sweep_harness_arn and self.harness_output_service:
                    try:
                        self.harness_output_service.spawn_delayed_sweep(
                            thread_id,
                            sweep_harness_arn,
                            current_message_id or last_message_id,
                        )
                    except Exception:
                        logger.warning(
                            "Could not schedule harness output recovery for %s",
                            thread_id,
                            exc_info=True,
                        )

                error_event = StreamEvent(
                    event="error",
                    data={"error": str(e)},
                )
                yield f"data: {json.dumps(error_event.model_dump(), default=str)}\n\n"

        # The generator is drained by the broker's task, not by this response.
        # Whatever happens to the connection below, the task runs the turn to
        # its end and the generator's own handlers save it. The response is one
        # subscriber: replay (empty for the starter) and live tail.
        run = self.run_broker.launch(reservation, event_generator(), baseline_message_ids)
        return StreamingResponse(
            run.subscribe(heartbeat_interval=HEARTBEAT_INTERVAL_SECONDS),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

