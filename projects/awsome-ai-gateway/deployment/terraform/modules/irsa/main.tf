# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# IRSA 모듈 — ServiceAccount별 IAM Role 생성 (FR-2.4 최소 권한 원칙)
# ------------------------------------------------------------------------------
# 생성되는 Role:
#   1. gateway-proxy-bedrock: Bedrock Runtime InvokeModel/InvokeModelWithResponseStream
#                             + Bedrock Mantle in-account (claude-opus-4-8-mantle)
#   2. admin-api: STS GetCallerIdentity (FR-2.1a VK 발급 검증)
# ==============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # Bedrock Mantle 서비스 엔드포인트 리전들 (일반 Bedrock 서울과 구별되는 별도 네임스페이스).
  #   - ap-northeast-1 (Tokyo): Claude Code in-account Mantle(claude-opus-4-8-mantle).
  #   - us-east-2 (Ohio): Codex in-account Mantle GPT-5.5 (openai.gpt-5.5, Responses API).
  #     Codex 호출 계정 == gateway-proxy IRSA 계정(123)이라 cross-account assume 불필요 —
  #     이 in-account 권한만으로 충분(라이브 probe 로 us-east-2 GPT-5.5 200 OK 확인).
  #   - 배포별로 다르므로 var.mantle_regions 로 주입한다. 이 모듈 변수는 **필수**다
  #     (default 없음, variables.tf 참조) — 기본값 ["ap-northeast-1", "us-east-2"] 은
  #     environments/*/variables.tf 에만 있고 그것이 단일 진실원천이다. 다른 리전
  #     배포는 tfvars 에서 mantle_regions = ["us-east-1"] 처럼 덮어쓴다.
  mantle_regions = var.mantle_regions
}

# ------------------------------------------------------------------------------
# 1. Gateway Proxy — Bedrock 호출 권한
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "bedrock" {
  statement {
    sid    = "BedrockInvoke"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:CountTokens",
    ]
    resources = var.bedrock_allowed_model_arns
  }

  statement {
    sid    = "BedrockListModels"
    effect = "Allow"
    actions = [
      "bedrock:ListFoundationModels",
      "bedrock:GetFoundationModel",
      "bedrock:ListInferenceProfiles",
      "bedrock:GetInferenceProfile",
    ]
    resources = ["*"]
  }

  # --------------------------------------------------------------------------
  # In-Account Mantle — Claude Code(Tokyo Opus 4.8) + Codex(Ohio GPT-5.5).
  # Mantle 은 일반 bedrock:InvokeModel 이 아닌 bedrock-mantle 네임스페이스를 사용한다.
  # 엔드포인트: bedrock-mantle.{region}.api.aws (local.mantle_regions — Tokyo + Ohio).
  # 라이브 검증(probe)으로 확인된 실제 필요 action 집합. Codex 는 같은 계정(123)이라
  # cross-account assume 없이 in-account 권한만으로 호출된다.
  # --------------------------------------------------------------------------
  statement {
    sid    = "InAccountMantleInference"
    effect = "Allow"
    actions = [
      "bedrock-mantle:CreateInference",
      "bedrock-mantle:GetInference",
    ]
    resources = [
      for r in local.mantle_regions :
      "arn:aws:bedrock-mantle:${r}:${data.aws_caller_identity.current.account_id}:*"
    ]
  }

  statement {
    sid       = "InAccountMantleBearer"
    effect    = "Allow"
    actions   = ["bedrock-mantle:CallWithBearerToken"]
    resources = ["*"]
  }

  # --------------------------------------------------------------------------
  # AgentCore Gateway — server-side web search (Architecture C).
  # gateway-proxy is the MCP *caller*: it SigV4-signs POSTs to the AgentCore Gateway
  # /mcp endpoint (tools/list, tools/call) so InvokeGateway is the only caller-side
  # permission needed. The gateway's own execution role holds InvokeWebSearch (created
  # out-of-band with the gateway). The managed WebSearch connector is us-east-1-only,
  # so we scope to that region; IRSA creds are global (cross-region call is fine).
  # --------------------------------------------------------------------------
  statement {
    sid     = "AgentCoreInvokeGateway"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeGateway"]
    resources = [
      "arn:aws:bedrock-agentcore:us-east-1:${data.aws_caller_identity.current.account_id}:gateway/*"
    ]
  }

  # --------------------------------------------------------------------------
  # Cowork cross-account Mantle — cowork routes to Bedrock Mantle Opus 4.8 in a
  # SEPARATE account (905, Tokyo), so gateway-proxy must AssumeRole into that
  # account's cowork role. Unlike codex/claude-code (in-account 859, no assume),
  # cowork is the ONLY cross-account client. The 905 role's trust policy allows
  # this 859 IRSA principal + sts:ExternalId=cowork-bedrock (see cowork_role_arn).
  # routing_profiles.account_role_arn(=cowork) must match cowork_role_arn.
  # --------------------------------------------------------------------------
  dynamic "statement" {
    for_each = var.cowork_role_arn != "" ? [1] : []
    content {
      sid       = "AssumeCoworkMantle"
      effect    = "Allow"
      actions   = ["sts:AssumeRole"]
      resources = [var.cowork_role_arn]
    }
  }

  # --------------------------------------------------------------------------
  # Claude Code cross-account Bedrock NATIVE — claude-code routes to Bedrock
  # native (bedrock-runtime, boto3 invoke_model) in a SEPARATE account (333).
  # Unlike cowork(Mantle), this is native; gateway-proxy assumes the 374 role and
  # builds a bedrock-runtime client from temp creds (BedrockAccountClientProvider).
  # The 374 role trust allows this 859 IRSA principal + sts:ExternalId=claude-code-bedrock.
  # routing_profiles.account_role_arn(=claude-code) must match claude_code_374_role_arn.
  # --------------------------------------------------------------------------
  dynamic "statement" {
    for_each = var.claude_code_374_role_arn != "" ? [1] : []
    content {
      sid       = "AssumeClaudeCode374Bedrock"
      effect    = "Allow"
      actions   = ["sts:AssumeRole"]
      resources = [var.claude_code_374_role_arn]
    }
  }

  # --------------------------------------------------------------------------
  # 요청/응답 **본문** 로깅 sink 쓰기 (modules/body-logging).
  #
  # ⚠️ 쓰기 전용이다. Get/List/Delete 를 넣지 않는다 — 게이트웨이는 자기가 넣은 본문을
  #    다시 읽을 이유가 없고, 그 권한이 있으면 게이트웨이 파드 침해가 곧 **누적된 전체
  #    프롬프트 이력의 유출**이 된다. 읽기는 사람이 별도 자격증명으로 한다.
  #
  # ⚠️ body-logging 모듈이 꺼져 있으면 ARN 이 빈 문자열로 와서 statement 자체가
  #    렌더되지 않는다. `resources = [""]` 로 남으면 MalformedPolicyDocument 로 apply 가
  #    깨지므로, 조건을 빼서는 안 된다.
  # --------------------------------------------------------------------------
  dynamic "statement" {
    for_each = var.body_log_firehose_arn != "" ? [1] : []
    content {
      sid    = "BodyLogFirehoseWrite"
      effect = "Allow"
      actions = [
        "firehose:PutRecord",
        "firehose:PutRecordBatch",
      ]
      resources = [var.body_log_firehose_arn]
    }
  }

  # Firehose 레코드 상한(1MB)을 넘는 본문의 S3 직행 fallback. 객체 하나를 넣는 것만
  # 허용하고 버킷 열람(s3:ListBucket)은 주지 않는다 — 목록 권한이 있으면 침해 시
  # 무엇이 쌓여 있는지 열거할 수 있다.
  dynamic "statement" {
    for_each = var.body_log_bucket_arn != "" ? [1] : []
    content {
      sid       = "BodyLogS3Fallback"
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = ["${var.body_log_bucket_arn}/*"]
    }
  }
}

resource "aws_iam_policy" "bedrock" {
  name        = "${var.project}-${var.environment}-gateway-proxy-bedrock"
  description = "Bedrock Runtime 호출 권한 for gateway-proxy"
  policy      = data.aws_iam_policy_document.bedrock.json
  tags        = var.tags
}

module "gateway_proxy_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.48"

  role_name        = "${var.project}-${var.environment}-gateway-proxy-bedrock"
  role_description = "IRSA - gateway-proxy to Bedrock"

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["${var.k8s_namespace}:gateway-proxy"]
    }
  }

  role_policy_arns = {
    bedrock = aws_iam_policy.bedrock.arn
  }

  tags = var.tags
}

# ------------------------------------------------------------------------------
# 2. Admin API — STS GetCallerIdentity 검증
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "admin_api" {
  # STS — CLI의 presigned GetCallerIdentity 검증에 필요
  statement {
    sid       = "StsGetCallerIdentity"
    effect    = "Allow"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }

  # Cognito — 사용자/팀 동기화 기능
  dynamic "statement" {
    for_each = var.cognito_user_pool_arn != "" ? [1] : []
    content {
      sid    = "CognitoSync"
      effect = "Allow"
      actions = [
        "cognito-idp:ListGroups",
        "cognito-idp:ListUsersInGroup",
        "cognito-idp:ListUsers",
        "cognito-idp:AdminListGroupsForUser",
        # AdminGetUser: OIDC exchange path enriches email from Cognito when the
        # access token lacks the email claim (Cognito access tokens have none) —
        # prevents users being stored as <sub>@unknown on login.
        "cognito-idp:AdminGetUser",
      ]
      resources = [var.cognito_user_pool_arn]
    }
  }

  # Price List API — 모델 단가 동기화(GetProducts serviceCode=AmazonBedrock).
  # Price List 는 리소스레벨 권한 미지원 → resources=["*"]. 읽기 전용.
  statement {
    sid    = "PriceListRead"
    effect = "Allow"
    actions = [
      "pricing:GetProducts",
      "pricing:DescribeServices",
      "pricing:GetAttributeValues",
    ]
    resources = ["*"]
  }

  # Bedrock invocation log 감사 대조 — CloudWatch Logs Insights **읽기 전용**.
  # 두 statement 로 쪼갠 이유: Insights 액션들의 리소스레벨 권한 지원이 다르다.
  #   StartQuery / FilterLogEvents / GetLogEvents / DescribeLogStreams → log-group ARN 지정 가능
  #   GetQueryResults / StopQuery / DescribeLogGroups                 → 리소스레벨 미지원(*)
  # 좁힐 수 있는 쪽만 좁힌다. 쓰기 액션(PutLogEvents, DeleteLogGroup 등)은 일절 없음.
  dynamic "statement" {
    for_each = var.bedrock_invocation_log_group_arn != "" ? [1] : []
    content {
      sid    = "BedrockInvocationLogRead"
      effect = "Allow"
      actions = [
        "logs:StartQuery",
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "logs:DescribeLogStreams",
      ]
      resources = [
        var.bedrock_invocation_log_group_arn,
        # log stream 대상 액션은 :* 접미사가 붙은 ARN 을 요구한다.
        "${var.bedrock_invocation_log_group_arn}:*",
      ]
    }
  }

  dynamic "statement" {
    for_each = var.bedrock_invocation_log_group_arn != "" ? [1] : []
    content {
      sid    = "BedrockInvocationLogQueryResults"
      effect = "Allow"
      actions = [
        # 이 세 액션은 IAM 리소스레벨 조건을 지원하지 않는다(queryId 는 ARN 이 아님).
        # StopQuery 는 우리가 띄운 쿼리를 타임아웃에 취소하는 용도로, 로그 데이터를
        # 변경하지 않는다.
        "logs:GetQueryResults",
        "logs:StopQuery",
        "logs:DescribeLogGroups",
      ]
      resources = ["*"]
    }
  }
}

resource "aws_iam_policy" "admin_api" {
  name        = "${var.project}-${var.environment}-admin-api"
  description = "STS + Cognito + Price List + Bedrock invocation-log read permissions for admin-api"
  policy      = data.aws_iam_policy_document.admin_api.json
  tags        = var.tags
}

module "admin_api_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.48"

  role_name        = "${var.project}-${var.environment}-admin-api"
  role_description = "IRSA - admin-api (STS)"

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["${var.k8s_namespace}:admin-api"]
    }
  }

  role_policy_arns = {
    admin_api = aws_iam_policy.admin_api.arn
  }

  tags = var.tags
}

# ------------------------------------------------------------------------------
# 4. External Secrets Operator — Secrets Manager 읽기 권한
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "external_secrets" {
  statement {
    sid    = "SecretsManagerRead"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:ListSecrets",
    ]
    resources = [
      "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:/${var.project}/${var.environment}/*",
      # RDS-managed master-user secret (Aurora ManageMasterUserPassword=on auto-rotates
      # this). The migration Job's master_password is sourced from it directly so it can
      # never drift on rotation. Name pattern: rds!cluster-<uuid>; suffix is random so
      # wildcard the whole rds!cluster-* namespace (this account only).
      "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:rds!cluster-*",
    ]
  }
  statement {
    sid       = "KmsDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = var.secrets_manager_kms_key_arns
  }
}

resource "aws_iam_policy" "external_secrets" {
  name        = "${var.project}-${var.environment}-external-secrets"
  description = "External Secrets Operator - Secrets Manager read"
  policy      = data.aws_iam_policy_document.external_secrets.json
  tags        = var.tags
}

module "external_secrets_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.48"

  role_name        = "${var.project}-${var.environment}-external-secrets"
  role_description = "IRSA - external-secrets controller"

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["external-secrets:external-secrets"]
    }
  }

  role_policy_arns = {
    eso = aws_iam_policy.external_secrets.arn
  }

  tags = var.tags
}
