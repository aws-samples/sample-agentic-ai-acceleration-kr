import pytest

pytest.importorskip("rich")

from pathlib import Path

from deployment.tui import cli
from deployment.tui.preflight import CheckResult
from deployment.tui.steps import Step

FIX = Path(__file__).parent / "fixtures"


def _step(name, script, *args, skippable=False):
    return Step(name, ["bash", str(FIX / script), *args], skippable=skippable)


def test_preflight_table_all_ok_true():
    checks = [CheckResult("aws", True, "/usr/bin/aws"), CheckResult("jq", True, "/usr/bin/jq")]
    assert cli.preflight_table(checks) is True


def test_preflight_table_reports_failure():
    checks = [CheckResult("aws", True, "ok"), CheckResult("terraform", False, "not found")]
    assert cli.preflight_table(checks) is False


def test_run_and_report_success_over_fake_steps():
    # fake echo/exit-0 스크립트만 실행 — 실제 배포 없음
    wf = [_step("first", "ok.sh"), _step("second", "ok.sh")]
    assert cli.run_and_report(wf, "fake") is True


def test_run_and_report_stops_and_reports_failure():
    wf = [_step("boom", "fail.sh"), _step("never", "ok.sh")]
    assert cli.run_and_report(wf, "fake") is False


def test_run_and_report_treats_skippable_failure_as_success():
    # skippable 스텝이 실패해도 필수 스텝이 모두 통과하면 전체는 성공(완료)
    wf = [_step("skip", "fail.sh", skippable=True), _step("required", "ok.sh")]
    assert cli.run_and_report(wf, "fake") is True


def test_aws_account_id_parses_stdout(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="123456789012\n", stderr=""),
    )
    assert cli.aws_account_id() == "123456789012"


def test_aws_account_id_none_on_failure(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=255, stdout="", stderr="denied"),
    )
    assert cli.aws_account_id() is None


def test_aws_account_id_none_when_cli_missing(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert cli.aws_account_id() is None


def test_llm_tfstate_defaults_includes_account_suffix():
    # 버킷은 S3 전역 유일성 때문에 계정 접미 필수, 락테이블은 접미 없음
    bucket, table = cli.llm_tfstate_defaults("913524902871")
    assert bucket == "llm-gateway-vanilla-tfstate-913524902871"
    assert table == "llm-gateway-vanilla-tflock"


def test_llm_tfstate_defaults_fallback_without_account():
    bucket, table = cli.llm_tfstate_defaults(None)
    assert bucket == "llm-gateway-vanilla-tfstate"
    assert table == "llm-gateway-vanilla-tflock"


def test_unwrap_none_raises_cancelled():
    # questionary .ask()는 Ctrl-C/Esc 시 None → 취소로 변환
    with pytest.raises(cli.Cancelled):
        cli._unwrap(None)
    assert cli._unwrap("dev") == "dev"


def test_menu_maps_workflows():
    labels = [label for label, _ in cli.MENU]
    handlers = [handler for _, handler in cli.MENU]
    assert "LLM Gateway 배포" in labels
    assert cli.flow_llm in handlers
    assert cli.flow_tool in handlers
    assert cli.flow_all in handlers


def test_flow_all_runs_tool_after_llm_success(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "flow_llm", lambda: calls.append("llm") or True)
    monkeypatch.setattr(cli, "flow_tool", lambda: calls.append("tool") or True)
    assert cli.flow_all() is True
    assert calls == ["llm", "tool"]  # 순서 A→B 고정


def test_flow_all_skips_tool_when_llm_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "flow_llm", lambda: calls.append("llm") or False)
    monkeypatch.setattr(cli, "flow_tool", lambda: calls.append("tool") or True)
    assert cli.flow_all() is False
    assert calls == ["llm"]  # A 실패 → B는 게이팅되어 미실행


def test_flow_tool_aborts_when_assets_missing(monkeypatch):
    # Tool GW 스크립트/terraform이 없으면 실제 배포 스텝 없이 즉시 중단
    ran = []
    monkeypatch.setattr(cli, "run_preflight", lambda tools: True)
    monkeypatch.setattr(cli, "tool_gateway_assets_ok", lambda: False)
    monkeypatch.setattr(cli, "run_and_report", lambda wf, title: ran.append(title) or True)
    assert cli.flow_tool() is False
    assert ran == []


def test_flow_teardown_tool_aborts_when_assets_missing(monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "ask_select", lambda msg, choices: "tool")
    monkeypatch.setattr(cli, "run_preflight", lambda tools: True)
    monkeypatch.setattr(cli, "tool_gateway_assets_ok", lambda: False)
    monkeypatch.setattr(cli, "run_and_report", lambda wf, title: ran.append(title) or True)
    assert cli.flow_teardown() is False
    assert ran == []


def test_flow_teardown_cancel_runs_nothing(monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "ask_select", lambda msg, choices: "__cancel__")
    monkeypatch.setattr(cli, "run_and_report", lambda wf, title: ran.append(title) or True)
    assert cli.flow_teardown() is False
    assert ran == []


def test_flow_teardown_wrong_token_aborts(monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "ask_select", lambda msg, choices: "tool")
    monkeypatch.setattr(cli, "run_preflight", lambda tools: True)
    monkeypatch.setattr(cli, "tool_gateway_assets_ok", lambda: True)
    # 확인 문구를 틀리게 입력 → 삭제 안 함
    monkeypatch.setattr(cli, "ask_text", lambda msg, default="": "nope")
    monkeypatch.setattr(cli, "run_and_report", lambda wf, title: ran.append(title) or True)
    assert cli.flow_teardown() is False
    assert ran == []


def test_flow_teardown_correct_token_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "ask_select", lambda msg, choices: "tool")
    monkeypatch.setattr(cli, "run_preflight", lambda tools: True)
    monkeypatch.setattr(cli, "tool_gateway_assets_ok", lambda: True)
    monkeypatch.setattr(cli, "ask_text", lambda msg, default="": "delete tool-gateway")
    monkeypatch.setattr(cli, "run_and_report", lambda wf, title: ran.append(title) or True)
    assert cli.flow_teardown() is True
    assert ran == ["Teardown Tool Gateway"]


def test_main_menu_exit_does_not_call_str(monkeypatch):
    # 종료 선택 시 센티널 반환 → 핸들러로 호출하지 않고 정상 종료 (회귀: 'str' not callable)
    monkeypatch.setattr(cli, "ask_select", lambda msg, choices: "__exit__")
    cli.main_menu()  # 예외 없이 반환되어야 함


def test_main_menu_runs_selected_handler_then_exits(monkeypatch):
    called = []
    seq = iter([lambda: called.append("ran"), "__exit__"])
    monkeypatch.setattr(cli, "ask_select", lambda msg, choices: next(seq))
    cli.main_menu()
    assert called == ["ran"]


def test_flow_all_returns_false_when_tool_fails(monkeypatch):
    # A 성공 + B 실패 → 전체는 False지만 B는 시도됨(non-fatal)
    calls = []
    monkeypatch.setattr(cli, "flow_llm", lambda: calls.append("llm") or True)
    monkeypatch.setattr(cli, "flow_tool", lambda: calls.append("tool") or False)
    assert cli.flow_all() is False
    assert calls == ["llm", "tool"]
