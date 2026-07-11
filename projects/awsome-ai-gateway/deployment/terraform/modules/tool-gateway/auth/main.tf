resource "aws_cognito_user_pool" "main" {
  name = "${var.project_name}-${var.environment}"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = true
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true

    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  tags = {
    Component = "auth"
  }
}

data "aws_caller_identity" "current" {}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${var.project_name}-${var.environment}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# Resource server for AgentCore API scopes
resource "aws_cognito_resource_server" "agentcore" {
  identifier   = "agentcore"
  name         = "AgentCore API"
  user_pool_id = aws_cognito_user_pool.main.id

  scope {
    scope_name        = "invoke"
    scope_description = "Invoke AgentCore Gateway"
  }
}

# App client (for CLI/M2M)
resource "aws_cognito_user_pool_client" "app" {
  name         = "${var.project_name}-app-client"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = true

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["agentcore/invoke"]
  supported_identity_providers         = ["COGNITO"]

  id_token_validity      = 24
  access_token_validity  = 24
  refresh_token_validity = 30
  token_validity_units {
    id_token      = "hours"
    access_token  = "hours"
    refresh_token = "days"
  }

  depends_on = [aws_cognito_resource_server.agentcore]
}

# Web client for dashboard
resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.project_name}-web-client"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile", "agentcore/invoke"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = concat(
    ["http://localhost:3000/callback"],
    var.cowork_redirect_uris,
  )
  logout_urls = ["http://localhost:3000/logout"]

  id_token_validity      = 24
  access_token_validity  = 24
  refresh_token_validity = 30
  token_validity_units {
    id_token      = "hours"
    access_token  = "hours"
    refresh_token = "days"
  }

  depends_on = [aws_cognito_resource_server.agentcore]
}

# M2M client for service-to-service
resource "aws_cognito_user_pool_client" "m2m" {
  name         = "${var.project_name}-m2m-client"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = true

  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["agentcore/invoke"]
  supported_identity_providers         = ["COGNITO"]

  depends_on = [aws_cognito_resource_server.agentcore]
}
