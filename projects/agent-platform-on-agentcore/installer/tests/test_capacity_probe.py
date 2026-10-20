"""The account-limit check before the first apply (probe.network_capacity).

A first apply on a shared account failed three times, minutes in each: EIPs at
the limit, then the first AZ's NAT gateways at the limit, then an `azs` change
stuck on the ALB's interfaces (us-west-2, 2026-10-18). The check names each
before anything is created.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import env as envmod  # noqa: E402
from installer.core import probe  # noqa: E402
from installer.core.probe import State  # noqa: E402

ENV = envmod.get("standalone")


class FakeShell:
    def __init__(self, table):
        self.table = table

    async def capture(self, cmd):
        line = cmd.display()
        for needle, response in self.table.items():
            if needle in line:
                return response
        return (127, f"unexpected command: {line}")


def shell(vpcs=(), eips=0, eip_max=5, nats_by_az=None, quotas=None):
    nats_by_az = nats_by_az or {}
    subnets, nats = [], []
    for i, az in enumerate(["us-west-2a", "us-west-2b", "us-west-2c"]):
        subnets.append({"SubnetId": f"subnet-{i}", "AvailabilityZone": az})
        nats += [{"SubnetId": f"subnet-{i}"}] * nats_by_az.get(az, 0)
    quotas = quotas or {}
    return FakeShell({
        "describe-vpcs": (0, json.dumps({"Vpcs": list(vpcs)})),
        "L-F678F1CE": (0, str(quotas.get("vpc", 5.0))),
        "L-FE5A380F": (0, str(quotas.get("nat", 5.0))),
        "describe-account-attributes": (0, json.dumps({"AccountAttributes": [{"AttributeValues": [{"AttributeValue": str(eip_max)}]}]})),
        "describe-addresses": (0, json.dumps({"Addresses": [{}] * eips})),
        "describe-nat-gateways": (0, json.dumps({"NatGateways": nats})),
        "describe-subnets": (0, json.dumps({"Subnets": subnets})),
    })


def check(monkeypatch, tmp_path, sh, azs='["us-west-2a", "us-west-2b"]'):
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text(f'project = "bst"\nregion = "us-west-2"\nazs = {azs}\n')
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)
    return asyncio.run(probe.network_capacity(ENV, sh))


def test_room_for_everything_is_done(monkeypatch, tmp_path):
    assert check(monkeypatch, tmp_path, shell()).state is State.DONE


def test_an_existing_stack_network_is_done(monkeypatch, tmp_path):
    vpc = {"Tags": [{"Key": "Name", "Value": "bst-vpc"}]}
    assert check(monkeypatch, tmp_path, shell(vpcs=[vpc] * 5, eips=5)).state is State.DONE


def test_full_elastic_ips_are_named(monkeypatch, tmp_path):
    out = check(monkeypatch, tmp_path, shell(eips=6, eip_max=5))
    assert out.state is State.PENDING and "Elastic IP" in out.detail and "6/5" in out.detail


def test_a_full_first_az_suggests_one_with_room(monkeypatch, tmp_path):
    out = check(monkeypatch, tmp_path, shell(nats_by_az={"us-west-2a": 5}))
    assert out.state is State.PENDING
    assert "us-west-2a" in out.detail and '"us-west-2b"' in out.detail


def test_the_vpc_limit_is_named(monkeypatch, tmp_path):
    out = check(monkeypatch, tmp_path, shell(vpcs=[{}] * 5))
    assert out.state is State.PENDING and "VPC" in out.detail


def test_an_unreadable_account_is_unknown(monkeypatch, tmp_path):
    sh = FakeShell({"describe-vpcs": (255, "Unable to locate credentials")})
    assert check(monkeypatch, tmp_path, sh).state is State.UNKNOWN
