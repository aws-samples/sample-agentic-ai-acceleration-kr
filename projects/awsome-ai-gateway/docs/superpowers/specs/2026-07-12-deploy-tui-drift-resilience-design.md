# Deploy TUI — 재배포 drift 근본 제거 설계

날짜: 2026-07-12
상태: 승인됨 (구현 대기)

## 배경 / 문제

deploy-tui 로 dev 스택을 배포하다 아래 3종 에러가 연쇄로 발생했다. 모두
"이전 apply/teardown 의 잔재(drift)"가 재배포를 막는 재현성 있는 함정이며,
사용자 환경뿐 아니라 **teardown→재배포하거나 apply 도중 프로세스가 죽는 누구든**
동일하게 겪는다.

| # | 에러 | 스텝 |
|---|------|------|
| 1 | `Error acquiring the state lock` (DynamoDB stale lock) | tf-plan |
| 2 | `cannot re-use a name that is still in use` (helm `alb_controller` `pending-install`) | tf-apply |
| 3 | `secret ... already scheduled for deletion` (`chat-agent/reader`) | tf-apply |

목표: **진단/안내 같은 반창고가 아니라, drift 가 애초에 생기지 않도록 소스에서
근본 원인을 제거한다.**

## 근본 원인

두 뿌리로 갈린다.

### 원인 A — terraform 설정 결함 (에러 #3)
`modules/agentcore-runtime/lambdas.tf` 의 `aws_secretsmanager_secret.chat_reader`
에만 `recovery_window_in_days` 가 **누락**되어 기본값 30일이 적용된다. 형제
secret 4개는 모두 `recovery_window_in_days = 0` 이다:

- `modules/elasticache-valkey/main.tf` (auth_token)
- `modules/aurora-postgresql/secrets.tf` (gateway_user, db)
- `modules/tool-gateway/tool-secret/main.tf` (this)

그래서 teardown 시 `chat_reader` 만 30일간 "scheduled for deletion" 으로 남고,
그 안에 재배포하면 같은 이름 재생성이 불가해 `InvalidRequestException` 이 뜬다.
**teardown→재배포하는 누구나 100% 재현.** 한 줄로 영구 제거되는 진짜 결함.

### 원인 B — apply 중단 시 잔재 (에러 #1, #2)
`runner.py` 의 `run_step` 은 subprocess 를 `Popen` 으로 띄운 뒤 stdout 을 읽는데,
`KeyboardInterrupt`(Ctrl+C) 나 프로세스 종료가 오면 **자식 terraform 에게 정리할
기회를 주지 않고** 예외만 전파한다. 그 결과:

- terraform 이 DynamoDB state lock 을 풀지 못하고 죽음 → 에러 #1
- helm provider 는 `atomic=true, cleanup_on_fail=true` 라 **정상 실패 시엔 스스로
  롤백**하지만, 강제 종료되면 롤백할 틈이 없어 `pending-install` 릴리스가 박제됨
  → 에러 #2

즉 B 의 뿌리는 하나: **TUI 가 중단될 때 자식 프로세스에 graceful 종료 신호를
전달하지 않는다.** terraform 은 SIGINT 를 받으면 현재 작업을 마무리하고 lock 을
스스로 해제하도록 설계돼 있으므로, 신호만 제대로 전달하면 #1, #2 가 소스에서
사라진다.

## 설계

### 수정 1 — chat_reader recovery_window (원인 A)

`modules/agentcore-runtime/lambdas.tf` 의 `chat_reader` 에 형제들과 동일하게
추가:

```hcl
resource "aws_secretsmanager_secret" "chat_reader" {
  count                   = local.db_tools_enabled
  name                    = "/${var.project}/${var.environment}/chat-agent/reader"
  description             = "..."
  kms_key_id              = aws_kms_key.chat_agent.arn
  recovery_window_in_days = 0   # ← 추가: teardown 시 즉시 완전 삭제 (형제 secret 과 일치)

  tags = merge(var.tags, { Name = "${local.name_prefix}-reader-secret" })
}
```

효과: teardown 이 secret 을 복구윈도우 없이 즉시 삭제 → 재배포 시 이름 충돌 없음.
dev 편의 설정이며 형제 4개가 이미 동일하므로 일관성도 맞다.

### 수정 2 — runner.py graceful shutdown (원인 B)

`run_step` 이 자식을 **process group leader** 로 띄우고(`start_new_session=True`),
`KeyboardInterrupt` 를 받으면 그룹 전체에 SIGINT 를 보내 정리를 기다린다. grace
시간 내 안 끝나면 SIGTERM→SIGKILL 로 에스컬레이션한다.

```python
import signal

GRACE_SECONDS = 30  # terraform 이 SIGINT 후 lock 해제 + 현재 리소스 마무리할 여유

def run_step(step, on_line=None, base_env=None):
    env = ...
    proc = subprocess.Popen(
        step.argv, cwd=..., env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        start_new_session=True,   # 자식을 새 프로세스 그룹의 리더로 → 그룹 신호 전파
    )
    try:
        with proc.stdout as out:
            for raw in out:
                ...
        proc.wait()
    except KeyboardInterrupt:
        _terminate_gracefully(proc)   # 그룹에 SIGINT → grace 대기 → SIGKILL
        raise                          # 취소 의도는 상위(main)로 계속 전파
    return StepResult(...)
```

`_terminate_gracefully(proc)`:
1. `os.killpg(os.getpgid(proc.pid), signal.SIGINT)` — terraform/helm 이 lock 해제·
   atomic 롤백을 수행할 기회를 준다
2. `proc.wait(timeout=GRACE_SECONDS)` — 정상 종료 대기
3. 타임아웃이면 `SIGTERM`, 다시 타임아웃이면 `killpg(SIGKILL)` 로 강제 종료
4. 이미 종료됐으면(ProcessLookupError) 조용히 통과

**경계/인터페이스**
- `run_step` 의 반환값·시그니처는 그대로 유지 (정상 경로 무변화).
- graceful 종료 로직은 `_terminate_gracefully` 순수 헬퍼로 분리 → 단독 테스트 가능.
- `run_workflow`, `cli.py` 는 변경 없음. `main()` 은 이미 `KeyboardInterrupt` 를
  잡아 "bye" 출력하므로 전파된 예외가 자연스럽게 처리된다.

## 테스트 전략 (TDD)

기존 fixture 철학(fake bash 스크립트) 유지. 실제 aws/terraform 불필요.

**수정 1 (terraform)** — terraform 은 이 리포에서 단위 테스트하지 않으므로,
정적 보증으로 충분: 형제 secret 과 동일 속성이 있는지 코드 리뷰 + `terraform
validate`(선택). 회귀 방지용으로 간단한 grep 기반 테스트를 추가할 수 있으나
YAGNI — 우선 제외.

**수정 2 (runner)** — 새 fixture `sleep.sh`(SIGINT 트랩해서 "cleaned up" 출력 후
종료 / 또는 무한 sleep) 를 추가하고:

1. `test_terminate_gracefully_sends_sigint_and_reaps` — 오래 도는 자식을
   `_terminate_gracefully` 로 종료 → 자식이 SIGINT 를 받아 정리 로그를 남기고
   종료했는지, 프로세스가 확실히 reap 됐는지 확인.
2. `test_terminate_gracefully_sigkills_after_grace` — SIGINT 를 무시하는 fixture
   에 대해 grace(테스트에선 짧게 monkeypatch) 후 SIGKILL 로 죽는지 확인.
3. `test_run_step_normal_path_unchanged` — 기존 ok.sh/fail.sh 동작 회귀 없음
   (기존 테스트가 이미 커버; 그대로 통과해야 함).

`GRACE_SECONDS` 는 모듈 상수로 두어 테스트에서 monkeypatch 로 짧게 만든다.

## 범위 밖 (YAGNI)
- 진단/복구 안내 모듈 (`diagnostics.py`): 근본 원인을 제거하므로 불필요.
- 자동 force-unlock / force-delete: 파괴적이고 동시 실행 apply 를 죽일 위험.
- helm 모듈 변경: 이미 `atomic + cleanup_on_fail` 이라 손댈 것 없음.
- 다른 secret 들: 이미 `recovery_window_in_days = 0`.

## 잔여 수동 조치 (이번 세션에서 이미 처리)
현재 계정의 이미 발생한 drift 3건은 세션 중 수동 정리 완료:
force-unlock, helm uninstall(pending 릴리스), secret force-delete. 위 수정은
**앞으로의 재발**을 막는다.
