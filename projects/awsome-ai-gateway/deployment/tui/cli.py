# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""인라인 콘솔 UI — Claude Code처럼 터미널에 흘려쓰는 가벼운 배포 오케스트레이터.

Textual 풀스크린 대신 rich로 append-only 출력을 낸다. 전체 화면을 점유하지
않고 일반 셸 스크롤버퍼에 남으므로, 로그를 그대로 복사/스크롤할 수 있다.

UI 조립만 담당하고 배포 로직은 config/steps/preflight/runner를 그대로 재사용한다."""
from __future__ import annotations

import questionary
from questionary import Choice
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config, paths, preflight
from .config import BackendConfig
from .steps import (
    build_llm_teardown,
    build_llm_workflow,
    build_tool_teardown,
    build_tool_workflow,
)

console = Console()


# --------------------------------------------------------------------------- #
# 입력 래퍼 — questionary 화살표/체크박스. 여기만 인터랙션을 캡슐화한다.
# .ask()는 Ctrl-C 시 None을 반환하므로 취소로 처리한다.
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    """사용자가 프롬프트를 Ctrl-C/Esc로 취소."""


def _unwrap(value):
    if value is None:
        raise Cancelled
    return value


def ask_select(message: str, choices) -> str:
    """화살표 단일선택. choices: [(label, value), ...] 또는 [str, ...]."""
    opts = [c if isinstance(c, str) else Choice(title=c[0], value=c[1]) for c in choices]
    return _unwrap(questionary.select(message, choices=opts).ask())


def ask_checkbox(message: str, choices) -> list:
    """스페이스 토글 다중선택. choices: [(label, value, checked), ...]."""
    opts = [Choice(title=c[0], value=c[1], checked=c[2]) for c in choices]
    return _unwrap(questionary.checkbox(message, choices=opts).ask())


def ask_text(message: str, default: str = "") -> str:
    return _unwrap(questionary.text(message, default=default).ask())


def ask_password(message: str) -> str:
    return _unwrap(questionary.password(message).ask())


def ask_confirm(message: str, default: bool = False) -> bool:
    return _unwrap(questionary.confirm(message, default=default).ask())

# 검색엔진: (id, tfvars_toggle, needs_key)
ENGINES = [
    ("duckduckgo", "enable_duckduckgo", False),
    ("tavily", "enable_tavily", True),
    ("brave", "enable_brave", True),
    ("serper", "enable_serper", True),
    ("exa", "enable_exa", True),
    ("perplexity", "enable_perplexity", True),
    ("anthropic", "enable_anthropic", True),
    ("firecrawl", "enable_firecrawl", True),
    ("you", "enable_you", True),
]


# --------------------------------------------------------------------------- #
# 표시 헬퍼
# --------------------------------------------------------------------------- #
def banner() -> None:
    console.print(
        Panel(
            Text("awsome-ai-gateway 배포 오케스트레이터", justify="center", style="bold cyan"),
            border_style="cyan",
        )
    )


def preflight_table(checks) -> bool:
    """CheckResult 리스트를 표로 출력하고 전부 통과했는지 반환."""
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", style="dim")
    all_ok = True
    for c in checks:
        mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
        table.add_row(c.name, mark, c.detail)
        all_ok = all_ok and c.ok
    console.print(table)
    return all_ok


def run_preflight(tools) -> bool:
    checks = preflight.check_tools(tools)
    checks.append(preflight.check_aws_auth())
    return preflight_table(checks)


def tool_gateway_assets_ok() -> bool:
    """Tool Gateway 스크립트/terraform이 이 리포에 있는지 확인하고 표로 출력.
    없으면(별도 PR 미머지 등) 안내하고 False."""
    checks = preflight.check_tool_gateway_assets()
    ok = preflight_table(checks)
    if not ok:
        console.print(
            "[yellow]Tool Gateway 자산(provision 스크립트/terraform)이 이 리포에 없습니다.[/yellow]\n"
            "[dim]Tool Gateway 통합 PR이 머지된 브랜치에서 실행하세요. "
            "LLM Gateway 워크플로우는 영향받지 않습니다.[/dim]"
        )
    return ok


def aws_account_id() -> str | None:
    """현재 자격증명의 AWS account ID (best-effort). 실패 시 None."""
    import subprocess

    try:
        proc = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    acct = proc.stdout.strip()
    return acct if proc.returncode == 0 and acct else None


def llm_tfstate_defaults(acct: str | None) -> tuple[str, str]:
    """LLM Gateway tfstate 버킷/락테이블 default.

    실제 배포·backend.tf 주석의 규칙은 `llm-gateway-vanilla-tfstate-<account>`
    (버킷은 S3 전역 유일성 때문에 계정 접미 필수) + `llm-gateway-vanilla-tflock`.
    account id를 못 구하면 접미 없는 형태로 fallback(사용자가 직접 수정)."""
    suffix = f"-{acct}" if acct else ""
    return f"llm-gateway-vanilla-tfstate{suffix}", "llm-gateway-vanilla-tflock"


# --------------------------------------------------------------------------- #
# 워크플로우 실행 — 스텝 단위 append-only 스트리밍
# --------------------------------------------------------------------------- #
def run_and_report(wf, title: str) -> bool:
    """runner.run_workflow를 인라인 스트리밍으로 구동. 전체 성공 여부 반환."""
    # 실행 지점에서 지연 import — runner는 subprocess를 돌리므로 테스트에서 격리
    from .runner import run_workflow

    console.rule(f"[bold]{title}[/bold]")

    def on_step_start(step) -> None:
        skip = " [dim](skippable)[/dim]" if step.skippable else ""
        console.print(f"[cyan]▶[/cyan] {step.name}{skip}")

    def on_line(line: str) -> None:
        console.print(f"  [dim]│[/dim] {line}", highlight=False)

    def on_step_done(result) -> None:
        if result.ok:
            console.print(f"[green]✓[/green] {result.step.name}")
        elif result.step.skippable:
            # best-effort 스텝 실패는 경고로만 — 워크플로우는 계속 진행된다
            console.print(f"[yellow]⚠[/yellow] {result.step.name} (exit {result.returncode}, skippable — 계속 진행)")
        else:
            console.print(f"[red]✗[/red] {result.step.name} (exit {result.returncode})")

    results = run_workflow(
        wf, on_line=on_line, on_step_done=on_step_done, on_step_start=on_step_start
    )
    # skippable 스텝 실패는 전체 성공 판정에서 제외(best-effort). 필수 스텝만 성공하면 완료.
    ok = bool(results) and all(r.ok or r.step.skippable for r in results)
    if ok:
        console.print(f"\n[bold green]완료[/bold green] — {title}")
    else:
        # 정지 사유는 필수(non-skippable) 스텝 실패 — 그걸 가리킨다
        failed = next((r for r in results if not r.ok and not r.step.skippable), None)
        where = f" ({failed.step.name})" if failed else ""
        console.print(f"\n[bold red]실패[/bold red]{where} — 위 로그를 확인하세요")
    return ok


# --------------------------------------------------------------------------- #
# 워크플로우 A — LLM Gateway
# --------------------------------------------------------------------------- #
def flow_llm() -> bool:
    """LLM Gateway 배포. 실제 배포 후 성공하면 True, 미배포/취소/실패면 False."""
    console.rule("[bold]LLM Gateway 배포[/bold]")
    if not run_preflight(preflight.LLM_TOOLS):
        console.print("[red]사전검증 실패[/red] — 누락 도구/인증을 해결한 뒤 다시 실행하세요.")
        return False

    env = ask_select("환경", ["dev", "prod"])

    # 기존 terraform.tfvars가 있으면 값 프리필 → Enter로 바로 넘어감
    tf_path = paths.llm_tf_dir(env) / "terraform.tfvars"
    existing_text = tf_path.read_text() if tf_path.exists() else ""
    existing = config.parse_tfvars(existing_text) if existing_text else {}

    # ★region: tfvars(aws_region)와 tfstate backend init 양쪽에 주입.
    #   azs는 region 기반으로 자동 유도(a/c 2 AZ — dev 비용 규칙)하되,
    #   기존 tfvars에 azs가 있으면 존중한다(멀티라인 list라 parse_tfvars엔 안 잡힘).
    region = ask_text("aws_region", default=existing.get("aws_region", "ap-northeast-2"))
    cognito = ask_text("cognito_domain_suffix", default=existing.get("cognito_domain_suffix", ""))
    # principal_arn은 eks_access_entries 블록 안에 있어 parse_tfvars엔 안 잡힘 →
    # 원문에서 직접 프리필해 재입력 없이 Enter로 넘어가게 한다.
    principal = ask_text(
        "eks_access_entries principal_arn",
        default=config.prefill_scalar(existing_text, "principal_arn"),
    )

    placeholders = config.find_placeholders(
        {"cognito_domain_suffix": cognito, "principal_arn": principal}
    )
    if placeholders:
        console.print(f"[red]플레이스홀더 남음:[/red] {', '.join(placeholders)}")
        return False

    # tfstate 버킷은 계정 접미 규칙(llm-gateway-vanilla-tfstate-<account>) →
    # account id를 조회해 default 구성(Enter로 수락). backend.tf 주석과 일치.
    b_default, t_default = llm_tfstate_defaults(aws_account_id())
    bucket = ask_text("tfstate bucket", default=b_default)
    table = ask_text("tfstate dynamodb table", default=t_default)
    enable_chat_agent = ask_confirm("enable_chat_agent?", default=True)
    enable_chat_db = ask_confirm("enable_chat_db_tools? (Lambda 빌드 선행)", default=True)
    # 실행 플래그는 체크박스 한 화면에서 토글
    selected_flags = ask_checkbox(
        "install-eks 실행 플래그 (스페이스로 토글)",
        [
            ("DEBUG_MODE", "DEBUG_MODE", False),
            ("MIGRATION_ENABLED", "MIGRATION_ENABLED", True),
            ("FORCE_CONFLICTS", "FORCE_CONFLICTS", False),
        ],
    )
    flags = {
        f: (f in selected_flags)
        for f in ("DEBUG_MODE", "MIGRATION_ENABLED", "FORCE_CONFLICTS")
    }

    # tfvars 병합 기록 (위에서 읽은 existing에 폼 값 반영)
    if cognito:
        existing["cognito_domain_suffix"] = cognito
    # region + region 기반 azs 자동 유도(2 AZ: a/c). write_tfvars가 dict 전체를
    # 재직렬화하므로 멀티라인 list인 azs가 파일에서 빠지지 않도록 명시 기록한다.
    existing["aws_region"] = region
    existing["azs"] = [f"{region}a", f"{region}c"]
    existing["enable_chat_agent"] = enable_chat_agent
    existing["enable_chat_db_tools"] = enable_chat_db
    # ★eks_access_entries는 중첩 블록으로 기록해야 한다. 이전엔 principal_arn/policy_arn/
    #   type이 최상위 scalar로 새어나가 'undeclared variable' 경고 + access entry 소실
    #   ('cluster unreachable')을 유발했다. dict로 넣어 write_tfvars가 블록으로 직렬화.
    existing["eks_access_entries"] = {
        "developer": {
            "principal_arn": principal,
            "policy_associations": {
                "admin": {
                    "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                    "access_scope": {"type": "cluster"},
                }
            },
        }
    }
    # tags도 최상위가 아니라 map으로 기록(변수 선언이 map(string)).
    existing["tags"] = {"CostCenter": "platform-ai", "Owner": "llm-gateway-team"}
    config.write_tfvars(tf_path, existing)
    console.print(f"[dim]tfvars 기록: {tf_path}[/dim]")

    backend = BackendConfig(bucket=bucket, dynamodb_table=table, region=region)
    wf = build_llm_workflow(
        env=env, backend=backend, enable_chat_db_tools=enable_chat_db, flags=flags
    )

    _preview_steps(wf)
    if not ask_confirm("위 스텝을 실행합니다 (실제 배포) — 계속?", default=False):
        console.print("[dim]취소됨[/dim]")
        return False
    return run_and_report(wf, f"LLM Gateway {env}")


# --------------------------------------------------------------------------- #
# 워크플로우 B — Tool Gateway
# --------------------------------------------------------------------------- #
def flow_tool() -> bool:
    """Tool Gateway 배포. 성공 True, 미배포/취소/실패 False (non-fatal 애드온)."""
    console.rule("[bold]Tool Gateway 배포[/bold]")
    if not run_preflight(preflight.TOOL_GW_TOOLS):
        console.print("[red]사전검증 실패[/red] — 누락 도구/인증을 해결한 뒤 다시 실행하세요.")
        return False
    if not tool_gateway_assets_ok():
        return False

    tfvars = {"project_name": "toolgw-demo", "environment": "dev", "aws_region": "us-east-1"}
    # 검색엔진 10종을 한 화면에서 스페이스로 토글
    enabled_ids = ask_checkbox(
        "검색엔진 활성화 (스페이스로 토글, Enter 확정)",
        [(eid, eid, False) for eid, _t, _k in ENGINES],
    )
    # ★함정: API 키는 tfvars에 넣지 않는다. 넣으면 (1) terraform "undeclared variable"
    #   경고 (2) 키가 tfstate(S3)에 평문 저장. 키는 별도 key file로 모아 TOOL_KEY_FILE로
    #   provision 스크립트에 넘기고, seed-tool-secrets.sh가 Secrets Manager에 직접 주입한다.
    keys: dict[str, str] = {}
    for eid, toggle, needs_key in ENGINES:
        enabled = eid in enabled_ids
        tfvars[toggle] = enabled
        if needs_key and enabled:
            keys[eid] = ask_password(f"{eid} API key")

    tf_path = paths.TOOL_TF_DIR / "terraform.tfvars"
    config.write_tfvars(tf_path, tfvars)
    console.print(f"[dim]tfvars 기록: {tf_path}[/dim]")

    key_file = config.write_key_file(keys)
    if key_file:
        console.print(f"[dim]API 키 {len(keys)}개 → Secrets Manager 주입 예정 (tfstate 미경유)[/dim]")

    # tool-gateway tfstate bucket은 account-id 포함이 규칙 → 자동 조회해 default 구성
    acct = aws_account_id()
    bucket_default = f"tool-gateway-tfstate-{acct}" if acct else "tool-gateway-tfstate"
    bucket = ask_text("tfstate bucket", default=bucket_default)
    table = ask_text("tfstate dynamodb table", default="tool-gateway-tflock")
    # Tool GW는 us-east-1 고정(terraform validation) → backend region도 고정.
    backend = BackendConfig(bucket=bucket, dynamodb_table=table, region="us-east-1")
    wf = build_tool_workflow(backend=backend, key_file=key_file)

    _preview_steps(wf)
    console.print("[dim]Tool Gateway는 non-fatal 애드온 — 실패해도 LLM GW에 영향 없음[/dim]")
    if not ask_confirm("위 스텝을 실행합니다 (실제 배포) — 계속?", default=False):
        console.print("[dim]취소됨[/dim]")
        _cleanup_key_file(key_file)
        return False
    try:
        return run_and_report(wf, "Tool Gateway")
    finally:
        # 키 파일은 seed 후 즉시 제거 — 평문 키가 디스크에 남지 않도록.
        _cleanup_key_file(key_file)


def _cleanup_key_file(key_file) -> None:
    if key_file:
        try:
            key_file.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 워크플로우 A+B — 전체 배포 (LLM → Tool 순서)
# --------------------------------------------------------------------------- #
def flow_all() -> bool:
    """LLM Gateway → Tool Gateway 순차 배포.

    Tool GW(B)의 dashboard-env 연동은 LLM GW(A)의 admin-ui가 떠 있어야 하므로
    순서는 A→B 고정. A가 실패/취소되면 B는 진행하지 않는다(의존성 게이팅)."""
    console.rule("[bold]전체 배포 (LLM → Tool)[/bold]")
    console.print("[dim]LLM Gateway를 먼저 배포한 뒤 Tool Gateway를 이어서 배포합니다.[/dim]")

    llm_ok = flow_llm()
    if not llm_ok:
        console.print(
            "\n[bold red]LLM Gateway 미완료[/bold red] — Tool Gateway는 admin-ui 의존성 때문에 "
            "진행하지 않습니다. LLM Gateway를 먼저 완료하세요."
        )
        return False

    console.print("\n[bold]LLM Gateway 완료 → Tool Gateway로 이어갑니다[/bold]")
    tool_ok = flow_tool()

    if tool_ok:
        console.print("\n[bold green]전체 배포 완료[/bold green] — LLM + Tool Gateway")
    else:
        console.print(
            "\n[yellow]LLM Gateway는 배포됨. Tool Gateway는 미완료(취소/실패) — "
            "non-fatal 애드온이라 LLM GW에는 영향 없습니다.[/yellow]"
        )
    return llm_ok and tool_ok


def _preview_steps(wf) -> None:
    console.print("\n[bold]실행 스텝:[/bold]")
    for i, step in enumerate(wf, 1):
        skip = " [dim](skippable)[/dim]" if step.skippable else ""
        console.print(f"  {i}. {step.name}{skip}")
    console.print()


# --------------------------------------------------------------------------- #
# 워크플로우 C — 스택 삭제 (Teardown)
# --------------------------------------------------------------------------- #
def flow_teardown() -> bool:
    """배포된 스택 삭제. 파괴적 작업이라 대상 요약 + 이중 확인(타이핑) 필수."""
    console.rule("[bold red]스택 삭제 (Teardown)[/bold red]")
    target = ask_select(
        "무엇을 삭제할까요?",
        [
            ("LLM Gateway (ap-northeast-2)", "llm"),
            ("Tool Gateway (us-east-1)", "tool"),
            ("취소", "__cancel__"),
        ],
    )
    if target == "__cancel__":
        console.print("[dim]취소됨[/dim]")
        return False

    if target == "llm":
        if not run_preflight(preflight.LLM_TOOLS):
            console.print("[red]사전검증 실패[/red] — 누락 도구/인증을 해결하세요.")
            return False
        env = ask_select("환경", ["dev", "prod"])
        # destroy도 backend init이 필요 → 배포 시 쓴 region과 일치해야 state를 찾는다.
        region = ask_text("aws_region", default="ap-northeast-2")
        # 배포와 동일한 계정 접미 규칙으로 default 구성(불일치 시 AccessDenied/301).
        b_default, t_default = llm_tfstate_defaults(aws_account_id())
        bucket = ask_text("tfstate bucket", default=b_default)
        tftable = ask_text("tfstate dynamodb table", default=t_default)
        backend = BackendConfig(bucket=bucket, dynamodb_table=tftable, region=region)
        wf = build_llm_teardown(env=env, backend=backend)
        summary = (
            f"llm-gateway-{env} 의 [bold]모든 terraform 리소스[/bold]"
            " (EKS 클러스터, VPC, Aurora, ElastiCache, Cognito, ECR 등)"
        )
        confirm_token = f"delete llm-gateway-{env}"
        title = f"Teardown LLM Gateway {env}"
    else:  # tool
        if not run_preflight(preflight.TOOL_GW_TOOLS):
            console.print("[red]사전검증 실패[/red] — 누락 도구/인증을 해결하세요.")
            return False
        if not tool_gateway_assets_ok():
            return False
        wf = build_tool_teardown()
        summary = "tool-gateway-dev 의 [bold]모든 terraform 리소스[/bold] (Gateway, Lambda tools, secrets 등)"
        confirm_token = "delete tool-gateway"
        title = "Teardown Tool Gateway"

    _preview_steps(wf)
    console.print(
        f"[bold red]⚠ 파괴적 작업[/bold red] — 삭제 대상: {summary}\n"
        "[red]이 작업은 되돌릴 수 없습니다.[/red]"
    )
    # 이중 확인: 정확한 문구를 타이핑해야 진행
    typed = ask_text(f'확인을 위해 [bold]{confirm_token}[/bold] 를 그대로 입력하세요')
    if typed.strip() != confirm_token:
        console.print("[dim]문구 불일치 — 삭제를 취소합니다[/dim]")
        return False
    return run_and_report(wf, title)


# --------------------------------------------------------------------------- #
# 메인 루프
# --------------------------------------------------------------------------- #
# (label, handler)
MENU = [
    ("LLM Gateway 배포", flow_llm),
    ("Tool Gateway 배포", flow_tool),
    ("전체 배포 (LLM → Tool)", flow_all),
    ("스택 삭제 (Teardown)", flow_teardown),
]


def main_menu() -> None:
    banner()
    while True:
        console.print()
        # 화살표 단일선택 — 각 워크플로우 핸들러를 value로.
        # questionary.Choice는 value=None을 title로 대체하므로 종료는 센티널 문자열 사용.
        choices = [(label, handler) for label, handler in MENU]
        choices.append(("종료", "__exit__"))
        handler = ask_select("워크플로우 선택 (↑↓ 이동, Enter 선택)", choices)
        if handler == "__exit__":
            console.print("[dim]bye[/dim]")
            return
        try:
            handler()
        except Cancelled:
            console.print("\n[dim]취소됨 — 메뉴로 돌아갑니다[/dim]")


def main() -> None:
    try:
        main_menu()
    except (Cancelled, KeyboardInterrupt, EOFError):
        console.print("\n[dim]bye[/dim]")


if __name__ == "__main__":
    main()
