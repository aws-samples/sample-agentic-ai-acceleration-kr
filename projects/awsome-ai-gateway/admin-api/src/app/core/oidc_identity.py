# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""IdP 클레임 → 애플리케이션 신원 변환 정책, **한 곳에서만**.

이 모듈이 왜 따로 있나: 같은 정책이 두 경로에서 필요하다.

  1. ``POST /v1/auth/exchange`` — CLI 가 IdP 토큰을 VK 로 바꾼다
     (``services/oidc_service.py``, 프로비저닝까지 함).
  2. ``admin_jwt`` 쿠키로 들어오는 admin-ui 세션 — admin-api 가 **리소스 서버**로서
     IdP id_token 을 읽어 신원을 세운다(``core/auth.py``).

두 경로가 각자 정책을 들고 있으면 조용히 어긋난다. 실제로 그 부류의 사고가
이 레포에 있었다: admin-ui 가 관리자 그룹 이름을 하드코딩하고 admin-api 는 설정
가능하게 두어서, 그룹을 개명하는 순간 API 는 인가하는데 UI 는 DEVELOPER 로
판정해 모든 페이지가 /403 이 됐다(관리자 잠김).

⚠️ 여기에는 **DB 접근도, 프로비저닝도 넣지 않는다.** 순수 함수만 둔다 —
   그래야 두 호출자가 각자의 트랜잭션·프로비저닝 정책을 유지할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.models.auth import UserRole


@dataclass(frozen=True)
class OIDCClaims:
    """설정된 claim 이름으로 뽑아낸 값들."""

    subject: str
    email: str
    name: str
    groups: list[str]


def extract_claims(claims: dict) -> OIDCClaims:
    """IdP 별 claim 이름 차이를 흡수한다(Cognito 면 groups 가 ``cognito:groups``).

    ⚠️ ``groups`` 를 리스트로 정규화하는 것이 load-bearing 이다. 어떤 IdP 는 그룹이
       하나일 때 문자열로 준다(Azure AD 의 일부 설정, Keycloak 의 단일 값 매퍼).
       그때 ``in`` 검사를 그대로 하면 **부분 문자열 매칭**이 된다 —
       ``"GatewayAdminReadOnly"`` 가 ``"GatewayAdmin"`` 을 포함하므로 읽기 전용
       그룹이 ADMIN 으로 승격된다. 리스트로 감싸면 원소 동등 비교가 된다.
    """
    settings = get_settings()
    raw_groups = claims.get(settings.OIDC_GROUPS_CLAIM) or []
    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]
    elif not isinstance(raw_groups, list):
        raw_groups = [raw_groups]

    subject = claims.get(settings.OIDC_USER_ID_CLAIM) or ""
    email = claims.get(settings.OIDC_EMAIL_CLAIM) or ""
    name = claims.get(settings.OIDC_NAME_CLAIM) or email or subject

    return OIDCClaims(
        subject=str(subject),
        email=str(email),
        name=str(name),
        groups=[str(g) for g in raw_groups],
    )


def derive_role(email: str, groups: list[str]) -> UserRole:
    """``ADMIN_EMAILS`` / ``ADMIN_GROUPS`` 중 하나라도 맞으면 ADMIN, 아니면 DEVELOPER.

    이메일 비교는 대소문자 무시(주소는 대소문자 구분이 없다). 그룹 비교는 **정확히
    일치**해야 한다 — 그룹 이름은 IdP 의 식별자이고, 느슨하게 비교하면 위 주석의
    부분 문자열 승격이 그대로 재현된다.
    """
    settings = get_settings()

    admin_emails = {e.lower() for e in settings.ADMIN_EMAILS}
    if email and email.lower() in admin_emails:
        return UserRole.ADMIN

    admin_groups = set(settings.ADMIN_GROUPS)
    if any(g in admin_groups for g in groups):
        return UserRole.ADMIN

    return UserRole.DEVELOPER


def reconcile_role(derived: UserRole, *, leads_own_team: bool) -> UserRole:
    """로그인·동기화가 **기존** 사용자에게 저장할 역할. ``derived`` 는 ``derive_role`` 의 값이다.

    IdP 는 팀 리더를 모른다 — ``derive_role`` 은 ADMIN 아니면 DEVELOPER 다. 그 값을 그대로 저장하던
    동안 관리자가 지정한 팀 리더가 CLI 의 매시간 교환 한 번에 DEVELOPER 로 돌아갔다(실측).

    ⚠️ 저장돼 있던 TEAM_LEADER 를 그냥 남기지 않고, 사용자가 **이번에 속하게 된 팀**의 리더 지정
       (``teams.leader_user_id``)을 본다. 인가는 ``role == TEAM_LEADER`` 와 ``users.team_id`` 를 "그
       팀의 리더" 로 읽는다 — IdP 그룹이 바뀌어 다른 팀으로 옮겨진 리더가 역할을 들고 가면 아무도
       지정하지 않은 팀의 리더가 된다. ADMIN 은 지금처럼 IdP 가 양방향으로 정한다.
    """
    if derived == UserRole.ADMIN:
        return UserRole.ADMIN
    return UserRole.TEAM_LEADER if leads_own_team else UserRole.DEVELOPER


def required_group_missing(groups: list[str]) -> str | None:
    """``OIDC_REQUIRED_GROUP`` 게이트. 통과면 None, 막히면 없는 그룹 이름.

    비어 있으면(기본값) 게이트가 없다 — 모든 인증 사용자가 통과한다.
    """
    settings = get_settings()
    required = settings.OIDC_REQUIRED_GROUP
    if required and required not in groups:
        return required
    return None
