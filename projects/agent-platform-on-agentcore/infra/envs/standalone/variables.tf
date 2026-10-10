variable "project" {
  description = "Name prefix for every resource in this environment."
  type        = string
  default     = "bap"
}

variable "cost_center" {
  description = "CostCenter tag applied to every resource for billing allocation."
  type        = string
  default     = "bap"
}

variable "enable_model_cost_aip" {
  description = <<-EOT
    Route each harness's model call through a per-agent application inference
    profile so Bedrock model spend is attributable per agent (AgentName tag),
    which fills the per-agent model cost on /insights with actual billed dollars.

    Off by default: turn on only after confirming AgentCore accepts an inference
    profile ARN as modelId (see docs/superpowers live-verification). Any AIP
    failure falls back to the base model id, so harness creation is never blocked.
    Sets the server's MODEL_COST_AIP_ENABLED.
  EOT
  type        = bool
  default     = false
}

variable "region" {
  type    = string
  default = "ap-northeast-1"
}

variable "azs" {
  description = "ALB requires two AZs, so both subnet lists are two long."
  type        = list(string)
  default     = ["ap-northeast-1a", "ap-northeast-1c"]
}

variable "bedrock_model_id" {
  type    = string
  default = "global.anthropic.claude-sonnet-5-5"
}

# Models a per-thread override may pick, for every agent (runtime or harness),
# in picker order. The server refuses any other; a team's list can only narrow
# this one. Empty list = no model override is offered. Use inference-profile
# ids (global.*): Claude 5 foundation-model ids are rejected by ConverseStream
# with an on-demand-throughput ValidationException.
variable "allowed_models" {
  type        = list(string)
  description = "Models a per-thread override may pick, for every agent, in picker order. Empty disables model overrides."
  default = [
    "global.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-opus-5-5",
    "global.anthropic.claude-haiku-5-5",
  ]
}

variable "admin_email" {
  type = string
}

variable "admin_password" {
  type      = string
  sensitive = true
}

variable "user_email" {
  type = string
}

variable "user_password" {
  type      = string
  sensitive = true
}

variable "server_image" {
  description = <<-EOT
    Full ECR image URI for the server, with tag.

    Defaults to a public placeholder because the ECR repositories are created by
    this same apply: on the first run there is nothing to reference yet. Push the
    real images, then set this and re-apply.
  EOT
  type        = string
  default     = "public.ecr.aws/docker/library/busybox:latest"
}

variable "web_image" {
  description = "Full ECR image URI for web, with tag. See `server_image` for why it starts as a placeholder."
  type        = string
  default     = "public.ecr.aws/docker/library/busybox:latest"
}

variable "agent_runtime_arn" {
  description = "Default AgentCore runtime the chat invokes. Optional: the UI can also pick one from the registry."
  type        = string
  default     = ""
}

variable "registry_auto_approval" {
  description = "Approve registry records on creation instead of requiring manual approval."
  type        = bool
  default     = true
}

variable "activate_cost_allocation_tags" {
  description = <<-EOT
    Hold `Platform`/`AgentName` Active as cost allocation tags, which is what
    gives /insights per-agent billed cost. Turn this off for the very first apply
    into a fresh account: Billing rejects a key it has not yet seen on billed
    usage, and that lags up to 24 hours after the first harness runs.
  EOT
  type        = bool
  default     = true
}

variable "web_search_backend" {
  description = "Web search on the MCP gateway: agentcore (native AgentCore Web Search connector, default) or none."
  type        = string
  default     = "agentcore"
}

variable "runtime_mcp_servers" {
  description = "MCP servers on AgentCore Runtime to attach to the MCP gateway: target name => runtime ARN. Set after deploying mcp-apps-server, e.g. { \"platform-status\" = \"<bap_platform_status runtime ARN>\" }."
  type        = map(string)
  default     = {}
}

variable "desired_count" {
  description = "Tasks per service. 0 parks the environment without destroying it."
  type        = number
  default     = 1
}

variable "knowledge_source_buckets" {
  description = <<-EOT
    Existing buckets users may attach as knowledge base sources, by name.

    Empty leaves uploads as the only kind of knowledge base. Each name here grants
    the kb-service role read access and appears in the UI's bucket picker, so a new
    bucket is a deploy — see modules/knowledge_roles for why that is preferred over
    letting the server edit IAM at runtime.
  EOT
  type        = list(string)
  default     = []
}

variable "browser_screenshot_bucket" {
  description = <<-EOT
    The built-in-tools gateway's screenshot bucket, by name.

    Created outside Terraform by infra/builtin_tools_gateway/deploy.py, which prints
    it on `up` (it is named "<project>-builtin-tools-<account>"). Setting it lets the
    chat re-render a browser screenshot after the Lambda's presigned URL has expired;
    empty leaves that route reporting 503 and the image simply stops loading after an
    hour.
  EOT
  type        = string
  default     = ""
}

variable "alert_email" {
  type        = string
  default     = ""
  description = "Address subscribed to the insights alerts topic. Empty disables it, so a fresh apply is silent."
}

variable "alb_ingress_cidrs" {
  description = "CIDRs allowed inbound to the ALB (80/8081, plus 443/8443 with TLS). Default open; narrow to office IPs or an external HTTPS proxy to lock the deployment down."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = <<-EOT
    ACM certificate ARN for the ALB, in this region. Setting it turns on TLS:
    :443 (web) and :8443 (MCP Apps sandbox) listeners, :80 redirecting to :443,
    and the sandbox origin moving to https. Empty keeps the HTTP-only ALB.

    Browser features that need a secure context (PKCE, clipboard, randomUUID)
    and IdPs that refuse http redirect URIs both need this. Pair it with
    public_host so the sandbox origin is a name the certificate covers.
  EOT
  type        = string
  default     = ""
}

variable "public_host" {
  description = "Hostname users open (a DNS name pointing at the ALB that the certificate covers). Used for the https sandbox origin; empty falls back to the ALB DNS name, which no certificate covers."
  type        = string
  default     = ""
}

variable "alb_enable_http2" {
  description = "ALB HTTP/2. Set false behind corporate SSL-inspection proxies that break h2 framing (static chunks fail with ERR_HTTP2_PROTOCOL_ERROR); the ALB then serves HTTP/1.1."
  type        = bool
  default     = true
}

variable "enable_agent_registry" {
  description = "Create the AgentCore Agent Registry. Set false where an org SCP blocks the bedrock-agentcore registry actions; AGENT_REGISTRY_ID is then empty and auto-registration is off."
  type        = bool
  default     = true
}

variable "bucket_suffix" {
  description = "Optional suffix for S3 bucket global uniqueness, passed to the s3 modules. Empty keeps the bare <project>-<purpose>-<region> names; set e.g. -<account_id> for a stack whose bare names collide."
  type        = string
  default     = ""
}

variable "agent_runtime_discovery_regions" {
  description = "Comma-separated extra AWS regions whose AgentCore runtimes are also offered when registering an agent (e.g. \"ap-northeast-2\"). Empty = this region only. Invocation always targets the region in each runtime ARN."
  type        = string
  default     = ""
}

variable "registry_custom_metadata_schema" {
  description = "Custom metadata schema for registry records: record type (DEFAULT, MCP, AGENT, SKILL, CUSTOM, GATEWAY) => JSON Schema string. Empty uses the module default (owner, team, tier enum; all optional, owner filled by the server). AWS only lets a schema grow, so never remove or retype a saved field."
  type        = map(string)
  default     = {}
}

variable "registry_kms_key_arn" {
  description = "Customer managed KMS key for the registry. Creation-time only: set it before the first apply; an existing registry keeps its key."
  type        = string
  default     = ""
}

variable "registry_tool_on_gateway" {
  description = "Attach the registry's MCP endpoint to the platform gateway as the \"registry\" target, so agents and harnesses can search the catalog at run time."
  type        = bool
  default     = true
}

variable "registry_python_bin" {
  description = "Python interpreter for the agent_registry module's registry.py (needs botocore >= 1.43 with the agent-registry-control model). Defaults to system python3; override with a venv python where that is too old."
  type        = string
  default     = "python3"
}

# ── Microsoft Entra ID login (optional second identity provider) ─────────────
# Leave `oidc_issuer_url` empty and the login screen offers only the Cognito
# password form. Fill these from the Entra app registration (an SPA platform
# with https://<public_host>/auth/callback as redirect URI, and the `groups`
# claim added to the ID token) and both logins appear side by side.

variable "oidc_issuer_url" {
  description = "Entra OIDC issuer. Must be the v2.0 endpoint: https://login.microsoftonline.com/<tenant-id>/v2.0. Empty = no Entra login."
  type        = string
  default     = ""
}

variable "oidc_client_id" {
  description = "Entra Application (client) ID used for the browser PKCE login."
  type        = string
  default     = ""
}

variable "oidc_audience" {
  description = "Expected `aud` of the id_token. Defaults to oidc_client_id, which is what Entra puts there."
  type        = string
  default     = ""
}

variable "oidc_redirect_uris" {
  description = "Callback URLs registered on the Entra app, e.g. [\"https://agents.example.com/auth/callback\", \"http://localhost:3000/auth/callback\"]. Empty derives https://<public_host>/auth/callback. The server hands each browser the one matching its Origin."
  type        = list(string)
  default     = []
}

variable "oidc_provider_label" {
  description = "Button text on the login screen."
  type        = string
  default     = "Microsoft 계정 (Entra ID)"
}

variable "oidc_required_group" {
  description = "Only members of this Entra group (display name or object id, as the groups claim emits it) may sign in; others get 403. Empty admits the whole tenant."
  type        = string
  default     = ""
}

variable "oidc_admin_groups" {
  description = "Entra groups whose members get the admin role (the Cognito equivalent is the `admin` group)."
  type        = list(string)
  default     = []
}

variable "oidc_admin_emails" {
  description = "Entra sign-in emails that get the admin role regardless of group."
  type        = list(string)
  default     = []
}

# ────── Hibernation and destruction safeguards ─────────────────────────────

variable "hibernate" {
  description = "Delete NAT gateway and EIP, removing private-subnet egress. Must be paired with desired_count=0 or tasks lose egress."
  type        = bool
  default     = false
}

variable "allow_destroy" {
  description = "Disable DynamoDB deletion protection before running destroy. Apply this first: `terraform apply -var allow_destroy=true`, then `terraform destroy`."
  type        = bool
  default     = false
}

# Teams. Each name becomes a Cognito group `team:<name>`, a harness execution
# role `${project}-harness-<name>`, and (with enable_gateway_policy) a set of
# Cedar policies naming the tools that role may call. Empty = no team features.
variable "teams" {
  type        = list(string)
  description = "Team names (lowercase, [a-z0-9-]). Empty disables every team feature."
  default     = []

  validation {
    # "execution" would collide with the default harness role
    # `${project}-harness-execution`; the length keeps the longest policy name
    # (`<project>_team_<team>_expense_limit`) near the 48-char limit.
    condition = alltrue([
      for t in var.teams : can(regex("^[a-z0-9-]{1,16}$", t)) && t != "execution"
    ])
    error_message = "Each team name must match ^[a-z0-9-]+$ (at most 16 characters) and must not be \"execution\"."
  }
}

variable "team_allowed_model_arns" {
  type        = map(string)
  description = "team name => comma-separated Bedrock model/inference-profile ARNs that team's harness role may invoke. A team not listed (or an empty value) may invoke any model."
  default     = {}
}

variable "enable_gateway_policy" {
  type        = bool
  description = "Create an AgentCore policy engine and attach it to the Lambda tools gateway with per-team Cedar policies."
  default     = false
}

variable "policy_mode" {
  type        = string
  description = "Gateway policy mode when enable_gateway_policy: LOG_ONLY (observe) or ENFORCE (deny)."
  default     = "LOG_ONLY"
}

variable "team_shared_actions" {
  type        = list(string)
  description = "Fully-qualified Cedar actions (<target>___<tool>) every team harness role may call on the MCP gateway beyond its Lambda tools. Empty = the web-search connector's tool (<project>-web-search___WebSearch) when web_search_backend = agentcore, else none. Every entry must exist on the gateway: an unknown action fails the whole policy."
  default     = []
}

variable "team_tools" {
  type        = map(string)
  description = "team => comma-separated tool names its harness role may call through the Lambda tools gateway. Teams not listed get no team policy (every call denied under ENFORCE)."
  default = {
    finance = "fetch_url,current_time,calculate,create_artifact,update_artifact,approve_expense"
    hr      = "fetch_url,current_time,calculate,create_artifact,update_artifact,lookup_salary"
    support = "fetch_url,current_time,calculate,create_artifact,update_artifact"
  }
}

variable "extra_platform_role_names" {
  type        = list(string)
  description = "Additional IAM role names allowed to call every gateway tool under the policy engine, e.g. a hand-deployed runtime that runs as the toolkit's AmazonBedrockAgentCoreSDKRuntime-* role instead of <project>-agent-runtime. Under ENFORCE a caller without a permit sees no tools."
  default     = []
}

variable "demo_tools" {
  type        = bool
  description = "Workshop mock tools (approve_expense, lookup_salary) on the Lambda tools target. They return canned results; leave off outside the workshop."
  default     = false
}
