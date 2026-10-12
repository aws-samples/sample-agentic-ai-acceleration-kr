"""Span timelines for one turn, out of CloudWatch Logs.

Two facts shape everything here.

**Spans land in one of two places.** Either the shared `aws/spans` log group or a
per-agent `spans` stream under `/aws/bedrock-agentcore/runtimes/<id>-<endpoint>`,
depending on Region and the agent's creation date. Querying one and stopping
produces an empty panel in some deployments and a working one in others — a bug
that reproduces for nobody who reports it. So both are discovered with
`DescribeLogGroups` and both are asked; only discovered groups are queried,
because `StartQuery` against a missing group throws.

**Logs Insights is asynchronous.** `StartQuery` then poll, several seconds. That
is why this is never on a list view — only when a user expands one turn — and why
a query that has not finished comes back as `{"status": "timeout"}` inside a 200.
A 5xx there would put SWR into backoff retry; what is wanted is a retry the user
initiates, when they choose.

Field paths in `_QUERY` come from the measurement recorded in the spec, not from
guesswork: Logs Insights addresses nested JSON by dotted path and an unknown path
returns an empty result set with no error at all.
"""
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import boto3

from core.config import AWS_REGION

logger = logging.getLogger(__name__)

# The two destinations from fact 6.
SHARED_SPAN_GROUP = "aws/spans"
RUNTIME_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes"

# Substitute the field paths the Task 1 probe recorded. `{session_id}` is the
# only interpolation, and it is a platform-generated id — but it still goes
# through a strict allowlist in `_safe_session_id` before reaching the query,
# because building a query string by interpolation is a filter-injection shape
# whether or not today's caller can reach it.
_QUERY = """fields @timestamp, name, durationNano / 1000000 as durationMs,
       attributes.session.id as sessionId, spanId, parentSpanId, startTimeUnixNano
| filter attributes.session.id = '{session_id}'
| sort startTimeUnixNano asc
| limit 200"""

_POLL_INTERVAL_SECONDS = 0.5


class TracesUnavailable(Exception):
    """Spans could not be read — no permission, or Transaction Search is off.

    The route turns this into `sources.traces: false` and a panel that explains
    the prerequisite, never a 5xx.
    """


class TraceService:
    """One turn's span timeline, on demand."""

    def __init__(
        self,
        region_name: Optional[str] = None,
        poll_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.region_name = region_name or AWS_REGION
        self.poll_seconds = poll_seconds
        # Injected so tests do not wait ten real seconds to prove a timeout.
        self._sleep = sleep
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("logs", region_name=self.region_name)
        return self._client

    @staticmethod
    def session_id_for(thread_id: str) -> str:
        """The AgentCore session id this platform derives from a thread id.

        **Replace this body with the rule the Task 1 probe recorded** by reading
        where `streaming_service` / `agentcore_client` construct the session id,
        and keep the two in one place afterwards. Nothing new is stored to make
        traces joinable — the key already exists — but only if this matches.
        """
        from agents.agentcore_client import AgentCoreClient

        return AgentCoreClient._session_id(thread_id)

    @staticmethod
    def _safe_session_id(session_id: str) -> str:
        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        )
        cleaned = "".join(ch for ch in session_id if ch in allowed)
        if not cleaned:
            raise TracesUnavailable("Session id has no queryable characters.")
        return cleaned

    def span_log_groups(self) -> List[str]:
        """Whichever of the two destinations exist in this account.

        Paginated with the explicit `nextToken` loop rather than a boto3
        paginator: the loop is the same three lines, and it is what lets a
        hand-written stub stand in for the client without also implementing
        `get_paginator`.
        """
        groups: List[str] = []
        try:
            for prefix in (SHARED_SPAN_GROUP, RUNTIME_GROUP_PREFIX):
                params: Dict[str, Any] = {"logGroupNamePrefix": prefix}
                while True:
                    response = self.client.describe_log_groups(**params)
                    groups.extend(
                        entry["logGroupName"]
                        for entry in response.get("logGroups", [])
                    )
                    token = response.get("nextToken")
                    if not token:
                        break
                    params["nextToken"] = token
        except Exception as exc:
            raise TracesUnavailable(str(exc)) from exc
        return groups

    @staticmethod
    def _to_span(result_row: List[Dict[str, str]]) -> Dict[str, Any]:
        """One Logs Insights row into a span.

        A missing or unparsable duration becomes `None` rather than dropping the
        row: one malformed span must not cost the user the whole timeline.
        """
        fields = {entry["field"]: entry["value"] for entry in result_row}
        duration = fields.get("durationMs")
        try:
            duration_ms: Optional[float] = float(duration) if duration else None
        except (TypeError, ValueError):
            duration_ms = None
        return {
            "name": fields.get("name") or "(unnamed)",
            "span_id": fields.get("spanId"),
            "parent_span_id": fields.get("parentSpanId"),
            "start_time": fields.get("startTimeUnixNano") or fields.get("@timestamp"),
            "duration_ms": duration_ms,
        }

    def _run_query(
        self, log_group: str, session_id: str, start: datetime, end: datetime
    ) -> Optional[List[List[Dict[str, str]]]]:
        """Rows for one group, or None if it did not finish in the budget."""
        query_string = _QUERY.format(session_id=session_id)
        try:
            started = self.client.start_query(
                logGroupName=log_group,
                startTime=int(start.timestamp()),
                endTime=int(end.timestamp()),
                queryString=query_string,
            )
        except Exception as exc:
            # A group that vanished between describe and query, or one this role
            # cannot read. The other destination may still answer.
            logger.info("Span query could not start on %s: %s", log_group, exc)
            return []

        query_id = started["queryId"]
        deadline = time.monotonic() + self.poll_seconds
        while True:
            try:
                response = self.client.get_query_results(queryId=query_id)
            except Exception as exc:
                logger.info("Span query failed on %s: %s", log_group, exc)
                return []
            status = response.get("status")
            if status == "Complete":
                return response.get("results", [])
            if status in ("Failed", "Cancelled", "Timeout"):
                return []
            if time.monotonic() >= deadline:
                return None
            self._sleep(_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _sort_key(span: Dict[str, Any]) -> Any:
        """Sort key for spans by start time, handling both nanosecond and string formats."""
        start_time = span.get("start_time")
        if not start_time:
            return (float('inf'), "")
        try:
            return (0, float(start_time))
        except (ValueError, TypeError):
            return (1, start_time if isinstance(start_time, str) else "")

    def spans_for_session(
        self, session_id: str, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        """The span timeline for one turn.

        `status` is one of:

        * `ok` — spans found; `log_group` names where.
        * `empty` — every destination answered, none had spans. Either the turn
          predates instrumentation or Transaction Search is off.
        * `timeout` — a query was still running when the budget ran out. Retry.

        `empty` and `timeout` are deliberately distinct: they lead the user to do
        different things, and collapsing them would make "wait a moment" look
        like "there is nothing here".
        """
        safe = self._safe_session_id(session_id)
        groups = self.span_log_groups()
        if not groups:
            return {"status": "empty", "spans": [], "log_group": None}

        timed_out = False
        for log_group in groups:
            rows = self._run_query(log_group, safe, start, end)
            if rows is None:
                timed_out = True
                continue
            if rows:
                spans = [self._to_span(row) for row in rows]
                spans.sort(key=self._sort_key)
                return {"status": "ok", "spans": spans, "log_group": log_group}

        return {
            "status": "timeout" if timed_out else "empty",
            "spans": [],
            "log_group": None,
        }
