"""
Tests for the file-writing gateway.

Every secret the installer collects reaches disk through this module, so the
rules live in one place: refuse to write a secret into a file git would track,
chmod 0600, and back up before overwriting.

The refusal has no exception: every file the installer writes (tfvars,
backend.hcl, .env) is git-ignored, so a write into a tracked file is always a
mistake that would put an account id or password into a commit.
"""
import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import files  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo that ignores *.tfvars, like infra/.gitignore does."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("*.tfvars\n*.bak-*\n.logs/\n")
    (tmp_path / "tracked.tf").write_text('bucket = "REPLACE_ME"\n')
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_ignored_file_is_written(repo):
    target = repo / "terraform.tfvars"
    result = files.write_secret_file(target, 'admin_password = "s3cret"\n')
    assert target.read_text() == 'admin_password = "s3cret"\n'
    assert result.warning is None


def test_written_file_is_0600(repo):
    target = repo / "terraform.tfvars"
    files.write_secret_file(target, "x = 1\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_tracked_file_is_refused(repo):
    """A secret must never land in a file git is tracking."""
    with pytest.raises(files.WriteRefused) as exc:
        files.write_secret_file(repo / "tracked.tf", 'password = "s3cret"\n')
    assert "tracked" in str(exc.value).lower()
    assert "REPLACE_ME" in (repo / "tracked.tf").read_text()   # untouched


def test_untracked_and_unignored_file_is_refused(repo):
    """Neither ignored nor tracked: writing a secret there invites `git add -A`."""
    with pytest.raises(files.WriteRefused):
        files.write_secret_file(repo / "loose.txt", "secret\n")


def test_existing_file_is_backed_up_before_overwrite(repo):
    target = repo / "terraform.tfvars"
    target.write_text("old = 1\n")
    result = files.write_secret_file(target, "new = 2\n")
    assert result.backup is not None
    assert result.backup.read_text() == "old = 1\n"
    assert target.read_text() == "new = 2\n"


def test_no_backup_when_file_is_new(repo):
    result = files.write_secret_file(repo / "terraform.tfvars", "x = 1\n")
    assert result.backup is None


def test_set_keys_hcl_style_preserves_the_rest(repo):
    target = repo / "terraform.tfvars"
    target.write_text('admin_email    = "old@x.com"\n\n# a comment\nregion = "us-east-1"\n')
    files.set_keys(target, {"admin_email": '"new@x.com"'}, style="hcl")
    text = target.read_text()
    assert 'admin_email    = "new@x.com"' in text
    assert "# a comment" in text
    assert 'region = "us-east-1"' in text


def test_set_keys_env_style_uses_no_spaces(repo):
    target = repo / ".env.tfvars"     # named to be ignored by the fixture
    target.write_text("AWS_REGION=us-east-1\nAGENT_REGISTRY_ID=\n")
    files.set_keys(target, {"AGENT_REGISTRY_ID": "reg-123"}, style="env")
    text = target.read_text()
    assert "AGENT_REGISTRY_ID=reg-123" in text
    assert "AWS_REGION=us-east-1" in text


def test_set_keys_creates_the_file_when_absent(repo):
    target = repo / "fresh.tfvars"
    files.set_keys(target, {"region": '"us-east-1"'}, style="hcl")
    assert target.read_text() == 'region = "us-east-1"\n'


def test_is_git_ignored_and_tracked(repo):
    assert files.is_git_ignored(repo / "a.tfvars") is True
    assert files.is_git_ignored(repo / "tracked.tf") is False
    assert files.is_git_tracked(repo / "tracked.tf") is True
    assert files.is_git_tracked(repo / "a.tfvars") is False



def test_backup_is_skipped_when_git_would_track_it(tmp_path):
    """Backups hold the same secrets as the file; an untracked copy next to an
    ignored tfvars defeats the ignore check."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("secret.env\n")        # not the .bak-*
    target = tmp_path / "secret.env"
    target.write_text("A=1\n")

    result = files.write_secret_file(target, "A=2\n")

    assert result.backup is None
    assert result.warning and "백업" in result.warning
    assert not list(tmp_path.glob("secret.env.bak-*"))


def test_backup_is_written_when_the_pattern_is_ignored(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("secret.env\n*.bak-*\n")
    target = tmp_path / "secret.env"
    target.write_text("A=1\n")

    result = files.write_secret_file(target, "A=2\n")

    assert result.backup is not None and result.backup.read_text() == "A=1\n"
    assert result.warning is None
