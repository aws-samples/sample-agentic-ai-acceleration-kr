"""Render the installer's screens to PNG for docs/installer-guide.md.

Runs the real Textual app headless (``App.run_test``) against canned probe
results and a fake command runner, so every capture is deterministic and no AWS
call is made. Textual exports SVG; Chrome turns it into PNG because cairosvg has
no per-glyph font fallback and draws Hangul as boxes.

    installer/.venv/bin/python -m installer.tools.screenshots            # all
    installer/.venv/bin/python -m installer.tools.screenshots dashboard  # one

Output: docs/images/installer/<name>.png (and the .svg next to it).
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from installer.core import env as envmod
from installer.core import probe, values
from installer.core.probe import State
from installer.core.runner import Command, Result

REPO_ROOT = envmod.REPO_ROOT
OUT_DIR = REPO_ROOT / "docs" / "images" / "installer"
SIZE = (112, 34)           # terminal columns × rows
SCALE = 2                  # device scale factor for crisp text
ACCOUNT = "123456789012"   # never a real account id
# Commands embed the checkout path (terraform -chdir=...). The app is pointed at
# a short symlink so captures do not carry this machine's directory layout.
SHOWN_ROOT = Path("/tmp/agent-platform-on-agentcore")

_FONT_FACE = re.compile(r"@font-face\s*\{.*?\}", re.S)
# Rich's SVG export sets textLength from len(text), so a Hangul run (2 cells per
# glyph) is squeezed into half its width. Recomputed from cell_len below.
_TEXT = re.compile(r'(<text\b[^>]*?\btextLength=")([\d.]+)("[^>]*>)(.*?)(</text>)', re.S)

SAMPLE_TFVARS = """\
admin_email    = "admin@example.com"
admin_password = "ChangeMe123!"
user_email     = "user@example.com"
user_password  = "ChangeMe123!"
"""

OUTPUTS = {
    "alb_url": "http://bap-alb-1234567890.ap-northeast-1.elb.amazonaws.com",
    "cognito_user_pool_id": "ap-northeast-1_AbCdEfGhI",
    "cognito_client_id": "4h9clientidexample0123456",
    "agent_registry_id": "bap-registry-k3j2h1",
    "artifacts_bucket": "bap-artifacts-ap-northeast-1",
    "artifacts_table": "bap-artifacts",
    "skills_bucket": "bap-skills-ap-northeast-1",
    "knowledge_bucket": "bap-kb-source-ap-northeast-1",
    "knowledge_table": "bap-knowledge-bases",
    "kb_service_role_arn": f"arn:aws:iam::{ACCOUNT}:role/bap-kb-service",
    "kb_gateway_role_arn": f"arn:aws:iam::{ACCOUNT}:role/bap-kb-gateway",
    "harness_execution_role_arn": f"arn:aws:iam::{ACCOUNT}:role/bap-harness-exec",
    "mcp_gateway_url": "https://bap-tools-abc123.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp",
    "cluster_name": "bap-cluster",
    "ecr_repository_urls": {
        "server": f"{ACCOUNT}.dkr.ecr.ap-northeast-1.amazonaws.com/bap/server",
        "web": f"{ACCOUNT}.dkr.ecr.ap-northeast-1.amazonaws.com/bap/web",
    },
    "usage_table_name": "bap-usage",
    "guardrail_id": "gr1abcdefgh2",
    "guardrail_version": "1",
}

TS_PENDING = (
    "Transaction Search 를 켜면 trace 패널이 작동합니다.\n\n"
    "계정 ID 를 확인한 후 아래 명령을 붙여넣으세요:\n\n"
    "aws xray update-trace-segment-destination --destination CloudWatchLogs\n"
    "aws xray update-indexing-rule --name Default \\\n"
    "  --rule '{\"Probabilistic\":{\"DesiredSamplingPercentage\":10}}'"
)

# (state, detail) per step, for the three dashboard moments the guide shows.
FRESH = {
    "preflight": (State.DONE, ""),
    "bootstrap": (State.PENDING, "state 버킷이 없습니다"),
    "backend": (State.PENDING, "backend.hcl 이 없습니다"),
    "transaction_search": (State.PENDING, TS_PENDING),
    "tfvars": (State.PENDING, "비어 있음: admin_email, admin_password, user_email, user_password"),
    "init": (State.PENDING, "terraform init 이 필요합니다"),
    "first_apply": (State.PENDING, "ECR 리포지토리 output 이 비어 있습니다"),
    "images": (State.PENDING, "server 리포지토리가 없습니다"),
    "second_apply": (State.PENDING, "ECS 서비스를 찾을 수 없습니다"),
    "memory": (State.PENDING, "AgentCore Memory 가 없습니다"),
    "local_env": (State.PENDING, ".env 이 없습니다"),
}
MIDWAY = {
    **FRESH,
    "bootstrap": (State.DONE, ""),
    "backend": (State.DONE, ""),
    "transaction_search": (State.DONE, ""),
    "tfvars": (State.DONE, ""),
    "init": (State.DONE, ""),
}
MIDWAY_UNKNOWN = {
    **MIDWAY,
    "first_apply": (State.UNKNOWN, "판정 불가 (ExpiredToken)"),
}
DONE = {step: (State.DONE, "") for step in FRESH}
DONE["memory"] = (State.DONE, "bap_conversations_default-AbCdEf1234")

APPLY_LINES = [
    "module.network.aws_vpc.this: Creating...",
    "module.ecr.aws_ecr_repository.repo[\"server\"]: Creating...",
    "module.ecr.aws_ecr_repository.repo[\"web\"]: Creating...",
    "module.cognito.aws_cognito_user_pool.this: Creating...",
    "module.ecr.aws_ecr_repository.repo[\"server\"]: Creation complete after 1s [id=bap/server]",
    "module.ecr.aws_ecr_repository.repo[\"web\"]: Creation complete after 1s [id=bap/web]",
    "module.network.aws_vpc.this: Creation complete after 3s [id=vpc-0a1b2c3d4e5f67890]",
    "module.network.aws_subnet.public[0]: Creating...",
    "module.network.aws_subnet.public[1]: Creating...",
    "module.network.aws_nat_gateway.this: Creating...",
    "module.network.aws_nat_gateway.this: Still creating... [1m30s elapsed]",
    "module.network.aws_nat_gateway.this: Creation complete after 1m42s [id=nat-0f1e2d3c4b5a69788]",
    "module.ecs.aws_lb.this: Creating...",
    "module.ecs.aws_lb.this: Still creating... [2m10s elapsed]",
    "module.ecs.aws_lb.this: Creation complete after 2m31s [id=arn:aws:elasticloadbalancing:...]",
    "module.mcp_gateway.aws_bedrockagentcore_gateway.this: Creating...",
    "module.mcp_gateway.aws_bedrockagentcore_gateway.this: Creation complete after 4s",
    "module.agent_registry.null_resource.registry: Provisioning with 'local-exec'...",
    "module.agent_registry.null_resource.registry: Creation complete after 6s",
    "",
    "Apply complete! Resources: 136 added, 0 changed, 0 destroyed.",
    "",
    "Outputs:",
    "",
    f"alb_url = \"{OUTPUTS['alb_url']}\"",
    "ecr_repository_urls = {",
    f"  \"server\" = \"{OUTPUTS['ecr_repository_urls']['server']}\"",
    f"  \"web\" = \"{OUTPUTS['ecr_repository_urls']['web']}\"",
    "}",
]

LOCK_LINES = [
    "Initializing the backend...",
    "╷",
    "│ Error: Error acquiring the state lock",
    "│ ",
    "│ Error message: operation error DynamoDB: PutItem, ConditionalCheckFailedException:",
    "│ The conditional request failed",
    "│ Lock Info:",
    "│   ID:        b7a9c1e2-3f4d-5a6b-7c8d-9e0f1a2b3c4d",
    "│   Path:      bap-tfstate-123456789012/standalone/terraform.tfstate",
    "│   Operation: OperationTypeApply",
    "│   Who:       ubuntu@build-host",
    "│   Created:   2026-10-06 07:41:12.3 +0000 UTC",
    "╵",
]


class FakeShell:
    """Answers the read-only commands the screens issue, without AWS."""

    async def capture(self, cmd: Command) -> tuple[int, str]:
        shown = cmd.display()
        if "get-caller-identity" in shown:
            return 0, ACCOUNT
        if "output -json" in shown:
            return 0, json.dumps({k: {"value": v} for k, v in OUTPUTS.items()})
        if "plan -destroy" in shown:
            return 0, "Plan: 0 to add, 0 to change, 136 to destroy."
        return 1, "fake shell: no canned answer"


class FakeRunner:
    """Streams canned lines instead of spawning terraform."""

    def __init__(self, lines: list[str], exit_code: int = 0) -> None:
        self.lines = lines
        self.exit_code = exit_code

    async def run(self, cmd: Command, step_id: str, on_line) -> Result:
        for line in self.lines:
            on_line(line)
        return Result(
            exit_code=self.exit_code,
            cancelled=False,
            log_path=Path("installer/.logs/20261006-074112-" + step_id + ".log"),
            tail=tuple(self.lines[-40:]),
        )

    async def cancel(self) -> None:  # pragma: no cover - never cancelled here
        return None


def _fake_refresh(app, scenario: dict):
    async def refresh_states():
        for step_id, (state, detail) in scenario.items():
            if step_id in app.states:
                app.states[step_id] = state
                app.details[step_id] = detail
        return app.states
    return refresh_states


def _make_app(scenario: dict, *, dry_run: bool = False, runner=None):
    from installer.app import InstallerApp

    app = InstallerApp(env="standalone", dry_run=dry_run)
    app.shell = FakeShell()
    app.refresh_states = _fake_refresh(app, scenario)
    if runner is not None:
        app.runner = runner
    return app


def _fix_text_lengths(svg: str) -> str:
    """Make every textLength match the cells the terminal gave the text."""
    import html
    from rich.cells import cell_len

    cell_width = None
    for m in _TEXT.finditer(svg):
        text = html.unescape(m.group(4))
        if text and text.isascii() and len(text) == cell_len(text):
            cell_width = float(m.group(2)) / len(text)
            break
    if cell_width is None:
        return svg

    def fix(m: re.Match) -> str:
        text = html.unescape(m.group(4))
        cells = cell_len(text)
        if cells == 0:
            return m.group(0)
        return f"{m.group(1)}{cells * cell_width:.1f}{m.group(3)}{m.group(4)}{m.group(5)}"

    return _TEXT.sub(fix, svg)


def _save(app, name: str) -> Path:
    svg = app.export_screenshot(title=f"install.sh — {name}")
    svg = _FONT_FACE.sub("", svg)  # no CDN fetch; local fonts only
    svg = svg.replace('"Fira Code"', '"NanumGothicCoding", "Fira Code", monospace')
    svg = _fix_text_lengths(svg)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = OUT_DIR / f"{name}.svg"
    svg_path.write_text(svg)
    return svg_path


def _to_png(svg_path: Path) -> Path:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if chrome is None:
        raise SystemExit("Chrome/Chromium is required to rasterise the SVG captures")
    svg = svg_path.read_text()
    width, height = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups()
    w, h = int(float(width)), int(float(height))
    png_path = svg_path.with_suffix(".png")
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [
                chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                "--hide-scrollbars", f"--user-data-dir={profile}",
                f"--force-device-scale-factor={SCALE}",
                f"--window-size={w},{h}",
                f"--screenshot={png_path}",
                svg_path.as_uri(),
            ],
            check=True, capture_output=True, timeout=120,
        )
    return png_path


# ── scenes ────────────────────────────────────────────────────────────────────

async def scene_dashboard_fresh():
    from textual.widgets import DataTable
    app = _make_app(FRESH)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#steps", DataTable)
        table.move_cursor(row=1)
        app.screen._show_detail()
        await pilot.pause()
        return _save(app, "01-dashboard-fresh")


async def scene_dashboard_transaction_search():
    from textual.widgets import DataTable
    app = _make_app(FRESH)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#steps", DataTable)
        table.move_cursor(row=3)
        app.screen._show_detail()
        await pilot.pause()
        return _save(app, "02-dashboard-transaction-search")


async def _settings(tab: str, name: str, *, with_error: bool = False):
    from textual.widgets import Input, TabbedContent
    app = _make_app(FRESH)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await app.push_screen("settings")
        await pilot.pause()
        screen = app.screen
        screen.query_one(TabbedContent).active = tab
        if with_error:
            screen.query_one("#in-user_password", Input).value = "short"
        await pilot.pause()
        await pilot.pause()
        return _save(app, name)


async def scene_settings_required():
    return await _settings("required", "03-settings-required", with_error=True)


async def scene_settings_advanced():
    return await _settings("advanced", "04-settings-advanced")


async def scene_settings_derived():
    return await _settings("derived", "05-settings-derived")


async def scene_dashboard_midway():
    from textual.widgets import DataTable
    app = _make_app(MIDWAY)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#steps", DataTable)
        table.move_cursor(row=6)
        app.screen._show_detail()
        await pilot.pause()
        return _save(app, "06-dashboard-midway")


async def scene_log_first_apply():
    from installer.screens.logs import LogScreen
    app = _make_app(MIDWAY, runner=FakeRunner(APPLY_LINES))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        # The apply "succeeds": the probe afterwards reports the step done.
        app.refresh_states = _fake_refresh(app, {**MIDWAY, "first_apply": (State.DONE, "")})
        await app.push_screen(LogScreen(step_id="first_apply"))
        await pilot.pause()
        await pilot.pause()
        return _save(app, "07-log-first-apply")


async def scene_log_failure():
    from installer.screens.logs import LogScreen
    app = _make_app(MIDWAY, runner=FakeRunner(LOCK_LINES, exit_code=1))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await app.push_screen(LogScreen(step_id="first_apply"))
        await pilot.pause()
        await pilot.pause()
        return _save(app, "08-log-failure-state-lock")


async def scene_dashboard_unknown():
    from textual.widgets import DataTable
    app = _make_app(MIDWAY_UNKNOWN)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#steps", DataTable)
        table.move_cursor(row=6)
        app.screen._show_detail()
        await pilot.pause()
        return _save(app, "09-dashboard-unknown")


async def scene_dashboard_done():
    from textual.widgets import DataTable
    app = _make_app(DONE)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#steps", DataTable)
        table.move_cursor(row=10)
        app.screen._show_detail()
        await pilot.pause()
        return _save(app, "10-dashboard-done")


async def scene_operations():
    app = _make_app(DONE)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await app.push_screen("operations")
        await pilot.pause()
        return _save(app, "11-operations")


async def scene_confirm_destroy():
    from installer.screens.confirm import ConfirmScreen
    app = _make_app(DONE)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await app.push_screen("operations")
        await pilot.pause()
        app.push_screen(ConfirmScreen(
            prompt="'destroy — 환경 전체 삭제' 를 실행합니다.",
            expected="bap",
            extra="Plan: 0 to add, 0 to change, 136 to destroy.",
        ))
        await pilot.pause()
        return _save(app, "12-confirm-destroy")


async def scene_dry_run():
    from installer.screens.logs import LogScreen
    app = _make_app(MIDWAY, dry_run=True)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await app.push_screen(LogScreen(step_id="first_apply"))
        await pilot.pause()
        await pilot.pause()
        return _save(app, "13-dry-run")


SCENES = {
    "dashboard": scene_dashboard_fresh,
    "transaction-search": scene_dashboard_transaction_search,
    "settings-required": scene_settings_required,
    "settings-advanced": scene_settings_advanced,
    "settings-derived": scene_settings_derived,
    "midway": scene_dashboard_midway,
    "log-apply": scene_log_first_apply,
    "log-failure": scene_log_failure,
    "unknown": scene_dashboard_unknown,
    "done": scene_dashboard_done,
    "operations": scene_operations,
    "confirm": scene_confirm_destroy,
    "dry-run": scene_dry_run,
}


def _isolate_tfvars(tmp: Path) -> None:
    """Point every tfvars read/write at a temp file so the repo is untouched."""
    path = tmp / "terraform.tfvars"
    path.write_text(SAMPLE_TFVARS)
    # env.resolve_project/resolve_region read through this module-level
    # function (steps.py has no other indirection), so it must be patched too
    # or the captures show whatever project the developer's real tfvars sets.
    envmod.tfvars_path = lambda env: path
    probe._tfvars_path = lambda env: path
    values._tfvars_path = lambda env: path


def _short_root() -> bool:
    """Run the app from a symlink so -chdir paths read /tmp/agent-platform-on-agentcore/...

    Env.tf_dir reads env.REPO_ROOT at call time, so swapping the module global
    is enough for the commands the dashboard displays. Returns whether a symlink
    was created (and so must be removed).
    """
    if SHOWN_ROOT.is_symlink() and SHOWN_ROOT.resolve() == REPO_ROOT:
        envmod.REPO_ROOT = SHOWN_ROOT
        return False
    if SHOWN_ROOT.exists():
        return False  # something else lives there; captures show the real path
    SHOWN_ROOT.symlink_to(REPO_ROOT)
    envmod.REPO_ROOT = SHOWN_ROOT
    return True


async def main(selected: list[str]) -> None:
    created = _short_root()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _isolate_tfvars(Path(tmp))
            for key in selected or list(SCENES):
                svg_path = await SCENES[key]()
                png_path = _to_png(svg_path)
                svg_path.unlink()  # the PNG is the artefact; the SVG is a 50 KB intermediate
                print(f"{key:20} -> {png_path.relative_to(REPO_ROOT)}")
    finally:
        if created:
            SHOWN_ROOT.unlink()


if __name__ == "__main__":
    unknown = [a for a in sys.argv[1:] if a not in SCENES]
    if unknown:
        raise SystemExit(f"unknown scene(s): {', '.join(unknown)}; choose from {', '.join(SCENES)}")
    asyncio.run(main(sys.argv[1:]))
