# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Step을 subprocess로 실행하고 stdout을 라인 스트리밍하는 순수 러너.

실제 aws/terraform/helm을 아는 로직이 없다 — argv를 그대로 실행할 뿐이라
fake 스크립트로 완전히 테스트 가능하다."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from .steps import Step


@dataclass
class StepResult:
    step: Step
    returncode: int
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_step(step: Step, on_line=None, base_env=None) -> StepResult:
    env = dict(os.environ if base_env is None else base_env)
    if step.env:
        env.update(step.env)
    lines: list[str] = []
    proc = subprocess.Popen(
        step.argv,
        cwd=str(step.cwd) if step.cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    with proc.stdout as out:  # 명시적 close — 다단계 워크플로우에서 FD 누수 방지
        for raw in out:
            line = raw.rstrip("\n")
            lines.append(line)
            if on_line:
                on_line(line)
    proc.wait()
    return StepResult(step=step, returncode=proc.returncode, lines=lines)


def run_workflow(wf, on_line=None, on_step_done=None, base_env=None,
                 on_step_start=None) -> list[StepResult]:
    results: list[StepResult] = []
    for step in wf:
        if on_step_start:
            on_step_start(step)
        result = run_step(step, on_line=on_line, base_env=base_env)
        results.append(result)
        if on_step_done:
            on_step_done(result)
        # skippable 스텝은 실패해도 계속 진행(best-effort). 예: teardown의
        # helm-uninstall은 릴리스/클러스터가 이미 없으면 non-zero로 끝날 수
        # 있는데, 그렇다고 뒤따르는 tf-destroy를 건너뛰면 안 된다.
        if not result.ok and not step.skippable:
            break  # 필수 스텝 실패 시 정지 — 이후 스텝 미실행
    return results
