# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""managed-settings 위치 — OS 별 경로와 macOS 의 예전 위치(/etc/claude-code) 처리.

회귀 대상 결함: `_managed_dir()` 가 Windows 가 아니면 전부 /etc/claude-code 를 돌려줘서,
macOS 에서 `gateway-cli setup` 은 성공을 보고하고 `status` 도 [ON] 이었지만 Claude Code 는
그 설정을 읽지 않았다. Claude Code 의 managed 디렉터리는 OS 마다 다르다
(https://code.claude.com/docs/en/managed-settings.md) — macOS
`/Library/Application Support/ClaudeCode/`, Linux·WSL `/etc/claude-code/`,
Windows `C:\\Program Files\\ClaudeCode\\`.

격리: 어떤 테스트도 실제 sudo 를 띄우거나 /etc, /Library 에 쓰지 않는다. `sys.platform` 을
바꾸고 `subprocess.run` 을 `_FakeSudo` 로 바꾼다. 파일을 실제로 만들고 지우는 테스트는 두
경로 헬퍼(`_managed_dir`, `_legacy_managed_file`)를 tmp 아래로 옮긴다(`mac_root`). 원래
함수가 돌려준 경로를 tmp 밑에 그대로 붙이는 방식이라, 출력에 찍힌 경로의 꼬리로 macOS 분기를
실제로 탔는지 확인할 수 있다 — 헬퍼를 tmp 상수로 바꿔치기하면 그 확인이 불가능하다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import managed
from cli.main import cli
from cli.managed import (
    GATEWAY_SETTINGS_FILENAME,
    GatewayWriteResult,
    build_gateway_settings,
    find_legacy_gateway_settings,
    is_gateway_enabled,
    read_gateway_settings,
    remove_gateway_settings,
    write_gateway_settings,
)

# 기대값은 코드에서 가져오지 않고 문서대로 적는다 — 코드가 틀리면 같이 틀리면 안 되므로.
MAC_DIR = "/Library/Application Support/ClaudeCode/managed-settings.d"
MAC_FILE = f"{MAC_DIR}/{GATEWAY_SETTINGS_FILENAME}"
LINUX_DIR = "/etc/claude-code/managed-settings.d"
LINUX_FILE = f"{LINUX_DIR}/{GATEWAY_SETTINGS_FILENAME}"
WIN_DIR = r"C:\Program Files\ClaudeCode\managed-settings.d"

_ARGS = {
    "gateway_url": "https://gw.example.com",
    "admin_api_url": "https://gw.example.com:8080",
    "api_key_helper_path": "/usr/local/bin/api-key-helper",
}


class _FakeSudo:
    """`subprocess.run` 대역 — sudo 를 띄우지 않고 받은 argv 를 기록한다.

    root 를 주면 그 명령이 했을 파일 조작을 root 아래에서만 대신 수행한다. 경로가 root
    밖이면(경로 헬퍼 패치가 빠진 테스트) 아무것도 건드리지 않고 실패한다.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self.calls: list[tuple[list[str], dict]] = []

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]

    def __call__(self, argv, **kwargs):
        # 문자열 명령이나 shell=True 는 공백 든 macOS 경로를 쪼갠다 — 모든 호출에서 막는다.
        assert isinstance(argv, list), f"argv 가 리스트가 아니다: {argv!r}"
        assert not kwargs.get("shell", False), f"shell=True 로 호출됐다: {argv!r}"
        self.calls.append((argv, kwargs))
        if self.root is not None:
            self._perform(argv, kwargs)
        return subprocess.CompletedProcess(argv, 0)

    def _perform(self, argv: list[str], kwargs: dict) -> None:
        sudo, cmd, *args = argv
        assert sudo == "sudo", argv
        path = Path(args[-1])
        assert path.is_relative_to(self.root), f"tmp 밖 경로를 건드리려 했다: {path}"
        if cmd == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
        elif cmd == "tee":
            path.write_bytes(kwargs["input"])
        elif cmd == "chmod":
            path.chmod(int(args[0], 8))
        elif cmd == "rm":
            path.unlink(missing_ok=True)
        elif cmd != "chown":  # root 소유로 바꾸는 건 일반 사용자로 불가 — argv 만 검증한다.
            raise AssertionError(f"예상하지 못한 명령: {argv}")


@pytest.fixture()
def mac_root(tmp_path: Path, monkeypatch) -> _FakeSudo:
    """macOS 를 흉내 내고, 두 경로 헬퍼를 tmp/root 아래로 옮기고, 가짜 sudo 를 건다."""
    monkeypatch.setattr(sys, "platform", "darwin")
    # 설정 파일도 개발자 머신의 실제 config.yaml 대신 없는 tmp 경로를 보게 한다.
    monkeypatch.setenv("GATEWAY_CLI_CONFIG", str(tmp_path / "no-config.yaml"))
    root = tmp_path / "root"
    real_dir, real_legacy = managed._managed_dir, managed._legacy_managed_file
    monkeypatch.setattr(managed, "_managed_dir", lambda: root / real_dir().relative_to("/"))
    monkeypatch.setattr(
        managed, "_legacy_managed_file", lambda: root / real_legacy().relative_to("/")
    )
    sudo = _FakeSudo(root)
    monkeypatch.setattr(subprocess, "run", sudo)
    return sudo


def _plant(path: Path) -> Path:
    """실제 빌더 산출물을 path 에 심는다(이전 버전이 남긴 파일, 또는 'ON' 상태)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_gateway_settings(**_ARGS)), encoding="utf-8")
    return path


def _setup():
    with patch("cli.setup.resolve_helper_path", return_value=_ARGS["api_key_helper_path"]):
        return CliRunner().invoke(
            cli, ["setup", "--gateway-url", _ARGS["gateway_url"]], catch_exceptions=False
        )


def _run(command: str):
    return CliRunner().invoke(cli, [command], catch_exceptions=False)


class TestManagedDirPerOS:
    @pytest.mark.parametrize(
        "platform, expected_dir",
        [("darwin", MAC_DIR), ("linux", LINUX_DIR), ("win32", WIN_DIR)],
    )
    def test_managed_dir_and_file(self, platform, expected_dir, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        assert managed._managed_dir() == Path(expected_dir)
        assert managed._managed_file() == Path(expected_dir) / GATEWAY_SETTINGS_FILENAME

    def test_legacy_path_exists_only_on_macos(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        assert managed._legacy_managed_file() == Path(LINUX_FILE)

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_no_legacy_path_elsewhere(self, platform, monkeypatch) -> None:
        """Linux/WSL 은 /etc/claude-code 가 지금의 정상 위치라 '예전 파일' 이 아니다."""
        monkeypatch.setattr(sys, "platform", platform)
        assert managed._legacy_managed_file() is None
        assert find_legacy_gateway_settings() is None


class TestMacOSWriteArgv:
    def test_write_targets_library_with_one_argv_element_per_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        # 예전 위치는 tmp 의 없는 경로로 — 개발자 Mac 의 실제 /etc 파일 유무에 흔들리지 않게.
        monkeypatch.setattr(managed, "_legacy_managed_file", lambda: tmp_path / "absent.json")
        sudo = _FakeSudo()  # 기록만 한다 — 실제 /Library 경로를 받아도 아무것도 쓰지 않는다.
        monkeypatch.setattr(subprocess, "run", sudo)

        result = write_gateway_settings(**_ARGS)

        assert result == GatewayWriteResult(Path(MAC_FILE), legacy_removed=None)
        assert sudo.argvs == [
            ["sudo", "mkdir", "-p", MAC_DIR],
            ["sudo", "tee", MAC_FILE],
            ["sudo", "chmod", "644", MAC_FILE],
            ["sudo", "chown", "root:wheel", MAC_FILE],
        ]
        for argv, kwargs in sudo.calls:
            assert kwargs.get("shell", False) is False
            # 공백 든 경로가 정확히 한 원소 — 쪼개졌다면 'Application' 만 든 원소가 생긴다.
            assert sum(" " in arg for arg in argv) == 1, argv
        tee_input = sudo.calls[1][1]["input"]
        assert json.loads(tee_input)["env"]["ANTHROPIC_BASE_URL"] == _ARGS["gateway_url"]


class TestOtherPlatformsUnchanged:
    def test_linux_write_runs_the_same_four_commands(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        sudo = _FakeSudo()  # 기록만 한다
        monkeypatch.setattr(subprocess, "run", sudo)

        result = write_gateway_settings(**_ARGS)

        assert result == GatewayWriteResult(Path(LINUX_FILE), legacy_removed=None)
        assert sudo.argvs == [
            ["sudo", "mkdir", "-p", LINUX_DIR],
            ["sudo", "tee", LINUX_FILE],
            ["sudo", "chmod", "644", LINUX_FILE],
            ["sudo", "chown", "root:root", LINUX_FILE],
        ]

    def test_linux_remove_runs_one_rm_and_none_when_absent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        managed_dir = tmp_path / "managed-settings.d"
        monkeypatch.setattr(managed, "_managed_dir", lambda: managed_dir)
        sudo = _FakeSudo(tmp_path)
        monkeypatch.setattr(subprocess, "run", sudo)
        target = _plant(managed_dir / GATEWAY_SETTINGS_FILENAME)

        assert remove_gateway_settings() is True
        assert sudo.argvs == [["sudo", "rm", "-f", str(target)]]
        assert not target.exists()

        sudo.calls.clear()
        assert remove_gateway_settings() is False
        assert sudo.calls == []  # 지울 게 없으면 sudo 를 띄우지 않는다

    def test_windows_write_and_remove_run_no_subprocess(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        # ⚠️ 반드시 tmp 로 돌린다 — 안 그러면 POSIX 호스트에서는 `C:\Program Files\...` 가
        # 현재 디렉터리 아래 상대 경로로 만들어진다.
        managed_dir = tmp_path / "ClaudeCode" / "managed-settings.d"
        monkeypatch.setattr(managed, "_managed_dir", lambda: managed_dir)
        sudo = _FakeSudo(tmp_path)
        monkeypatch.setattr(subprocess, "run", sudo)

        result = write_gateway_settings(**_ARGS)

        assert result == GatewayWriteResult(managed_dir / GATEWAY_SETTINGS_FILENAME, None)
        assert json.loads(result.path.read_text(encoding="utf-8"))["apiKeyHelper"] == (
            _ARGS["api_key_helper_path"]
        )
        assert remove_gateway_settings() is True
        assert not result.path.exists()
        assert sudo.calls == []


class TestMacOSLegacyFile:
    def test_legacy_file_alone_is_not_enabled(self, mac_root: _FakeSudo) -> None:
        """예전 위치의 파일은 Claude Code 가 안 읽으므로 '켜짐' 으로 세면 안 된다."""
        legacy = _plant(managed._legacy_managed_file())

        assert is_gateway_enabled() is False
        assert read_gateway_settings() is None
        assert find_legacy_gateway_settings() == legacy

    def test_write_creates_new_file_then_removes_legacy(self, mac_root: _FakeSudo) -> None:
        legacy = _plant(managed._legacy_managed_file())

        result = write_gateway_settings(**_ARGS)

        assert result.path == managed._managed_file()
        assert str(result.path).endswith(MAC_FILE)
        assert result.legacy_removed == legacy
        assert result.path.is_file()
        assert not legacy.exists()
        assert is_gateway_enabled() is True
        # 지우기는 새 파일을 다 쓴 *뒤* 마지막 한 번이고, 같은 sudo 경로를 쓴다.
        assert mac_root.argvs[-1] == ["sudo", "rm", "-f", str(legacy)]
        assert [argv[1] for argv in mac_root.argvs].count("rm") == 1

    def test_write_without_legacy_runs_no_rm(self, mac_root: _FakeSudo) -> None:
        result = write_gateway_settings(**_ARGS)

        assert result.legacy_removed is None
        assert "rm" not in [argv[1] for argv in mac_root.argvs]

    def test_remove_removes_both(self, mac_root: _FakeSudo) -> None:
        new = _plant(managed._managed_file())
        legacy = _plant(managed._legacy_managed_file())

        assert remove_gateway_settings() is True
        assert not new.exists()
        assert not legacy.exists()

    def test_remove_reports_removed_when_only_legacy_existed(self, mac_root: _FakeSudo) -> None:
        legacy = _plant(managed._legacy_managed_file())

        assert remove_gateway_settings() is True
        assert not legacy.exists()
        assert mac_root.argvs == [["sudo", "rm", "-f", str(legacy)]]


class TestMacOSCommands:
    """setup / status / disable 를 macOS 흉내 상태에서 CliRunner 로 끝까지 태운다."""

    def test_setup_writes_library_file_and_removes_legacy(self, mac_root: _FakeSudo) -> None:
        legacy = _plant(managed._legacy_managed_file())

        result = _setup()

        assert result.exit_code == 0, result.output
        new = managed._managed_file()
        assert f"Gateway enabled: {new}" in result.output
        assert MAC_FILE in result.output
        assert new.is_file()
        assert not legacy.exists()
        # 예전 파일은 켜짐이 아니었으므로 '이미 켜져 있음' 이라고 하면 안 된다.
        assert "already enabled" not in result.output.lower()
        # 지운 사실과 이유를 한 줄로 알린다. stdout 만 센다 — output 에는 stderr 의
        # structlog JSON 줄(legacy_managed_settings_removed)도 섞여 들어온다.
        legacy_lines = [line for line in result.stdout.splitlines() if str(legacy) in line]
        assert len(legacy_lines) == 1, result.stdout
        assert "macOS" in legacy_lines[0]
        assert ["sudo", "chown", "root:wheel", str(new)] in mac_root.argvs

    def test_setup_without_legacy_prints_no_legacy_line(self, mac_root: _FakeSudo) -> None:
        result = _setup()

        assert result.exit_code == 0, result.output
        assert MAC_FILE in result.output
        assert "/etc/claude-code" not in result.output

    def test_status_legacy_only_is_off_and_says_rerun_setup(self, mac_root: _FakeSudo) -> None:
        legacy = _plant(managed._legacy_managed_file())

        result = _run("status")

        assert result.exit_code == 0, result.output
        assert "[OFF]" in result.output
        assert "[ON]" not in result.output
        assert f"Managed settings: {managed._managed_file()}" in result.output
        assert MAC_FILE in result.output
        assert str(legacy) in result.output
        assert "gateway-cli setup" in result.output

    def test_status_on_mentions_that_legacy_file_is_ignored(self, mac_root: _FakeSudo) -> None:
        _plant(managed._managed_file())
        legacy = _plant(managed._legacy_managed_file())

        result = _run("status")

        assert result.exit_code == 0, result.output
        assert "[ON]" in result.output
        assert MAC_FILE in result.output
        legacy_line = result.output.index(str(legacy))
        assert "무시" in result.output[legacy_line:]

    def test_disable_removes_both_and_prints_paths(self, mac_root: _FakeSudo) -> None:
        new = _plant(managed._managed_file())
        legacy = _plant(managed._legacy_managed_file())

        result = _run("disable")

        assert result.exit_code == 0, result.output
        assert "disabled" in result.output.lower()
        assert f"Removed: {new}" in result.output
        assert MAC_FILE in result.output
        assert f"Removed: {legacy}" in result.output
        assert not new.exists()
        assert not legacy.exists()

    def test_disable_removes_legacy_only_so_status_stops_asking_for_setup(
        self, mac_root: _FakeSudo
    ) -> None:
        """남겨 두면 disable 직후 status 가 'setup 을 다시 실행하라' 고 정반대로 안내한다."""
        legacy = _plant(managed._legacy_managed_file())

        result = _run("disable")

        assert result.exit_code == 0, result.output
        assert "not currently enabled" not in result.output
        assert f"Removed: {legacy}" in result.output
        # 새 위치에는 처음부터 파일이 없었다 — 지웠다고 말하면 안 된다.
        assert f"Removed: {managed._managed_file()}" not in result.output
        assert not legacy.exists()
        assert str(legacy) not in _run("status").output

    def test_disable_with_nothing_present_runs_no_sudo(self, mac_root: _FakeSudo) -> None:
        result = _run("disable")

        assert result.exit_code == 0, result.output
        assert "not currently enabled" in result.output
        assert mac_root.calls == []
