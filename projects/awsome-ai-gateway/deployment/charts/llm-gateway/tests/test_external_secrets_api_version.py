# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ExternalSecret 의 apiVersion 을 클러스터가 서빙하는 쪽으로 렌더하는가.

ESO 0.10.x(이 레포 terraform 기본값)는 ``external-secrets.io/v1beta1`` 만, 0.17+ 는 ``v1`` 을
서빙하고 1.x/2.x 는 ``v1beta1`` 을 더 이상 서빙하지 않는다. ExternalSecret 은 pre-install/
pre-upgrade 훅(weight -30)이라, 틀린 apiVersion 이면 helm upgrade 가 migration Job(-5) 에
가기도 전에 실패한다 — ESO 를 올린 클러스터에 main 차트를 그대로 못 올렸던 이유다.

``helm template`` 은 클러스터를 모르므로 ``--api-versions`` 로 서빙 여부를 흉내 낸다.
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml 이 필요하다(렌더 결과 파싱)")

CHART = Path(__file__).resolve().parents[1]
# externalSecrets.enabled=true 인 overlay — 기본 values.yaml 은 ESO 를 끈다.
VALUES = CHART / "values-eks-fargate-dev.yaml"


def _helm() -> str:
    exe = shutil.which("helm")
    if exe is None:
        if os.environ.get("CI"):
            raise AssertionError("CI 인데 helm 이 없다 — ESO apiVersion 렌더가 아무 데서도 검증되지 않는다.")
        pytest.skip("helm 바이너리가 없다")
    return exe


def _render(*extra: str) -> list[dict]:
    cmd = [_helm(), "template", "eso-test", str(CHART), "-f", str(VALUES), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template 실패:\n{proc.stderr[-2000:]}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _external_secret_versions(docs: list[dict]) -> list[str]:
    versions = [d["apiVersion"] for d in docs if d.get("kind") == "ExternalSecret"]
    # 대조군 — app/db/redis 세 개가 실제로 렌더됐는가. 0개를 "전부 v1" 로 오판하지 않는다.
    assert len(versions) == 3, f"ExternalSecret 이 {len(versions)}개 — overlay/게이팅을 확인"
    return versions


def test_without_v1_on_the_cluster_it_stays_v1beta1():
    """ESO 0.10.x(v1 미서빙) 설치 경로는 그대로여야 한다."""
    assert set(_external_secret_versions(_render())) == {"external-secrets.io/v1beta1"}


def test_a_cluster_serving_v1_gets_v1():
    docs = _render("--api-versions", "external-secrets.io/v1")
    assert set(_external_secret_versions(docs)) == {"external-secrets.io/v1"}


def test_an_explicit_value_wins_over_detection():
    docs = _render(
        "--api-versions", "external-secrets.io/v1",
        "--set", "externalSecrets.apiVersion=external-secrets.io/v1beta1",
    )
    assert set(_external_secret_versions(docs)) == {"external-secrets.io/v1beta1"}


def test_nothing_but_the_external_secret_api_version_changes():
    """감지 결과가 apiVersion 줄 외의 렌더를 바꾸지 않는다.

    ``checksum/secret`` 만 예외다 — secret.yaml 렌더 결과(apiVersion 줄 포함)의 해시라서 같이
    바뀐다. 그래서 감지 결과가 처음 v1 으로 바뀌는 upgrade 에서는 Deployment 가 한 번씩 롤된다.
    """
    def scrub(docs: list[dict]) -> list[dict]:
        out = []
        for d in docs:
            d = copy.deepcopy(d)
            if d.get("kind") == "ExternalSecret":
                d["apiVersion"] = "<eso>"
            ann = d.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})
            if "checksum/secret" in ann:
                ann["checksum/secret"] = "<derived from secret.yaml>"
            out.append(d)
        return out

    old, new = _render(), _render("--api-versions", "external-secrets.io/v1")
    assert scrub(old) == scrub(new)
