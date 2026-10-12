# Standalone AgentCore Runtime execution role.
#
# The agent-runtime samples (agent-runtime/scripts/deploy.sh -> `agentcore launch`)
# normally let the starter toolkit auto-create an `AmazonBedrockAgentCoreSDKRuntime-*`
# role. That auto-created role does NOT include `bedrock-agentcore:InvokeGateway`, and
# deploy.sh's own fix-up (`ensure_bedrock_permissions`) runs before the role exists on a
# fresh deploy, so it silently skips — the runtime then gets AccessDenied the moment it
# tries to SigV4-call the AWS_IAM MCP gateway (see agent-runtime/auth/access_token.py).
#
# This role is the declarative alternative: pass its ARN to deploy.sh via
# `EXECUTION_ROLE` (agent-runtime/.env) so `agentcore configure --execution-role` uses it
# instead of auto-creating one. The permission set mirrors the toolkit's managed policy
# (ECR pull, logs, X-Ray, InvokeAgentRuntime, Memory, Identity/workload, Bedrock model)
# and adds the missing InvokeGateway. Distinct from modules/harness_role: that role runs
# harness sessions (ECR *public*, browser tools), this one runs a container-image runtime
# built into the account's *private* ECR.

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "runtime" {
  name               = "${var.project}-agent-runtime"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

locals {
  acct = data.aws_caller_identity.current.account_id
}

data "aws_iam_policy_document" "runtime" {
  # Pull the runtime's container image from the account's private ECR (CodeBuild
  # pushes it there during `agentcore launch`).
  statement {
    sid       = "ECRImageAccess"
    effect    = "Allow"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = ["arn:aws:ecr:${var.region}:${local.acct}:repository/*"]
  }

  statement {
    sid       = "ECRTokenAccess"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "RuntimeLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["arn:aws:logs:${var.region}:${local.acct}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }

  statement {
    sid       = "DescribeLogGroups"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${local.acct}:log-group:*"]
  }

  statement {
    sid    = "Observability"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
      "cloudwatch:PutMetricData",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "BedrockAgentCoreRuntime"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = ["arn:aws:bedrock-agentcore:${var.region}:${local.acct}:runtime/*"]
  }

  # The whole point of this module over the toolkit's auto-created role: the
  # AWS_IAM MCP gateway is SigV4-called by the runtime's own execution role.
  statement {
    sid       = "InvokeGateway"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = ["arn:aws:bedrock-agentcore:${var.region}:${local.acct}:gateway/*"]
  }

  statement {
    sid       = "AgentCoreMemoryCreate"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:CreateMemory"]
    resources = ["*"]
  }

  statement {
    sid    = "AgentCoreMemory"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:GetMemory",
      "bedrock-agentcore:GetMemoryRecord",
      "bedrock-agentcore:ListActors",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:ListSessions",
      "bedrock-agentcore:DeleteEvent",
      "bedrock-agentcore:DeleteMemoryRecord",
      "bedrock-agentcore:RetrieveMemoryRecords",
    ]
    resources = ["arn:aws:bedrock-agentcore:${var.region}:${local.acct}:memory/*"]
  }

  # Workload identity / token vault, scoped to the default directory. Agent-name
  # coupling is dropped (toolkit scopes to `<agent>-*`) so one role serves any
  # AGENT_MODULE deployed under this stack.
  statement {
    sid     = "IdentityApiKey"
    effect  = "Allow"
    actions = ["bedrock-agentcore:GetResourceApiKey"]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:token-vault/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:token-vault/default/apikeycredentialprovider/*",
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:workload-identity-directory/default/workload-identity/*",
    ]
  }

  statement {
    sid     = "IdentityOauth2"
    effect  = "Allow"
    actions = ["bedrock-agentcore:GetResourceOauth2Token"]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:token-vault/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:token-vault/default/oauth2credentialprovider/*",
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:workload-identity-directory/default/workload-identity/*",
    ]
  }

  statement {
    sid    = "IdentityWorkloadToken"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.acct}:workload-identity-directory/default/workload-identity/*",
    ]
  }

  statement {
    sid    = "BedrockModelInvocation"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:ApplyGuardrail",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:${var.region}:${local.acct}:*",
    ]
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "${var.project}-agent-runtime-policy"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}
