# A self-contained environment: VPC, ALB, ECS services and every backing
# resource the platform needs, all named from `var.project`.
#
# The full stack: every resource is named from `var.project` (default `bap`), so
# the whole deployment shares one greppable prefix.

locals {
  # Service discovery is per-VPC, but the namespace is a global name in Cloud
  # Map, so it is prefixed like everything else. Held here rather than read back
  # from the ecs module's output, which the module's own input cannot reference.
  namespace_name = "${var.project}.local"

  # The stack's own source bucket plus anything the operator added. Both the IAM
  # grant and the server's picker read this one list, so a bucket cannot be
  # offered in the UI without also being readable by Bedrock.
  knowledge_source_buckets = compact(concat(
    [module.s3_knowledge.source_bucket_name],
    var.knowledge_source_buckets,
  ))

  # TLS is on exactly when a certificate is named. Known at plan time (it is a
  # plain variable, not a resource attribute), which the listener counts need.
  https_enabled = var.certificate_arn != ""

  # What users type in the address bar. With TLS this must be a name the
  # certificate covers; without it the ALB's own DNS name is the only option.
  public_host = var.public_host != "" ? var.public_host : module.ecs.alb_dns_name

  # Login providers besides the Cognito password form. One Microsoft Entra ID
  # entry when `oidc_issuer_url` is set, nothing otherwise — the server then
  # offers only the password login. The browser does Authorization Code + PKCE
  # against the issuer and the server verifies the id_token with the audience
  # below; `admin_groups` / `admin_emails` are the server's role rules and are
  # never sent to the browser (/api/auth/config omits them).
  #
  # The callback must be registered on the Entra app as an SPA redirect URI.
  # With no explicit `oidc_redirect_uris` it is derived from public_host, which
  # needs TLS (Entra refuses http:// except localhost).
  oidc_redirect_uris = (
    length(var.oidc_redirect_uris) > 0
    ? var.oidc_redirect_uris
    : (var.public_host != "" ? ["https://${var.public_host}/auth/callback"] : [])
  )
  oidc_providers = var.oidc_issuer_url == "" ? [] : [
    {
      id             = "entra"
      label          = var.oidc_provider_label
      issuer_url     = var.oidc_issuer_url
      audience       = var.oidc_audience != "" ? var.oidc_audience : var.oidc_client_id
      client_id      = var.oidc_client_id
      redirect_uris  = local.oidc_redirect_uris
      scopes         = "openid profile email offline_access"
      groups_claim   = "groups"
      required_group = var.oidc_required_group
      admin_groups   = var.oidc_admin_groups
      admin_emails   = var.oidc_admin_emails
    },
  ]
}

module "network" {
  source            = "../../modules/network"
  project           = var.project
  azs               = var.azs
  alb_ingress_cidrs = var.alb_ingress_cidrs
  enable_https      = local.https_enabled
  hibernate         = var.hibernate
}

module "dynamodb" {
  source = "../../modules/dynamodb"

  # Unlike every other module, this one's names do not derive from `project`, so
  # they have to be namespaced by hand — `agent-artifacts` already exists in the
  # account and Terraform would fail trying to create it again.
  table_name           = "${var.project}-threads"
  artifacts_table_name = "${var.project}-artifacts"
  knowledge_table_name = "${var.project}-knowledge-bases"
  usage_table_name     = "${var.project}-usage"
  prefs_table_name     = "${var.project}-prefs"
  users_table_name     = "${var.project}-users"
  allow_destroy        = var.allow_destroy
}

module "ecr" {
  source = "../../modules/ecr"

  repositories = [
    "${var.project}/server",
    "${var.project}/web",
    "${var.project}/agent-runtime",
  ]
}

module "cognito" {
  source         = "../../modules/cognito"
  project        = var.project
  admin_email    = var.admin_email
  admin_password = var.admin_password
  user_email     = var.user_email
  user_password  = var.user_password
}

# Agent Registry. Off (enable_agent_registry=false) where an org SCP blocks the
# bedrock-agentcore registry actions — AGENT_REGISTRY_ID then wires empty and
# auto-registration is off. Replaces commenting the module out by hand.
module "agent_registry" {
  count         = var.enable_agent_registry ? 1 : 0
  source        = "../../modules/agent_registry"
  project       = var.project
  region        = var.region
  auto_approval = var.registry_auto_approval
  python_bin    = var.registry_python_bin
}

# The activation status is account-global: two states declaring the same keys
# would fight over one resource, so a second stack in the same account sets
# activate_cost_allocation_tags = false.
module "cost_allocation_tags" {
  source = "../../modules/cost_allocation_tags"
  count  = var.activate_cost_allocation_tags ? 1 : 0
}

module "bedrock_guardrail" {
  source  = "../../modules/bedrock_guardrail"
  project = var.project
}

module "mcp_gateway" {
  source              = "../../modules/mcp_gateway"
  project             = var.project
  region              = var.region
  web_search_backend  = var.web_search_backend
  runtime_mcp_servers = var.runtime_mcp_servers
}

module "s3_skills" {
  source        = "../../modules/s3_skills"
  project       = var.project
  region        = var.region
  bucket_suffix = var.bucket_suffix
}

module "s3_artifacts" {
  source        = "../../modules/s3_artifacts"
  project       = var.project
  region        = var.region
  bucket_suffix = var.bucket_suffix
}

module "s3_knowledge" {
  source        = "../../modules/s3_knowledge"
  project       = var.project
  region        = var.region
  bucket_suffix = var.bucket_suffix
}

module "knowledge_roles" {
  source               = "../../modules/knowledge_roles"
  project              = var.project
  region               = var.region
  knowledge_bucket_arn = module.s3_knowledge.bucket_arn
  source_buckets       = local.knowledge_source_buckets
}

module "harness_role" {
  source            = "../../modules/harness_role"
  project           = var.project
  region            = var.region
  skills_bucket_arn = module.s3_skills.bucket_arn
}

# Execution role for the hand-deployed agent runtimes (agent-runtime/scripts/
# deploy.sh with EXECUTION_ROLE set). Mirrors the AgentCore toolkit's managed
# policy plus the InvokeGateway grant the toolkit omits — see the module.
module "agent_runtime_role" {
  source  = "../../modules/agent_runtime_role"
  project = var.project
  region  = var.region
}

# Per-session runtime usage: the log group and delivery destination the server
# points every runtime's USAGE_LOGS at. See modules/observability.
module "observability" {
  source  = "../../modules/observability"
  project = var.project
}

module "iam" {
  source                      = "../../modules/iam"
  project                     = var.project
  region                      = var.region
  dynamodb_table_arn          = module.dynamodb.table_arn
  skills_bucket_arn           = module.s3_skills.bucket_arn
  artifacts_table_arn         = module.dynamodb.artifacts_table_arn
  artifacts_bucket_arn        = module.s3_artifacts.bucket_arn
  harness_execution_role_arn  = module.harness_role.role_arn
  knowledge_bucket_arn        = module.s3_knowledge.bucket_arn
  knowledge_table_arn         = module.dynamodb.knowledge_table_arn
  kb_service_role_arn         = module.knowledge_roles.kb_service_role_arn
  kb_gateway_role_arn         = module.knowledge_roles.kb_gateway_role_arn
  knowledge_source_bucket_arn = module.s3_knowledge.source_bucket_arn
  browser_screenshot_bucket_arn = (
    var.browser_screenshot_bucket != "" ?
    "arn:aws:s3:::${var.browser_screenshot_bucket}" : ""
  )
  usage_table_arn = module.dynamodb.usage_table_arn
  prefs_table_arn = module.dynamodb.prefs_table_arn
  users_table_arn = module.dynamodb.users_table_arn
  user_pool_arn   = module.cognito.user_pool_arn
}

module "ecs" {
  source             = "../../modules/ecs"
  project            = var.project
  region             = var.region
  vpc_id             = module.network.vpc_id
  public_subnet_ids  = module.network.public_subnet_ids
  private_subnet_ids = module.network.private_subnet_ids
  alb_sg_id          = module.network.alb_sg_id
  web_sg_id          = module.network.web_sg_id
  server_sg_id       = module.network.server_sg_id
  execution_role_arn = module.iam.execution_role_arn
  task_role_arn      = module.iam.task_role_arn
  server_image       = var.server_image
  web_image          = var.web_image
  desired_count      = var.desired_count
  enable_https       = local.https_enabled
  certificate_arn    = var.certificate_arn
  enable_http2       = var.alb_enable_http2

  namespace_name = local.namespace_name

  server_env = {
    AWS_REGION             = var.region
    PLATFORM               = var.project
    MODEL_COST_AIP_ENABLED = tostring(var.enable_model_cost_aip)
    DYNAMODB_THREADS_TABLE = module.dynamodb.table_name
    BEDROCK_MODEL_ID       = var.bedrock_model_id
    # Basic chat answers from the default runtime (AGENT_RUNTIME_ARN) with one
    # of these models; the server refuses any other.
    BASIC_CHAT_ALLOWED_MODELS   = join(",", var.basic_chat_allowed_models)
    COGNITO_USER_POOL_CLIENT_ID = module.cognito.client_id
    COGNITO_USER_POOL_ID        = module.cognito.user_pool_id
    COGNITO_REGION              = var.region
    # OIDC login providers (Microsoft Entra ID). Empty array = password login
    # only. The server routes each bearer token by `iss` to Cognito or to one
    # of these (server/core/auth.py).
    OIDC_PROVIDERS_JSON             = jsonencode(local.oidc_providers)
    USERS_TABLE                     = module.dynamodb.users_table_name
    AGENT_RUNTIME_ARN               = var.agent_runtime_arn
    AGENT_RUNTIME_DISCOVERY_REGIONS = var.agent_runtime_discovery_regions
    AGENT_REGISTRY_ID               = var.enable_agent_registry ? module.agent_registry[0].registry_id : ""
    HARNESS_EXECUTION_ROLE_ARN      = module.harness_role.role_arn
    SKILLS_BUCKET                   = module.s3_skills.bucket_name
    ARTIFACTS_BUCKET                = module.s3_artifacts.bucket_name
    ARTIFACTS_TABLE                 = module.dynamodb.artifacts_table_name
    USAGE_TABLE                     = module.dynamodb.usage_table_name
    PREFS_TABLE                     = module.dynamodb.prefs_table_name
    # The insights collector runs inside the server task (one per stack; a DDB
    # lease guards a second replica) and reads per-session usage from the
    # vended log group below.
    COLLECTOR_ENABLED         = "true"
    USAGE_LOG_GROUP           = module.observability.log_group_name
    USAGE_LOG_DESTINATION_ARN = module.observability.destination_arn
    BROWSER_SCREENSHOT_BUCKET = var.browser_screenshot_bucket
    KNOWLEDGE_BUCKET          = module.s3_knowledge.bucket_name
    KNOWLEDGE_TABLE           = module.dynamodb.knowledge_table_name
    KB_SERVICE_ROLE_ARN       = module.knowledge_roles.kb_service_role_arn
    KB_GATEWAY_ROLE_ARN       = module.knowledge_roles.kb_gateway_role_arn
    # Same list the kb-service role was granted above, so the buckets the UI
    # offers are exactly the ones Bedrock can read.
    KNOWLEDGE_SOURCE_BUCKETS = join(",", local.knowledge_source_buckets)
    # The stack's own source bucket, which managed S3 knowledge bases write to.
    KNOWLEDGE_PLATFORM_SOURCE_BUCKET = module.s3_knowledge.source_bucket_name
  }

  web_env = {
    # The web container proxies API calls at runtime rather than baking in a
    # destination; see web/src/middleware.ts.
    BACKEND_ORIGIN = "http://server.${local.namespace_name}:8000"
    # An https page cannot embed an http sandbox (mixed content), so with TLS the
    # sandbox moves to :8443 on the same host — a different port is still a
    # different browser origin, and one certificate covers both.
    SANDBOX_ORIGIN = (
      local.https_enabled
      ? "https://${local.public_host}:8443"
      : "http://${module.ecs.alb_dns_name}:8081"
    )
  }
}

module "alarms" {
  source                  = "../../modules/alarms"
  project                 = var.project
  alert_email             = var.alert_email
  alb_arn_suffix          = module.ecs.alb_arn_suffix
  target_group_arn_suffix = module.ecs.target_group_arn_suffix
  usage_table_name        = module.dynamodb.usage_table_name
}
