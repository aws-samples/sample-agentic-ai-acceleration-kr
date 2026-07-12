# Deploy CLI

awsome-ai-gateway 배포 오케스트레이션 대화형 콘솔.

Claude Code처럼 **인라인 콘솔**로 동작한다 — 전체 화면을 점유하지 않고 일반 셸
스크롤버퍼에 append-only로 출력하므로 로그를 그대로 스크롤/복사할 수 있다.

- 메뉴·선택: **화살표(↑↓)로 이동, Enter로 선택** (`questionary`)
- 검색엔진 10종: **스페이스로 체크박스 토글** — 한 화면에서 다중선택
- 출력(배너·사전검증 표·스텝 스트리밍): `rich`

의존성은 `rich` + `questionary` 둘뿐. 풀스크린 점유 없이 인라인 유지.
(화살표는 실제 터미널에서만 동작 — 파이프 입력 시 취소 처리)

## 실행

    ./deployment/scripts/deploy-tui.sh

또는 repo 루트에서 직접:

    deployment/tui/.venv/bin/python -m deployment.tui

메뉴에서 워크플로우를 고르면 프롬프트로 값을 입력받고, 사전검증 표를 보여준 뒤
확인을 받아 스텝을 하나씩 스트리밍 실행한다. `q`로 종료.

대부분의 입력은 **default가 채워져 있어 Enter만 치면 넘어간다**:

- aws_region (LLM GW): 기본 `ap-northeast-2`. tfvars의 `aws_region`과 `tf-init` backend region에 함께 주입되고, `azs`는 이 region 기반으로 자동 유도(`<region>a`/`<region>c`, 2 AZ). Tool GW는 us-east-1 고정(terraform validation)이라 입력받지 않는다.
- tfstate bucket/table (LLM GW): 계정 접미 규칙 `llm-gateway-vanilla-tfstate-<account-id>` / `llm-gateway-vanilla-tflock` (account id는 `aws sts`로 자동 조회, backend.tf 주석과 일치). 버킷은 S3 전역 유일성 때문에 계정 접미가 필수다.
- Tool GW bucket: `tool-gateway-tfstate-<account-id>` (account id는 `aws sts`로 자동 조회)
- cognito_domain_suffix 등: 기존 `terraform.tfvars`가 있으면 그 값으로 프리필

## 무엇을 하나

기존 배포 스크립트(`bootstrap-tfstate.sh`, `install-eks.sh`, `provision_tool_gateway.sh`, `smoke-test.sh`)와 `terraform`/`build-lambdas.sh`를 올바른 순서·env·인자로 호출한다. 배포 로직을 재구현하지 않는다.

## 워크플로우

- **LLM Gateway 배포** (region 선택, 기본 ap-northeast-2): 사전검증 → build-lambdas(조건부) → tf-init(backend 주입) → plan → apply → install-eks(KUBECONFIG 격리) → 검증
- **Tool Gateway 배포** (us-east-1, 옵션): 사전검증 → 검색엔진 토글 + 키 → tfvars → tf-init → provision deploy
- **전체 배포 (LLM → Tool)**: 위 둘을 순차 실행. Tool GW의 dashboard-env 연동은 LLM GW의 admin-ui가 떠 있어야 하므로 순서는 LLM→Tool 고정이며, LLM 단계가 실패/취소되면 Tool 단계는 게이팅되어 실행하지 않는다. Tool은 non-fatal 애드온이라 실패해도 LLM 배포에는 영향 없음.
- **스택 삭제 (Teardown)**: 배포된 스택 삭제 (파괴적). LLM GW는 **helm uninstall(ALB 먼저 제거) → terraform destroy** 순서로 orphaned ENI 없이 VPC까지 삭제. Tool GW는 `provision_tool_gateway.sh teardown` 후 **AgentCore api-key credential provider orphan 정리**(terraform destroy가 못 지우는 경우 대비 — 이 스택이 만드는 bare 엔진명만 정확 일치로 삭제, `-creds` 등 타 스택 리소스는 보존). 실행 전 삭제 대상 요약을 보여주고, `delete <stack>` 문구를 **정확히 타이핑**해야 진행된다(이중 확인).

## 함정 흡수

`docs/superpowers/specs/2026-07-11-deploy-tui-design.md`의 "알려진 함정" 표 참조. TUI는 순서+env/인자 주입+검증 스텝으로 함정을 흡수한다.

## 테스트

    python -m pytest deployment/tui/tests/ -v
