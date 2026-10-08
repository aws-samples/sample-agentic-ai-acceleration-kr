import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.config as cfg  # noqa: E402


def test_config_reports_capabilities(monkeypatch):
    monkeypatch.setattr(cfg, "registry_enabled", lambda: False)
    monkeypatch.setattr(cfg, "HARNESS_EXECUTION_ROLE_ARN", "arn:role")
    monkeypatch.setattr(cfg, "BASIC_CHAT_ALLOWED_MODELS", [])
    assert cfg.get_config() == {
        "registryEnabled": False,
        "harnessEnabled": True,
        "basicChat": {"configured": False, "models": []},
    }


def test_basic_chat_needs_a_runtime_to_answer(monkeypatch):
    """The model list ships by default; without a runtime the option would
    appear and every turn would fail with "no runtime to run on"."""
    monkeypatch.setattr(cfg, "registry_enabled", lambda: False)
    monkeypatch.setattr(cfg, "BASIC_CHAT_ALLOWED_MODELS", ["m1"])
    monkeypatch.setattr(cfg, "BASIC_CHAT_RUNTIME_ARN", "")
    monkeypatch.delenv("AGENT_RUNTIME_ARN", raising=False)
    assert cfg.get_config()["basicChat"] == {"configured": False, "models": ["m1"]}

    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:runtime")
    assert cfg.get_config()["basicChat"] == {"configured": True, "models": ["m1"]}

    monkeypatch.delenv("AGENT_RUNTIME_ARN")
    monkeypatch.setattr(cfg, "BASIC_CHAT_RUNTIME_ARN", "arn:basic")
    assert cfg.get_config()["basicChat"]["configured"] is True
