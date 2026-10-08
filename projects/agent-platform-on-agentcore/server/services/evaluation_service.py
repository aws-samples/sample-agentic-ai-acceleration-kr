"""AgentCore batch evaluations, run on demand against real sessions.

Only batch evaluations are supported — no online configs. Online configs sample
and bill continuously on a shared account where three of them already run daily.
On-demand evaluation is the whole point: measure quality when the human decides,
never billable background work.

The module follows the same client lazy-loading and lock discipline that
`telemetry_service` uses, with one critical difference: any method that both
takes `self._lock` and needs a client must bind the client to a local **before**
entering the locked region. `self._lock` is not reentrant, and the client property
takes the same lock — the first call to `self.data` inside a locked region would
deadlock.
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import boto3

from core.config import AWS_REGION
from services.trace_service import TraceService, TracesUnavailable
from services.trace_service import (
    RUNTIME_GROUP_PREFIX,
)

logger = logging.getLogger(__name__)

NAME_PREFIX = "ap_"          # must start with a letter: [a-zA-Z][a-zA-Z0-9_]{0,47}
_MAX_NAME = 48               # measured; a longer name is a ValidationException

# An insights run is a batch evaluation with `insights` in place of `evaluators`,
# and ListBatchEvaluations returns neither field — the name is the only thing
# that tells the two kinds apart. A distinct prefix keeps `latest_for` (scores)
# from returning a triage run that has none, and vice versa. `apin_` rather than
# `ap_insights_` so a record literally named "insights" cannot collide with it.
INSIGHT_PREFIX = "apin_"

# The three built-in insight types (AgentCore insights, public preview 2026-09).
# Checked before the paid call: a misspelt id is a ValidationException that the
# route would relabel "unavailable", which is the misdirection the evaluator cap
# already caused once.
INSIGHT_IDS = (
    "Builtin.Insight.FailureAnalysis",
    "Builtin.Insight.UserIntent",
    "Builtin.Insight.ExecutionSummary",
)
_MAX_INSIGHTS = 10           # documented cap on `insights` per request

# Measured 2026-08-16 by sending all 42 per-runtime groups:
#   ValidationException: logGroupNames … Member must have length less than or
#   equal to 5; serviceNames … less than or equal to 1
# The serviceNames cap is the design constraint, not just a limit: **one batch
# evaluates exactly one agent**, so the data source is derived from that agent's
# runtime rather than assembled from everything the account has.
_MAX_LOG_GROUPS = 5
_MAX_SERVICE_NAMES = 1

# Measured 2026-08-19 with 18 evaluator ids — the number of built-in evaluators
# this account offers:
#   ValidationException: Value at 'evaluators' failed to satisfy constraint:
#   Member must have length less than or equal to 10
# So "score with every built-in evaluator" is not a request AWS accepts, and it
# has to be refused here: wrapped as `EvaluationsUnavailable`, a ValidationException
# reaches the reader as "the evaluation service is unavailable", which sent the
# /insights panel looking for a missing permission that was never missing.
_MAX_EVALUATORS = 10
_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)

TERMINAL_STATUSES = frozenset(
    {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "STOPPED"}
)

# Task 1 measured which form AgentCore matches. Our spans carry
# `resource.attributes.service.name` of `<runtimeName>.DEFAULT`, while the one
# batch in this account that ever evaluated sessions passed the suffixed form.
# Measured 2026-08-16: both `["bap_default.DEFAULT"]` and `["bap_default"]` completed
# successfully with identical scores, but we use the suffixed form because it matches
# what our spans actually carry in `resource.attributes.service.name`.
_SERVICE_NAME_SUFFIX = ".DEFAULT"


class EvaluationsUnavailable(Exception):
    """Batch evaluation could not be started or read — no permission, or region error.

    The routes turn this into an error response, never a 5xx for operations the
    caller could retry differently.
    """


def log_group_for(runtime_arn: str) -> str:
    """`arn:…:runtime/harness_x-AbC` -> `/aws/bedrock-agentcore/runtimes/harness_x-AbC-DEFAULT`.

    The endpoint qualifier is `DEFAULT` because that is the only one this platform
    creates — `harness_service` never passes another, and the span log groups in
    the account all end in `-DEFAULT`.
    """
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    return f"{RUNTIME_GROUP_PREFIX}/{runtime_id}-DEFAULT"


def service_name_for(log_group: str) -> str:
    """`/aws/bedrock-agentcore/runtimes/harness_x-AbC-DEFAULT` -> the service name.

    The group name ends in `-<endpointQualifier>-DEFAULT`; the service name
    AgentCore matches on is built from the runtime name (everything before the
    endpoint), not from the group name verbatim.
    """
    tail = log_group.rsplit("/", 1)[-1]
    # Strip -DEFAULT suffix, then take everything before the last hyphen (endpoint)
    if tail.endswith("-DEFAULT"):
        tail = tail[:-8]  # len("-DEFAULT") == 8
    runtime, _, _endpoint = tail.rpartition("-")
    return f"{runtime or tail}{_SERVICE_NAME_SUFFIX}"


def batch_name(slug: str, epoch: int, prefix: str = NAME_PREFIX) -> str:
    """`ap_<slug>_<epoch>`, inside AgentCore's measured charset and length limit.

    Measured 2026-08-16: `batchEvaluationName` must satisfy
    `[a-zA-Z][a-zA-Z0-9_]{0,47}`. **Hyphens are rejected** and the limit is 48,
    not 63 — a hyphenated name comes back as a ValidationException, which is at
    least loud, but the length bound is tight enough to matter for record ids.

    The epoch is never truncated. Trimming the tail instead of the slug would let
    two runs of the same record collide onto one name, and `latest_for` would then
    return whichever AWS happened to keep.
    """
    # ASCII only, explicitly. `str.isalnum()` accepts Unicode letters, so a
    # Korean or Greek record name would sail through and be rejected by AWS with a
    # ValidationException — the same class of non-ASCII boundary bug that already
    # broke S3 metadata and Content-Disposition in this repository.
    cleaned = "".join(ch if ch in _NAME_CHARS else "_" for ch in slug)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip("_") or "run"
    suffix = f"_{epoch}"
    room = _MAX_NAME - len(prefix) - len(suffix)
    return f"{prefix}{cleaned[:room]}{suffix}"


class EvaluationService:
    """Vended batch evaluations on real conversation data."""

    def __init__(
        self,
        region_name: Optional[str] = None,
        cache_seconds: int = 3600,
        trace_service: Optional[TraceService] = None,
        now=time.time,
    ):
        self.region_name = region_name or AWS_REGION
        self._cache_seconds = cache_seconds
        self._now = now
        self._traces = trace_service
        self._control = None
        self._data = None
        self._evaluators: Optional[List[Dict[str, Any]]] = None
        self._evaluators_at = 0.0
        self._lock = threading.Lock()

    @property
    def traces(self) -> TraceService:
        if self._traces is None:
            self._traces = TraceService(region_name=self.region_name)
        return self._traces

    @property
    def control(self):
        """bedrock-agentcore-control client for listing evaluators."""
        if self._control is None:
            with self._lock:
                if self._control is None:
                    self._control = boto3.client(
                        "bedrock-agentcore-control", region_name=self.region_name
                    )
        return self._control

    @property
    def data(self):
        """bedrock-agentcore client for batch evaluation lifecycle."""
        if self._data is None:
            with self._lock:
                if self._data is None:
                    self._data = boto3.client(
                        "bedrock-agentcore", region_name=self.region_name
                    )
        return self._data

    def evaluators(self) -> List[Dict[str, Any]]:
        """Active evaluators, cached for `cache_seconds`.

        Returns a list of dicts with:
        - evaluator_id: the evaluator identifier
        - name: human-readable name
        - level: one of TRACE, SESSION, TOOL_CALL
        - builtin: True for built-in evaluators, False for custom ones
        - status: always "ACTIVE" in the listing
        """
        # Bind client before lock to avoid deadlock on the non-reentrant lock.
        client = self.control

        with self._lock:
            fresh = (
                self._evaluators is not None
                and time.monotonic() - self._evaluators_at < self._cache_seconds
            )
            if fresh:
                return list(self._evaluators)

        evaluators: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {}
        try:
            while True:
                response = client.list_evaluators(**params)
                for item in response.get("evaluators", []):
                    evaluators.append({
                        "evaluator_id": item.get("evaluatorId"),
                        "name": item.get("evaluatorName"),
                        "level": item.get("level"),
                        "builtin": item.get("evaluatorType") == "Builtin",
                        "status": item.get("status"),
                    })
                # camelCase. The bedrock-agentcore data plane is camelCase
                # throughout, unlike cloudwatch and ce, and botocore rejects the
                # PascalCase form outright — which is only reachable once there is
                # a second page, so it survived every test.
                token = response.get("nextToken")
                if not token:
                    break
                params["nextToken"] = token
        except Exception as exc:
            raise EvaluationsUnavailable(str(exc)) from exc

        with self._lock:
            self._evaluators = evaluators
            self._evaluators_at = time.monotonic()

        return list(evaluators)

    def start(
        self,
        thread_ids: List[str],
        evaluator_ids: List[str],
        runtime_arn: str,
        agent_name: Optional[str] = None,
        record_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a batch evaluation over the sessions in the given threads.

        `runtime_arn` is required and identifies the one agent this batch covers —
        AWS caps `serviceNames` at a single entry, so a batch cannot span agents.

        Returns the normalised status shape. Raises `ValueError` for an empty list
        or a missing `runtime_arn`, and `EvaluationsUnavailable` when the agent has
        no span log group to evaluate over.
        """
        if not thread_ids or not evaluator_ids:
            raise ValueError("thread_ids and evaluator_ids must not be empty")

        # Not truncated to the cap: dropping eight of eighteen evaluators silently
        # would bill for a run that answers less than the caller asked for, and the
        # missing rows would read as "these scored nothing".
        if len(evaluator_ids) > _MAX_EVALUATORS:
            raise ValueError(
                f"AgentCore accepts at most {_MAX_EVALUATORS} evaluators per batch; "
                f"{len(evaluator_ids)} were requested"
            )

        return self._start_batch(
            name=batch_name(record_id or thread_ids[0], int(self._now())),
            data_source=self._data_source(thread_ids, runtime_arn),
            work={"evaluators": [{"evaluatorId": eid} for eid in evaluator_ids]},
            agent_name=agent_name,
        )

    def start_insights(
        self,
        thread_ids: List[str],
        runtime_arn: str,
        agent_name: Optional[str] = None,
        record_id: Optional[str] = None,
        insight_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Start an AgentCore insights run — failure triage, user intents,
        execution patterns — over the sessions in the given threads.

        Same batch machinery and the same one-agent scope as `start`; the request
        carries `insights` **instead of** `evaluators` (the API rejects both at
        once) and the result is three clustered trees rather than scores. Named
        under `INSIGHT_PREFIX` so the two kinds of run never answer each other's
        "latest" lookup.

        Billed per session analysed, like an evaluation, so the same "only when a
        human presses" rule applies — the service never schedules one.
        """
        if not thread_ids:
            raise ValueError("thread_ids must not be empty")

        ids = list(INSIGHT_IDS) if insight_ids is None else list(insight_ids)
        if not ids:
            raise ValueError("insight_ids must not be empty")
        unknown = [iid for iid in ids if iid not in INSIGHT_IDS]
        if unknown:
            raise ValueError(f"Unknown insight ids: {unknown}")
        if len(ids) > _MAX_INSIGHTS:
            raise ValueError(
                f"AgentCore accepts at most {_MAX_INSIGHTS} insights per batch"
            )

        return self._start_batch(
            name=batch_name(
                record_id or thread_ids[0], int(self._now()), prefix=INSIGHT_PREFIX
            ),
            data_source=self._data_source(thread_ids, runtime_arn),
            work={"insights": [{"insightId": iid} for iid in ids]},
            agent_name=agent_name,
        )

    def _data_source(
        self, thread_ids: List[str], runtime_arn: Optional[str]
    ) -> Dict[str, Any]:
        """`dataSourceConfig` for one agent's sessions — shared by both run kinds."""
        if not runtime_arn:
            raise ValueError("runtime_arn is required: a batch evaluates one agent")

        trace_svc = self.traces

        # One group, derived from this agent's own runtime. Two measurements force
        # this shape rather than "keep the per-runtime groups and pass them all":
        #
        # * `serviceNames` accepts at most 1 entry and `logGroupNames` at most 5,
        #   so scoping to one agent is the API's design, not our simplification.
        # * `aws/spans` holds spans without the correlated log events, so a batch
        #   aimed there fails every session with `LogEventMissingException:
        #   Session span data is incomplete. Span with ID … is missing a
        #   corresponding log event` — after AWS has charged for it.
        log_group = log_group_for(runtime_arn)

        # Checked against what exists so the failure is a sentence rather than a
        # batch that completes with zero sessions, which is how 60 of the 62
        # pre-existing batches in this account ended.
        available = trace_svc.span_log_groups()
        if log_group not in available:
            raise EvaluationsUnavailable(
                f"No span log group for this agent ({log_group}). Either the "
                "agent has not been invoked since tracing was enabled, or "
                "CloudWatch Transaction Search is off for the account."
            )

        log_groups = [log_group][:_MAX_LOG_GROUPS]
        service_names = [service_name_for(log_group)][:_MAX_SERVICE_NAMES]
        session_ids = [trace_svc.session_id_for(tid) for tid in thread_ids]

        return {
            "cloudWatchLogs": {
                "logGroupNames": log_groups,
                "serviceNames": service_names,
                "filterConfig": {
                    "sessionIds": session_ids,
                },
            },
        }

    def _start_batch(
        self,
        name: str,
        data_source: Dict[str, Any],
        work: Dict[str, Any],
        agent_name: Optional[str],
    ) -> Dict[str, Any]:
        """The one paid call. `work` is either `{"evaluators": […]}` or
        `{"insights": […]}`, never both."""
        start_params: Dict[str, Any] = {
            "batchEvaluationName": name,
            "dataSourceConfig": data_source,
            **work,
        }

        # Tag the run so LLM-as-judge spend is attributable through the same
        # keys the harness uses.
        if agent_name:
            start_params["tags"] = {
                "Platform": "bap",
                "AgentName": agent_name,
            }

        # Bind client before lock to avoid deadlock.
        client = self.data

        try:
            with self._lock:
                response = client.start_batch_evaluation(**start_params)
        except Exception as exc:
            raise EvaluationsUnavailable(str(exc)) from exc

        # Return normalized status with terminal: False since the batch just started.
        result = self._normalise(response)
        result["terminal"] = False
        return result

    def status(self, batch_id: str) -> Dict[str, Any]:
        """One batch's current status and results.

        Calls GetBatchEvaluation, never ListBatchEvaluations, because the list
        response omits the session counts. Measured 2026-08-16: a filter over
        the list saw 0 completed sessions across 62 batches, while Get on the
        same ids showed 27 and 30. Same trap as ListHarnesses omitting `tools`.
        """
        # Bind client before lock.
        client = self.data

        try:
            with self._lock:
                response = client.get_batch_evaluation(
                    batchEvaluationId=batch_id
                )
        except Exception as exc:
            raise EvaluationsUnavailable(str(exc)) from exc

        return self._normalise(response)

    def latest_for(self, record_id: str) -> Optional[Dict[str, Any]]:
        """The most recent *scored* batch for this record, or None if there are none.

        Pages ListBatchEvaluations and filters to batches that belong to us
        (name prefix of `ap_<slug>_`). Filters because 62 batches exist in this
        shared account and 60 belong to other teams — without the prefix filter,
        `latest_for` would return their batches instead.

        Returns None if no batch matches.
        """
        return self._latest(record_id, NAME_PREFIX)

    def latest_insights_for(self, record_id: str) -> Optional[Dict[str, Any]]:
        """The most recent insights (triage) run for this record, or None."""
        return self._latest(record_id, INSIGHT_PREFIX)

    def _latest(self, record_id: str, name_prefix: str) -> Optional[Dict[str, Any]]:
        # Build the prefix we're looking for: <kind>_<slugified>_
        slug = record_id
        prefix = f"{name_prefix}{slug.replace(' ', '_').replace('/', '_').replace('\\', '_')}_"

        # Bind client before lock.
        client = self.data

        candidates = []
        params: Dict[str, Any] = {}

        try:
            while True:
                with self._lock:
                    response = client.list_batch_evaluations(**params)

                for batch in response.get("batchEvaluations", []):
                    name = batch.get("batchEvaluationName") or ""
                    # Match prefix: ap_<slug>_
                    if name.startswith(prefix):
                        candidates.append(batch)

                token = response.get("nextToken")
                if not token:
                    break
                params["nextToken"] = token
        except Exception as exc:
            raise EvaluationsUnavailable(str(exc)) from exc

        if not candidates:
            return None

        # Sort by createdAt descending, return the most recent.
        candidates.sort(
            key=lambda b: b.get("createdAt", ""),
            reverse=True,
        )

        batch_id = candidates[0].get("batchEvaluationId")
        if batch_id:
            return self.status(batch_id)

        return None

    def _normalise(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """One `GetBatchEvaluation` response into the shape the routes return.

        Counts come from `evaluationResults` on a **Get** response only.
        `ListBatchEvaluations` omits them entirely — measured 2026-08-16, where a
        filter over the list saw 0 completed sessions across 62 batches while Get
        on the same ids showed 27 and 30. Same trap as `ListHarnesses` omitting
        `tools`.
        """
        results = response.get("evaluationResults") or {}
        completed = int(results.get("numberOfSessionsCompleted") or 0)
        failed = int(results.get("numberOfSessionsFailed") or 0)
        in_progress = int(results.get("numberOfSessionsInProgress") or 0)
        ignored = int(results.get("numberOfSessionsIgnored") or 0)
        # A partial response must not report 0/0, which reads as "nothing ran".
        total = int(
            results.get("totalNumberOfSessions")
            or (completed + failed + in_progress + ignored)
        )
        status = response.get("status") or "PENDING"
        created = response.get("createdAt")
        return {
            "batch_id": response.get("batchEvaluationId"),
            "name": response.get("batchEvaluationName"),
            "status": status,
            "terminal": status in TERMINAL_STATUSES,
            "sessions": {
                "total": total,
                "completed": completed,
                "failed": failed,
                "in_progress": in_progress,
                "ignored": ignored,
            },
            # Each score keeps its evaluator. Never an aggregate: one measured run
            # scored Conciseness 0.02 and Correctness 1.0, and their mean says
            # nothing true about either.
            "scores": [
                {
                    "evaluator_id": summary.get("evaluatorId"),
                    "average_score": (summary.get("statistics") or {}).get(
                        "averageScore"
                    ),
                    "evaluated": summary.get("totalEvaluated"),
                    "failed": summary.get("totalFailed"),
                }
                for summary in (results.get("evaluatorSummaries") or [])
            ],
            "errors": list(response.get("errorDetails") or []),
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            "result_log_group": (
                (response.get("outputConfig") or {}).get("cloudWatchConfig") or {}
            ).get("logGroupName"),
            # None on a scored run, so the quality panel and the triage panel —
            # which read the same shape — never mistake each other's run.
            "insights": _normalise_insights(response),
        }


def _normalise_insights(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The three clustered trees of an insights run, or None for a scored run.

    Every level keeps AgentCore's own `affectedSessionCount`. Nothing is summed
    across levels or across trees here or downstream: one session can sit in
    several categories, so a total of category counts is not a count of sessions.
    The per-session lists are kept in full — the route decides who may see them,
    because `userMessages` and `explanation` quote the conversation.
    """
    requested = [
        item.get("insightId")
        for item in (response.get("insights") or [])
        if item.get("insightId")
    ]
    trees = (
        response.get("failureAnalysisResult"),
        response.get("userIntentResult"),
        response.get("executionSummaryResult"),
    )
    if not requested and not any(trees):
        return None

    def cluster(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "cluster_id": item.get("clusterId"),
            "name": item.get("name"),
            "description": item.get("description"),
            "affected_session_count": int(item.get("affectedSessionCount") or 0),
        }

    failures = []
    for category in (trees[0] or {}).get("failures") or []:
        subs = []
        for sub in category.get("subCategories") or []:
            roots = []
            for root in sub.get("rootCauses") or []:
                roots.append({
                    **cluster(root),
                    "root_cause": root.get("rootCause"),
                    "recommendation": root.get("recommendation"),
                    "sessions": [
                        {
                            "session_id": hit.get("sessionId"),
                            "explanation": hit.get("explanation"),
                            "fix_type": hit.get("fixType"),
                            "recommendation": hit.get("recommendation"),
                        }
                        for hit in root.get("affectedSessions") or []
                    ],
                })
            subs.append({**cluster(sub), "root_causes": roots})
        failures.append({**cluster(category), "sub_categories": subs})

    intents = [
        {
            **cluster(item),
            "sessions": [
                {
                    "session_id": hit.get("sessionId"),
                    "user_messages": list(hit.get("userMessages") or []),
                }
                for hit in item.get("affectedSessions") or []
            ],
        }
        for item in (trees[1] or {}).get("userIntents") or []
    ]

    summaries = [
        {
            **cluster(item),
            "sessions": [
                {
                    "session_id": hit.get("sessionId"),
                    "approach_taken": hit.get("approachTaken"),
                    "final_outcome": hit.get("finalOutcome"),
                }
                for hit in item.get("affectedSessions") or []
            ],
        }
        for item in (trees[2] or {}).get("executionSummaries") or []
    ]

    return {
        "requested": requested,
        "failures": failures,
        "user_intents": intents,
        "execution_summaries": summaries,
    }
