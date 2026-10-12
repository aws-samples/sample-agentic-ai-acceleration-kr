"""
Tests for the subprocess runner.

These run real subprocesses. Mocking asyncio here would test the mock: the two
behaviours that matter are precisely the ones a mock cannot exhibit.

1. stdout and stderr must arrive interleaved in emission order. Reading them as
   separate streams reorders terraform's output, and out-of-order logs are worse
   than no logs when diagnosing a failed apply.
2. Cancelling must kill the whole process group. `terraform` spawns provider
   plugins as children, so signalling only the parent leaves them running —
   the same problem run.sh solves with kill_tree.
"""
import asyncio
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core.runner import Command, Runner  # noqa: E402


@pytest.fixture
def runner(tmp_path):
    return Runner(log_dir=tmp_path / "logs")


def _collect(runner, cmd, step_id="test"):
    lines: list[str] = []
    result = asyncio.run(runner.run(cmd, step_id, lines.append))
    return result, lines


def test_streams_lines_in_order(runner):
    result, lines = _collect(runner, Command(argv=["sh", "-c", "printf 'a\\nb\\nc\\n'"]))
    assert result.ok
    assert lines == ["a", "b", "c"]


def test_stdout_and_stderr_are_interleaved(runner):
    _, lines = _collect(
        runner, Command(argv=["sh", "-c", "echo one; echo two >&2; echo three"])
    )
    assert lines == ["one", "two", "three"]


def test_nonzero_exit_is_reported_not_raised(runner):
    result, _ = _collect(runner, Command(argv=["sh", "-c", "echo boom >&2; exit 3"]))
    assert result.exit_code == 3
    assert result.ok is False
    assert result.cancelled is False


def test_tail_holds_the_last_lines(runner):
    result, _ = _collect(runner, Command(argv=["sh", "-c", "seq 1 100"]))
    assert result.tail[-1] == "100"
    assert len(result.tail) == 40


def test_full_output_is_written_to_a_log_file(runner):
    result, _ = _collect(runner, Command(argv=["sh", "-c", "seq 1 100"]), step_id="apply")
    assert result.log_path.is_file()
    written = result.log_path.read_text().splitlines()
    assert written[0].startswith("$ ")          # the command, for reproducibility
    assert "1" in written and "100" in written
    assert "apply" in result.log_path.name


def test_log_file_is_0600(runner):
    import stat
    result, _ = _collect(runner, Command(argv=["sh", "-c", "echo hi"]))
    assert stat.S_IMODE(result.log_path.stat().st_mode) == 0o600


def test_cwd_is_honoured(runner, tmp_path):
    result, lines = _collect(runner, Command(argv=["pwd"], cwd=tmp_path))
    assert result.ok
    assert lines[0] == str(tmp_path)


def test_env_overrides_are_applied(runner):
    _, lines = _collect(
        runner, Command(argv=["sh", "-c", "echo $MY_VAR"], env={"MY_VAR": "set"})
    )
    assert lines == ["set"]


def test_missing_executable_is_a_failed_result_not_a_crash(runner):
    result, lines = _collect(runner, Command(argv=["definitely-not-a-real-binary-xyz"]))
    assert result.ok is False
    assert result.exit_code != 0
    assert any("definitely-not-a-real-binary-xyz" in line for line in lines)


def test_cancel_kills_the_whole_process_group(runner):
    """A grandchild must not survive cancellation."""
    child_pids: list[int] = []

    def capture(line):
        if line.startswith("CHILD="):
            child_pids.append(int(line.split("=")[1]))

    async def scenario():
        cmd = Command(argv=["sh", "-c", "sleep 60 & echo CHILD=$!; wait"])
        task = asyncio.create_task(runner.run(cmd, "cancel-test", capture))
        while not child_pids:
            await asyncio.sleep(0.05)
        await runner.cancel()
        return await task

    result = asyncio.run(scenario())
    assert result.cancelled is True
    assert result.ok is False

    import time
    time.sleep(0.3)
    alive = subprocess.run(["ps", "-p", str(child_pids[0])], capture_output=True).returncode == 0
    assert not alive, "grandchild survived cancellation"


def test_command_display_quotes_arguments_with_spaces():
    cmd = Command(argv=["terraform", "apply", "-var", "project=ap x"])
    assert cmd.display() == "terraform apply -var 'project=ap x'"


def test_command_display_leaves_plain_arguments_bare():
    assert Command(argv=["terraform", "init"]).display() == "terraform init"
