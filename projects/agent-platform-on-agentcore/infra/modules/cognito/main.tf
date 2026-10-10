resource "aws_cognito_user_pool" "this" {
  name = "${var.project}-user-pool"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = true
  }

  schema {
    name                     = "email"
    attribute_data_type      = "String"
    required                 = true
    mutable                  = true
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }
}

locals {
  oauth_enabled = length(var.callback_urls) > 0
}

# Hosted UI domain: the authorize/token endpoints of an Authorization Code +
# PKCE login live here (OIDC discovery points at it). The prefix is AWS-global.
resource "aws_cognito_user_pool_domain" "this" {
  count        = var.domain_prefix != "" ? 1 : 0
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_pool_client" "this" {
  name         = "${var.project}-app-client"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = false

  # Browser OIDC login. Only when callback_urls is set: a public client (no
  # secret) with the code flow + PKCE. Cognito rejects offline_access as a scope.
  allowed_oauth_flows_user_pool_client = local.oauth_enabled
  allowed_oauth_flows                  = local.oauth_enabled ? ["code"] : null
  allowed_oauth_scopes                 = local.oauth_enabled ? ["openid", "email", "profile"] : null
  callback_urls                        = local.oauth_enabled ? var.callback_urls : null
  logout_urls                          = length(var.logout_urls) > 0 ? var.logout_urls : null
  supported_identity_providers         = local.oauth_enabled ? ["COGNITO"] : null

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Administrators"
}

resource "aws_cognito_user_group" "user" {
  name         = "user"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Regular users"
}

# One group per team. Membership is managed outside terraform (console, seed
# script): the demo accounts are not terraform either (server/scripts/seed_demo_users.py).
resource "aws_cognito_user_group" "team" {
  for_each     = toset(var.teams)
  name         = "team:${each.value}"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Members of team ${each.value}"
}

resource "aws_cognito_user" "admin" {
  user_pool_id = aws_cognito_user_pool.this.id
  username     = var.admin_email
  password     = var.admin_password

  attributes = {
    email          = var.admin_email
    email_verified = "true"
  }
}

resource "aws_cognito_user" "user" {
  user_pool_id = aws_cognito_user_pool.this.id
  username     = var.user_email
  password     = var.user_password

  attributes = {
    email          = var.user_email
    email_verified = "true"
  }
}

resource "aws_cognito_user_in_group" "admin" {
  user_pool_id = aws_cognito_user_pool.this.id
  group_name   = aws_cognito_user_group.admin.name
  username     = aws_cognito_user.admin.username
}

resource "aws_cognito_user_in_group" "user" {
  user_pool_id = aws_cognito_user_pool.this.id
  group_name   = aws_cognito_user_group.user.name
  username     = aws_cognito_user.user.username
}
