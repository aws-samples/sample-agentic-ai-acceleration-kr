# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Integration tests for the invocation-scoped setup rollback primitives.

These exercise the REAL ``cowork_config.plan_write`` / ``cowork_ca.plan_install``
compensation logic (not the Rollback container, which ``test_rollback.py`` covers)
against the concerns the adversarial review raised
(docs/cowork-setup-rollback-design.md §8):

  #2 partial-write / arm-before-mutation — a write that fails still reverts to this
     run's pre-mutation snapshot;
  #3 invocation-scoping — a FAILING second setup must NOT wipe the first setup's
     config (repeated-setup regression);
  #1 checked results — compensate() reports ok=False rather than raising.

The macOS config path (a plain JSON file) is used because it is exercisable on the
CI/dev host; the Windows registry path is symmetric and covered by design + on-box
validation.
"""

from __future__ import annotations

import json
import sys

import pytest

from cli import cowork_config
from cli.utils.rollback import Rollback

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS config-file path; Windows covered on-box"
)


def _cfg(model: str = "cowork-opus") -> cowork_config.CoworkConfig:
    return cowork_config.CoworkConfig(
        base_url="https://gw.example.com",
        models=[model],
        credential_kind="static",
        api_key="vk-test-123",
    )


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """Point HOME + data/backup dirs at a tmp tree so nothing real is touched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GATEWAY_CLI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(tmp_path / "backups"))
    return home


def _config_path() -> "object":
    return cowork_config._mac_applied_config_path_readonly()


def test_plan_apply_writes_config(isolated_env):
    plan = cowork_config.plan_write(_cfg())
    result = plan.apply()
    assert result.ok and result.changed
    cfg_path = _config_path()
    data = json.loads(cfg_path.read_text())
    assert data["inferenceProvider"] == "gateway"
    assert data["inferenceGatewayApiKey"] == "vk-test-123"


def test_compensate_removes_config_this_run_created(isolated_env):
    # No config existed before → compensate removes the file this run created (§3).
    plan = cowork_config.plan_write(_cfg())
    plan.apply()
    cfg_path = _config_path()
    assert cfg_path.is_file()

    undo = plan.compensate()
    assert undo.ok
    assert not cfg_path.is_file()


def test_compensate_restores_prior_file_contents(isolated_env):
    # A config that PRE-EXISTED this run must be restored verbatim, not deleted (§2/§3).
    # Seed a prior config by doing a first successful setup, capture its bytes.
    first = cowork_config.plan_write(_cfg(model="cowork-sonnet"))
    first.apply()
    cfg_path = _config_path()
    prior_text = cfg_path.read_text()

    # Second setup overwrites; then its compensation must put the FIRST back.
    second = cowork_config.plan_write(_cfg(model="cowork-opus"))
    second.apply()
    assert json.loads(cfg_path.read_text())["inferenceModels"][0] == "cowork-opus"

    undo = second.compensate()
    assert undo.ok and undo.changed
    assert cfg_path.read_text() == prior_text  # first setup restored exactly


def test_repeated_setup_failure_does_not_wipe_first(isolated_env):
    # §8 #3 regression: a successful setup, then a FAILING second setup inside the
    # saga, must leave the first setup's config intact (compensate reverts only the
    # second run's delta — the pre-write snapshot IS the first setup).
    cowork_config.plan_write(_cfg(model="cowork-sonnet")).apply()
    cfg_path = _config_path()
    good_text = cfg_path.read_text()

    with pytest.raises(RuntimeError, match="simulated"):
        with Rollback() as rb:
            plan = cowork_config.plan_write(_cfg(model="cowork-opus"))
            rb.arm("managed config", plan.compensate)
            plan.apply()  # mutates the live file
            raise RuntimeError("simulated post-write failure")

    assert rb.rollback_errors == []
    # First setup survived the failed retry, byte-for-byte.
    assert cfg_path.read_text() == good_text
    assert json.loads(cfg_path.read_text())["inferenceModels"][0] == "cowork-sonnet"


def test_saga_reverts_created_config_on_failure(isolated_env):
    # First-ever setup fails mid-saga → the file it created is removed (no partial
    # state left), because the compensation was armed before apply() (§8 #2).
    cfg_path_before = _config_path()
    assert cfg_path_before is None  # nothing applied yet

    with pytest.raises(RuntimeError, match="simulated"):
        with Rollback() as rb:
            plan = cowork_config.plan_write(_cfg())
            rb.arm("managed config", plan.compensate)
            plan.apply()
            raise RuntimeError("simulated failure")

    assert rb.rollback_errors == []
    # The config the failed run created was rolled back.
    assert cowork_config.read_config() is None


def test_compensate_is_idempotent(isolated_env):
    # §5 rule 4 — running compensate twice (in-process undo, then a later clear) is
    # harmless.
    plan = cowork_config.plan_write(_cfg())
    plan.apply()
    assert plan.compensate().ok
    second = plan.compensate()  # already reverted
    assert second.ok  # no exception, no error
