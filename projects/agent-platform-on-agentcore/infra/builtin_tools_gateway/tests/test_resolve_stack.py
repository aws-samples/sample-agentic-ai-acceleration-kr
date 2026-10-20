"""deploy.py must never guess which stack it is deploying for.

Its IAM role names are account-global, and the old `--project bap` default, run
for a stack deployed under another project or region, rewrote the live `bap-*`
roles for the new region (found on the 2026-10-18 us-west-2 load-test stack).
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deploy  # noqa: E402


def _args(**kw):
    return SimpleNamespace(project=kw.get("project"), region=kw.get("region"))


def test_flags_win_over_the_stack():
    args = deploy.resolve_stack(_args(project="x", region="eu-west-1"), environ={},
                                output=lambda name: "stack-value")
    assert (args.project, args.region) == ("x", "eu-west-1")


def test_blanks_come_from_the_stack_outputs():
    outputs = {"project": "acme", "region": "us-west-2"}
    args = deploy.resolve_stack(_args(), environ={"AWS_REGION": "ap-northeast-2"}, output=outputs.get)
    assert (args.project, args.region) == ("acme", "us-west-2")


def test_no_project_anywhere_stops_instead_of_defaulting_to_bap():
    with pytest.raises(SystemExit) as exc:
        deploy.resolve_stack(_args(), environ={}, output=lambda name: None)
    assert "--project" in str(exc.value)
