# Managed Agent Harness execution role.
#
# Assumed by AgentCore when running a harness session. Kept separate from the ECS
# task role in modules/iam: the task role *creates* harnesses, this role is what
# they *run as*. Its own module so it can be applied without the ECS stack.

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "harness_assume" {
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

resource "aws_iam_role" "harness_execution" {
  name               = "${var.project}-harness-${var.role_suffix}"
  assume_role_policy = data.aws_iam_policy_document.harness_assume.json
}

data "aws_iam_policy_document" "harness_execution" {
  statement {
    sid    = "BedrockModelInvocation"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = var.allowed_model_arns
  }

  statement {
    sid    = "ContainerImagePull"
    effect = "Allow"
    actions = [
      # The harness pulls its agent image from ECR Public at session start.
      "ecr-public:GetAuthorizationToken",
      "sts:GetServiceBearerToken",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "Observability"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
      "cloudwatch:PutMetricData",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "WorkloadIdentity"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    ]
    resources = ["*"]
  }

  # The session APIs alone are not enough for the *built-in* browser tool. It
  # drives the session over a CDP WebSocket rather than the InvokeBrowser API, and
  # opening that socket is its own action — without it the tool starts a session
  # and then dies on "403 Forbidden — User is not authorized to access automation
  # stream", which reaches the model as a failed tool call.
  #
  # Easy to miss because the tools this platform builds itself do not need it: the
  # built-in-tools gateway Lambda calls InvokeBrowser, which is covered above.
  #
  # ConnectBrowserLiveViewStream is deliberately *not* granted. That is the
  # watch-and-take-over stream, which nothing here consumes; adding it would hand
  # every harness the ability to open a viewer onto a live session.
  statement {
    sid    = "BuiltInTools"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:StartBrowserSession",
      "bedrock-agentcore:StopBrowserSession",
      "bedrock-agentcore:GetBrowserSession",
      "bedrock-agentcore:ListBrowserSessions",
      "bedrock-agentcore:ConnectBrowserAutomationStream",
      "bedrock-agentcore:StartCodeInterpreterSession",
      "bedrock-agentcore:StopCodeInterpreterSession",
      "bedrock-agentcore:GetCodeInterpreterSession",
      "bedrock-agentcore:ListCodeInterpreterSessions",
      "bedrock-agentcore:InvokeCodeInterpreter",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "InvokeGateway"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = ["*"]
  }

  statement {
    sid    = "AgentCoreMemory"
    effect = "Allow"
    actions = [
      # Managed memory is provisioned by the harness itself, but the running
      # session is what reads and writes the conversation's events — without
      # these a memoryful harness fails at invoke time rather than at create.
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:ListSessions",
      "bedrock-agentcore:RetrieveMemoryRecords",
      "bedrock-agentcore:GetMemoryRecord",
      "bedrock-agentcore:ListMemoryRecords",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "SkillsBucketRead"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = [
      var.skills_bucket_arn,
      "${var.skills_bucket_arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "harness_execution" {
  name   = "${var.project}-harness-${var.role_suffix}-policy"
  role   = aws_iam_role.harness_execution.id
  policy = data.aws_iam_policy_document.harness_execution.json
}
