"""MODEL_ID과 REGION_NAME이 실제로 런타임에 반영되는지.

deploy.sh는 두 값을 매 배포마다 `agentcore launch --env`로 넣는다. 그런데
entrypoint가 `payload.get("model_id", <리터럴>)`로 읽고 있었고, 서버의
_prepare_payload는 prompt/system_prompt/actor_id만 보낸다 — 즉 키가 한 번도
오지 않으므로 리터럴이 항상 이겼고, 두 환경변수는 죽은 설정이었다.

같은 종류의 실수가 다시 들어오는 것을 막기 위해 소스에서 직접 확인한다.
entrypoint를 import하려면 bedrock_agentcore 런타임 전체가 필요해서, 여기서는
설정 해석만 테스트하고 fallback 형태는 소스로 고정한다.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402

MAIN_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
)


def test_env_overrides_reach_the_config(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "global.anthropic.claude-sonnet-5-5")
    monkeypatch.setenv("REGION_NAME", "ap-northeast-2")

    config = Config.from_env()

    assert config.model_id == "global.anthropic.claude-sonnet-5-5"
    assert config.region_name == "ap-northeast-2"


def test_defaults_match_the_platform(monkeypatch):
    """server/agents/agent_config.py와 infra/envs/standalone/variables.tf가 쓰는 값과
    같아야 한다. 어긋나면 같은 배포 안에서 서버와 런타임이 다른 모델을 부른다.
    """
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("REGION_NAME", raising=False)

    config = Config.from_env()

    assert config.model_id == "global.anthropic.claude-sonnet-5-5"
    assert config.region_name == "ap-northeast-1"
    # dataclass 기본값도 같아야 한다 — from_env를 거치지 않고 Config()를
    # 만드는 코드가 다른 모델을 쓰게 되면 안 된다.
    assert Config().model_id == config.model_id
    assert Config().region_name == config.region_name


def test_entrypoint_falls_back_to_the_config_not_a_literal():
    with open(MAIN_PY, encoding="utf-8") as handle:
        source = handle.read()

    for field in ("model_id", "region_name"):
        assert f'payload.get("{field}") or config.{field}' in source, (
            f"{field} must fall back to the deployed config"
        )

    # 리터럴 fallback이 다시 들어오면 환경변수가 또 죽는다.
    assert not re.search(r'payload\.get\("(model_id|region_name)",', source)
