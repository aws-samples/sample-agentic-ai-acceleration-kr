# Artifacts produced by agents (documents, code, charts) that the chat UI renders
# in its side panel and users share by link.
#
# Versioning is on for recovery, but artifact versions the UI exposes are their
# own S3 objects (`v1.md`, `v2.md`) — sharing a presigned URL for a specific
# version must not depend on S3 object version ids.

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project}-artifacts-${var.region}${var.bucket_suffix}"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}
