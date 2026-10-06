terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.50"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  gateway_name        = "${var.project}-gateway"
  target_name         = "${var.project}-platform-tools"
  websearch_target    = "${var.project}-web-search"
  websearch_connector = "web-search"
  script              = "${path.module}/scripts/gateway_target.py"

  # Pin the managed web-search connector to a version; empty means the gateway's
  # default. Passed as a flag only when set, so an empty value cannot swallow the
  # following argument on the provisioner command line.
  websearch_version_arg = var.web_search_connector_version != "" ? ["--connector-version", var.web_search_connector_version] : []
  tool_schema           = "${path.module}/tools.json"

  # Which backend serves web search on the gateway:
  #   agentcore -> native AgentCore Web Search connector target (no API key)
  #   none      -> no web search on the gateway
  use_agentcore_search = var.web_search_backend == "agentcore"
}

# --- Tool Lambda -------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project}-tools-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/.build/tools.zip"
  excludes    = ["__pycache__"]
}

resource "aws_lambda_function" "tools" {
  function_name    = "${var.project}-tools"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
}

resource "aws_lambda_permission" "gateway" {
  statement_id  = "AllowAgentCoreGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tools.function_name
  principal     = "bedrock-agentcore.amazonaws.com"
  source_arn    = awscc_bedrockagentcore_gateway.this.gateway_arn
}

# --- Gateway -----------------------------------------------------------------

data "aws_iam_policy_document" "gateway_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${var.project}-gateway"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
}

# The native Web Search connector target invokes the managed tool through the
# gateway, so the role needs InvokeGateway + InvokeWebSearch — but only when that
# backend is selected. The web-search tool is service-owned (account "aws").
locals {
  websearch_iam_statements = local.use_agentcore_search ? [
    {
      Sid    = "WebSearch"
      Effect = "Allow"
      Action = [
        "bedrock-agentcore:InvokeGateway",
        "bedrock-agentcore:InvokeWebSearch",
      ]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:gateway/*",
        "arn:aws:bedrock-agentcore:${var.region}:aws:tool/web-search.v1",
      ]
    }
  ] : []
}

# The gateway assumes this role to invoke targets on the agent's behalf.
resource "aws_iam_role_policy" "gateway" {
  name = "${var.project}-gateway-policy"
  role = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.tools.arn]
      },
      {
        # An AgentCore *runtime* running an MCP server can be attached as an
        # mcpServer target. Its endpoint requires SigV4, which the gateway signs
        # with this role — a harness `remote_mcp` tool cannot, because
        # `remoteMcp` only accepts static `headers`. Without this the target
        # fails to create with "Authorization error when sending message".
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = ["*"]
      },
    ], local.websearch_iam_statements)
  })
}

# Gateway itself has an awscc resource; its targets do not (see scripts/).
resource "awscc_bedrockagentcore_gateway" "this" {
  name            = local.gateway_name
  role_arn        = aws_iam_role.gateway.arn
  authorizer_type = "AWS_IAM"

  # runtime과 harness 모두 실행 역할 SigV4로 호출한다. bearer 토큰 경로는 제거됐다
  # (agent-runtime/auth/sigv4.py). authorizer 는 생성 후 변경 불가라 이 변경은 게이트웨이를
  # 재생성한다.

  # protocol_type is intentionally omitted (see below).
}

# awscc 게이트웨이 리소스는 tags 인자를 지원하지 않으므로 control-plane TagResource로
# 부착한다. aws_* 리소스는 provider default_tags 로 이미 태그된다.
resource "null_resource" "gateway_tags" {
  triggers = {
    gateway_arn = awscc_bedrockagentcore_gateway.this.gateway_arn
    project     = var.project
  }
  provisioner "local-exec" {
    command = join(" ", [
      "python3", local.script, "tag",
      "--gateway-arn", awscc_bedrockagentcore_gateway.this.gateway_arn,
      "--tags", "Platform=${var.project}", "Project=${var.project}",
      "--region", var.region,
    ])
  }
}

# --- Gateway target ----------------------------------------------------------

resource "null_resource" "target" {
  triggers = {
    gateway_id  = awscc_bedrockagentcore_gateway.this.gateway_identifier
    lambda_arn  = aws_lambda_function.tools.arn
    schema_sha  = filesha256(local.tool_schema)
    script_sha  = filesha256(local.script)
    target_name = local.target_name
    # Destroy-time provisioners cannot read vars, so the command is frozen here.
    delete_command = join(" ", [
      "python3", abspath(local.script), "delete",
      "--gateway-id", awscc_bedrockagentcore_gateway.this.gateway_identifier,
      "--name", local.target_name,
      "--region", var.region,
    ])
  }

  provisioner "local-exec" {
    command = join(" ", [
      "python3", local.script, "upsert",
      "--gateway-id", awscc_bedrockagentcore_gateway.this.gateway_identifier,
      "--name", local.target_name,
      "--lambda-arn", aws_lambda_function.tools.arn,
      "--schema", local.tool_schema,
      "--region", var.region,
    ])
  }

  provisioner "local-exec" {
    when    = destroy
    command = self.triggers.delete_command
  }

  depends_on = [aws_iam_role_policy.gateway, aws_lambda_permission.gateway]
}

# --- Web search target (native connector) ------------------------------------

# The managed Web Search tool is a real gateway target (connectorId "web-search"),
# not a Lambda. awscc still has no gateway_target resource, so it goes through the
# same boto3 script. Offered in us-east-1, eu-west-1 and ap-northeast-1.
resource "null_resource" "websearch_target" {
  count = local.use_agentcore_search ? 1 : 0
  triggers = {
    gateway_id        = awscc_bedrockagentcore_gateway.this.gateway_identifier
    connector         = local.websearch_connector
    connector_version = var.web_search_connector_version
    script_sha        = filesha256(local.script)
    target_name       = local.websearch_target
    # Destroy-time provisioners cannot read vars, so the command is frozen here.
    delete_command = join(" ", [
      "python3", abspath(local.script), "delete",
      "--gateway-id", awscc_bedrockagentcore_gateway.this.gateway_identifier,
      "--name", local.websearch_target,
      "--region", var.region,
    ])
  }

  provisioner "local-exec" {
    command = join(" ", concat([
      "python3", local.script, "upsert",
      "--gateway-id", awscc_bedrockagentcore_gateway.this.gateway_identifier,
      "--name", local.websearch_target,
      "--connector-id", local.websearch_connector,
      "--region", var.region,
    ], local.websearch_version_arg))
  }

  provisioner "local-exec" {
    when    = destroy
    command = self.triggers.delete_command
  }

  # Serialize with the Lambda target: two CreateGatewayTarget calls racing on the
  # same gateway can fail with "gateway is in UPDATING status", which the script
  # does not retry.
  depends_on = [aws_iam_role_policy.gateway, null_resource.target]
}

# --- MCP servers hosted on AgentCore Runtime -----------------------------------

# A runtime deployed with `-p MCP` (e.g. mcp-apps-server's bap_platform_status) is
# attached as an mcpServer target, so the runtime agent and harnesses reach its
# tools through this one gateway. The gateway role signs each call (SigV4, service
# bedrock-agentcore) with the InvokeAgentRuntime grant above; a harness cannot call
# the runtime's endpoint directly because `remoteMcp` takes only static headers.
locals {
  runtime_mcp_endpoints = {
    for name, arn in var.runtime_mcp_servers :
    # Same shape as bedrock_agentcore.runtime.build_runtime_url (ARN fully encoded).
    name => "https://bedrock-agentcore.${var.region}.amazonaws.com/runtimes/${urlencode(arn)}/invocations"
  }
}

resource "null_resource" "runtime_mcp_target" {
  for_each = local.runtime_mcp_endpoints
  triggers = {
    gateway_id  = awscc_bedrockagentcore_gateway.this.gateway_identifier
    endpoint    = each.value
    script_sha  = filesha256(local.script)
    target_name = each.key
    delete_command = join(" ", [
      "python3", abspath(local.script), "delete",
      "--gateway-id", awscc_bedrockagentcore_gateway.this.gateway_identifier,
      "--name", each.key,
      "--region", var.region,
    ])
  }

  provisioner "local-exec" {
    command = join(" ", [
      "python3", local.script, "upsert",
      "--gateway-id", awscc_bedrockagentcore_gateway.this.gateway_identifier,
      "--name", each.key,
      "--mcp-endpoint", each.value,
      "--region", var.region,
    ])
  }

  provisioner "local-exec" {
    when    = destroy
    command = self.triggers.delete_command
  }

  # Same serialization as the web search target: concurrent CreateGatewayTarget
  # calls on one gateway can fail while it is UPDATING.
  depends_on = [aws_iam_role_policy.gateway, null_resource.target, null_resource.websearch_target]
}
