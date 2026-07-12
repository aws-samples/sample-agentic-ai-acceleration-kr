from types import SimpleNamespace
from deployment.tui import preflight


def test_check_tools_reports_missing():
    fake_which = lambda name: "/usr/bin/aws" if name == "aws" else None
    results = preflight.check_tools(["aws", "helm"], which=fake_which)
    by_name = {r.name: r for r in results}
    assert by_name["aws"].ok is True
    assert by_name["helm"].ok is False


def test_check_aws_auth_ok():
    fake_run = lambda *a, **k: SimpleNamespace(returncode=0)
    assert preflight.check_aws_auth(runner=fake_run).ok is True


def test_check_aws_auth_fail():
    fake_run = lambda *a, **k: SimpleNamespace(returncode=255)
    assert preflight.check_aws_auth(runner=fake_run).ok is False


def test_check_paths_reports_existing_and_missing(tmp_path):
    exists = tmp_path / "here.sh"
    exists.write_text("#!/bin/sh\n")
    missing = tmp_path / "gone.tf"
    results = preflight.check_paths([("here", exists), ("gone", missing)])
    by_name = {r.name: r for r in results}
    assert by_name["here"].ok is True
    assert by_name["gone"].ok is False
    assert str(missing) in by_name["gone"].detail


def test_check_tool_gateway_assets_returns_checks():
    # 실제 리포 상태와 무관하게 (name, ok) 쌍을 반환해야 함
    results = preflight.check_tool_gateway_assets()
    names = {r.name for r in results}
    assert "provision_tool_gateway.sh" in names
    assert "tool-gateway-dev terraform" in names
