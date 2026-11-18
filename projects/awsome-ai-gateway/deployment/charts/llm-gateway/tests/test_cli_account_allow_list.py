# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""CLI(STS) 키 발급의 AWS 계정 허용 목록 — ``aws.allowedAccountIds`` 가 admin-api 에 제대로 닿는지.

admin-api 는 CLI 로 virtual key 를 받으려는 호출자의 **AWS 계정**을 먼저 대조한다. 기본값은
게이트웨이가 배포된 계정 하나이고(IRSA 역할 ARN 에서 읽는다), 다른 계정도 받으려면
``aws.allowedAccountIds`` 에 계정 ID 를 적는다.

이 값은 차트를 지나면서 조용히 망가지기 쉽다. YAML 에서 따옴표 없이 쓰거나 ``--set`` 으로 주면
숫자로 해석되어 ``1.11122223333e+11`` 로 렌더되고, 0 으로 시작하는 계정 ID 는 앞자리가 사라진다.
그 값은 어떤 계정과도 일치하지 않으므로 CLI 로그인이 전부 거부된다. 그래서:

  1. 비어 있으면 env 를 **렌더하지 않는다** — 이 설정을 안 쓰는 설치의 파드 스펙이 그대로다.
  2. 따옴표로 감싼 12자리 문자열은 쉼표로 이어져 admin-api 에 닿는다.
  3. 숫자로 해석된 값·자릿수가 틀린 값은 렌더가 실패한다.

⚠️ ``helm`` 바이너리를 쓴다. 없으면 skip 하되 CI 에서는 실패시킨다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml 이 필요하다(렌더 결과 파싱)")

CHART = Path(__file__).resolve().parents[1]
ENV_NAME = "ALLOWED_AWS_ACCOUNT_IDS"


def _helm() -> str:
    exe = shutil.which("helm")
    if exe is None:
        if os.environ.get("CI"):
            raise AssertionError("CI 인데 helm 이 없다 — 계정 허용 목록 렌더가 아무 데서도 검증되지 않는다.")
        pytest.skip("helm 바이너리가 없다")
    return exe


def _template(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_helm(), "template", "acct-test", str(CHART), *args], capture_output=True, text=True)


def _admin_api_env(*args: str) -> dict[str, str | None]:
    proc = _template(*args)
    assert proc.returncode == 0, f"helm template 실패:\n{proc.stderr[-2000:]}"
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    assert len(docs) > 5, f"렌더 결과가 {len(docs)}개뿐이다 — 차트 경로/값을 확인"
    for d in docs:
        if d.get("kind") == "Deployment" and "admin-api" in d["metadata"]["name"]:
            return {e["name"]: e.get("value") for e in d["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    raise AssertionError("admin-api Deployment 를 렌더 결과에서 찾지 못했다")


@pytest.mark.parametrize(
    "overlay", [None, "values-eks-fargate-dev.yaml", "values-eks-fargate-prod.yaml"], ids=["defaults", "dev", "prod"]
)
def test_nothing_is_rendered_by_default(overlay: str | None):
    env = _admin_api_env(*(("-f", str(CHART / overlay)) if overlay else ()))
    assert ENV_NAME not in env
    # 대조군 — 같은 헬퍼의 다른 값은 렌더된다(이름을 잘못 찾아서 "없다" 고 읽는 게 아니다).
    assert "ALLOWED_IAM_ROLES" in env


def test_quoted_account_ids_reach_admin_api_comma_joined():
    env = _admin_api_env("--set-string", "aws.allowedAccountIds={111122223333,012345678901}")
    assert env[ENV_NAME] == "111122223333,012345678901"


def test_account_ids_from_a_values_file_keep_their_leading_zero(tmp_path: Path):
    values = tmp_path / "accounts.yaml"
    values.write_text('aws:\n  allowedAccountIds:\n    - "012345678901"\n', encoding="utf-8")
    assert _admin_api_env("-f", str(values))[ENV_NAME] == "012345678901"


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--set", "aws.allowedAccountIds={111122223333}"), id="number from --set"),
        pytest.param(("--set-string", "aws.allowedAccountIds={11112222333}"), id="eleven digits"),
        pytest.param(("--set-string", "aws.allowedAccountIds={1111222233334}"), id="thirteen digits"),
        pytest.param(("--set-string", "aws.allowedAccountIds={11112222333a}"), id="not digits"),
    ],
)
def test_malformed_account_ids_fail_the_render(args: tuple[str, ...]):
    proc = _template(*args)
    assert proc.returncode != 0, "렌더가 성공했다 — 일치할 수 없는 계정 ID 가 배포된다"
    assert "aws.allowedAccountIds" in proc.stderr


def test_an_unquoted_account_id_in_a_values_file_fails_the_render(tmp_path: Path):
    """가장 흔한 실수 — YAML 은 따옴표 없는 12자리를 숫자로 읽는다."""
    values = tmp_path / "accounts.yaml"
    values.write_text("aws:\n  allowedAccountIds:\n    - 111122223333\n", encoding="utf-8")
    proc = _template("-f", str(values))
    assert proc.returncode != 0
    assert "aws.allowedAccountIds" in proc.stderr


def test_notes_no_longer_claim_an_empty_role_list_denies_everyone():
    """코드는 빈 역할 목록을 "역할 이름 제한 없음" 으로 읽는다. NOTES 가 반대로 안내하고 있었다."""
    notes = (CHART / "templates" / "NOTES.txt").read_text(encoding="utf-8")
    assert "CLI 인증이 전부 거부됩니다" not in notes
    assert "aws.allowedAccountIds" in notes
