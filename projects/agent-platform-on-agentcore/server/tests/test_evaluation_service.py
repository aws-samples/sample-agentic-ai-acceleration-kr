"""AgentCore Evaluations, on demand and only on demand.

The shapes below are copied from real 2026-08-16 responses. Three of them are
traps this file exists to hold shut:

* `ListBatchEvaluations` omits the session counts that `GetBatchEvaluation`
  returns, so a reader that trusts the list sees every batch as empty.
* Both real hand-run batches in this account finished COMPLETED_WITH_ERRORS, so
  partial failure is the normal terminal state, not an exception.
* 62 batches exist in this shared account and 60 belong to other people, so every
  listing must be filtered by our own name prefix.
"""
import pytest

from services.evaluation_service import (
    EvaluationService,
    EvaluationsUnavailable,
    NAME_PREFIX,
    batch_name,
)

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/harness_academic_writer-EfGhI55667"
RUNTIME_GROUP = "/aws/bedrock-agentcore/runtimes/harness_academic_writer-EfGhI55667-DEFAULT"
SERVICE_NAME = "harness_academic_writer.DEFAULT"   # Task 1's measured form
THREAD = "129f18e1-8285-45eb-80de-d3dc7589c806"


def evaluator(eid, level="TRACE", etype="Builtin"):
    return {
        "evaluatorArn": f"arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/{eid}",
        "evaluatorId": eid,
        "evaluatorName": eid,
        "evaluatorType": etype,
        "level": level,
        "status": "ACTIVE",
    }


class StubControl:
    """`bedrock-agentcore-control` — evaluator listing only."""

    def __init__(self, pages=None, error=None):
        self.calls = 0
        self.error = error
        self.pages = pages or [{
            "evaluators": [
                evaluator("Builtin.Correctness"),
                evaluator("Builtin.GoalSuccessRate", level="SESSION"),
                evaluator("Builtin.SkillSelectionAccuracy", level="TOOL_CALL"),
                evaluator("search_relevance_v2-4X8s0c3H8T", etype="Custom"),
            ]
        }]

    # botocore rejects unknown parameters, so a stub that swallows **params makes
    # a misspelled one unfalsifiable. `params["NextToken"] = token` — PascalCase —
    # shipped and only failed once a second page existed.
    _ALLOWED = {"nextToken", "maxResults"}

    def list_evaluators(self, **params):
        unknown = set(params) - self._ALLOWED
        assert not unknown, f"botocore would reject: {sorted(unknown)}"
        self.calls += 1
        if self.error:
            raise self.error
        index = min(self.calls - 1, len(self.pages) - 1)
        return self.pages[index]


class StubData:
    """`bedrock-agentcore` — batch evaluation lifecycle."""

    def __init__(self, get=None, listing=None, start_error=None, list_pages=None):
        self._list_pages = list_pages
        self.started = []
        self.start_error = start_error
        self._get = get or {
            "batchEvaluationId": "ap_rec1_1786000000-abc123",
            "batchEvaluationName": "ap_rec1_1786000000",
            "status": "COMPLETED_WITH_ERRORS",
            "createdAt": "2026-08-16T05:00:00+00:00",
            "outputConfig": {"cloudWatchConfig": {
                "logGroupName": "/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default",
                "logStreamName": "run-x",
            }},
            "evaluationResults": {
                "numberOfSessionsCompleted": 27,
                "numberOfSessionsInProgress": 0,
                "numberOfSessionsFailed": 1,
                "numberOfSessionsIgnored": 0,
                "totalNumberOfSessions": 28,
                "evaluatorSummaries": [
                    {"evaluatorId": "Builtin.ResponseRelevance",
                     "statistics": {"averageScore": 0.98},
                     "totalEvaluated": 32, "totalFailed": 0},
                    {"evaluatorId": "Builtin.Conciseness",
                     "statistics": {"averageScore": 0.02},
                     "totalEvaluated": 32, "totalFailed": 0},
                ],
            },
            "errorDetails": ["1 of 28 sessions failed during batch evaluation."],
        }
        self.list_calls = []
        self.get_ids = []
        # Deliberately missing evaluationResults counts, exactly as the real
        # ListBatchEvaluations response does.
        self._listing = listing if listing is not None else [
            {"batchEvaluationId": "ap_rec1_1786000000-abc123",
             "batchEvaluationName": "ap_rec1_1786000000",
             "status": "COMPLETED_WITH_ERRORS",
             "createdAt": "2026-08-16T05:00:00+00:00"},
            {"batchEvaluationId": "ap_rec1_1785000000-old",
             "batchEvaluationName": "ap_rec1_1785000000",
             "status": "COMPLETED",
             "createdAt": "2026-08-15T05:00:00+00:00"},
            {"batchEvaluationId": "someone-elses-batch",
             "batchEvaluationName": "insightsquickstart17828071_daily_20260717T000000",
             "status": "COMPLETED",
             "createdAt": "2026-08-17T05:00:00+00:00"},
        ]

    def start_batch_evaluation(self, **params):
        self.started.append(params)
        if self.start_error:
            raise self.start_error
        return {
            "batchEvaluationId": params["batchEvaluationName"] + "-abc123",
            "batchEvaluationName": params["batchEvaluationName"],
            "status": "PENDING",
            "createdAt": "2026-08-16T05:00:00+00:00",
        }

    def get_batch_evaluation(self, **params):
        self.get_ids.append(params["batchEvaluationId"])
        return dict(self._get)

    def list_batch_evaluations(self, **params):
        unknown = set(params) - {"nextToken", "maxResults"}
        assert not unknown, f"botocore would reject: {sorted(unknown)}"
        self.list_calls.append(params)
        if self._list_pages is not None:
            return self._list_pages[
                min(len(self.list_calls) - 1, len(self._list_pages) - 1)
            ]
        return {"batchEvaluations": list(self._listing)}


class StubTraces:
    def __init__(self, groups=None):
        self.groups = groups if groups is not None else [
            "aws/spans", RUNTIME_GROUP
        ]

    def span_log_groups(self):
        return list(self.groups)

    @staticmethod
    def session_id_for(thread_id):
        return thread_id


def service(control=None, data=None, traces=None, **kwargs):
    svc = EvaluationService(region_name="us-east-1", now=lambda: 1786000000, **kwargs)
    svc._control = control or StubControl()
    svc._data = data or StubData()
    svc._traces = traces or StubTraces()
    return svc


def test_evaluators_are_listed_with_their_level_and_type():
    svc = service()
    result = svc.evaluators()

    ids = {e["evaluator_id"]: e for e in result}
    assert ids["Builtin.Correctness"]["level"] == "TRACE"
    assert ids["Builtin.GoalSuccessRate"]["level"] == "SESSION"
    assert ids["Builtin.SkillSelectionAccuracy"]["level"] == "TOOL_CALL"
    # Custom evaluators in a shared account belong to other teams but are real
    # and usable, so they are listed and marked rather than hidden.
    assert ids["search_relevance_v2-4X8s0c3H8T"]["builtin"] is False
    assert ids["Builtin.Correctness"]["builtin"] is True


def test_evaluators_are_cached():
    control = StubControl()
    svc = service(control=control)
    svc.evaluators()
    svc.evaluators()
    assert control.calls == 1


def test_evaluator_pagination_is_followed():
    control = StubControl(pages=[
        {"evaluators": [evaluator("Builtin.Correctness")], "nextToken": "more"},
        {"evaluators": [evaluator("Builtin.Coherence")]},
    ])
    svc = service(control=control)
    result = svc.evaluators()
    assert {e["evaluator_id"] for e in result} == {
        "Builtin.Correctness", "Builtin.Coherence"
    }
    assert control.calls == 2


def test_a_failed_listing_raises_and_is_not_cached():
    control = StubControl(error=RuntimeError("AccessDeniedException"))
    svc = service(control=control)
    for _ in range(2):
        with pytest.raises(EvaluationsUnavailable):
            svc.evaluators()
    assert control.calls == 2


def test_start_scopes_the_run_to_the_thread_session_and_runtime_groups():
    data = StubData()
    svc = service(data=data)
    svc.start([THREAD], ["Builtin.Correctness"], RUNTIME_ARN, agent_name="academic_writer",
              record_id="rec1")

    params = data.started[0]
    source = params["dataSourceConfig"]["cloudWatchLogs"]
    assert source["filterConfig"]["sessionIds"] == [THREAD]
    # `aws/spans` is not a per-runtime group and carries no service name, so it
    # is excluded: the API requires serviceNames to line up with logGroupNames.
    assert source["logGroupNames"] == [RUNTIME_GROUP]
    assert source["serviceNames"] == [SERVICE_NAME]
    assert params["evaluators"] == [{"evaluatorId": "Builtin.Correctness"}]
    assert params["tags"] == {
        "Platform": "bap", "AgentName": "academic_writer"
    }
    assert params["batchEvaluationName"].startswith(NAME_PREFIX)


def test_aws_spans_never_reaches_logGroupNames():
    """aws/spans holds spans without the correlated log events, so a batch
    aimed at it fails every session with LogEventMissingException: 'Session span
    data is incomplete. Span with ID … is missing a corresponding log event'
    — after AWS has charged for it.
    """
    data = StubData()
    svc = service(data=data)
    svc.start([THREAD], ["Builtin.Correctness"], RUNTIME_ARN)

    params = data.started[0]
    source = params["dataSourceConfig"]["cloudWatchLogs"]
    assert "aws/spans" not in source["logGroupNames"]


def test_start_refuses_an_empty_evaluator_list():
    """An empty run costs a request and returns nothing anyone asked for."""
    svc = service()
    with pytest.raises(ValueError):
        svc.start([THREAD], [], RUNTIME_ARN)


def test_start_refuses_more_evaluators_than_aws_accepts():
    """Measured 2026-08-19 against StartBatchEvaluation:

        ValidationException: Value at 'evaluators' failed to satisfy constraint:
        Member must have length less than or equal to 10

    The account offers **18** built-in evaluators, so "run every built-in one" is
    over the cap — which is exactly what the /insights panel asked for, and the
    ValidationException came back to the reader as "Evaluation service
    unavailable". Refused here so the caller is told the number is wrong instead
    of being told the service is down.
    """
    svc = service()
    with pytest.raises(ValueError) as excinfo:
        svc.start([THREAD], [f"Builtin.E{i}" for i in range(11)], RUNTIME_ARN)
    assert "10" in str(excinfo.value)


def test_start_accepts_the_cap_itself():
    data = StubData()
    svc = service(data=data)
    svc.start([THREAD], [f"Builtin.E{i}" for i in range(10)], RUNTIME_ARN)

    assert len(data.started[0]["evaluators"]) == 10


def test_start_refuses_empty_thread_list():
    """An empty thread list results in no sessions to evaluate."""
    svc = service()
    with pytest.raises(ValueError):
        svc.start([], ["Builtin.Correctness"], RUNTIME_ARN)


def test_start_refuses_when_no_runtime_span_group_exists():
    svc = service(traces=StubTraces(groups=["aws/spans"]))
    with pytest.raises(EvaluationsUnavailable):
        svc.start([THREAD], ["Builtin.Correctness"], RUNTIME_ARN)


def test_batch_name_is_slugged_and_bounded():
    """AgentCore rejects names outside its charset, and record ids are opaque."""
    name = batch_name("rec/with spaces!", 1786000000)
    assert name.startswith(NAME_PREFIX)
    # Measured regex: [a-zA-Z][a-zA-Z0-9_]{0,47}. A hyphen is rejected outright.
    assert name[0].isalpha()
    assert all(c.isalnum() or c == "_" for c in name)
    assert "-" not in name
    assert len(name) <= 48


def test_batch_name_preserves_epoch():
    """The epoch is never truncated, so two runs of the same record don't collide."""
    epoch = 1786000000
    name = batch_name("a" * 100, epoch)
    assert name.endswith(f"_{epoch}")


def test_status_reads_counts_from_get_never_from_list():
    """ListBatchEvaluations omits evaluationResults counts (fact 37)."""
    svc = service()
    result = svc.status("ap_rec1_1786000000-abc123")

    assert result["sessions"] == {
        "total": 28, "completed": 27, "failed": 1,
        "in_progress": 0, "ignored": 0,
    }
    assert result["status"] == "COMPLETED_WITH_ERRORS"
    assert result["terminal"] is True
    assert result["errors"] == ["1 of 28 sessions failed during batch evaluation."]


def test_status_keeps_each_score_with_its_evaluator():
    svc = service()
    scores = {s["evaluator_id"]: s for s in svc.status("x")["scores"]}

    assert scores["Builtin.Conciseness"]["average_score"] == 0.02
    assert scores["Builtin.ResponseRelevance"]["average_score"] == 0.98
    # No aggregate: averaging 0.02 with 0.98 would produce a number that means
    # nothing about either quality.
    assert "average_score" not in svc.status("x")


def test_a_running_batch_is_not_terminal():
    data = StubData(get={
        "batchEvaluationId": "b1",
        "batchEvaluationName": "ap_x_1",
        "status": "IN_PROGRESS",
        "createdAt": "2026-08-16T05:00:00+00:00",
        "evaluationResults": {"numberOfSessionsInProgress": 3,
                              "totalNumberOfSessions": 3},
    })
    result = service(data=data).status("b1")

    assert result["terminal"] is False
    assert result["sessions"]["in_progress"] == 3
    assert result["scores"] == []


def test_latest_for_ignores_other_peoples_batches():
    svc = service()
    latest = svc.latest_for("rec1")

    # `someone-elses-batch` is newer but is not ours.
    assert latest["batch_id"] == "ap_rec1_1786000000-abc123"


def test_latest_for_returns_none_when_the_record_has_never_been_evaluated():
    assert service().latest_for("rec-never") is None


def test_the_service_never_creates_an_online_evaluation_config():
    """Online configs sample and bill continuously, on a shared account where
    three of them already run daily. Batch on demand is the whole design."""
    import inspect
    from services import evaluation_service

    source = inspect.getsource(evaluation_service)
    for forbidden in (
        "create_online_evaluation_config",
        "update_online_evaluation_config",
        "delete_online_evaluation_config",
    ):
        assert forbidden not in source


def test_batch_name_is_ascii_only_even_for_a_korean_agent_name():
    """`str.isalnum()` accepts Unicode letters, so a Korean name would pass the
    sanitiser and be rejected by AWS with a ValidationException. This repository
    has already been bitten by non-ASCII boundaries in S3 metadata and
    Content-Disposition; the charset is spelled out rather than inferred."""
    import re

    for slug in ("논문작성_에이전트", "αβγδ", "日本語エージェント", "rec/with spaces!", "", "9lead"):
        name = batch_name(slug, 1786000000)
        assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", name), (slug, name)


# --- the AWS caps -----------------------------------------------------------
#
# Measured 2026-08-16 by sending the 42 per-runtime groups the first version of
# `start` built:
#   ValidationException: 2 validation errors detected:
#     logGroupNames … Member must have length less than or equal to 5;
#     serviceNames … Member must have length less than or equal to 1
# That version passed every unit test and would have thrown on every real call.


def test_the_data_source_carries_one_group_and_one_service_name():
    """`serviceNames` is capped at 1, so a batch covers exactly one agent."""
    data = StubData()
    # A whole account's worth of groups is available; only this agent's is used.
    noisy = StubTraces(groups=[
        "aws/spans",
        RUNTIME_GROUP,
        "/aws/bedrock-agentcore/runtimes/cardnews_agent-JkLmN77889-DEFAULT",
        "/aws/bedrock-agentcore/runtimes/ks_sap_agent-g9WUnMDl9r-DEFAULT",
        "/aws/bedrock-agentcore/runtimes/ecommerce_analytics-Do2AbeGoo6-DEFAULT",
        "/aws/bedrock-agentcore/runtimes/strands_agent-DeFgH56473-DEFAULT",
        "/aws/bedrock-agentcore/runtimes/bap_default-FgHiJ67890-DEFAULT",
    ])
    svc = service(data=data, traces=noisy)
    svc.start([THREAD], ["Builtin.Correctness"], RUNTIME_ARN)

    source = data.started[0]["dataSourceConfig"]["cloudWatchLogs"]
    assert source["logGroupNames"] == [RUNTIME_GROUP]
    assert source["serviceNames"] == [SERVICE_NAME]
    assert len(source["logGroupNames"]) <= 5
    assert len(source["serviceNames"]) <= 1


def test_a_missing_runtime_arn_is_refused_before_anything_is_charged():
    data = StubData()
    svc = service(data=data)
    for bad in ("", None):
        try:
            svc.start([THREAD], ["Builtin.Correctness"], bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")
    assert data.started == []


def test_an_agent_with_no_span_log_group_fails_with_a_sentence_not_a_paid_no_op():
    """60 of the 62 batches already in this account completed with zero sessions.
    A run that cannot find its group must say so instead of joining them."""
    data = StubData()
    svc = service(data=data, traces=StubTraces(groups=["aws/spans"]))
    try:
        svc.start([THREAD], ["Builtin.Correctness"], RUNTIME_ARN)
    except EvaluationsUnavailable as exc:
        assert RUNTIME_GROUP in str(exc)
        assert "Transaction Search" in str(exc)
    else:
        raise AssertionError("expected EvaluationsUnavailable")
    assert data.started == []


def test_log_group_for_derives_the_group_from_a_runtime_arn():
    from services.evaluation_service import log_group_for

    assert log_group_for(RUNTIME_ARN) == RUNTIME_GROUP
    assert log_group_for(
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/bap_default-FgHiJ67890"
    ) == "/aws/bedrock-agentcore/runtimes/bap_default-FgHiJ67890-DEFAULT"


def test_latest_for_follows_pagination_with_the_camelcase_token():
    """The bedrock-agentcore data plane is camelCase; botocore rejects `NextToken`.

    Shipped as `params["NextToken"] = token` and survived every test, because the
    stub swallowed `**params` and the first page was the only page. It failed on
    the first real call against an account holding more than one page of batches.
    """
    data = StubData(list_pages=[
        {
            "batchEvaluations": [
                {"batchEvaluationId": "someone-elses",
                 "batchEvaluationName": "insightsquickstart17828071_daily_20260717T000000",
                 "status": "COMPLETED",
                 "createdAt": "2026-08-17T05:00:00+00:00"},
            ],
            "nextToken": "page2",
        },
        {
            "batchEvaluations": [
                {"batchEvaluationId": "ap_rec1_1786000000-abc123",
                 "batchEvaluationName": "ap_rec1_1786000000",
                 "status": "COMPLETED_WITH_ERRORS",
                 "createdAt": "2026-08-16T05:00:00+00:00"},
            ],
        },
    ])
    svc = service(data=data)
    latest = svc.latest_for("rec1")

    assert len(data.list_calls) == 2
    assert data.list_calls[1]["nextToken"] == "page2"
    # Ours, found on the second page, and the newer foreign batch ignored.
    assert latest["batch_id"] == "ap_rec1_1786000000-abc123"


def test_evaluator_pagination_also_uses_the_camelcase_token():
    control = StubControl(pages=[
        {"evaluators": [evaluator("Builtin.Correctness")], "nextToken": "more"},
        {"evaluators": [evaluator("Builtin.Coherence")]},
    ])
    svc = service(control=control)
    result = svc.evaluators()

    assert {e["evaluator_id"] for e in result} == {
        "Builtin.Correctness", "Builtin.Coherence"
    }


# --- AgentCore insights (triage), on the same batch machinery --------------------
#
# `StartBatchEvaluation` takes `insights` *instead of* `evaluators` — the two are
# mutually exclusive — and `GetBatchEvaluation` answers with three clustered result
# trees rather than scores. Everything else (data source, one agent per batch,
# name charset) is identical, so the service reuses the evaluation path and the
# tests below pin only what differs.

from services.evaluation_service import INSIGHT_IDS, INSIGHT_PREFIX  # noqa: E402

INSIGHTS_GET = {
    "batchEvaluationId": "apin_rec1_1786000000-abc123",
    "batchEvaluationName": "apin_rec1_1786000000",
    "status": "COMPLETED",
    "createdAt": "2026-09-23T05:00:00+00:00",
    "insights": [
        {"insightId": "Builtin.Insight.FailureAnalysis"},
        {"insightId": "Builtin.Insight.UserIntent"},
    ],
    "evaluationResults": {
        "numberOfSessionsCompleted": 12,
        "numberOfSessionsInProgress": 0,
        "numberOfSessionsFailed": 0,
        "numberOfSessionsIgnored": 2,
        "totalNumberOfSessions": 14,
        "evaluatorSummaries": [],
    },
    "failureAnalysisResult": {"failures": [
        {"clusterId": 1, "name": "Execution errors", "description": "Tool calls failed.",
         "affectedSessionCount": 3,
         "subCategories": [
             {"clusterId": 11, "name": "Rate limiting", "description": "429s from the model.",
              "affectedSessionCount": 3,
              "rootCauses": [
                  {"clusterId": 111, "name": "No retry on throttle",
                   "rootCause": "The agent gives up on the first ThrottlingException.",
                   "recommendation": "Add exponential backoff.",
                   "affectedSessionCount": 3,
                   "affectedSessions": [
                       {"sessionId": THREAD, "explanation": "Throttled twice.",
                        "fixType": "CODE", "recommendation": "Retry.",
                        "failureSpans": [{"spanId": "s1"}]},
                   ]},
              ]},
         ]},
    ]},
    "userIntentResult": {"userIntents": [
        {"clusterId": 1, "name": "Summarise a paper", "description": "Users paste a PDF.",
         "affectedSessionCount": 9,
         "affectedSessions": [{"sessionId": THREAD, "userMessages": ["요약해줘"]}]},
    ]},
    "errorDetails": [],
}


def test_start_insights_sends_insights_and_never_evaluators():
    data = StubData()
    svc = service(data=data)
    svc.start_insights([THREAD], RUNTIME_ARN, agent_name="academic_writer",
                       record_id="rec1")

    params = data.started[0]
    # Same data source as an evaluation: one agent's group, one service name, the
    # thread sessions.
    source = params["dataSourceConfig"]["cloudWatchLogs"]
    assert source["logGroupNames"] == [RUNTIME_GROUP]
    assert source["serviceNames"] == [SERVICE_NAME]
    assert source["filterConfig"]["sessionIds"] == [THREAD]
    # `insights` and `evaluators` are mutually exclusive on the API; sending both
    # is a ValidationException after nothing has run.
    assert params["insights"] == [{"insightId": iid} for iid in INSIGHT_IDS]
    assert "evaluators" not in params
    assert params["tags"] == {"Platform": "bap", "AgentName": "academic_writer"}


def test_insights_runs_carry_their_own_prefix_so_the_two_latest_lookups_stay_apart():
    """An insights run over `rec1` must not become `rec1`'s "latest evaluation":
    it has no scores, so the quality panel would draw an empty run over a real one.
    The two kinds of run are told apart by name prefix, the only field that
    ListBatchEvaluations returns for both."""
    data = StubData()
    svc = service(data=data)
    svc.start_insights([THREAD], RUNTIME_ARN, record_id="rec1")

    name = data.started[0]["batchEvaluationName"]
    assert name.startswith(INSIGHT_PREFIX)
    assert not name.startswith(f"{NAME_PREFIX}rec1_")


def test_latest_insights_for_picks_the_insights_run_and_latest_for_ignores_it():
    listing = [
        {"batchEvaluationId": "apin_rec1_1786000001-i1",
         "batchEvaluationName": "apin_rec1_1786000001",
         "status": "COMPLETED", "createdAt": "2026-09-23T05:00:00+00:00"},
        {"batchEvaluationId": "ap_rec1_1786000000-abc123",
         "batchEvaluationName": "ap_rec1_1786000000",
         "status": "COMPLETED", "createdAt": "2026-08-16T05:00:00+00:00"},
    ]
    data = StubData(listing=listing, get=INSIGHTS_GET)
    svc = service(data=data)

    # The stub answers every Get with the same body, so the id each lookup asked
    # for is the assertion, not the body it got back.
    svc.latest_for("rec1")
    assert data.get_ids[-1] == "ap_rec1_1786000000-abc123"
    svc.latest_insights_for("rec1")
    assert data.get_ids[-1] == "apin_rec1_1786000001-i1"


def test_latest_insights_for_returns_none_when_never_analysed():
    assert service().latest_insights_for("rec-never") is None


def test_start_insights_refuses_an_insight_id_aws_does_not_know():
    """A typo would be a ValidationException wrapped as 'unavailable' — the same
    misdirection the evaluator cap caused. Refused here, for free."""
    svc = service()
    with pytest.raises(ValueError):
        svc.start_insights([THREAD], RUNTIME_ARN, insight_ids=["Builtin.Insight.Nope"])
    with pytest.raises(ValueError):
        svc.start_insights([THREAD], RUNTIME_ARN, insight_ids=[])


def test_status_normalises_the_failure_tree_with_counts_at_every_level():
    svc = service(data=StubData(get=INSIGHTS_GET))
    result = svc.status("apin_rec1_1786000000-abc123")

    assert result["scores"] == []
    assert result["sessions"] == {
        "total": 14, "completed": 12, "failed": 0, "in_progress": 0, "ignored": 2,
    }
    insights = result["insights"]
    assert insights["requested"] == [
        "Builtin.Insight.FailureAnalysis", "Builtin.Insight.UserIntent",
    ]
    category = insights["failures"][0]
    assert category["name"] == "Execution errors"
    assert category["affected_session_count"] == 3
    sub = category["sub_categories"][0]
    assert sub["name"] == "Rate limiting"
    root = sub["root_causes"][0]
    assert root["root_cause"].startswith("The agent gives up")
    assert root["recommendation"] == "Add exponential backoff."
    assert root["affected_session_count"] == 3
    assert root["sessions"] == [{
        "session_id": THREAD,
        "explanation": "Throttled twice.",
        "fix_type": "CODE",
        "recommendation": "Retry.",
    }]
    intent = insights["user_intents"][0]
    assert intent["affected_session_count"] == 9
    assert intent["sessions"] == [{"session_id": THREAD, "user_messages": ["요약해줘"]}]
    # Not requested, so absent as an empty list rather than a missing key: the
    # client reads three lists, not three optionals.
    assert insights["execution_summaries"] == []


def test_an_evaluation_run_reports_no_insights():
    """The quality panel and the triage panel read the same shape; `insights`
    is None on a scored run so neither mistakes the other's run for its own."""
    result = service().status("ap_rec1_1786000000-abc123")
    assert result["insights"] is None
