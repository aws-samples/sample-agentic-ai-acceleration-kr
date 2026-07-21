locals {
  src_dir    = "${var.source_root}/${var.tool_name}"
  shared_dir = "${var.source_root}/_shared"
  build_dir  = "${path.module}/.build/${var.tool_name}"
  zip_path   = "${path.module}/.build/${var.tool_name}.zip"
  fn_name    = "${var.project_name}-${var.environment}-tool-${var.tool_name}"

  is_image = var.package_type == "Image"
  is_zip   = var.package_type == "Zip"

  # Docker --platform string for the Image build.
  docker_platform = var.architecture == "arm64" ? "linux/arm64" : "linux/amd64"

  # Hash includes handler, requirements, and every file in _shared/ so the package
  # is rebuilt whenever any shared helper changes. The Image path adds the
  # Dockerfile so a Dockerfile-only change still triggers a rebuild/push.
  shared_files = fileset(local.shared_dir, "*.py")
  source_hash = sha1(join("", concat(
    compact([
      fileexists("${local.src_dir}/handler.py") ? filesha1("${local.src_dir}/handler.py") : "",
      fileexists("${local.src_dir}/requirements.txt") ? filesha1("${local.src_dir}/requirements.txt") : "",
      local.is_image && fileexists("${local.src_dir}/Dockerfile") ? filesha1("${local.src_dir}/Dockerfile") : "",
    ]),
    [for f in local.shared_files : filesha1("${local.shared_dir}/${f}")],
  )))

  ecr_repo_name = local.fn_name
  image_tag     = substr(local.source_hash, 0, 12)
  image_uri     = local.is_image ? "${aws_ecr_repository.this[0].repository_url}:${local.image_tag}" : ""
}

# ============================================================
# Build Lambda deployment package (Zip mode)
# ============================================================

resource "null_resource" "build" {
  count = local.is_zip ? 1 : 0

  triggers = {
    source_hash   = local.source_hash
    build_present = fileexists("${local.build_dir}/handler.py") ? "present" : "missing"
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      rm -rf "${local.build_dir}"
      mkdir -p "${local.build_dir}/_shared"
      cp "${local.src_dir}/handler.py" "${local.build_dir}/"
      cp "${local.shared_dir}"/*.py "${local.build_dir}/_shared/"
      if [ -f "${local.src_dir}/requirements.txt" ]; then
        ${var.pip_command} install -q --upgrade --target "${local.build_dir}" \
          --platform manylinux2014_aarch64 \
          --python-version 3.12 \
          --only-binary=:all: \
          --implementation cp \
          -r "${local.src_dir}/requirements.txt"
      fi
    EOT
  }
}

data "archive_file" "zip" {
  count       = local.is_zip ? 1 : 0
  type        = "zip"
  source_dir  = local.build_dir
  output_path = local.zip_path
  depends_on  = [null_resource.build]
}

# ============================================================
# Build & push container image (Image mode)
# ============================================================

resource "aws_ecr_repository" "this" {
  count                = local.is_image ? 1 : 0
  name                 = local.ecr_repo_name
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = false
  }
}

# Build the arm64 image from <src_dir>/Dockerfile (build context = source_root so
# _shared is in scope) and push it to ECR, tagged with the source hash. Rebuilds
# only when source_hash changes.
resource "null_resource" "image_build" {
  count = local.is_image ? 1 : 0

  triggers = {
    source_hash = local.source_hash
    image_uri   = local.image_uri
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      aws ecr get-login-password --region ${var.aws_region} \
        | docker login --username AWS --password-stdin ${aws_ecr_repository.this[0].repository_url}
      docker build --platform ${local.docker_platform} \
        -t ${local.image_uri} \
        -f "${local.src_dir}/Dockerfile" \
        "${var.source_root}"
      docker push ${local.image_uri}
    EOT
  }

  depends_on = [aws_ecr_repository.this]
}

# ============================================================
# IAM Role for Lambda execution
# ============================================================

resource "aws_iam_role" "lambda" {
  name = "${local.fn_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "logs" {
  name = "logs"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:*"
    }]
  })
}

# Allow Lambda to call GetResourceApiKey for API key providers
resource "aws_iam_role_policy" "bedrock_agentcore" {
  name = "bedrock-agentcore"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock-agentcore:GetResourceApiKey",
      ]
      Resource = "*"
    }]
  })
}

# Tool secret: read this engine's API key from Secrets Manager at runtime.
# Conditionally created so tools without a key (duckduckgo, searxng) are unaffected.
resource "aws_iam_role_policy" "secret" {
  count = var.enable_secret_policy ? 1 : 0
  name  = "tool-secret"
  role  = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = var.secret_arn
    }]
  })
}

# Browser tool only: drive AgentCore browser sessions + invoke Bedrock models.
# Conditionally created so existing search tools are unaffected.
resource "aws_iam_role_policy" "browser" {
  count = var.enable_browser_policy ? 1 : 0
  name  = "browser"
  role  = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:StartBrowserSession",
          "bedrock-agentcore:StopBrowserSession",
          "bedrock-agentcore:ConnectBrowserAutomationStream",
          "bedrock-agentcore:GetBrowserSession",
        ]
        Resource = var.browser_arn
      }],
      length(var.bedrock_model_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = var.bedrock_model_arns
      }] : [],
    )
  })
}

# VPC-attached tools (SearXNG) need ENI management perms to run in a VPC.
# AWS-managed AWSLambdaVPCAccessExecutionRole grants exactly these actions.
resource "aws_iam_role_policy_attachment" "vpc_access" {
  count      = var.vpc_config != null ? 1 : 0
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# ============================================================
# CloudWatch Log Group
# ============================================================

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.fn_name}"
  retention_in_days = var.log_retention_days
}

# ============================================================
# Lambda Function
# ============================================================

resource "aws_lambda_function" "this" {
  function_name = local.fn_name
  role          = aws_iam_role.lambda.arn
  architectures = [var.architecture]
  timeout       = var.timeout
  memory_size   = var.memory_size

  package_type = var.package_type

  # Zip mode: handler/runtime + the pip-built zip uploaded directly.
  handler = local.is_zip ? "handler.lambda_handler" : null
  runtime = local.is_zip ? "python3.12" : null

  filename         = local.is_zip ? data.archive_file.zip[0].output_path : null
  source_code_hash = local.is_zip ? data.archive_file.zip[0].output_base64sha256 : null

  # Image mode: run from the ECR image pushed above. CMD is baked into the image.
  image_uri = local.is_image ? local.image_uri : null

  environment {
    variables = merge(
      {
        PROJECT_NAME = var.project_name
        ENVIRONMENT  = var.environment
        # AWS_REGION is a Lambda-reserved environment variable that the runtime injects automatically.
      },
      var.env_vars,
    )
  }

  dynamic "vpc_config" {
    for_each = var.vpc_config != null ? [var.vpc_config] : []
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.logs,
    aws_iam_role_policy.bedrock_agentcore,
    aws_iam_role_policy.browser,
    aws_iam_role_policy_attachment.vpc_access,
    null_resource.image_build,
    aws_iam_role_policy.secret,
  ]
}
