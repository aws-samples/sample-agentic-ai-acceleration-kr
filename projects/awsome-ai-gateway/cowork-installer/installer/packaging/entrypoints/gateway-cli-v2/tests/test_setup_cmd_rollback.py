# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""End-to-end CLI wiring test for ``setup``'s rollback saga.

Drives the real ``setup_cmd`` through Click's CliRunner and asserts that when the
config write fails AFTER a CA install succeeded, the CA compensation runs, the
rollback summary is printed, and the ORIGINAL failure is the command's exit status
(§8 #1/#2 + the chosen reporting model). All OS mutation is stubbed — this test is
about the wiring, not the trust store / registry.
"""

from __future__ import annotations

from click.testing import CliRunner

from cli import cowork_ca, cowork_config, main


def test_setup_rolls_back_ca_when_config_write_fails(monkeypatch):
    events: list[str] = []

    # --- stub the CA plan: apply() "installs" (changed=True), compensate() records.
    class FakeCaPlan:
        def apply(self):
            events.append("ca.apply")
            return cowork_ca.CaResult(True, True, "installed CA (stub)", store="Cert:\\stub")

        def compensate(self):
            events.append("ca.compensate")
            return cowork_ca.CaResult(True, True, "removed CA (stub)", store="Cert:\\stub")

    monkeypatch.setattr(cowork_ca, "resolve_pem", lambda: "/stub/ca.pem")
    monkeypatch.setattr(cowork_ca, "plan_install", lambda *, force=False: FakeCaPlan())
    monkeypatch.setattr(main.cowork_ca, "plan_install", lambda *, force=False: FakeCaPlan())
    monkeypatch.setattr(main.cowork_ca, "resolve_pem", lambda: "/stub/ca.pem")

    # --- stub the config plan: apply() FAILS after the CA install.
    class FakeCfgPlan:
        def apply(self):
            events.append("cfg.apply")
            return cowork_config.ConfigResult(False, False, "config write needs elevation")

        def compensate(self):
            events.append("cfg.compensate")
            return cowork_config.ConfigResult(True, False, "nothing to revert (stub)")

    monkeypatch.setattr(main.cowork_config, "plan_write", lambda config: FakeCfgPlan())

    # Provide a base URL + helper so build_config succeeds.
    monkeypatch.setenv("GATEWAY_CLI_COWORK_GATEWAY_URL", "https://gw.example.com")

    result = CliRunner().invoke(
        main.cli,
        ["setup", "--credential-kind", "static", "--api-key", "vk-abc"],
    )

    # Command failed (config write not ok) — original error is the exit.
    assert result.exit_code != 0
    assert "config write needs elevation" in result.output
    # The CA installed by THIS run was rolled back (LIFO: cfg compensate, then ca).
    assert events == ["ca.apply", "cfg.apply", "cfg.compensate", "ca.compensate"]
    # Rollback summary was printed.
    assert "Rolled back the steps that had already succeeded" in result.output
    assert "corporate CA" in result.output
