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
  #     Codex 호출 계정 == gateway-proxy IRSA 계정(859)이라 cross-account assume 불필요 —
  #     이 in-account 권한만으로 충분(라이브 probe 로 us-east-2 GPT-5.5 200 OK 확인).
  mantle_regions = ["ap-northeast-1", "us-east-2"]
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
  # 라이브 검증(probe)으로 확인된 실제 필요 action 집합. Codex 는 같은 계정(859)이라
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
  # native (bedrock-runtime, boto3 invoke_model) in a SEPARATE account (374).
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
}

resource "aws_iam_policy" "admin_api" {
  name        = "${var.project}-${var.environment}-admin-api"
  description = "STS + Cognito + Price List permissions for admin-api"
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
