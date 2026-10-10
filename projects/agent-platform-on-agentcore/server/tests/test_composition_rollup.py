"""Reuse figures are rolled up, never stored.

One skill attached to three agents must report the sum of those three agents'
traffic — and must say "reach", because no telemetry anywhere counts a skill
being read. Nothing here writes: if a harness's composition changes, the same
history re-aggregates under the new composition on the next read.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.usage_service import UsageService  # noqa: E402

from services.registry_service import SKILL_SOURCE_META_KEY  # noqa: E402

SKILL_URI = "s3://bucket/skills/citation-style/"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/company-handbook-abc"
MCP_URL = "https://mcp.example.com/mcp"


def skill_descriptor(uri):
    """The real shape `skill_source_of` reads — nested under `_meta`.

    Not `{"skillSource": {...}}`: the registry stores the pointer at
    `skillDefinition._meta["com.amazonaws.bap/skillSource"]`, and a
    stub with the wrong nesting makes this test pass against code that would
    find nothing in production. That exact class of miss is why the spec says
    boto3-shaped stubs missed the registry update descriptor's nesting in all
    four record types.
    """
    return {"skillDefinition": {"_meta": {SKILL_SOURCE_META_KEY: {"uri": uri}}}}


class Harness:
    def __init__(self, harness_id, harness_arn, tools=None, skills=None):
        self.harness_id = harness_id
        self.harness_arn = harness_arn
        self.harness_name = harness_id
        self.model_id = "anthropic.claude-sonnet-5"
        self.tools = tools or []
        self.skills = skills or []


class StubHarness:
    def __init__(self, harnesses, targets_by_arn=None, endpoints_by_arn=None):
        self._harnesses = harnesses
        self._targets = targets_by_arn or {}
        self._endpoints = endpoints_by_arn or {}

    def list_harnesses(self, with_tools=False):
        return self._harnesses

    def gateway_target_names(self, gateway_arn):
        return self._targets.get(gateway_arn, [])

    def gateway_target_endpoints(self, gateway_arn):
        return self._endpoints.get(gateway_arn, {})


class StubRecord:
    def __init__(self, record_id, name, harness_arn=None, descriptor_type="A2A"):
        self.record_id = record_id
        self.name = name
        self.harness_arn = harness_arn
        self.agent_runtime_arn = None
        self.descriptor_type = descriptor_type


class StubRegistry:
    def __init__(self, records, contents=None):
        self._records = records
        self._contents = contents or {}

    def agent_records(self):
        return [r for r in self._records if r.descriptor_type == "A2A"]

    def list_records(self, descriptor_type=None, **kwargs):
        return [
            r for r in self._records
            if descriptor_type is None or r.descriptor_type == descriptor_type
        ]

    def get_record(self, record_id):
        record = next(r for r in self._records if r.record_id == record_id)
        record.descriptor_content = self._contents.get(record_id)
        return record


class StubRepo:
    def __init__(self, items_by_pk):
        self.items_by_pk = items_by_pk

    def query(self, pk, start_date, end_date):
        return self.items_by_pk.get(pk, [])


def turns(record_id, count):
    return {"sk": f"D#2026-08-15#A#{record_id}", "turns": Decimal(count),
            "input_tokens": Decimal(count * 100)}


def three_agents_sharing_one_skill():
    arns = {rid: f"arn:aws:bedrock-agentcore:us-east-1:1:harness/{rid}"
            for rid in ("rec-1", "rec-2", "rec-3")}
    harnesses = [
        Harness(rid, arn, skills=[{"s3": {"uri": SKILL_URI}}])
        for rid, arn in arns.items()
    ]
    records = [
        StubRecord(rid, f"agent {rid}", harness_arn=arns[rid]) for rid in arns
    ] + [
        StubRecord("skill-1", "Citation style", descriptor_type="AGENT_SKILLS"),
    ]
    contents = {"skill-1": skill_descriptor(SKILL_URI)}
    repo = StubRepo({
        "AGENTS#2026-08": [turns("rec-1", 2), turns("rec-2", 3), turns("rec-3", 5)],
    })
    return UsageService(
        repository=repo,
        registry=StubRegistry(records, contents),
        harness=StubHarness(harnesses),
    )


def test_one_skill_across_three_agents_sums_their_traffic():
    composition = three_agents_sharing_one_skill().composition(
        "2026-08-15", "2026-08-15"
    )

    skills = {entry["name"]: entry for entry in composition["skills"]}
    assert skills["Citation style"]["turns"] == 10
    assert skills["Citation style"]["agents"] == 3


def test_a_skill_figure_is_labelled_reach_not_calls():
    """No telemetry counts a skill being read. Saying "calls" would be a lie."""
    composition = three_agents_sharing_one_skill().composition(
        "2026-08-15", "2026-08-15"
    )

    assert all(entry["metric"] == "reach" for entry in composition["skills"])


def test_an_mcp_server_is_matched_by_its_endpoint_url():
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    service = UsageService(
        repository=StubRepo({"AGENTS#2026-08": [turns("rec-1", 4)]}),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer", harness_arn=arn),
                StubRecord("mcp-1", "example/tools", descriptor_type="MCP"),
            ],
            {"mcp-1": {"server": {"remotes": [{"url": MCP_URL}]}}},
        ),
        harness=StubHarness([
            Harness("rec-1", arn, tools=[
                {"type": "remote_mcp", "name": "tools",
                 "config": {"remoteMcp": {"url": MCP_URL}}},
            ])
        ]),
    )

    mcp = service.composition("2026-08-15", "2026-08-15")["mcp"]

    assert [(entry["name"], entry["turns"]) for entry in mcp] == [
        ("example/tools", 4)
    ]


def test_a_knowledge_base_is_matched_by_its_gateway_arn():
    """A knowledge base attaches to a harness as a gateway tool — the same
    shape knowledge_service._gateway_arns_of already reads."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    service = UsageService(
        repository=StubRepo({"AGENTS#2026-08": [turns("rec-1", 6)]}),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer", harness_arn=arn),
                StubRecord("kb-1", "Company handbook", descriptor_type="MCP"),
            ],
            {"kb-1": {"server": {"gatewayArn": GATEWAY_ARN}}},
        ),
        harness=StubHarness([
            Harness("rec-1", arn, tools=[
                {"type": "agentcore_gateway", "name": "handbook",
                 "config": {"agentCoreGateway": {"gatewayArn": GATEWAY_ARN}}},
            ])
        ]),
    )

    entries = service.composition("2026-08-15", "2026-08-15")
    names = [e["name"] for e in entries["mcp"] + entries["knowledge_bases"]]

    assert "Company handbook" in names


def test_tools_are_reported_by_real_call_count():
    """Unlike skills, tool calls are counted by the stream, so this figure is
    exact and labelled as such."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [turns("rec-1", 4)],
            "AGENT#rec-1#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#web_search", "tool_calls": Decimal(3)},
            ],
        }),
        registry=StubRegistry([StubRecord("rec-1", "writer", harness_arn=arn)]),
        harness=StubHarness([Harness("rec-1", arn)]),
    )

    tools = service.composition("2026-08-15", "2026-08-15")["tools"]

    assert tools == [{"name": "web_search", "tool_calls": 3, "metric": "calls"}]


# Two targets on one gateway, matching the live shape: a gateway serves each
# target's tools as `<target>___<tool>`, and ListGatewayTargets names the targets.
GATEWAY_TARGETS = {GATEWAY_ARN: ["platform-tools", "web-search"]}


def test_a_gateways_tool_calls_roll_up_by_target_across_all_agents():
    """A gateway is called *through*, so `record_turn` books its tool calls under
    the calling agents. `mcp_gateway_usage` reads them back by target prefix — no
    harness config involved — even though the agent attaches a second gateway and
    also calls a bare built-in tool. It counts the calling agents and their turns."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    arn2 = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-2"
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [turns("rec-1", 4), turns("rec-2", 6)],
            "AGENT#rec-1#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#platform-tools___current_time", "tool_calls": Decimal(3)},
                {"sk": "D#2026-08-15#T#web-search___WebSearch", "tool_calls": Decimal(2)},
                # A different gateway's target — must not be counted here.
                {"sk": "D#2026-08-15#T#other-gw___thing", "tool_calls": Decimal(9)},
                # A bare built-in tool — no target, so never a gateway's.
                {"sk": "D#2026-08-15#T#calculator", "tool_calls": Decimal(5)},
            ],
            "AGENT#rec-2#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#platform-tools___current_time", "tool_calls": Decimal(1)},
            ],
        }),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer", harness_arn=arn),
                StubRecord("rec-2", "editor", harness_arn=arn2),
                StubRecord("gw-1", "ks-agent-platform-gateway", descriptor_type="MCP"),
            ],
            {"gw-1": {"server": {"gatewayArn": GATEWAY_ARN}}},
        ),
        harness=StubHarness([], targets_by_arn=GATEWAY_TARGETS),
    )

    usage = service.mcp_gateway_usage("gw-1", "2026-08-15", "2026-08-15")

    assert usage["tools"] == {"current_time": 4, "WebSearch": 2}
    assert usage["agents"] == 2
    assert usage["turns"] == 10


def test_gateway_usage_is_none_for_an_agent_record():
    """None, not empty: an agent record shows its own counters, so the caller must
    tell 'not a gateway' apart from 'a gateway with no traffic'."""
    service = UsageService(
        repository=StubRepo({}),
        registry=StubRegistry([StubRecord("rec-1", "writer")]),
        harness=StubHarness([]),
    )

    assert service.mcp_gateway_usage("rec-1", "2026-08-15", "2026-08-15") is None


def test_gateway_usage_is_none_for_a_plain_remote_mcp_server():
    """A bare-name MCP server carries no target prefix, so its calls cannot be told
    apart from built-ins — `None`, and the record shows its own (empty) counters."""
    service = UsageService(
        repository=StubRepo({}),
        registry=StubRegistry(
            [StubRecord("mcp-1", "example/tools", descriptor_type="MCP")],
            {"mcp-1": {"server": {"remotes": [{"url": MCP_URL}]}}},
        ),
        harness=StubHarness([]),
    )

    assert service.mcp_gateway_usage("mcp-1", "2026-08-15", "2026-08-15") is None


def test_gateway_usage_is_empty_when_targets_cannot_be_listed():
    """If ListGatewayTargets fails there is no authority for what belongs to the
    gateway, so nothing is attributed rather than guessed."""
    class NoTargets:
        def gateway_target_names(self, gateway_arn):
            raise RuntimeError("throttled")

    service = UsageService(
        repository=StubRepo({
            "AGENT#rec-1#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#platform-tools___current_time", "tool_calls": Decimal(3)},
            ],
        }),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer",
                           harness_arn="arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"),
                StubRecord("gw-1", "gateway", descriptor_type="MCP"),
            ],
            {"gw-1": {"server": {"gatewayArn": GATEWAY_ARN}}},
        ),
        harness=NoTargets(),
    )

    assert service.mcp_gateway_usage("gw-1", "2026-08-15", "2026-08-15") == {
        "agents": 0, "turns": 0, "tools": {},
    }


def test_a_harness_failure_yields_empty_sections_not_an_exception():
    class Exploding:
        def list_harnesses(self, with_tools=False):
            raise RuntimeError("throttled")

    service = UsageService(
        repository=StubRepo({}),
        registry=StubRegistry([]),
        harness=Exploding(),
    )

    composition = service.composition("2026-08-15", "2026-08-15")

    assert composition["skills"] == []
    assert composition["mcp"] == []


# --- record_reach: the Usage tab's one entry point for every record kind -------
#
# `record_insights` used to know one non-agent shape (a gateway) and fell back to
# the record's own counters for everything else. A skill's own counters are zero
# by construction — nothing records a turn under a skill id — so every skill's
# Usage tab read 0/0/0 while the composition page, reading the same ledger through
# the harnesses, showed its reach. These pin the per-kind answers.


def test_record_reach_is_none_for_an_agent_record():
    """An agent has counters of its own; the tab must show those, not a rollup."""
    service = three_agents_sharing_one_skill()

    assert service.record_reach("rec-1", "2026-08-15", "2026-08-15") is None


def test_a_skills_reach_is_the_traffic_of_the_agents_that_attach_it():
    """The same answer the composition page gives, with the agents named so the
    tab can send the reader to them — evaluation lives on the agent, not here."""
    reach = three_agents_sharing_one_skill().record_reach(
        "skill-1", "2026-08-15", "2026-08-15"
    )

    assert reach["metric"] == "reach"
    assert reach["agents"] == 3
    assert reach["turns"] == 10
    assert reach["tools"] == {}
    assert [a["record_id"] for a in reach["agent_records"]] == ["rec-3", "rec-2", "rec-1"]
    assert reach["agent_records"][0] == {"record_id": "rec-3", "name": "agent rec-3", "turns": 5}


def test_a_skill_nobody_attaches_has_zero_reach_not_none():
    """Zero is an answer ("approved, unused"); None would make the tab draw an
    agent's empty counters and trend chart instead."""
    service = UsageService(
        repository=StubRepo({}),
        registry=StubRegistry(
            [StubRecord("skill-1", "Citation style", descriptor_type="AGENT_SKILLS")],
            {"skill-1": skill_descriptor(SKILL_URI)},
        ),
        harness=StubHarness([]),
    )

    assert service.record_reach("skill-1", "2026-08-15", "2026-08-15") == {
        "metric": "reach", "agents": 0, "turns": 0, "tools": {}, "agent_records": [],
        "daily": [{"date": "2026-08-15", "turns": 0, "tool_calls": 0}],
    }


def test_a_gateway_record_reach_names_the_calling_agents():
    """Same figures as `mcp_gateway_usage`, plus who called — the gateway's tab
    links evaluation to those agents too."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [turns("rec-1", 4), turns("rec-2", 6)],
            "AGENT#rec-1#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#platform-tools___current_time", "tool_calls": Decimal(3)},
            ],
        }),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer", harness_arn=arn),
                StubRecord("rec-2", "editor"),
                StubRecord("gw-1", "gateway", descriptor_type="MCP"),
            ],
            {"gw-1": {"server": {"gatewayArn": GATEWAY_ARN}}},
        ),
        harness=StubHarness([], targets_by_arn=GATEWAY_TARGETS),
    )

    reach = service.record_reach("gw-1", "2026-08-15", "2026-08-15")

    assert reach["metric"] == "traffic"
    assert reach["tools"] == {"current_time": 3}
    assert reach["agents"] == 1 and reach["turns"] == 4
    assert reach["agent_records"] == [{"record_id": "rec-1", "name": "writer", "turns": 4}]


def test_a_runtime_hosted_mcp_server_is_credited_through_the_gateway_target_that_fronts_it():
    """Live shape: the record describes a Runtime invocations URL, but no agent
    attaches that URL — they call it through a gateway target whose
    `mcpServer.endpoint` is that same URL (platform-status on bap-gateway). The
    67 calls that went that way were credited to the gateway and the record
    read 0. The target's endpoint is the link, so the calls land here too."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    runtime_url = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3A...%2Fstatus-abc/invocations?qualifier=DEFAULT"
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [turns("rec-1", 4), turns("rec-2", 6)],
            "AGENT#rec-1#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#platform-status___get_platform_telemetry", "tool_calls": Decimal(3)},
                {"sk": "D#2026-08-15#T#platform-status___show_content", "tool_calls": Decimal(2)},
                # Another target on the same gateway — not this server's.
                {"sk": "D#2026-08-15#T#platform-tools___current_time", "tool_calls": Decimal(9)},
            ],
            "AGENT#rec-2#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#platform-status___get_platform_telemetry", "tool_calls": Decimal(1)},
            ],
        }),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer", harness_arn=arn),
                StubRecord("rec-2", "editor"),
                StubRecord("gw-1", "gateway", descriptor_type="MCP"),
                StubRecord("mcp-1", "platform status", descriptor_type="MCP"),
            ],
            {
                "gw-1": {"server": {"gatewayArn": GATEWAY_ARN}},
                "mcp-1": {"server": {"remotes": [{"url": runtime_url}]}},
            },
        ),
        harness=StubHarness(
            [],
            targets_by_arn={GATEWAY_ARN: ["platform-tools", "platform-status"]},
            endpoints_by_arn={GATEWAY_ARN: {"platform-status": runtime_url}},
        ),
    )

    reach = service.record_reach("mcp-1", "2026-08-15", "2026-08-15")

    assert reach["metric"] == "traffic"
    assert reach["tools"] == {"get_platform_telemetry": 4, "show_content": 2}
    assert reach["agents"] == 2
    assert reach["turns"] == 10
    assert [a["record_id"] for a in reach["agent_records"]] == ["rec-2", "rec-1"]


def test_a_remote_mcp_server_attached_directly_counts_the_attaching_agents():
    """The harness `remoteMcp` path still works on its own: an agent that attaches
    the URL directly is reach even with no gateway in between. Its bare-name tool
    calls cannot be told from built-ins, so `tools` stays empty rather than guessed."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    service = UsageService(
        repository=StubRepo({"AGENTS#2026-08": [turns("rec-1", 4)]}),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer", harness_arn=arn),
                StubRecord("mcp-1", "example/tools", descriptor_type="MCP"),
            ],
            {"mcp-1": {"server": {"remotes": [{"url": MCP_URL}]}}},
        ),
        harness=StubHarness([
            Harness("rec-1", arn, tools=[
                {"type": "remote_mcp", "name": "tools",
                 "config": {"remoteMcp": {"url": MCP_URL}}},
            ])
        ]),
    )

    reach = service.record_reach("mcp-1", "2026-08-15", "2026-08-15")

    assert reach == {
        "metric": "traffic", "agents": 1, "turns": 4, "tools": {},
        "agent_records": [{"record_id": "rec-1", "name": "writer", "turns": 4}],
        "daily": [{"date": "2026-08-15", "turns": 4, "tool_calls": 0}],
    }


def test_composition_credits_a_runtime_hosted_mcp_server_through_its_fronting_target():
    """The Insights page and the record's Usage tab read the same ledger; after
    the tab learned to follow the gateway target's endpoint, the page must agree
    or the two numbers for one server disagree on one screen."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    runtime_url = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations?qualifier=DEFAULT"
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [turns("rec-1", 4)],
            "AGENT#rec-1#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#platform-status___get_platform_telemetry", "tool_calls": Decimal(3)},
            ],
        }),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer", harness_arn=arn),
                StubRecord("gw-1", "gateway", descriptor_type="MCP"),
                StubRecord("mcp-1", "platform status", descriptor_type="MCP"),
            ],
            {
                "gw-1": {"server": {"gatewayArn": GATEWAY_ARN}},
                "mcp-1": {"server": {"remotes": [{"url": runtime_url}]}},
            },
        ),
        harness=StubHarness(
            [Harness("rec-1", arn, tools=[
                {"type": "agentcore_gateway", "name": "gw",
                 "config": {"agentCoreGateway": {"gatewayArn": GATEWAY_ARN}}},
            ])],
            targets_by_arn={GATEWAY_ARN: ["platform-status"]},
            endpoints_by_arn={GATEWAY_ARN: {"platform-status": runtime_url}},
        ),
    )

    mcp = service.composition("2026-08-15", "2026-08-15")["mcp"]

    assert [(e["name"], e["turns"], e["agents"]) for e in mcp] == [
        ("platform status", 4, 1)
    ]


def test_reach_carries_a_dense_daily_series_of_the_agents_turns():
    """The Usage tab draws a trend for an agent; a skill's trend is the same
    quantity through the agents that attach it, day by day, idle days as zero."""
    reach = three_agents_sharing_one_skill().record_reach(
        "skill-1", "2026-08-14", "2026-08-15"
    )

    assert reach["daily"] == [
        {"date": "2026-08-14", "turns": 0, "tool_calls": 0},
        {"date": "2026-08-15", "turns": 10, "tool_calls": 0},
    ]


def test_a_gateways_daily_series_counts_only_its_own_targets_calls():
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [turns("rec-1", 4)],
            "AGENT#rec-1#TOOLS#2026-08": [
                {"sk": "D#2026-08-15#T#platform-tools___current_time", "tool_calls": Decimal(3)},
                {"sk": "D#2026-08-14#T#platform-tools___current_time", "tool_calls": Decimal(2)},
                {"sk": "D#2026-08-15#T#other-gw___thing", "tool_calls": Decimal(9)},
                {"sk": "D#2026-08-15#T#calculator", "tool_calls": Decimal(5)},
            ],
        }),
        registry=StubRegistry(
            [
                StubRecord("rec-1", "writer", harness_arn=arn),
                StubRecord("gw-1", "gateway", descriptor_type="MCP"),
            ],
            {"gw-1": {"server": {"gatewayArn": GATEWAY_ARN}}},
        ),
        harness=StubHarness([], targets_by_arn=GATEWAY_TARGETS),
    )

    reach = service.record_reach("gw-1", "2026-08-14", "2026-08-15")

    assert reach["daily"] == [
        {"date": "2026-08-14", "turns": 0, "tool_calls": 2},
        {"date": "2026-08-15", "turns": 4, "tool_calls": 3},
    ]
