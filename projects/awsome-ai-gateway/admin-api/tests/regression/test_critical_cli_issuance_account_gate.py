# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""CLI(STS) virtual key 발급은 허용된 AWS 계정의 허용된 principal 만 받는다(``services/cli_service.py``).

배경 — ``POST /cli/auth/virtual-key`` 는 호출자가 서명한 STS ``GetCallerIdentity`` 요청을 AWS 에
대신 보내 신원을 확인한 뒤 virtual key 를 발급한다. 그 신원을 거르는 게이트에 구멍이 둘 있었다:

  * **계정을 보지 않았다.** ``ALLOWED_IAM_ROLES`` 는 역할 **이름**만 대조한다. 역할 이름은 어느
    AWS 계정에서나 지을 수 있으므로, 자기 계정에 같은 이름의 역할을 만든 사람은 누구나 통과했다.
  * **IAM 사용자는 검사를 건너뛰었다.** 역할이 없으면(``role_name is None``) 조건식이 거짓이 되어
    목록을 아무리 좁혀도 그대로 들어왔다 — 어느 계정의 IAM 사용자든.

여기서 고정하는 것(``UserRepository`` 는 대체하지만 게이트 로직은 진짜를 탄다):

  1. 허용 계정이 아니면 역할 이름이 맞아도 거부한다. 기본 허용 계정은 admin-api 자신의 계정이다.
  2. 역할 허용 목록이 있으면 IAM 사용자는 거부한다.
  3. 거부된 요청은 사용자도 예산도 키도 만들지 않는다.
  4. 각 거부에는 **같은 조건에서 통과하는 대조군**이 붙어 있다.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.core.exceptions import ForbiddenError
from app.models.auth import User, UserRole
from app.schemas.cli import STSRequestPayload, VirtualKeyIssueRequest
from app.services.cli_service import CLIService, allowed_account_ids, own_account_id
from app.services.key_service import KeyService

GATEWAY_ACCOUNT = "111122223333"
OTHER_ACCOUNT = "444455556666"
GATEWAY_ROLE_ARN = f"arn:aws:iam::{GATEWAY_ACCOUNT}:role/gateway-admin-api"


def _role_arn(account: str, role: str = "Developer") -> str:
    return f"arn:aws:sts::{account}:assumed-role/{role}/dev@example.com"


def _user_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:user/dev"


class _Outcome:
    def __init__(self, result, user_repo: MagicMock, key_service: AsyncMock) -> None:
        self.result = result
        self.user_repo = user_repo
        self.key_service = key_service


async def _issue(
    arn: str,
    *,
    roles: list[str] | None = None,
    accounts: list[str] | None = None,
    app_env: str = "prod",
) -> _Outcome:
    """STS 가 ``arn`` 을 돌려준 상황에서 발급을 시도한다. 게이트 로직은 실제 코드다."""
    key_service = AsyncMock(spec=KeyService)
    key_service.issue_key = AsyncMock(return_value=MagicMock(virtual_key="vk-test", expires_at=MagicMock()))
    service = CLIService(key_service=key_service)

    data = VirtualKeyIssueRequest(
        sts_request=STSRequestPayload(
            url="https://sts.ap-northeast-2.amazonaws.com/?Action=GetCallerIdentity", headers={}
        ),
        device_name="test",
    )
    sts_xml = (
        '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        f"<GetCallerIdentityResult><Arn>{arn}</Arn></GetCallerIdentityResult>"
        "</GetCallerIdentityResponse>"
    )

    with (
        patch("app.services.cli_service.get_settings") as get_settings,
        patch("app.services.cli_service.httpx") as httpx_mod,
        patch("app.services.cli_service.UserRepository") as user_repo,
        patch("app.services.cli_service.BudgetRepository") as budget_repo,
    ):
        settings = get_settings.return_value
        settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
        settings.ALLOWED_IAM_ROLES = roles or []
        settings.ALLOWED_AWS_ACCOUNT_IDS = accounts or []
        settings.APP_ENV = app_env
        settings.DEFAULT_TEAM_ID = str(uuid.uuid4())
        settings.DEFAULT_USER_BUDGET_USD = 100.0

        client = AsyncMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=200, text=sts_xml))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock()
        httpx_mod.AsyncClient.return_value = client

        user = MagicMock(spec=User)
        user.id = uuid.uuid4()
        user.email = "dev@example.com"
        user.role = UserRole.DEVELOPER
        user.team_id = uuid.UUID(settings.DEFAULT_TEAM_ID)
        user.is_active = True
        user_repo.return_value.get_by_sso_subject = AsyncMock(return_value=user)
        budget_repo.return_value.get_first_active_config = AsyncMock(return_value=None)

        result = None
        try:
            result = await service.verify_sts_and_issue_key(AsyncMock(), data=data)
        except ForbiddenError as exc:
            result = exc
        return _Outcome(result, user_repo, key_service)


def _refused(outcome: _Outcome, needle: str) -> None:
    assert isinstance(outcome.result, ForbiddenError), f"expected a refusal, got {outcome.result!r}"
    assert needle in str(outcome.result.message)
    # 거부된 요청은 DB 에 손대지 않고 키도 만들지 않는다.
    outcome.user_repo.assert_not_called()
    outcome.key_service.issue_key.assert_not_awaited()


def _issued(outcome: _Outcome) -> None:
    assert not isinstance(outcome.result, Exception), f"expected a key, got {outcome.result!r}"
    outcome.key_service.issue_key.assert_awaited_once()


@pytest.fixture(autouse=True)
def _gateway_runs_with_irsa(monkeypatch):
    """기본 상황 — admin-api 가 IRSA 로 떠 있다. 필요한 테스트만 지운다."""
    monkeypatch.setenv("AWS_ROLE_ARN", GATEWAY_ROLE_ARN)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 계정
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_allowed_role_name_in_another_account_is_refused():
    """보고된 구멍 그대로 — 남의 계정에서 허용 목록과 같은 이름의 역할을 만들었다."""
    control = await _issue(_role_arn(GATEWAY_ACCOUNT), roles=["Developer"])
    _issued(control)

    outcome = await _issue(_role_arn(OTHER_ACCOUNT), roles=["Developer"])
    _refused(outcome, "AWS account not allowed")


async def test_without_a_role_list_another_account_is_still_refused():
    """역할 목록을 비워 두는 설치가 있다 — 그때도 계정은 본다."""
    _issued(await _issue(_role_arn(GATEWAY_ACCOUNT)))
    _refused(await _issue(_role_arn(OTHER_ACCOUNT)), "AWS account not allowed")


async def test_configured_accounts_replace_the_default():
    """목록을 주면 그 목록이 전부다 — 다른 계정을 받으려면 자기 계정도 함께 적어야 한다."""
    _issued(await _issue(_role_arn(OTHER_ACCOUNT), accounts=[OTHER_ACCOUNT]))
    _refused(await _issue(_role_arn(GATEWAY_ACCOUNT), accounts=[OTHER_ACCOUNT]), "AWS account not allowed")
    _issued(await _issue(_role_arn(GATEWAY_ACCOUNT), accounts=[OTHER_ACCOUNT, GATEWAY_ACCOUNT]))


@pytest.mark.parametrize("app_env", ["prod", "production", "staging", ""])
async def test_outside_development_an_unknown_gateway_account_means_nobody(monkeypatch, app_env):
    """IRSA 도 없고 목록도 없다 — 어느 계정을 믿을지 모르면 아무도 믿지 않는다."""
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    _refused(await _issue(_role_arn(GATEWAY_ACCOUNT), app_env=app_env), "AWS account not allowed")


@pytest.mark.parametrize("app_env", ["dev", "development", "local", "test"])
async def test_local_development_without_irsa_is_not_account_checked(monkeypatch, app_env):
    """로컬 실행에는 "자기 계정" 이 없다. 배포된 dev 는 IRSA 가 있으므로 계정을 본다(아래)."""
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    _issued(await _issue(_role_arn(OTHER_ACCOUNT), app_env=app_env))


async def test_a_deployed_development_environment_is_account_checked():
    _refused(await _issue(_role_arn(OTHER_ACCOUNT), app_env="dev"), "AWS account not allowed")


# ─────────────────────────────────────────────────────────────────────────────
# 2. IAM 사용자
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_iam_user_does_not_bypass_the_role_list():
    """예전에는 역할이 없다는 이유로 검사 자체를 건너뛰었다."""
    outcome = await _issue(_user_arn(GATEWAY_ACCOUNT), roles=["Developer"])
    _refused(outcome, "IAM role not allowed")


async def test_an_iam_user_passes_only_when_roles_are_not_restricted():
    _issued(await _issue(_user_arn(GATEWAY_ACCOUNT)))


async def test_an_iam_user_of_another_account_is_refused_on_the_account():
    _refused(await _issue(_user_arn(OTHER_ACCOUNT)), "AWS account not allowed")


async def test_a_role_outside_the_list_is_still_refused():
    """기존 동작이 그대로인지 — 계정이 맞아도 역할 이름이 목록에 없으면 거부한다."""
    _refused(await _issue(_role_arn(GATEWAY_ACCOUNT, role="Intern"), roles=["Developer"]), "IAM role not allowed")


# ─────────────────────────────────────────────────────────────────────────────
# 3. 계정 판정 함수
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("role_arn", "expected"),
    [
        (GATEWAY_ROLE_ARN, GATEWAY_ACCOUNT),
        (f"arn:aws-cn:iam::{GATEWAY_ACCOUNT}:role/x", GATEWAY_ACCOUNT),
        (f"arn:aws-us-gov:iam::{GATEWAY_ACCOUNT}:role/path/x", GATEWAY_ACCOUNT),
        ("", None),
        ("not-an-arn", None),
        (f"arn:aws:iam::{GATEWAY_ACCOUNT}:user/x", None),
        ("arn:aws:iam::12345:role/x", None),
    ],
)
def test_own_account_is_read_from_the_irsa_role_arn(monkeypatch, role_arn, expected):
    monkeypatch.setenv("AWS_ROLE_ARN", role_arn)
    assert own_account_id() == expected


def test_allowed_accounts_resolution_order(monkeypatch):
    def settings(accounts, app_env):
        return MagicMock(ALLOWED_AWS_ACCOUNT_IDS=accounts, APP_ENV=app_env)

    assert allowed_account_ids(settings([OTHER_ACCOUNT], "prod")) == {OTHER_ACCOUNT}
    assert allowed_account_ids(settings([], "prod")) == {GATEWAY_ACCOUNT}
    monkeypatch.delenv("AWS_ROLE_ARN")
    assert allowed_account_ids(settings([], "prod")) == frozenset()
    assert allowed_account_ids(settings([], "dev")) is None


def test_account_ids_are_validated_when_settings_load():
    """오타 난 계정 ID 는 어떤 요청과도 일치하지 않는다 — 조용히 전부 거부하지 않고 기동에서 알린다."""
    ok = Settings(_env_file=None, ALLOWED_AWS_ACCOUNT_IDS=f"{GATEWAY_ACCOUNT}, {OTHER_ACCOUNT}")
    assert ok.ALLOWED_AWS_ACCOUNT_IDS == [GATEWAY_ACCOUNT, OTHER_ACCOUNT]
    assert Settings(_env_file=None).ALLOWED_AWS_ACCOUNT_IDS == []

    # 마지막 값은 ASCII 가 아닌 숫자 12자리다 — str.isdigit() 는 받지만 STS 가 돌려주는 값과는 절대 일치하지 않는다.
    for bad in ("12345", "11112222333a", f"{GATEWAY_ACCOUNT},1234", "١٢٣٤٥٦٧٨٩٠١٢"):
        with pytest.raises(ValueError, match="12-digit"):
            Settings(_env_file=None, ALLOWED_AWS_ACCOUNT_IDS=bad)
