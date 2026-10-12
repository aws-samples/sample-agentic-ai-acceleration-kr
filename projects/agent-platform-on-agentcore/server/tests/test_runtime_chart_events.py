"""A chart the runtime produced must survive the trip to the UI.

`ks_text2sql_agent` renders charts itself (matplotlib inside its container, which
is where the Korean font lives) and emits, after the answer text:

    {"type": "chart", "url": "https://…s3…/step-1.png?…", "source": "render_chart",
     "spec": {"kind": "bar", "data": [...], "encoding": {...}, "title": "…"}}
    {"type": "verification", "result": {"method": "execution_consensus", …}}

Neither had a branch in `_process_sse_stream`, so both fell through to the `else`
and were dropped — verbatim frames captured from the deployed runtime confirm it.

The chart still *appeared*, because the orchestrator also writes the URL into the
answer as a markdown image and MarkdownContent has no `img` override. That is not
a substitute: the URL is presigned for 300s (measured — `Expires` five minutes
after `LastModified`), while the object itself lives eight days under the
`expire-charts` lifecycle rule. So reopening the thread later shows a broken
image, with the chart data sitting in an event nobody read.

Forwarding `spec` is what fixes that: it has no expiry, and the UI can redraw it on
every render. `url` rides along as a fallback.

An `s3://` URI does not: `ask_code_specialist` stashes `{"s3_uri": …, "spec": None}`
for a sandbox-rendered PNG, and that is neither a spec nor a URL a browser can load,
with no presign route here to turn it into one. Such a frame is dropped rather than
forwarded — the alternative is a chart event that renders nothing and a UI branch
that can never be satisfied. Sandbox charts (boxplots, dual-axis, anything outside
the runtime's six kinds) therefore do not appear; making them appear means adding a
presign route.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agentcore_client import AgentCoreClient  # noqa: E402


class FakeStream:
    def __init__(self, frames):
        self._frames = frames

    def iter_lines(self, chunk_size=1):
        for frame in self._frames:
            yield f"data: {frame}".encode("utf-8")


def _collect(frames):
    client = AgentCoreClient.__new__(AgentCoreClient)

    async def run():
        return [e async for e in client._process_sse_stream(FakeStream(frames))]

    return asyncio.run(run())


def _inner(events, key):
    return [e["event"][key] for e in events if key in e.get("event", {})]


# Trimmed from a real turn: "금형비를 가장 많이 받은 업체 3곳을 차트로 보여줘".
CHART_SPEC = {
    "kind": "bar",
    "data": [
        {"업체": "주식회사대림테크", "금형비합계": 3890173100},
        {"업체": "세원플러스㈜", "금형비합계": 3317537739},
    ],
    "encoding": {"x": "업체", "y": "금형비합계", "color": None},
    "title": "금형비 지급액 상위 3개 업체",
}
CHART_URL = "https://ks-text2sql-staging.s3.amazonaws.com/charts/s/step-1.png?Expires=1"

def _chart_frame(**overrides):
    frame = {
        "type": "chart",
        "url": CHART_URL,
        "source": "render_chart",
        "spec": CHART_SPEC,
    }
    frame.update(overrides)
    return json.dumps(frame, ensure_ascii=False)


def _turn(*extra):
    return [
        '{"type": "text", "chunk": "상위 3개 업체입니다."}',
        *extra,
        '{"type": "done", "session_id": "s-1"}',
    ]


def test_a_chart_reaches_the_ui():
    """The frame must become an event, not be swallowed by the else branch."""
    events = _collect(_turn(_chart_frame()))

    charts = _inner(events, "chart")
    assert len(charts) == 1, "the chart frame was dropped"


def test_the_chart_carries_its_spec():
    """The spec is the part that does not expire, so it must survive intact.

    Redrawing from the spec is what keeps a reopened thread from showing a broken
    image five minutes on.
    """
    events = _collect(_turn(_chart_frame()))

    chart = _inner(events, "chart")[0]
    assert chart["spec"] == CHART_SPEC
    # Korean labels are the whole dataset here — a mangled encoding would be
    # invisible in a smoke test but obvious to a user.
    assert chart["spec"]["data"][0]["업체"] == "주식회사대림테크"


def test_the_chart_keeps_its_image_url_too():
    """`url` is the only handle for the sandbox path, which has no spec."""
    events = _collect(_turn(_chart_frame()))

    assert _inner(events, "chart")[0]["url"] == CHART_URL


def test_a_chart_that_is_only_an_s3_uri_is_dropped():
    """`ask_code_specialist` stashes {"s3_uri": …, "spec": None} and nothing else.

    An `s3://` URI is not a URL a browser can load, and this server has no presign
    route for the runtime's staging bucket, so forwarding it would put a chart event
    on screen that renders nothing at all — worse than not sending it, because the
    UI would have to carry a branch for a case it can never satisfy.

    So this frame is dropped, deliberately: the sandbox path (arbitrary charts —
    boxplots, dual-axis) is not renderable here today. Should it need to be, the fix
    is a presign route, not passing the raw URI through.
    """
    events = _collect(
        _turn(
            _chart_frame(
                spec=None,
                url=None,
                s3_uri="s3://ks-text2sql-staging/charts/s/plot.png",
                source="code_interpreter",
            )
        )
    )

    assert _inner(events, "chart") == []


def test_a_chart_with_a_spec_is_kept_even_if_it_also_has_an_s3_uri():
    """The spec is what renders; an accompanying s3 uri must not disqualify it."""
    events = _collect(
        _turn(
            _chart_frame(
                url=None, s3_uri="s3://ks-text2sql-staging/charts/s/plot.png"
            )
        )
    )

    charts = _inner(events, "chart")
    assert len(charts) == 1
    assert charts[0]["spec"] == CHART_SPEC
    # Not forwarded: the UI has no way to load it, so carrying it would only invite
    # a consumer to try.
    assert "s3Uri" not in charts[0]


def test_a_chart_does_not_open_an_assistant_message():
    """A chart is not message content.

    It arrives after the text, so `started` is normally already set; but a chart
    on its own must not fabricate an empty bubble, the same rule status frames
    follow.
    """
    events = _collect([_chart_frame(), '{"type": "done", "session_id": "s-2"}'])

    assert _inner(events, "chart") != []
    assert _inner(events, "messageStart") == []
    assert _inner(events, "messageStop") == []


def test_a_chart_does_not_land_in_the_answer_text():
    """The spec must not be concatenated into the prose the message persists."""
    events = _collect(_turn(_chart_frame()))

    stop = _inner(events, "messageStop")[0]
    assert stop["fullText"] == "상위 3개 업체입니다."


def test_an_empty_chart_frame_is_dropped():
    """Nothing to draw and nothing to link: rendering it would be a blank card."""
    events = _collect(_turn(_chart_frame(spec=None, url=None)))

    assert _inner(events, "chart") == []


def test_a_verification_result_reaches_the_ui():
    """How the number was checked is what makes it trustworthy.

    ask_sql_verified runs k candidate queries and adopts the majority result; the
    consensus meta is the evidence for that, and it was being dropped.
    """
    frame = json.dumps(
        {
            "type": "verification",
            "result": {
                "method": "execution_consensus",
                "k": 3,
                "n_valid": 3,
                "agreement": 0.667,
                "verdict": "PASS",
            },
        }
    )
    events = _collect(_turn(frame))

    results = _inner(events, "verification")
    assert len(results) == 1
    assert results[0]["verdict"] == "PASS"
    assert results[0]["agreement"] == 0.667


def test_a_verification_without_a_verdict_is_dropped():
    """A card with no verdict says nothing; it would be noise in the transcript."""
    events = _collect(_turn('{"type": "verification", "result": {}}'))

    assert _inner(events, "verification") == []


def test_the_existing_typed_turn_is_unaffected():
    """Text, tools, status and framing keep working around the new branches."""
    events = _collect(
        [
            '{"type": "thinking", "text": "분석 중…"}',
            '{"type": "tool_call", "tool": "ask_sql_verified", "args": {}}',
            '{"type": "tool_result", "tool": "ask_sql_verified", "result": "3 rows"}',
            '{"type": "text", "chunk": "설비 3건입니다."}',
            _chart_frame(),
            '{"type": "done", "session_id": "s-3"}',
        ]
    )

    assert len(_inner(events, "messageStart")) == 1
    assert _inner(events, "messageStop")[0]["fullText"] == "설비 3건입니다."
    assert len(_inner(events, "toolResult")) == 1
    assert len(_inner(events, "agentStatus")) == 1
    assert len(_inner(events, "chart")) == 1


# A chart has two viable destinations, and which one a runtime picks is its choice:
#
#   `chart`   — a spec the UI redraws with recharts, inside the message. Interactive
#               and theme-aware, but only covers the runtime's six kinds.
#   `artifact` — a rendered SVG in the side panel. Static, but covers the forms that
#               have no spec at all (boxplot, heatmap, waterfall, dual-axis).
#
# So the server must not care. It already does not: `{"event": {...}}` frames are
# passed through untouched, ahead of the `type` dispatch, and the artifact pipeline
# picks them up from there. These tests pin that down, because "it happens to work"
# and "it is guaranteed to work" are different things — a future change to the typed
# branches could quietly break the passthrough.
def test_a_runtime_may_send_both_chart_shapes_in_one_turn():
    """Six-kind charts as specs, arbitrary ones as artifacts — together."""
    events = _collect(
        [
            '{"type": "text", "chunk": "두 차트입니다."}',
            '{"event": {"artifact": {"artifactId": "chart-sandbox-1", "kind": "svg",'
            ' "title": "분포", "content": "<svg/>"}}}',
            _chart_frame(),
            '{"type": "done", "session_id": "s-6"}',
        ]
    )

    assert len(_inner(events, "artifact")) == 1
    assert len(_inner(events, "chart")) == 1


def test_an_artifact_frame_survives_untouched():
    """The artifact pipeline reads these keys, so none may be renamed or dropped."""
    events = _collect(
        [
            '{"event": {"artifact": {"artifactId": "chart-sandbox-1", "kind": "svg",'
            ' "title": "분석 차트", "content": "<svg viewBox=\\"0 0 1 1\\"/>"}}}',
            '{"type": "done", "session_id": "s-7"}',
        ]
    )

    artifact = _inner(events, "artifact")[0]
    assert artifact == {
        "artifactId": "chart-sandbox-1",
        "kind": "svg",
        "title": "분석 차트",
        "content": '<svg viewBox="0 0 1 1"/>',
    }


def test_an_artifact_alone_does_not_open_a_message():
    """Consistent with charts and status: an artifact is not message content.

    It is also why a turn that only produced an artifact still gets no empty
    bubble — the artifact carries its own `messageId`, filled in server-side.
    """
    events = _collect(
        [
            '{"event": {"artifact": {"artifactId": "a-1", "kind": "svg",'
            ' "title": "t", "content": "<svg/>"}}}',
            '{"type": "done", "session_id": "s-8"}',
        ]
    )

    assert len(_inner(events, "artifact")) == 1
    assert _inner(events, "messageStart") == []
    assert _inner(events, "messageStop") == []
