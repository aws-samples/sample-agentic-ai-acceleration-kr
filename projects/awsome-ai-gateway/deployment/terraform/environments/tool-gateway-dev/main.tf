data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # Tools root directory
  tools_root = "${path.module}/../../../tool-gateway/tools"

  # Determine which Lambda tools are enabled based on API key presence
  # Note: use local variables for existence check to avoid sensitive value exposure in for_each
  serper_enabled        = var.enable_serper
  exa_enabled           = var.enable_exa
  duckduckgo_enabled    = var.enable_duckduckgo
  perplexity_enabled    = var.enable_perplexity
  brave_enabled         = var.enable_brave
  anthropic_enabled     = var.enable_anthropic
  firecrawl_enabled     = var.enable_firecrawl
  you_enabled           = var.enable_you
  tavily_lambda_enabled = var.enable_tavily_lambda

  lambda_tools_enabled = {
    serper        = local.serper_enabled
    exa           = local.exa_enabled
    duckduckgo    = local.duckduckgo_enabled
    perplexity    = local.perplexity_enabled
    brave         = local.brave_enabled
    anthropic     = local.anthropic_enabled
    firecrawl     = local.firecrawl_enabled
    you           = local.you_enabled
    tavily_lambda = local.tavily_lambda_enabled
  }

  # Filter to only enabled tools
  enabled_lambda_tools = {
    for name, enabled in local.lambda_tools_enabled : name => enabled if enabled
  }

  # MCP server targets (external hosted MCP servers)
  mcp_server_targets = {
    # Tavily: hosted MCP server endpoint
    tavily = var.enable_tavily ? {
      endpoint = "https://mcp.tavily.com/mcp/"
    } : null
  }

  # Filter out null entries
  enabled_mcp_targets = {
    for k, v in local.mcp_server_targets : k => v if v != null
  }

  # List of all enabled engines (for outputs)
  enabled_engines = concat(
    keys(local.enabled_lambda_tools),
    keys(local.enabled_mcp_targets),
  )
}

# ============================================================
# Authentication (Cognito)
# ============================================================

module "auth" {
  source = "../../modules/tool-gateway/auth"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region

  auth_mode              = var.auth_mode
  external_oidc_issuer   = var.external_oidc_issuer
  external_oidc_audience = var.external_oidc_audience
  cowork_redirect_uris   = var.cowork_redirect_uris
}

# ============================================================
# Identity Providers (API Key Credential Providers)
# ============================================================

module "identity_providers" {
  source = "../../modules/tool-gateway/identity-providers"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region

  api_key_providers = {
    tavily = var.enable_tavily ? {
      display_name = "Tavily Search"
      description  = "Tavily web search API"
    } : null

    brave = var.enable_brave ? {
      display_name = "Brave Search"
      description  = "Brave independent web search"
    } : null

    serper = var.enable_serper ? {
      display_name = "Serper Search"
      description  = "Serper SERP API"
    } : null

    exa = var.enable_exa ? {
      display_name = "Exa Search"
      description  = "Exa neural search API"
    } : null

    perplexity = var.enable_perplexity ? {
      display_name = "Perplexity Sonar"
      description  = "Perplexity Sonar API"
    } : null

    anthropic = var.enable_anthropic ? {
      display_name = "Anthropic Claude"
      description  = "Anthropic Claude built-in web search"
    } : null

    firecrawl = var.enable_firecrawl ? {
      display_name = "Firecrawl Search"
      description  = "Firecrawl web search API"
    } : null

    you = var.enable_you ? {
      display_name = "You.com Search"
      description  = "You.com web search API"
    } : null
  }
}

# Flatten and filter identity provider map.
# nonsensitive() unwraps the sensitive marking that propagates from the api_key_wo
# argument on aws_bedrockagentcore_api_key_credential_provider. ARNs are not secrets;
# only the API keys themselves (write-only) are.
locals {
  filtered_identity_providers = nonsensitive({
    for k, v in module.identity_providers.credential_provider_arns : k => v
  })
}

# ============================================================
# Per-engine Secrets Manager containers (values seeded out-of-band)
# ============================================================
locals {
  tool_secret_engines = {
    serper        = "Serper SERP API key"
    exa           = "Exa neural search API key"
    perplexity    = "Perplexity Sonar API key"
    brave         = "Brave Search API key"
    anthropic     = "Anthropic Claude API key"
    firecrawl     = "Firecrawl web search API key"
    you           = "You.com search API key"
    tavily_lambda = "Tavily API key (Lambda tool)"
  }
}

module "tool_secret" {
  for_each = local.tool_secret_engines
  source   = "../../modules/tool-gateway/tool-secret"

  name        = "${var.project_name}/${var.environment}/tool/${each.key}"
  description = each.value
  tags = {
    Environment = var.environment
    Engine      = each.key
  }
}

locals {
  tool_secret_arns = { for k, m in module.tool_secret : k => m.secret_arn }
}

# ============================================================
# Lambda Tools (conditional for each enabled engine)
# ============================================================

module "lambda_tools" {
  for_each = local.enabled_lambda_tools

  source = "../../modules/tool-gateway/gateway-lambda-tool"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region
  account_id   = local.account_id

  tool_name   = each.key
  source_root = local.tools_root

  vpc_config = null

  secret_arn = lookup(local.tool_secret_arns, each.key, "")

  enable_secret_policy = contains(keys(local.tool_secret_engines), each.key)

  env_vars = merge(
    # AgentCore Identity provider ARN (vault fallback path; kept for MCP-token parity).
    contains(keys(local.filtered_identity_providers), each.key) ? {
      IDENTITY_PROVIDER_ARN = local.filtered_identity_providers[each.key]
    } : {},
    # Secrets Manager ARN for the engine's API key (primary path). The Lambda
    # fetches the value at runtime with its own IAM role — no key in env/state.
    lookup(local.tool_secret_arns, each.key, "") != "" ? {
      "${upper(each.key == "tavily_lambda" ? "tavily" : each.key)}_SECRET_ARN" = local.tool_secret_arns[each.key]
    } : {},
  )

  timeout            = 60
  memory_size        = 512
  log_retention_days = 7

  depends_on = [module.identity_providers, module.tool_secret]
}

# ============================================================
# MCP Server Targets (external MCP servers)
# ============================================================

module "gateway_mcp_targets" {
  source = "../../modules/tool-gateway/gateway-mcp-target"

  mcp_server_targets = {
    for k, v in local.enabled_mcp_targets : k => {
      endpoint            = v.endpoint
      credential_provider = local.filtered_identity_providers[k]
      description         = "${k} hosted MCP server"
    }
  }
}

# ============================================================
# AgentCore Gateway
# ============================================================

module "gateway" {
  source = "../../modules/tool-gateway/gateway"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region
  account_id   = local.account_id

  cognito_issuer_url      = module.auth.issuer_url
  cognito_allowed_clients = [module.auth.app_client_id, module.auth.web_client_id, module.auth.m2m_client_id]

  lambda_tool_arns = {
    for k, m in module.lambda_tools : k => m.function_arn
  }

  mcp_server_targets     = module.gateway_mcp_targets.mcp_server_endpoints
  mcp_server_credentials = module.gateway_mcp_targets.mcp_server_credentials

  # How each external MCP server expects its API key to be injected.
  # Tavily's hosted MCP server reads the key from the `tavilyApiKey` query param.
  mcp_server_credential_location = {
    tavily = "QUERY_PARAMETER"
  }
  mcp_server_credential_param = {
    tavily = "tavilyApiKey"
  }

  enable_web_search = var.enable_web_search

  depends_on = [module.auth, module.lambda_tools, module.gateway_mcp_targets]
}

# ============================================================
# Observability (CloudWatch Logs + Vended Logs)
# ============================================================

module "observability" {
  source = "../../modules/tool-gateway/observability"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region

  gateway_id         = module.gateway.gateway_id
  gateway_arn        = module.gateway.gateway_arn
  enable_otlp_export = var.enable_otlp_export
  otlp_endpoint      = var.otlp_endpoint

  trace_sampling_percentage  = var.trace_sampling_percentage
  manage_trace_indexing_rule = var.manage_trace_indexing_rule

  depends_on = [module.gateway]
}
