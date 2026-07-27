"""bundle_config 테스트 (SSM 포인터 → bundle 오버라이드, 캐시·폴백)."""

import json

import pytest

from orchestrator import bundle_config
from orchestrator.bundle_config import (
    CACHE_TTL_SECONDS,
    DEFAULT_BUNDLE_LABEL,
    BundleOverride,
    clear_cache,
    load_bundle_override,
)

PARAM = "/agentic-t2sql/active-bundle"


class FakeSsm:
    def __init__(self, value, error=None):
        self._value = value
        self._error = error
        self.calls = 0

    def get_parameter(self, Name):  # noqa: N803 - boto3 케이싱
        self.calls += 1
        if self._error is not None:
            raise self._error
        return {"Parameter": {"Name": Name, "Value": self._value}}


class FakeControl:
    def __init__(self, response, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def get_configuration_bundle_version(self, bundleId, versionId):  # noqa: N803
        self.calls.append((bundleId, versionId))
        if self._error is not None:
            raise self._error
        return self._response


def _pointer(bundle_id="b-1", version_id="v-2"):
    return json.dumps({"bundleId": bundle_id, "versionId": version_id})


def _bundle(system_prompt="새 프롬프트", model_id="us.anthropic.claude-x"):
    configuration = {}
    if system_prompt is not None:
        configuration["system_prompt"] = system_prompt
    if model_id is not None:
        configuration["model_id"] = model_id
    return {"components": {"orchestrator": {"configuration": configuration}}}


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()


def test_disabled_when_param_empty():
    ssm = FakeSsm(_pointer())
    assert load_bundle_override("", ssm_client=ssm) is None
    assert load_bundle_override("   ", ssm_client=ssm) is None
    assert ssm.calls == 0


def test_loads_override_and_label():
    ssm = FakeSsm(_pointer("bundle-abc", "ver-9"))
    control = FakeControl(_bundle())
    override = load_bundle_override(PARAM, ssm_client=ssm, control_client=control)
    assert override == BundleOverride(
        system_prompt="새 프롬프트",
        model_id="us.anthropic.claude-x",
        bundle_label="bundle-abc@ver-9",
    )
    assert control.calls == [("bundle-abc", "ver-9")]
    assert override.has_override() is True


def test_partial_override_only_model():
    ssm = FakeSsm(_pointer())
    control = FakeControl(_bundle(system_prompt=None))
    override = load_bundle_override(PARAM, ssm_client=ssm, control_client=control)
    assert override.system_prompt is None
    assert override.model_id == "us.anthropic.claude-x"


def test_ttl_cache_avoids_refetch():
    ssm = FakeSsm(_pointer())
    control = FakeControl(_bundle())
    first = load_bundle_override(
        PARAM, ssm_client=ssm, control_client=control, now=100.0
    )
    second = load_bundle_override(
        PARAM, ssm_client=ssm, control_client=control, now=100.0 + CACHE_TTL_SECONDS - 1
    )
    assert first == second
    assert ssm.calls == 1
    assert len(control.calls) == 1


def test_cache_expires_after_ttl():
    ssm = FakeSsm(_pointer())
    control = FakeControl(_bundle())
    load_bundle_override(PARAM, ssm_client=ssm, control_client=control, now=100.0)
    load_bundle_override(
        PARAM, ssm_client=ssm, control_client=control, now=100.0 + CACHE_TTL_SECONDS + 1
    )
    assert ssm.calls == 2


def test_cache_also_memoizes_none_result():
    ssm = FakeSsm(None, error=RuntimeError("ParameterNotFound"))
    control = FakeControl(_bundle())
    assert load_bundle_override(PARAM, ssm_client=ssm, control_client=control, now=1.0) is None
    assert load_bundle_override(PARAM, ssm_client=ssm, control_client=control, now=2.0) is None
    # 실패 결과도 캐시 → 재시도 폭주 방지.
    assert ssm.calls == 1


def test_ssm_failure_falls_back_to_none(caplog):
    ssm = FakeSsm(None, error=RuntimeError("AccessDenied"))
    control = FakeControl(_bundle())
    with caplog.at_level("WARNING"):
        assert load_bundle_override(PARAM, ssm_client=ssm, control_client=control) is None
    assert control.calls == []
    assert any("코드 기본값" in r.getMessage() for r in caplog.records)


def test_malformed_pointer_falls_back():
    ssm = FakeSsm("not json")
    control = FakeControl(_bundle())
    assert load_bundle_override(PARAM, ssm_client=ssm, control_client=control) is None


def test_pointer_missing_ids_falls_back():
    ssm = FakeSsm(json.dumps({"bundleId": "b-1"}))
    control = FakeControl(_bundle())
    assert load_bundle_override(PARAM, ssm_client=ssm, control_client=control) is None


def test_pointer_snake_case_accepted():
    ssm = FakeSsm(json.dumps({"bundle_id": "b-2", "version_id": "v-3"}))
    control = FakeControl(_bundle())
    override = load_bundle_override(PARAM, ssm_client=ssm, control_client=control)
    assert override.bundle_label == "b-2@v-3"


def test_control_plane_failure_falls_back():
    ssm = FakeSsm(_pointer())
    control = FakeControl(None, error=RuntimeError("ValidationException"))
    assert load_bundle_override(PARAM, ssm_client=ssm, control_client=control) is None


def test_missing_component_key_falls_back():
    ssm = FakeSsm(_pointer())
    control = FakeControl({"components": {"other": {"configuration": {"model_id": "x"}}}})
    assert load_bundle_override(PARAM, ssm_client=ssm, control_client=control) is None


def test_empty_configuration_falls_back():
    ssm = FakeSsm(_pointer())
    control = FakeControl({"components": {"orchestrator": {"configuration": {}}}})
    assert load_bundle_override(PARAM, ssm_client=ssm, control_client=control) is None


def test_configuration_as_json_string_is_parsed():
    ssm = FakeSsm(_pointer())
    control = FakeControl(
        {
            "components": {
                "orchestrator": {"configuration": json.dumps({"model_id": "m-1"})}
            }
        }
    )
    override = load_bundle_override(PARAM, ssm_client=ssm, control_client=control)
    assert override.model_id == "m-1"


def test_default_override_label():
    assert BundleOverride().bundle_label == DEFAULT_BUNDLE_LABEL
    assert BundleOverride().has_override() is False


def test_use_cache_false_bypasses_cache():
    ssm = FakeSsm(_pointer())
    control = FakeControl(_bundle())
    load_bundle_override(PARAM, ssm_client=ssm, control_client=control, use_cache=False)
    load_bundle_override(PARAM, ssm_client=ssm, control_client=control, use_cache=False)
    assert ssm.calls == 2


def test_lazy_boto3_clients_not_created_when_injected(monkeypatch):
    # 클라이언트 주입 시 boto3 를 건드리지 않아야 한다(오프라인 테스트 보장).
    def _boom(*_a, **_k):
        raise AssertionError("boto3 클라이언트를 생성하면 안 됩니다")

    monkeypatch.setattr(bundle_config, "_ssm_client", _boom)
    monkeypatch.setattr(bundle_config, "_control_client", _boom)
    override = load_bundle_override(
        PARAM, ssm_client=FakeSsm(_pointer()), control_client=FakeControl(_bundle())
    )
    assert override is not None
