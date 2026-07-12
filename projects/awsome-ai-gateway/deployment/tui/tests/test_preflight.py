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
