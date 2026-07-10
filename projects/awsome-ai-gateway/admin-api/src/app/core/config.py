# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator(
        "ALLOWED_STS_REGIONS", "ALLOWED_IAM_ROLES", "ADMIN_EMAILS", "ADMIN_GROUPS",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept comma-separated env var values as lists.

        `NoDecode` on the field disables pydantic-settings' default JSON parsing,
        so this validator is responsible for turning strings into lists.
        """
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v

    # ── Application ──
    APP_NAME: str = "llm-gateway-admin-api"
    APP_ENV: str = "development"  # development | staging | production
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ── Database (PostgreSQL) ──
    DATABASE_URL: str = "postgresql+asyncpg://admin_api_user:changeme@localhost:5432/ds_gateway"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800
    DB_ECHO: bool = False
    # RDS Proxy 경유 시 0 으로 설정 (PostgreSQL pinning 회피). Aurora 직접 연결 시엔 기본값 유지.
    DB_STATEMENT_CACHE_SIZE: int = 100

    # ── Redis ──
    REDIS_URL: str = "redis://localhost:6379/0"
    # None = auto-detect (startup probe). True = ElastiCache cluster mode, False = standalone
    REDIS_CLUSTER_MODE: bool | None = None
    REDIS_POOL_SIZE: int = 50

    # ── Encryption ──
    # SecretStr wrapping prevents accidental exposure via repr/__str__/logs.
    # Always unwrap via .get_secret_value() at the single boundary (main.py lifespan).
    VIRTUAL_KEY_ENCRYPTION_KEY: SecretStr = SecretStr("")  # 64-char hex for AES-256-GCM DEK

    # ── JWT Verification ──
    JWT_ALGORITHM: str = "RS256"
    JWT_ISSUER: str = ""
    JWT_AUDIENCE: str = ""

    # ── STS / CLI Auth (legacy, dual-mode 유지) ──
    ALLOWED_STS_REGIONS: Annotated[list[str], NoDecode] = ["ap-northeast-2"]
    ALLOWED_IAM_ROLES: Annotated[list[str], NoDecode] = []

    # ── OIDC Auth (신규) ──
    # OIDC_ISSUER_URL 이 비어있으면 OIDC 비활성. 채워지면 활성 (POST /v1/auth/exchange 노출).
    OIDC_ISSUER_URL: str = ""
    OIDC_AUDIENCE: str = ""
    # users.provider 컬럼에 들어가는 값. 다중 IDP 운영 시 식별자.
    # 예: 'oidc:keycloak', 'oidc:cognito', 'oidc:identity_center'
    OIDC_PROVIDER_NAME: str = "oidc"
    OIDC_JWKS_CACHE_TTL_SECONDS: int = 3600
    # Dev 환경 (docker network) 등에서 issuer URL 을 직접 fetch 못 할 때 우회용.
    # 비워두면 OIDC_ISSUER_URL 로 fetch. 채우면 그 URL 의 .well-known 로 fetch.
    # 토큰의 iss claim 검증은 여전히 OIDC_ISSUER_URL 기준.
    OIDC_DISCOVERY_URL_OVERRIDE: str = ""

    # OIDC 흐름으로 발급되는 VK 의 TTL (시간). STS legacy 흐름은 rotation_policies 의
    # expiry_days (기본 90일) 그대로 사용. OIDC 는 짧게 두는 것이 권장 — 도용 영향 최소화.
    # 1h: Cognito group 변경이 최대 [VK TTL + id_token TTL] = ~2h 안에 자동 반영됨.
    # apiKeyHelper 가 silent 하게 refresh_token 으로 새 id_token 받아 admin-api 재호출 →
    # _resolve_team 이 현재 그룹으로 transfer_user 트리거 → admin UI 자동 동기화.
    OIDC_VK_TTL_HOURS: int = 1

    # IDP 별 claim 이름 차이 흡수 (Cognito 면 'cognito:groups')
    OIDC_USER_ID_CLAIM: str = "sub"
    OIDC_EMAIL_CLAIM: str = "email"
    OIDC_NAME_CLAIM: str = "name"
    OIDC_GROUPS_CLAIM: str = "groups"

    # 그룹 → 팀 매핑 — underscore 개수 기반 결정론적 파싱:
    #   "Claude_<team>"          (underscore 1개) → Default Department 하위 <team> 팀
    #   "Claude_<dept>_<team>"   (underscore 2개) → <dept> 부서 자동 생성 후 <team> 팀
    #   (underscore 3개+ 또는 "Claude_" prefix 없음) → reject
    # 팀명/부서명에 underscore 사용 금지 (Cognito group 네이밍 규약).
    # ClaudeAdmin 은 팀 매핑 제외 (ADMIN_GROUPS 부트스트랩 전용, _parse_group 이 None 반환).
    OIDC_GROUP_PREFIX: str = "Claude_"

    # 매칭되는 그룹이 하나도 없을 때 동작
    #   False           → DEFAULT_TEAM_ID 로 fallback
    #   True  (default) → 거부 (403 no_matching_team_group).
    #                     Cognito 가 팀 구조의 single source of truth 가 되려면 true 권장.
    OIDC_REJECT_UNMATCHED_GROUPS: bool = True

    # 게이팅 (옵션). 비우면 모든 사용자 통과.
    OIDC_REQUIRED_GROUP: str = ""

    # Admin 부트스트랩 (둘 중 하나라도 매칭되면 ADMIN role 부여)
    ADMIN_EMAILS: Annotated[list[str], NoDecode] = []
    ADMIN_GROUPS: Annotated[list[str], NoDecode] = []

    # ── Auto-provisioning Defaults ──
    # 기본 시드 (db/init/03_seed_data.sql) 의 UUID 와 정확히 일치해야 함.
    DEFAULT_TEAM_ID: str = "00000000-0000-4000-a000-000000000003"  # "Default Team"
    DEFAULT_DEPT_ID: str = "00000000-0000-4000-a000-000000000002"  # "Default Department"
    DEFAULT_USER_BUDGET_USD: float = 1000.0
    # OIDC 자동 생성 팀의 BudgetConfig.allocated_by — system user (admin@dev.local)
    SYSTEM_USER_ID: str = "00000000-0000-4000-a000-000000000010"

    # ── Cognito Sync ──
    # Cognito User Pool ID (ap-northeast-2_XXXXXXXXX). 비어있으면 sync 비활성.
    COGNITO_USER_POOL_ID: str = ""
    # AWS region for Cognito API calls
    COGNITO_REGION: str = "ap-northeast-2"
    # Cognito 에 없는 OIDC 사용자를 비활성화할지 여부
    COGNITO_SYNC_DEACTIVATE_MISSING: bool = False

    # AWS Price List API 리전(GetProducts). Price List 는 us-east-1/ap-south-1/
    # eu-central-1 엔드포인트만 지원 — 우리 홈리전 ap-northeast-2 가 아니어도 정상.
    # 가격 동기화(모델관리 화면 버튼)용. IAM: admin-api 역할에 pricing:GetProducts.
    PRICING_API_REGION: str = "us-east-1"

    # ── Server ──
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    WORKERS: int = 2

    # ── Monitoring ──
    # 사용로그 "지연(SLOW_REQUEST)" 판정 임계값 (TTFT 기준, ms).
    TTFT_SLOW_MS: int = 3000

    # ── Scheduler ──
    ROI_AGGREGATION_CRON: str = "*/15 * * * *"
    KEY_EXPIRY_CRON: str = "0 * * * *"
    # DAILY_USAGE_AGG_CRON: 2026-04-21 cost-recorder-worker 로 이관됨. 이 setting
    # 은 worker 서비스 환경변수로 관리.


@lru_cache
def get_settings() -> Settings:
    return Settings()
