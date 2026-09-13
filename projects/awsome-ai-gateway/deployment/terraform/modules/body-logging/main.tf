# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# body-logging 모듈 — gateway-proxy 요청/응답 **본문** 로깅 sink (Firehose → S3)
# ------------------------------------------------------------------------------
# 무엇을 담는가
#   게이트웨이가 본 요청 JSON 전문과, 스트리밍이면 재구성된 SSE 텍스트 전문.
#   **마스킹하지 않는다** — 사용자가 프롬프트에 붙여 넣은 것이 그대로 들어온다.
#   같은 코드베이스의 trace 경로는 PII 마스커를 기본 ON 으로 쓰는데 본문 로거에는
#   적용되지 않는다. 알고 있는 격차이고 향후 개선 대상이다.
#   그 때문에 이 모듈의 기본값은 전부 "안 만든다 / 오래 두지 않는다" 쪽이다.
#
# 왜 필요한가 (AWS 네이티브 로그로 대체되지 않는 이유)
#   Mantle 엔드포인트(`bedrock-mantle.{region}.api.aws`)는 AWS model invocation
#   logging 에 **전혀** 잡히지 않는다(실측: runtime 스트리밍/비스트리밍/chat 은 각각
#   1건 기록, Mantle 두 경우는 0건). Codex·Cowork 가 그 평면을 쓰므로, 그 트래픽의
#   본문에 대해서는 이 sink 가 유일한 정본이다.
#
# 이 모듈이 만드는 것 (var.enabled = true 일 때만)
#   1. S3 버킷 — SSE, public 차단, 소유권 강제, lifecycle 만료
#   2. Firehose delivery stream — ExtendedS3, provider/client/dt 동적 파티셔닝, gzip
#   3. Firehose → S3 전용 IAM role
#
# gateway-proxy 의 write 권한(firehose:PutRecordBatch + s3:PutObject fallback)은
# irsa 모듈이 이 모듈의 output ARN 을 받아 부여한다.
#
# ★ AWS 네이티브 invocation logging 과 혼동하지 말 것
#   이 레포에는 별개의 `bedrock-invocation-logging` 모듈이 있다. 그쪽은 **계정×리전
#   단위** AWS 설정(provider alias 로 리전을 못 박고, 켜는 절차가 문서화돼 있다)이고,
#   버킷 이름은 `…-bedrock-invlogs-<account>-<region>` 이다. 이 모듈의 버킷은
#   `…-gateway-body-logs-<account>` 로, 구조적으로 다르게 지었다 — 원본 구현은 두
#   이름이 한 단어 차이여서, 대조 도구(두 버킷을 모두 인자로 받는다)에 바꿔 넣으면
#   오류가 아니라 "Athena 테이블은 정상, 스캔 0건" 이 되는 함정이 있었다.
# ==============================================================================

data "aws_caller_identity" "current" {}

locals {
  # ⚠️ count 를 지역값으로 두는 이유: 리소스마다 `var.enabled ? 1 : 0` 을 반복하면
  #    한 곳을 빼먹었을 때 그 리소스만 항상 만들어진다. 형제 모듈
  #    (bedrock-invocation-logging)과 같은 관용구다.
  count = var.enabled ? 1 : 0

  name_prefix = "${var.project}-${var.environment}"

  # 위 ★ 항목 참조 — 네이티브 싱크 버킷과 구조적으로 다른 이름이어야 한다.
  bucket_name = "${local.name_prefix}-gateway-body-logs-${data.aws_caller_identity.current.account_id}"
  stream_name = "${local.name_prefix}-body-logs"

  tags = merge(var.tags, {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform:body-logging"
  })
}

# ------------------------------------------------------------------------------
# 1. S3 버킷 — 본문 로그 적재 대상
# ------------------------------------------------------------------------------
resource "aws_s3_bucket" "body_logs" {
  count  = local.count
  bucket = local.bucket_name

  # ⚠️ 마스킹되지 않은 프롬프트 본문이 들어가는 버킷이다. dev 라도 기본은 false —
  #    `tofu destroy` 한 번으로 비워지게 두면, 내용물이 무엇인지 아는 사람만 그
  #    위험을 인지한다. 정말로 teardown 편의가 필요한 환경에서만 명시적으로 켠다.
  force_destroy = var.force_destroy

  tags = merge(local.tags, { Name = local.bucket_name })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "body_logs" {
  count  = local.count
  bucket = aws_s3_bucket.body_logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      # KMS 키를 주면 aws:kms, 아니면 SSE-S3(AES256). 본문에 PII 가 들어갈 수 있으므로
      # prod 는 고객관리 KMS 키를 주는 것을 권장한다(var.kms_key_arn).
      sse_algorithm     = var.kms_key_arn != "" ? "aws:kms" : "AES256"
      kms_master_key_id = var.kms_key_arn != "" ? var.kms_key_arn : null
    }
    # KMS 일 때만 켠다 — SSE-S3 에는 요청 비용 절감 효과가 없다.
    bucket_key_enabled = var.kms_key_arn != ""
  }
}

resource "aws_s3_bucket_public_access_block" "body_logs" {
  count                   = local.count
  bucket                  = aws_s3_bucket.body_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ACL 기반 크로스 계정 부여를 원천 차단한다. public_access_block 은 *public* 만 막고,
# 특정 계정에 ACL 로 읽기를 주는 것은 막지 않는다 — 본문 버킷에서는 그 경로도 닫는다.
resource "aws_s3_bucket_ownership_controls" "body_logs" {
  count  = local.count
  bucket = aws_s3_bucket.body_logs[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "body_logs" {
  count  = local.count
  bucket = aws_s3_bucket.body_logs[0].id
  versioning_configuration {
    # ⚠️ 의도적으로 끈다. 버저닝을 켜면 lifecycle expiration 이 현재 버전만 만료시키고
    #    비현재 버전은 남으므로, "N일 뒤 삭제" 라고 믿는 본문이 무기한 남는다
    #    (noncurrent_version_expiration 을 별도로 걸어야 한다). 감사 로그는 append-only
    #    성격이라 덮어쓰기 보호가 필요하지 않다.
    status = "Disabled"
  }
}

# Lifecycle 만료.
#
# ⚠️ 기본값이 0(무기한)이면 안 된다. 마스킹되지 않은 프롬프트 본문을 만료 없이 쌓는
#    것은 켜는 순간부터 조용히 늘어나는 부채다. 기본 90일은 형제 모듈
#    (bedrock-invocation-logging)의 보존 기간과 같은 축에 맞춘 값이다.
#    0 을 명시하면 만료 없음이 되지만, 그것은 의식적인 선택이어야 한다.
resource "aws_s3_bucket_lifecycle_configuration" "body_logs" {
  count  = var.enabled && var.log_retention_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.body_logs[0].id

  rule {
    id     = "expire-body-logs"
    status = "Enabled"
    filter {} # 버킷 전체 (모든 provider/client/dt 파티션)

    expiration {
      days = var.log_retention_days
    }

    # 빼지 말 것. 큰 본문은 멀티파트로 올라오고, 실패한 업로드가 남긴 파트는
    # expiration 으로는 **절대** 지워지지 않아 계속 과금되고 계속 보관된다.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.body_logs]
}

# ------------------------------------------------------------------------------
# 2. Firehose delivery stream 용 IAM role
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "firehose_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["firehose.amazonaws.com"]
    }
    # confused deputy 방어 — 다른 계정의 Firehose 가 이 role 을 assume 하지 못하게 한다.
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "firehose" {
  count              = local.count
  name               = "${local.name_prefix}-body-logs-firehose"
  assume_role_policy = data.aws_iam_policy_document.firehose_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "firehose" {
  count = local.count

  # S3 write (동적 파티셔닝 + 에러 backup 포함).
  statement {
    sid    = "S3Delivery"
    effect = "Allow"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetBucketLocation",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:PutObject",
    ]
    resources = [
      aws_s3_bucket.body_logs[0].arn,
      "${aws_s3_bucket.body_logs[0].arn}/*",
    ]
  }

  # 배달 실패를 CloudWatch 에 남길 권한. 이게 없으면 Firehose 가 조용히 실패한다 —
  # 레코드는 사라지고 우리 쪽 지표에는 아무 변화가 없다.
  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:PutLogEvents",
      "logs:CreateLogStream",
    ]
    resources = ["arn:aws:logs:*:${data.aws_caller_identity.current.account_id}:log-group:/aws/kinesisfirehose/${local.stream_name}:*"]
  }

  # KMS 를 쓰는 경우 Firehose 가 암호화 write 를 할 수 있도록.
  dynamic "statement" {
    for_each = var.kms_key_arn != "" ? [1] : []
    content {
      sid       = "KmsForS3"
      effect    = "Allow"
      actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
      resources = [var.kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "firehose" {
  count  = local.count
  name   = "${local.name_prefix}-body-logs-firehose-s3"
  role   = aws_iam_role.firehose[0].id
  policy = data.aws_iam_policy_document.firehose[0].json
}

# ------------------------------------------------------------------------------
# 3. Firehose delivery stream — ExtendedS3 + 동적 파티셔닝
# ------------------------------------------------------------------------------
# 레코드의 provider/client 필드로 파티션을 나눈다:
#   provider=<...>/client=<...>/dt=<YYYY-MM-DD>/
# gateway-proxy 의 `BodyLogRecord.partition_key()` 와 같은 레이아웃이어야 한다 —
# 어긋나면 적재는 되고 조회만 안 된다.
resource "aws_kinesis_firehose_delivery_stream" "body_logs" {
  count       = local.count
  name        = local.stream_name
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose[0].arn
    bucket_arn = aws_s3_bucket.body_logs[0].arn

    dynamic_partitioning_configuration {
      enabled = true
    }

    # 동적 파티셔닝의 버퍼 하한은 64MB / 60s 다(그보다 작게 주면 API 가 거부한다).
    buffering_size     = var.buffer_size_mb
    buffering_interval = var.buffer_interval_seconds
    compression_format = "GZIP"

    # dt= 는 Firehose 도착 시각 기준이다(레코드의 date 필드가 아니라). 자정 근처의
    # 요청이 다음 날 파티션에 들어갈 수 있다 — 하루 경계에서 조회할 때는 이틀을 본다.
    prefix              = "provider=!{partitionKeyFromQuery:provider}/client=!{partitionKeyFromQuery:client}/dt=!{timestamp:yyyy-MM-dd}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/"

    processing_configuration {
      enabled = true
      processors {
        type = "MetadataExtraction"
        parameters {
          parameter_name  = "MetadataExtractionQuery"
          parameter_value = "{provider:.provider,client:.client}"
        }
        parameters {
          parameter_name  = "JsonParsingEngine"
          parameter_value = "JQ-1.6"
        }
      }
    }

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = "/aws/kinesisfirehose/${local.stream_name}"
      log_stream_name = "S3Delivery"
    }
  }

  server_side_encryption {
    # 스트림 안에 머무는 동안(버퍼링 중)의 암호화. S3 도착 후는 위 버킷 SSE 가 담당한다.
    enabled  = true
    key_type = "AWS_OWNED_CMK"
  }

  tags = local.tags
}
