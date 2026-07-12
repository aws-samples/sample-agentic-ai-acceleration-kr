import os
from pathlib import Path

from deployment.tui import runner
from deployment.tui.steps import Step

FIX = Path(__file__).parent / "fixtures"


def _mkstep(name, script, *args, env=None):
    return Step(name, ["bash", str(FIX / script), *args], env=env)


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
