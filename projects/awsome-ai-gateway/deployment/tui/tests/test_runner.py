import os
from pathlib import Path

from deployment.tui import runner
from deployment.tui.steps import Step

FIX = Path(__file__).parent / "fixtures"


def _mkstep(name, script, *args, env=None, skippable=False):
    return Step(name, ["bash", str(FIX / script), *args], env=env, skippable=skippable)


def test_run_step_captures_lines_and_success():
    result = runner.run_step(_mkstep("ok", "ok.sh"))
    assert result.ok is True
    assert any("hello" in ln for ln in result.lines)


def test_run_step_streams_via_callback():
    seen = []
    runner.run_step(_mkstep("ok", "ok.sh"), on_line=seen.append)
    assert any("hello" in ln for ln in seen)


def test_run_step_reports_failure():
    result = runner.run_step(_mkstep("fail", "fail.sh"))
    assert result.ok is False
    assert result.returncode == 1


def test_run_step_merges_env():
    # ok.sh echoes $GREETING if set
    result = runner.run_step(_mkstep("ok", "ok.sh", env={"GREETING": "yo"}))
    assert any("yo" in ln for ln in result.lines)


def test_run_workflow_stops_on_failure():
    wf = [_mkstep("fail", "fail.sh"), _mkstep("never", "ok.sh")]
    results = runner.run_workflow(wf)
    assert len(results) == 1
    assert results[0].ok is False


def test_run_workflow_continues_past_skippable_failure():
    # ★함정: teardown의 helm-uninstall은 릴리스/클러스터가 없으면 실패할 수
    # 있는데, skippable이므로 뒤따르는 tf-destroy는 반드시 실행되어야 한다.
    wf = [_mkstep("skip-fail", "fail.sh", skippable=True), _mkstep("after", "ok.sh")]
    results = runner.run_workflow(wf)
    assert len(results) == 2
    assert results[0].ok is False and results[0].step.skippable is True
    assert results[1].step.name == "after" and results[1].ok is True


def test_run_workflow_still_stops_on_non_skippable_failure_after_skippable():
    # skippable 통과 후 필수 스텝이 실패하면 거기서 정지해야 한다.
    wf = [
        _mkstep("skip-fail", "fail.sh", skippable=True),
        _mkstep("required-fail", "fail.sh"),
        _mkstep("never", "ok.sh"),
    ]
    results = runner.run_workflow(wf)
    assert [r.step.name for r in results] == ["skip-fail", "required-fail"]
