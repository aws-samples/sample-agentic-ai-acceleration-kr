import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.config as cfg  # noqa: E402


def test_config_reports_capabilities(monkeypatch):
    monkeypatch.setattr(cfg, "registry_enabled", lambda: False)
    monkeypatch.setattr(cfg, "HARNESS_EXECUTION_ROLE_ARN", "arn:role")
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", [])
    assert cfg.get_config() == {
        "registryEnabled": False,
        "harnessEnabled": True,
        "allowedModels": [],
    }


def test_the_allow_list_is_published_in_picker_order(monkeypatch):
    """The web offers exactly what the server will accept, in the operator's order."""
    monkeypatch.setattr(cfg, "registry_enabled", lambda: True)
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", ["m2", "m1"])
    body = cfg.get_config()
    assert body["allowedModels"] == ["m2", "m1"]
    assert "basicChat" not in body
