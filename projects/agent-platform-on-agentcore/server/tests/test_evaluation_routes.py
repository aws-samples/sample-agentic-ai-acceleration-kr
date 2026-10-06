"""Tests for batch evaluation routes.

Coverage: evaluators(), start_evaluation(), evaluation_status(),
record_evaluation(). Each route degrades gracefully on EvaluationsUnavailable
rather than raising 5xx.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

import routes.insights as insights
from core.auth import AuthUser
from services.evaluation_service import EvaluationsUnavailable


def user(sub="sub-1", is_admin=False):
    """Create a real AuthUser instance for testing."""
    groups = ["admin"] if is_admin else []
    return AuthUser(sub=sub, username=sub, groups=groups)


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/harness_x-AbC"


def teardown_function():
    insights._evaluations_override = None
    insights._threads_override = None
    insights._usage_override = None


class StubEvaluationService:
    """Stub with hand-written responses, no AWS calls."""

    def __init__(self, unavailable=False, evaluators_list=None):
        self.unavailable = unavailable
        self.evaluators_list = evaluators_list or [
            {
                "evaluator_id": "builtin-conciseness",
                "name": "Conciseness",
                "level": "SESSION",
                "builtin": True,
                "status": "ACTIVE",
            },
            {
                "evaluator_id": "builtin-correctness",
                "name": "Correctness",
                "level": "SESSION",
                "builtin": True,
                "status": "ACTIVE",
            },
        ]
        self.started_batches = []

    def evaluators(self):
        if self.unavailable:
            raise EvaluationsUnavailable("Mock unavailable")
        return self.evaluators_list

    def start(
        self,
        thread_ids,
        evaluator_ids,
        runtime_arn,
        agent_name=None,
        record_id=None,
    ):
        if self.unavailable:
            raise EvaluationsUnavailable("Mock unavailable")
        if not thread_ids or not evaluator_ids:
            raise ValueError("thread_ids and evaluator_ids must not be empty")
        if not runtime_arn:
            raise ValueError("runtime_arn is required")
        # Record that a batch was started for assertion
        self.started_batches.append({
            "thread_ids": thread_ids,
            "evaluator_ids": evaluator_ids,
            "runtime_arn": runtime_arn,
            "record_id": record_id,
        })
        return {
            "batch_id": "batch-123",
            "name": "ap_test_123",
            "status": "PENDING",
            "terminal": False,
            "sessions": {
                "total": len(thread_ids),
                "completed": 0,
                "failed": 0,
                "in_progress": 0,
                "ignored": 0,
            },
            "scores": [],
            "errors": [],
            "created_at": "2026-08-16T12:00:00",
            "result_log_group": None,
        }

    def status(self, batch_id):
        if self.unavailable:
            raise EvaluationsUnavailable("Mock unavailable")
        return {
            "batch_id": batch_id,
            "name": "ap_test_123",
            "status": "IN_PROGRESS",
            "terminal": False,
            "sessions": {
                "total": 1,
                "completed": 0,
                "failed": 0,
                "in_progress": 1,
                "ignored": 0,
            },
            "scores": [],
            "errors": [],
            "created_at": "2026-08-16T12:00:00",
            "result_log_group": None,
        }

    def latest_for(self, record_id):
        if self.unavailable:
            raise EvaluationsUnavailable("Mock unavailable")
        # Return None by default (no prior run)
        return None


class StubThread:
    def __init__(self, thread_id, owner_sub, agent_record_id="rec1"):
        self.thread_id = thread_id
        self.owner_sub = owner_sub
        # A batch is scoped to one agent because AWS caps `serviceNames` at 1, so
        # the route resolves this record to a runtime ARN before spending anything.
        self.agent_record_id = agent_record_id
        self.created_at = "2026-08-07T00:00:00"
        self.updated_at = "2026-08-07T00:00:00"


class StubRecord:
    def __init__(self, record_id, agent_runtime_arn=RUNTIME_ARN):
        self.record_id = record_id
        self.name = record_id
        self.agent_runtime_arn = agent_runtime_arn
        self.harness_arn = None


class StubRegistry:
    def __init__(self, records=None, error=None):
        self.records = records if records is not None else [StubRecord("rec1")]
        self.error = error

    def agent_records(self):
        if self.error:
            raise self.error
        return list(self.records)


class StubUsage:
    """Only `registry` is read by these routes."""

    def __init__(self, registry=None):
        self.registry = registry or StubRegistry()
        self.configured = True


class StubThreadService:
    def __init__(self, threads=None):
        self.threads = threads or {}

    def get_thread(self, thread_id):
        return self.threads.get(thread_id)


def test_evaluators_returns_catalogue_and_sources_true():
    """Test 1: evaluators() returns the catalogue and sources.evaluations: True."""
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc

    result = insights.evaluators(user=user())

    assert result["sources"]["evaluations"] is True
    assert len(result["evaluators"]) == 2
    assert result["evaluators"][0]["evaluator_id"] == "builtin-conciseness"


def test_evaluators_unavailable_returns_empty_with_sources_false():
    """Test 2: EvaluationsUnavailable from evaluators() gives sources.evaluations: False
    with an empty list and no exception."""
    evals_svc = StubEvaluationService(unavailable=True)
    insights._evaluations_override = evals_svc

    result = insights.evaluators(user=user())

    assert result["sources"]["evaluations"] is False
    assert result["evaluators"] == []


def test_start_evaluation_non_admin_is_403():
    """Test 3: start_evaluation() as a non-admin raises HTTPException 403."""
    evals_svc = StubEvaluationService()
    thread = StubThread("t1", "sub-1")
    thread_svc = StubThreadService({"t1": thread})
    insights._evaluations_override = evals_svc
    insights._threads_override = thread_svc
    insights._usage_override = StubUsage()

    request_body = {
        "thread_ids": ["t1"],
        "evaluator_ids": ["builtin-conciseness"],
        "record_id": None,
    }

    try:
        insights.start_evaluation(
            body=type("Request", (), request_body)(),
            user=user(is_admin=False),
        )
        raise AssertionError("expected 403")
    except HTTPException as exc:
        assert exc.status_code == 403


def test_start_evaluation_missing_thread_returns_404_before_start():
    """Test 4: start_evaluation() as an admin over a missing thread returns 404
    before any start_batch_evaluation call."""
    evals_svc = StubEvaluationService()
    thread_svc = StubThreadService({})  # No threads
    insights._evaluations_override = evals_svc
    insights._threads_override = thread_svc
    insights._usage_override = StubUsage()

    from pydantic import BaseModel

    class Request(BaseModel):
        thread_ids: list
        evaluator_ids: list
        record_id: str | None

    request_body = Request(
        thread_ids=["t-missing"],
        evaluator_ids=["builtin-conciseness"],
        record_id=None,
    )

    try:
        insights.start_evaluation(body=request_body, user=user(is_admin=True))
        raise AssertionError("expected 404")
    except HTTPException as exc:
        assert exc.status_code == 404

    # Assert no batch was started
    assert len(evals_svc.started_batches) == 0


def test_start_evaluation_admin_over_owned_thread_succeeds():
    """Test 5: start_evaluation() as an admin over an owned thread returns the
    status shape with terminal: False."""
    evals_svc = StubEvaluationService()
    thread = StubThread("t1", "sub-1")
    thread_svc = StubThreadService({"t1": thread})
    insights._evaluations_override = evals_svc
    insights._threads_override = thread_svc
    insights._usage_override = StubUsage()

    from pydantic import BaseModel

    class Request(BaseModel):
        thread_ids: list
        evaluator_ids: list
        record_id: str | None

    request_body = Request(
        thread_ids=["t1"],
        evaluator_ids=["builtin-conciseness"],
        record_id=None,
    )

    result = insights.start_evaluation(body=request_body, user=user(is_admin=True))

    assert result["batch_id"] == "batch-123"
    assert result["terminal"] is False
    assert result["status"] == "PENDING"


def test_start_evaluation_empty_evaluator_ids_returns_422():
    """Test 6: start_evaluation() with an empty evaluator_ids returns 422 and
    asserts zero starts."""
    evals_svc = StubEvaluationService()
    thread = StubThread("t1", "sub-1")
    thread_svc = StubThreadService({"t1": thread})
    insights._evaluations_override = evals_svc
    insights._threads_override = thread_svc
    insights._usage_override = StubUsage()

    from pydantic import BaseModel

    class Request(BaseModel):
        thread_ids: list
        evaluator_ids: list
        record_id: str | None

    request_body = Request(
        thread_ids=["t1"],
        evaluator_ids=[],
        record_id=None,
    )

    try:
        insights.start_evaluation(body=request_body, user=user(is_admin=True))
        raise AssertionError("expected 422")
    except HTTPException as exc:
        assert exc.status_code == 422

    # Assert no batch was started
    assert len(evals_svc.started_batches) == 0


def test_start_evaluation_503_states_why_rather_than_just_unavailable():
    """The reason has to survive the status code.

    This route answered a bare "Evaluation service unavailable" for every
    `EvaluationsUnavailable`, so a ValidationException about the request we sent
    (measured: `evaluators` capped at 10, and the panel was sending 18) read on
    screen as an AgentCore entitlement the account did not have.
    """
    evals_svc = StubEvaluationService(unavailable=True)
    thread = StubThread("t1", "sub-1")
    insights._evaluations_override = evals_svc
    insights._threads_override = StubThreadService({"t1": thread})
    insights._usage_override = StubUsage()

    from pydantic import BaseModel

    class Request(BaseModel):
        thread_ids: list
        evaluator_ids: list
        record_id: str | None

    body = Request(thread_ids=["t1"], evaluator_ids=["builtin-conciseness"], record_id=None)

    try:
        insights.start_evaluation(body=body, user=user(is_admin=True))
        raise AssertionError("expected 503")
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "Mock unavailable" in exc.detail


def test_evaluation_status_returns_normalised_shape():
    """Test 7: evaluation_status() returns the normalised shape."""
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc

    result = insights.evaluation_status(batch_id="batch-123", user=user())

    assert result["batch_id"] == "batch-123"
    assert result["status"] == "IN_PROGRESS"
    assert result["sources"]["evaluations"] is True


def test_evaluation_status_unavailable_returns_200_with_status_unavailable():
    """Test 8: evaluation_status() on EvaluationsUnavailable returns a 200 with
    status: unavailable and sources.evaluations: False."""
    evals_svc = StubEvaluationService(unavailable=True)
    insights._evaluations_override = evals_svc

    result = insights.evaluation_status(batch_id="batch-123", user=user())

    assert result["batch_id"] == "batch-123"
    assert result["status"] == "unavailable"
    assert result["sources"]["evaluations"] is False


def test_record_evaluation_no_prior_run_returns_status_none():
    """Test 9: record_evaluation() with no prior run returns {status: none}."""
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc

    result = insights.record_evaluation(record_id="rec-1", user=user())

    assert result == {"status": "none"}


def test_record_evaluation_auth_user_construction():
    """Test 10: AuthUser instances are constructed with sub, username, groups."""
    # This test verifies the pattern used in other tests.
    auth_user = user(sub="sub-1", is_admin=False)
    assert auth_user.sub == "sub-1"
    assert auth_user.username == "sub-1"
    assert auth_user.groups == []
    assert auth_user.is_admin is False

    admin_user = user(sub="sub-2", is_admin=True)
    assert admin_user.sub == "sub-2"
    assert admin_user.is_admin is True
    assert "admin" in admin_user.groups


# --- one batch, one agent ---------------------------------------------------
#
# AWS caps `dataSourceConfig.cloudWatchLogs.serviceNames` at a single entry
# (measured 2026-08-16), so the route must resolve exactly one runtime ARN before
# it spends anything. Every failure below has to happen *before* the paid call.


def _request(thread_ids, evaluator_ids=("builtin-correctness",), record_id=None):
    from pydantic import BaseModel

    class Request(BaseModel):
        thread_ids: list
        evaluator_ids: list
        record_id: str | None

    return Request(
        thread_ids=list(thread_ids),
        evaluator_ids=list(evaluator_ids),
        record_id=record_id,
    )


def test_the_resolved_runtime_arn_reaches_the_service():
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc
    insights._threads_override = StubThreadService({"t1": StubThread("t1", "sub-1")})
    insights._usage_override = StubUsage()

    insights.start_evaluation(body=_request(["t1"]), user=user(is_admin=True))

    assert evals_svc.started_batches[0]["runtime_arn"] == RUNTIME_ARN


def test_threads_from_two_agents_are_refused_before_the_paid_call():
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc
    insights._threads_override = StubThreadService({
        "t1": StubThread("t1", "sub-1", agent_record_id="rec1"),
        "t2": StubThread("t2", "sub-1", agent_record_id="rec2"),
    })
    insights._usage_override = StubUsage()

    try:
        insights.start_evaluation(body=_request(["t1", "t2"]), user=user(is_admin=True))
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("expected 422")
    assert evals_svc.started_batches == []


def test_a_record_without_a_runtime_arn_is_refused_before_the_paid_call():
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc
    insights._threads_override = StubThreadService({"t1": StubThread("t1", "sub-1")})
    insights._usage_override = StubUsage(
        registry=StubRegistry(records=[StubRecord("rec1", agent_runtime_arn=None)])
    )

    try:
        insights.start_evaluation(body=_request(["t1"]), user=user(is_admin=True))
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("expected 422")
    assert evals_svc.started_batches == []


def test_a_thread_whose_agent_is_not_registered_is_refused():
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc
    insights._threads_override = StubThreadService({
        "t1": StubThread("t1", "sub-1", agent_record_id="rec-unknown")
    })
    insights._usage_override = StubUsage()

    try:
        insights.start_evaluation(body=_request(["t1"]), user=user(is_admin=True))
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("expected 422")
    assert evals_svc.started_batches == []


def test_a_thread_with_no_agent_at_all_is_refused():
    """Five threads in the live table have an empty `agent_record_id`."""
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc
    insights._threads_override = StubThreadService({
        "t1": StubThread("t1", "sub-1", agent_record_id=None)
    })
    insights._usage_override = StubUsage()

    try:
        insights.start_evaluation(body=_request(["t1"]), user=user(is_admin=True))
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("expected 422")
    assert evals_svc.started_batches == []


def test_an_unreadable_registry_is_a_503_not_a_paid_guess():
    evals_svc = StubEvaluationService()
    insights._evaluations_override = evals_svc
    insights._threads_override = StubThreadService({"t1": StubThread("t1", "sub-1")})
    insights._usage_override = StubUsage(
        registry=StubRegistry(error=RuntimeError("AccessDeniedException"))
    )

    try:
        insights.start_evaluation(body=_request(["t1"]), user=user(is_admin=True))
    except HTTPException as exc:
        assert exc.status_code == 503
    else:
        raise AssertionError("expected 503")
    assert evals_svc.started_batches == []


# --- AgentCore insights (triage) routes ----------------------------------------
#
# Same admin gate and one-agent scoping as the evaluation routes. What is new is
# who may read the per-session detail: `userMessages` and `explanation` quote the
# conversation, so a plain user gets the cluster counts and nothing underneath.

THREAD_A = "129f18e1-8285-45eb-80de-d3dc7589c806"
SESSION_A = f"{THREAD_A}-padded-to-33"


def _insights_run(with_sessions=True):
    hits = [{"session_id": SESSION_A, "explanation": "Throttled.",
             "fix_type": "CODE", "recommendation": "Retry."}] if with_sessions else []
    return {
        "batch_id": "apin_rec1_1-i1",
        "name": "apin_rec1_1",
        "status": "COMPLETED",
        "terminal": True,
        "sessions": {"total": 3, "completed": 3, "failed": 0, "in_progress": 0, "ignored": 0},
        "scores": [],
        "errors": [],
        "created_at": "2026-09-23T05:00:00",
        "result_log_group": None,
        "insights": {
            "requested": ["Builtin.Insight.FailureAnalysis", "Builtin.Insight.UserIntent"],
            "failures": [{
                "cluster_id": 1, "name": "Execution errors", "description": "d",
                "affected_session_count": 1,
                "sub_categories": [{
                    "cluster_id": 11, "name": "Rate limiting", "description": "d",
                    "affected_session_count": 1,
                    "root_causes": [{
                        "cluster_id": 111, "name": "No retry", "description": None,
                        "root_cause": "Gives up.", "recommendation": "Retry.",
                        "affected_session_count": 1, "sessions": hits,
                    }],
                }],
            }],
            "user_intents": [{
                "cluster_id": 1, "name": "Summarise", "description": "d",
                "affected_session_count": 1,
                "sessions": [{"session_id": SESSION_A, "user_messages": ["요약해줘"]}] if with_sessions else [],
            }],
            "execution_summaries": [],
        },
    }


class StubInsightsService(StubEvaluationService):
    def __init__(self, latest=None, **kwargs):
        super().__init__(**kwargs)
        self.started_insights = []
        self._latest_insights = latest

    def start_insights(self, thread_ids, runtime_arn, agent_name=None,
                       record_id=None, insight_ids=None):
        if self.unavailable:
            raise EvaluationsUnavailable("Mock unavailable")
        if not thread_ids:
            raise ValueError("thread_ids must not be empty")
        self.started_insights.append({
            "thread_ids": thread_ids, "runtime_arn": runtime_arn,
            "record_id": record_id, "insight_ids": insight_ids,
        })
        run = _insights_run(with_sessions=False)
        run.update({"status": "PENDING", "terminal": False, "insights": None})
        return run

    def status(self, batch_id):
        if self.unavailable:
            raise EvaluationsUnavailable("Mock unavailable")
        return _insights_run()

    def latest_insights_for(self, record_id):
        if self.unavailable:
            raise EvaluationsUnavailable("Mock unavailable")
        return self._latest_insights


class StubThreadServiceWithSearch(StubThreadService):
    def search_threads(self, **kwargs):
        self.search_kwargs = kwargs
        return list(self.threads.values())


class StubTraceService:
    @staticmethod
    def session_id_for(thread_id):
        return f"{thread_id}-padded-to-33"


def _analysis_request(thread_ids, record_id=None, insight_ids=None):
    return insights.StartAnalysisRequest(
        thread_ids=list(thread_ids), record_id=record_id, insight_ids=insight_ids,
    )


def _wire(svc, threads=None):
    insights._evaluations_override = svc
    insights._threads_override = StubThreadServiceWithSearch(
        threads if threads is not None else {THREAD_A: StubThread(THREAD_A, "sub-1")}
    )
    insights._usage_override = StubUsage()
    insights._traces_override = StubTraceService()


def teardown_module():
    insights._traces_override = None


def test_start_analysis_non_admin_is_403_and_nothing_is_started():
    svc = StubInsightsService()
    _wire(svc)
    try:
        insights.start_analysis(body=_analysis_request([THREAD_A]), user=user())
        assert False, "expected 403"
    except HTTPException as exc:
        assert exc.status_code == 403
    assert svc.started_insights == []


def test_start_analysis_admin_reaches_the_service_with_the_resolved_runtime():
    svc = StubInsightsService()
    _wire(svc)

    result = insights.start_analysis(
        body=_analysis_request([THREAD_A], record_id="rec1"), user=user(is_admin=True)
    )

    started = svc.started_insights[0]
    assert started["runtime_arn"] == RUNTIME_ARN
    assert started["thread_ids"] == [THREAD_A]
    assert started["record_id"] == "rec1"
    assert result["status"] == "PENDING"
    assert result["terminal"] is False


def test_start_analysis_missing_thread_is_404_before_the_paid_call():
    svc = StubInsightsService()
    _wire(svc, threads={})
    try:
        insights.start_analysis(body=_analysis_request(["ghost"]), user=user(is_admin=True))
        assert False, "expected 404"
    except HTTPException as exc:
        assert exc.status_code == 404
    assert svc.started_insights == []


def test_start_analysis_unavailable_is_a_503_that_states_why():
    svc = StubInsightsService(unavailable=True)
    _wire(svc)
    try:
        insights.start_analysis(body=_analysis_request([THREAD_A]), user=user(is_admin=True))
        assert False, "expected 503"
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "Mock unavailable" in exc.detail


def test_record_analysis_with_no_prior_run_is_status_none():
    _wire(StubInsightsService(latest=None))
    assert insights.record_analysis("rec1", user=user()) == {"status": "none"}


def test_record_analysis_unavailable_degrades_inside_a_200():
    _wire(StubInsightsService(unavailable=True))
    result = insights.record_analysis("rec1", user=user())
    assert result["status"] == "unavailable"
    assert result["sources"] == {"evaluations": False}


def test_record_analysis_keeps_counts_but_strips_session_detail_for_a_plain_user():
    """Cluster counts are about a public agent; the sessions under them quote
    people's conversations. A plain user sees the former only."""
    _wire(StubInsightsService(latest=_insights_run()))

    result = insights.record_analysis("rec1", user=user())

    tree = result["insights"]
    assert tree["session_details"] is False
    root = tree["failures"][0]["sub_categories"][0]["root_causes"][0]
    assert root["affected_session_count"] == 1
    assert root["sessions"] == []
    assert tree["user_intents"][0]["sessions"] == []
    assert result["sources"] == {"evaluations": True}


def test_record_analysis_maps_session_ids_back_to_thread_ids_for_an_admin():
    """AgentCore only knows the padded session id; the admin drilling in wants
    the thread. The map is built from the record's own threads."""
    _wire(StubInsightsService(latest=_insights_run()))

    result = insights.record_analysis("rec1", user=user(is_admin=True))

    tree = result["insights"]
    assert tree["session_details"] is True
    hit = tree["failures"][0]["sub_categories"][0]["root_causes"][0]["sessions"][0]
    assert hit["session_id"] == SESSION_A
    assert hit["thread_id"] == THREAD_A
    assert tree["user_intents"][0]["sessions"][0]["thread_id"] == THREAD_A
    assert tree["user_intents"][0]["sessions"][0]["user_messages"] == ["요약해줘"]


def test_analysis_status_strips_session_detail_for_a_plain_user_too():
    _wire(StubInsightsService())
    result = insights.analysis_status("apin_rec1_1-i1", user=user())
    assert result["insights"]["session_details"] is False
    assert result["insights"]["user_intents"][0]["sessions"] == []
