"""AgentCore Runtime platformVersion (V1/V2) 전환 스크립트.

starter toolkit(0.3.14 포함)과 CloudFormation/CDK 는 platformVersion 을 노출하지
않으므로 deploy.sh 가 launch 뒤에 UpdateAgentRuntime 으로 직접 맞춘다.
UpdateAgentRuntime 은 artifact 와 roleArn 이 필수라 Get 응답을 그대로 되돌려
줘야 하고, 읽기 전용 필드를 섞어 보내면 ValidationException 이 난다 — 그 경계를
여기서 고정한다. AWS 호출은 가짜 클라이언트로 대신한다(실 호출은 배포에서).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.set_platform_version import (  # noqa: E402
    build_update_request,
    ensure_platform_version,
    find_runtime_id,
)

GET_RESPONSE = {
    "ResponseMetadata": {"HTTPStatusCode": 200},
    "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/bap_default-abc",
    "agentRuntimeId": "bap_default-abc",
    "agentRuntimeName": "bap_default",
    "agentRuntimeVersion": "20",
    "createdAt": "2026-01-01T00:00:00Z",
    "lastUpdatedAt": "2026-10-06T06:22:16Z",
    "status": "READY",
    "platformVersion": "V1",
    "roleArn": "arn:aws:iam::111122223333:role/bap-agent-runtime",
    "agentRuntimeArtifact": {
        "containerConfiguration": {
            "containerUri": "111122223333.dkr.ecr.us-east-1.amazonaws.com/bedrock-agentcore-bap_default:latest"
        }
    },
    "networkConfiguration": {"networkMode": "PUBLIC"},
    "protocolConfiguration": {"serverProtocol": "HTTP"},
    "environmentVariables": {"MODEL_ID": "global.anthropic.claude-sonnet-5-5"},
    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
    "metadataConfiguration": {"requireMMDSV2": False},
    "workloadIdentityDetails": {"workloadIdentityArn": "arn:aws:bedrock-agentcore:...:workload-identity/x"},
}


class FakeControlClient:
    """get_agent_runtime 은 미리 정한 상태 순서를 돌려주고 update 는 기록만 한다."""

    def __init__(self, get_responses, runtimes=None):
        self._gets = list(get_responses)
        self.update_calls = []
        self._runtimes = runtimes or []

    def get_agent_runtime(self, agentRuntimeId):
        resp = self._gets.pop(0) if len(self._gets) > 1 else self._gets[0]
        return dict(resp, agentRuntimeId=agentRuntimeId)

    def update_agent_runtime(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"status": "UPDATING", "agentRuntimeVersion": "21"}

    def list_agent_runtimes(self, **kwargs):
        return {"agentRuntimes": self._runtimes}


def test_update_request_replays_config_and_only_changes_platform_version():
    req = build_update_request(GET_RESPONSE, "V2")

    assert req["agentRuntimeId"] == "bap_default-abc"
    assert req["platformVersion"] == "V2"
    # Update 필수 필드 두 개는 Get 에서 그대로 온다.
    assert req["roleArn"] == GET_RESPONSE["roleArn"]
    assert req["agentRuntimeArtifact"] == GET_RESPONSE["agentRuntimeArtifact"]
    # 선택 필드도 빠지면 기본값으로 덮이므로 전부 따라온다.
    for key in (
        "networkConfiguration",
        "protocolConfiguration",
        "environmentVariables",
        "lifecycleConfiguration",
        "metadataConfiguration",
    ):
        assert req[key] == GET_RESPONSE[key]


def test_update_request_drops_read_only_fields():
    req = build_update_request(GET_RESPONSE, "V2")

    for key in (
        "ResponseMetadata",
        "agentRuntimeArn",
        "agentRuntimeName",
        "agentRuntimeVersion",
        "createdAt",
        "lastUpdatedAt",
        "status",
        "workloadIdentityDetails",
    ):
        assert key not in req


def test_update_request_omits_optional_fields_the_runtime_does_not_have():
    """bap_platform_status 같은 MCP 런타임은 environmentVariables 가 없다. 빈 dict 를
    지어내 보내지 않고 키 자체를 뺀다."""
    slim = {k: v for k, v in GET_RESPONSE.items() if k != "environmentVariables"}

    req = build_update_request(slim, "V2")

    assert "environmentVariables" not in req


def test_already_on_target_version_makes_no_update_call():
    client = FakeControlClient([dict(GET_RESPONSE, platformVersion="V2")])

    result = ensure_platform_version(client, "bap_default-abc", "V2", sleep=lambda s: None)

    assert client.update_calls == []
    assert result == {"changed": False, "platformVersion": "V2", "status": "READY"}


def test_missing_platform_version_counts_as_v1():
    """오래된 런타임은 Get 응답에 platformVersion 키 자체가 없다(= V1 기본값)."""
    absent = {k: v for k, v in GET_RESPONSE.items() if k != "platformVersion"}
    client = FakeControlClient(
        [absent, dict(GET_RESPONSE, status="UPDATING"), dict(GET_RESPONSE, platformVersion="V2")]
    )

    result = ensure_platform_version(client, "bap_default-abc", "V2", sleep=lambda s: None)

    assert len(client.update_calls) == 1
    assert result["changed"] is True
    assert result["platformVersion"] == "V2"


def test_flips_then_waits_until_ready():
    slept = []
    client = FakeControlClient(
        [
            GET_RESPONSE,  # before: V1 READY
            dict(GET_RESPONSE, status="UPDATING", platformVersion="V2"),
            dict(GET_RESPONSE, status="UPDATING", platformVersion="V2"),
            dict(GET_RESPONSE, status="READY", platformVersion="V2"),
        ]
    )

    result = ensure_platform_version(client, "bap_default-abc", "V2", sleep=slept.append)

    assert client.update_calls[0]["platformVersion"] == "V2"
    assert result == {"changed": True, "platformVersion": "V2", "status": "READY"}
    # V2 스냅샷 준비는 분 단위라 폴링은 반드시 쉬어 가며 한다.
    assert len(slept) == 2


def test_waits_for_in_progress_update_before_touching_the_runtime():
    """진행 중인 런타임에 Update 를 치면 ConflictException. 먼저 READY 를 기다린다."""
    client = FakeControlClient(
        [
            dict(GET_RESPONSE, status="UPDATING"),
            dict(GET_RESPONSE, status="READY"),
            dict(GET_RESPONSE, status="READY", platformVersion="V2"),
        ]
    )

    ensure_platform_version(client, "bap_default-abc", "V2", sleep=lambda s: None)

    assert len(client.update_calls) == 1


def test_failed_update_raises_with_the_status():
    client = FakeControlClient(
        [GET_RESPONSE, dict(GET_RESPONSE, status="UPDATE_FAILED", platformVersion="V2")]
    )

    with pytest.raises(RuntimeError, match="UPDATE_FAILED"):
        ensure_platform_version(client, "bap_default-abc", "V2", sleep=lambda s: None)


def test_find_runtime_id_resolves_by_exact_name():
    client = FakeControlClient(
        [GET_RESPONSE],
        runtimes=[
            {"agentRuntimeName": "bap_default_old", "agentRuntimeId": "bap_default_old-zzz"},
            {"agentRuntimeName": "bap_default", "agentRuntimeId": "bap_default-abc"},
        ],
    )

    assert find_runtime_id(client, "bap_default") == "bap_default-abc"
    assert find_runtime_id(client, "nope") is None
