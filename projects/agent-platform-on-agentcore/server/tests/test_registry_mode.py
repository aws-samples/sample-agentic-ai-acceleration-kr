"""registry_enabled / is_registry_unavailable: the two predicates every
registry-off fallback branches on."""
import os
import sys

from botocore.exceptions import ClientError, EndpointConnectionError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.registry_service as rs  # noqa: E402


def _configure(monkeypatch, *, flag: str, registry_id: str):
    """Patch the module globals registry_enabled() reads, rather than setenv +
    reload: core.config loads .env with override=True at import, so a reload
    would re-apply a developer's local AGENT_REGISTRY_ID over the test's value."""
    monkeypatch.setattr(rs, "AP_USE_REGISTRY", flag)
    monkeypatch.setattr(rs, "AGENT_REGISTRY_ID", registry_id)
    return rs


def test_enabled_when_auto_and_id_present(monkeypatch):
    assert _configure(monkeypatch, flag="auto", registry_id="reg-123").registry_enabled() is True


def test_disabled_when_id_empty(monkeypatch):
    assert _configure(monkeypatch, flag="auto", registry_id="").registry_enabled() is False


def test_disabled_when_flag_off_even_with_id(monkeypatch):
    assert _configure(monkeypatch, flag="0", registry_id="reg-123").registry_enabled() is False


def test_aws_side_failures_are_unavailable_but_bugs_are_not():
    err = ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListRegistryRecords")
    assert rs.is_registry_unavailable(err) is True
    assert rs.is_registry_unavailable(EndpointConnectionError(endpoint_url="x")) is True
    assert rs.is_registry_unavailable(rs.RegistryNotConfigured("no id")) is True
    assert rs.is_registry_unavailable(ValueError("nope")) is False
