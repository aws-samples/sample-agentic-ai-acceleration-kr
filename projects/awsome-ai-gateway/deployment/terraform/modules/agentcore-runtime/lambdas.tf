# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# admin-chat-agent deterministic tools — Lambda layer
# ------------------------------------------------------------------------------
# The Strands agent (main.py) invokes two Lambdas directly via boto3
# (`lambda:InvokeFunction`) — NOT through AgentCore Gateway (the spec's Gateway
# is a future optimization; MVP is direct invoke):
#
#   1. query_db   — read-only SQL execution (sqlglot AST + EXPLAIN + LIMIT) as
#                   the `gateway_chat_reader` Postgres role; stages large
#                   results to the S3 staging bucket.
#   2. get_schema — schema/whitelist introspection (no DB connection; reads the
#                   bundled schema_whitelist.yaml).
#
# Both run INSIDE the VPC (private subnets) so query_db can reach Aurora. They
# share one execution role and one security group. The agent's execution role
# (main.tf) is granted lambda:InvokeFunction on these two functions.
#
# Gated behind var.enable_db_tools so the module stays backward-compatible:
# applies that only want the agent runtime skip all Lambda/ENI provisioning.
# ==============================================================================

locals {
  db_tools_enabled = var.enable_db_tools ? 1 : 0
  query_db_name    = "${local.name_prefix}-query-db"
  get_schema_name  = "${local.name_prefix}-get-schema"
  # Build artifacts produced by build-lambdas.sh (platform-targeted deps +
  # lambda_function.py + schema_whitelist.yaml).
  build_dir = "${path.module}/build"
}

# ------------------------------------------------------------------------------
# Packaging — zip the prebuilt artifact dirs. build-lambdas.sh must run first.
# source_code_hash triggers redeploy only when the zip content changes.
# ------------------------------------------------------------------------------
data "archive_file" "query_db" {
  count       = local.db_tools_enabled
  type        = "zip"
  source_dir  = "${local.build_dir}/query_db"
  output_path = "${local.build_dir}/query_db.zip"
}

data "archive_file" "get_schema" {
  count       = local.db_tools_enabled
  type        = "zip"
  source_dir  = "${local.build_dir}/get_schema"
  output_path = "${local.build_dir}/get_schema.zip"
}

# ------------------------------------------------------------------------------
# Security group — Lambda ENIs. Egress-only; Aurora ingress is added on the
# Aurora SG (see aws_security_group_rule.aurora_from_lambda) to avoid a cycle.
# ------------------------------------------------------------------------------
resource "aws_security_group" "lambda" {
  count       = local.db_tools_enabled
  name        = "${local.name_prefix}-lambda"
  description = "admin-chat-agent tool Lambdas - egress to Aurora and AWS APIs"
  vpc_id      = var.vpc_id

  egress {
    description = "All egress (Aurora 5432 + Secrets Manager/S3 via NAT or endpoints)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${local.name_prefix}-lambda" })
}

# Allow the Lambda SG into Aurora's SG on 5432. Added on the Aurora side so the
# DB stays the single source of truth for who may connect.
resource "aws_security_group_rule" "aurora_from_lambda" {
  count                    = local.db_tools_enabled
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = var.aurora_security_group_id
  source_security_group_id = aws_security_group.lambda[0].id
  description              = "admin-chat-agent query_db Lambda"
}

# ------------------------------------------------------------------------------
# Reader DB credentials secret — query_db reads password from here.
# Terraform creates the secret shell; the actual password is set out-of-band
# (apply-time random + ALTER ROLE) so it never lands in state as plaintext.
# ------------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "chat_reader" {
  count       = local.db_tools_enabled
  name        = "/${var.project}/${var.environment}/chat-agent/reader"
  description = "gateway_chat_reader DB password for admin-chat-agent query_db Lambda"
  kms_key_id  = aws_kms_key.chat_agent.arn

  tags = merge(var.tags, { Name = "${local.name_prefix}-reader-secret" })
}

# ------------------------------------------------------------------------------
# Lambda execution role — VPC ENI + Secrets Manager + S3 staging + logs
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "lambda" {
  count              = local.db_tools_enabled
  name               = "${local.name_prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = merge(var.tags, { Name = "${local.name_prefix}-lambda" })
}

# VPC ENI management (managed policy is the AWS-recommended path for VPC Lambdas)
resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  count      = local.db_tools_enabled
  role       = aws_iam_role.lambda[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "lambda_inline" {
  # Reader DB secret + KMS to decrypt it
  statement {
    sid       = "ReadDBSecret"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.chat_reader[0].arn]
  }

  statement {
    sid       = "KMSDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.chat_agent.arn]
  }

  # S3 staging — query_db writes large result sets here
  statement {
    sid     = "StagingWrite"
    effect  = "Allow"
    actions = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.staging.arn,
      "${aws_s3_bucket.staging.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_inline" {
  count  = local.db_tools_enabled
  name   = "chat-agent-lambda"
  role   = aws_iam_role.lambda[0].id
  policy = data.aws_iam_policy_document.lambda_inline.json
}

# ------------------------------------------------------------------------------
# Lambda functions
# ------------------------------------------------------------------------------
resource "aws_lambda_function" "query_db" {
  count            = local.db_tools_enabled
  function_name    = local.query_db_name
  role             = aws_iam_role.lambda[0].arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.query_db[0].output_path
  source_code_hash = data.archive_file.query_db[0].output_base64sha256
  timeout          = 30
  memory_size      = 512

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda[0].id]
  }

  environment {
    variables = {
      DB_HOST              = var.aurora_endpoint
      DB_NAME              = var.db_name
      DB_USER              = "gateway_chat_reader"
      DB_SECRET_ARN        = aws_secretsmanager_secret.chat_reader[0].arn
      S3_STAGING_BUCKET    = aws_s3_bucket.staging.bucket
      SCHEMA_WHITELIST_PATH = "/var/task/schema_whitelist.yaml"
      EXPLAIN_COST_LIMIT   = "50000"
      QUERY_LIMIT          = "1000"
      STATEMENT_TIMEOUT_MS = "10000"
    }
  }

  tags = merge(var.tags, { Name = local.query_db_name })
}

resource "aws_lambda_function" "get_schema" {
  count            = local.db_tools_enabled
  function_name    = local.get_schema_name
  role             = aws_iam_role.lambda[0].arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.get_schema[0].output_path
  source_code_hash = data.archive_file.get_schema[0].output_base64sha256
  timeout          = 10
  memory_size      = 256

  # get_schema reads only the bundled whitelist — no DB, no VPC needed.
  environment {
    variables = {
      SCHEMA_WHITELIST_PATH = "/var/task/schema_whitelist.yaml"
    }
  }

  tags = merge(var.tags, { Name = local.get_schema_name })
}

# ------------------------------------------------------------------------------
# Grant the AgentCore execution role permission to invoke the tool Lambdas.
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "agent_invoke_lambdas" {
  count  = local.db_tools_enabled
  statement {
    sid       = "InvokeToolLambdas"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.query_db[0].arn,
      aws_lambda_function.get_schema[0].arn,
    ]
  }
}

resource "aws_iam_role_policy" "agent_invoke_lambdas" {
  count  = local.db_tools_enabled
  name   = "invoke-tool-lambdas"
  role   = aws_iam_role.agent_execution.id
  policy = data.aws_iam_policy_document.agent_invoke_lambdas[0].json
}
