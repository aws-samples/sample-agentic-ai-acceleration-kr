# The two roles a user-created Bedrock Managed Knowledge Base needs.
#
# Both are shared by every knowledge base rather than created per KB, for the
# same reason modules/harness_role declares one execution role: a fresh role is
# not usable for the first ~10-100 seconds after creation, and paying that
# propagation delay on every "Create knowledge base" click would make the
# feature feel broken. The cost is that the gateway role is scoped to
# `knowledge-base/*` — per-KB isolation comes from the server's ownership checks
# and from each gateway target being pinned to one knowledgeBaseId.

data "aws_caller_identity" "current" {}

# --- kb-service: assumed by Bedrock while ingesting and indexing -------------

data "aws_iam_policy_document" "kb_service_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"]
    }
  }
}

resource "aws_iam_role" "kb_service" {
  name               = "${var.project}-kb-service"
  assume_role_policy = data.aws_iam_policy_document.kb_service_assume.json
}

data "aws_iam_policy_document" "kb_service" {
  statement {
    sid    = "KnowledgeBucketRead"
    effect = "Allow"
    actions = [
      # Documents are ingested by S3 URI, so Bedrock — not the server — is what
      # reads the uploaded file.
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = [
      var.knowledge_bucket_arn,
      "${var.knowledge_bucket_arn}/*",
    ]
  }

  # Buckets users may attach as a source, enumerated rather than wildcarded so the
  # role can read exactly what an administrator has offered and nothing else. The
  # dynamic block collapses when the list is empty: a statement with no resources
  # is not valid IAM.
  dynamic "statement" {
    for_each = length(var.source_buckets) > 0 ? [1] : []

    content {
      sid    = "SourceBucketRead"
      effect = "Allow"
      actions = [
        "s3:GetObject",
        "s3:ListBucket",
      ]
      resources = flatten([
        for bucket in var.source_buckets : [
          "arn:aws:s3:::${bucket}",
          "arn:aws:s3:::${bucket}/*",
        ]
      ])
    }
  }
}

resource "aws_iam_role_policy" "kb_service" {
  name   = "${var.project}-kb-service-policy"
  role   = aws_iam_role.kb_service.id
  policy = data.aws_iam_policy_document.kb_service.json
}

# --- kb-gateway: assumed by AgentCore when a gateway target retrieves --------

data "aws_iam_policy_document" "kb_gateway_assume" {
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

resource "aws_iam_role" "kb_gateway" {
  name               = "${var.project}-kb-gateway"
  assume_role_policy = data.aws_iam_policy_document.kb_gateway_assume.json
}

data "aws_iam_policy_document" "kb_gateway" {
  statement {
    sid    = "KnowledgeBaseRetrieve"
    effect = "Allow"
    actions = [
      "bedrock:GetKnowledgeBase",
      "bedrock:Retrieve",
    ]
    resources = ["arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"]
  }

  # No bedrock:AgenticRetrieveStream. The connector rejects `knowledgeBaseId` on
  # that tool, so it cannot be pinned to one knowledge base and the target does
  # not expose it; granting the action would only widen this role, and it is the
  # one Bedrock action that cannot be scoped to a resource.

  statement {
    sid    = "ModelInvocation"
    effect = "Allow"
    # Managed retrieval embeds the query and reranks results with models of its
    # own, invoked with this role's credentials.
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "kb_gateway" {
  name   = "${var.project}-kb-gateway-policy"
  role   = aws_iam_role.kb_gateway.id
  policy = data.aws_iam_policy_document.kb_gateway.json
}
