# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Cognito User Pool — OIDC IDP for LLM Gateway
# ------------------------------------------------------------------------------
# - User Pool 1개 (env 별)
# - Public App Client (PKCE 강제, refresh token rotation)
# - Hosted UI 도메인 (`{prefix}.auth.{region}.amazoncognito.com`)
# - 사용자 그룹:
#   - Claude_<team>             → Default Department 하위 <team> 팀 자동 매핑
#   - Claude_<department>_<team> → <department> 부서 자동 생성 + 그 아래 <team> 팀
#   - ClaudeAdmin               → admin role 부트스트랩 (팀 매핑 제외)
#   - "Claude_" prefix 없는 그룹은 OIDC 로그인 거부 (REJECT_UNMATCHED_GROUPS=true)
#   (admin 이 콘솔에서 사용자를 그룹에 직접 추가)
#
# 토큰 형식 주의:
#   - Cognito access_token: 표준 OIDC `aud` claim 없음 (대신 client_id claim).
#     admin-api 의 OIDC_AUDIENCE 는 비워두고 audience 검증 skip 사용.
#   - issuer URL: https://cognito-idp.{region}.amazonaws.com/{user_pool_id}
# ==============================================================================

locals {
  user_pool_name = "${var.project}-${var.environment}-userpool"
}

# ------------------------------------------------------------------------------
# User Pool
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool" "this" {
  name = local.user_pool_name

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # MFA off (MVP — v2 에서 검토)
  mfa_configuration = "OFF"

  # 사용자 self-signup 차단 — admin 이 콘솔에서 직접 생성
  admin_create_user_config {
    allow_admin_create_user_only = true
    invite_message_template {
      email_subject = "[LLM Gateway] 임시 자격증명"
      email_message = "안녕하세요, {username}.\n임시 패스워드: {####}\n\n첫 로그인 시 패스워드를 변경해주세요."
      sms_message   = "{username} 임시 패스워드: {####}"
    }
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 320
    }
  }

  schema {
    name                = "name"
    attribute_data_type = "String"
    required            = false
    mutable             = true
    string_attribute_constraints {
      min_length = 0
      max_length = 255
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = merge(var.tags, {
    Name        = local.user_pool_name
    Environment = var.environment
  })
}

# ------------------------------------------------------------------------------
# Hosted UI 도메인
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${var.project}-${var.environment}-${var.domain_suffix}"
  user_pool_id = aws_cognito_user_pool.this.id
}

# ------------------------------------------------------------------------------
# App Client — Public (PKCE 강제), gateway-cli 용
# ------------------------------------------------------------------------------
resource "aws_cognito_user_pool_client" "cli" {
  name         = "${var.project}-${var.environment}-cli"
  user_pool_id = aws_cognito_user_pool.this.id

  # Public client — secret 없음 (PKCE 로 보안)
  generate_secret = false

  # Authorization Code flow (PKCE 강제) + refresh
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  # Callback / logout URLs
  callback_urls = var.callback_urls
  logout_urls   = var.logout_urls

  # Token TTL — 우리 spec 의 권장값
  access_token_validity  = 1 # 1시간
  id_token_validity      = 1 # 1시간
  refresh_token_validity = 7 # 7일
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  # Refresh token rotation
  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"

  # API 호출용 Auth flows (테스트 ROPC 허용)
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_PASSWORD_AUTH", # admin 이 직접 사용자 생성 후 임시 PW 사용
  ]

  read_attributes  = ["email", "email_verified", "name"]
  write_attributes = ["email", "name"]
}

# ------------------------------------------------------------------------------
# App Client — admin-ui (브라우저 SSO), admin_ui_callback_urls 를 채웠을 때만
# ------------------------------------------------------------------------------
# CLI client 와 따로 둔다. admin-api 는 관리 콘솔 세션 토큰이 **이 client 에게 발급됐는지**
# 대조한다(OIDC_ADMIN_UI_CLIENT_ID) — client 를 같이 쓰면 CLI 로 받은 토큰이 그대로
# 관리 콘솔 세션이 된다.
resource "aws_cognito_user_pool_client" "admin_ui" {
  count = length(var.admin_ui_callback_urls) > 0 ? 1 : 0

  name         = "${var.project}-${var.environment}-admin-ui"
  user_pool_id = aws_cognito_user_pool.this.id

  # Public client + PKCE(S256). admin-ui 가 /api/auth/login 에서 PKCE 를 붙이고
  # 서버(Next.js route)에서 code 를 교환한다. secret 을 쓰려면 Secrets Manager 로 전달해야
  # 하므로 기본은 두지 않는다.
  generate_secret = false

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = var.admin_ui_callback_urls
  logout_urls   = var.admin_ui_logout_urls

  # admin-ui 는 받은 토큰을 그대로 세션 쿠키로 쓰고 refresh 하지 않는다 — 세션 길이 = 토큰 수명.
  access_token_validity  = var.admin_ui_token_validity_hours
  id_token_validity      = var.admin_ui_token_validity_hours
  refresh_token_validity = 1
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"

  # Hosted UI 로그인만 쓴다. 비밀번호를 API 로 직접 받는 흐름(USER_PASSWORD_AUTH)은 열지 않는다.
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  read_attributes = ["email", "email_verified", "name"]
}

# ------------------------------------------------------------------------------
# 사용자 그룹 (terraform 으로 미리 생성, admin 이 사용자를 콘솔에서 추가)
# ------------------------------------------------------------------------------
resource "aws_cognito_user_group" "groups" {
  for_each = toset(var.groups)

  user_pool_id = aws_cognito_user_pool.this.id
  name         = each.value
  description  = "LLM Gateway team mapping group"
}
