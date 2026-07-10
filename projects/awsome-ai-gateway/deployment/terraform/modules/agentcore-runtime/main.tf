# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Bedrock AgentCore Runtime — admin-chat-agent
# ------------------------------------------------------------------------------
# 5-agent BI assistant 를 호스팅. Strands SDK 로 작성된 agent 를 컨테이너
# image (ECR) 로 push 하고 AgentCore Runtime 에 등록.
#
# 본 모듈이 만드는 자원:
#   1. ECR repository (admin-chat-agent image 저장)
#   2. S3 staging bucket (SQL → Code Specialist 데이터 전달, 1일 lifecycle)
#   3. KMS key (S3 + AgentCore 세션 암호화)
#   4. IAM execution role (Bedrock InvokeModel + S3 + CloudWatch + AgentCore)
#
# AgentCore Runtime 자체 (CreateAgentRuntime) 는 image push 후 별도 단계에서
# `aws bedrock-agentcore-control create-agent-runtime` 또는 별도 terraform
# resource (provider 가 GA 되면) 으로 생성. 본 모듈은 인프라 사전 준비만.
# ==============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  name_prefix = "${var.project}-${var.environment}-chat-agent"
  agent_name  = "${var.project}-${var.environment}-admin-chat-agent"
}

# ------------------------------------------------------------------------------
# 1. KMS key — S3 staging + AgentCore 세션 암호화
# ------------------------------------------------------------------------------
resource "aws_kms_key" "chat_agent" {
  description             = "Encryption key for ${local.agent_name} S3 staging + AgentCore"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = merge(var.tags, { Name = "${local.name_prefix}-kms" })
}

resource "aws_kms_alias" "chat_agent" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.chat_agent.key_id
}

# ------------------------------------------------------------------------------
# 2. ECR repository — admin-chat-agent 컨테이너 image
# ------------------------------------------------------------------------------
resource "aws_ecr_repository" "chat_agent" {
  name                 = local.agent_name
  image_tag_mutability = var.ecr_image_tag_mutability

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.chat_agent.arn
  }

  tags = merge(var.tags, { Name = local.agent_name })
}

resource "aws_ecr_lifecycle_policy" "chat_agent" {
  repository = aws_ecr_repository.chat_agent.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 production images"
      selection = {
        tagStatus     = "tagged"
        tagPrefixList = ["v"]
        countType     = "imageCountMoreThan"
        countNumber   = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ------------------------------------------------------------------------------
# 3. S3 staging bucket — SQL Specialist → Code Specialist 데이터 전달
# ------------------------------------------------------------------------------
resource "aws_s3_bucket" "staging" {
  bucket        = "${local.name_prefix}-staging-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # dev 만. prod 는 false 로 override 권장

  tags = merge(var.tags, { Name = "${local.name_prefix}-staging" })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "staging" {
  bucket = aws_s3_bucket.staging.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.chat_agent.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "staging" {
  bucket                  = aws_s3_bucket.staging.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "staging" {
  bucket = aws_s3_bucket.staging.id
  versioning_configuration {
    status = "Disabled" # staging 은 짧게 살아있어 버전 불필요
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "staging" {
  bucket = aws_s3_bucket.staging.id

  # 중간 데이터(query_db jsonl, code PNG/CSV) — staging/ prefix, 단기 만료.
  rule {
    id     = "expire-staging-data"
    status = "Enabled"
    filter {
      prefix = "staging/"
    }

    expiration {
      days = var.staging_lifecycle_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  # 다운로드 리포트(report_specialist) — reports/ prefix, 장기 만료(다운로드 창).
  # staging/ 와 키 공간이 분리돼 broad 만료(1일)에 안 걸린다(§49). 두 규칙의 filter
  # prefix 가 겹치지 않으므로 S3 의 "겹칠 때 짧은 만료" 함정에 빠지지 않음.
  rule {
    id     = "expire-report-files"
    status = "Enabled"
    filter {
      prefix = "reports/"
    }

    expiration {
      days = var.report_lifecycle_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# ------------------------------------------------------------------------------
# 4. IAM execution role — AgentCore Runtime 이 사용하는 role
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "agent_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "agent_execution" {
  name               = "${local.name_prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.agent_assume_role.json

  tags = merge(var.tags, { Name = "${local.name_prefix}-execution" })
}

# Bedrock InvokeModel — 5개 agent 가 호출하는 Claude (Opus 4.7 / Sonnet 4.6 / Haiku 4.5)
data "aws_iam_policy_document" "bedrock" {
  statement {
    sid       = "BedrockInvoke"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = var.bedrock_allowed_model_arns
  }

  statement {
    sid    = "BedrockMeta"
    effect = "Allow"
    actions = [
      "bedrock:ListFoundationModels",
      "bedrock:GetFoundationModel",
      "bedrock:ListInferenceProfiles",
      "bedrock:GetInferenceProfile",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.agent_execution.id
  policy = data.aws_iam_policy_document.bedrock.json
}

# S3 staging bucket — read/write within own session prefix
data "aws_iam_policy_document" "staging" {
  statement {
    sid    = "StagingRW"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.staging.arn,
      "${aws_s3_bucket.staging.arn}/*",
    ]
  }

  statement {
    sid       = "KMSStaging"
    effect    = "Allow"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.chat_agent.arn]
  }
}

resource "aws_iam_role_policy" "staging" {
  name   = "s3-staging"
  role   = aws_iam_role.agent_execution.id
  policy = data.aws_iam_policy_document.staging.json
}

# CloudWatch Logs — AgentCore observability
data "aws_iam_policy_document" "logs" {
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/${local.agent_name}*"]
  }

  statement {
    sid       = "CloudWatchLogsCreate"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup"]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/*"]
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "cloudwatch-logs"
  role   = aws_iam_role.agent_execution.id
  policy = data.aws_iam_policy_document.logs.json
}

# ECR pull — AgentCore 가 우리 이미지 pull
data "aws_iam_policy_document" "ecr_pull" {
  statement {
    sid       = "ECRPull"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "ECRImage"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = [aws_ecr_repository.chat_agent.arn]
  }
}

resource "aws_iam_role_policy" "ecr" {
  name   = "ecr-pull"
  role   = aws_iam_role.agent_execution.id
  policy = data.aws_iam_policy_document.ecr_pull.json
}

# AgentCore Code Interpreter — Code Specialist 의 execute_python sandbox.
# Tier B 분석(outlier/STL/SARIMAX/heatmap)이 code_session() 으로 기본
# Code Interpreter 를 띄운다. 이 권한이 없으면 StartCodeInterpreterSession
# AccessDenied 로 분석이 전부 실패(DEVLOG §32). Resource 의 account 부분은
# AWS 관리형 기본 인터프리터(`:aws:`) + 커스텀(`:<account>:`) 둘 다 허용.
data "aws_iam_policy_document" "code_interpreter" {
  statement {
    sid    = "AgentCoreCodeInterpreter"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:StartCodeInterpreterSession",
      "bedrock-agentcore:StopCodeInterpreterSession",
      "bedrock-agentcore:GetCodeInterpreterSession",
      "bedrock-agentcore:ListCodeInterpreterSessions",
      "bedrock-agentcore:InvokeCodeInterpreter",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${data.aws_region.current.name}:aws:code-interpreter/*",
      "arn:aws:bedrock-agentcore:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:code-interpreter/*",
    ]
  }
}

resource "aws_iam_role_policy" "code_interpreter" {
  name   = "code-interpreter"
  role   = aws_iam_role.agent_execution.id
  policy = data.aws_iam_policy_document.code_interpreter.json
}
