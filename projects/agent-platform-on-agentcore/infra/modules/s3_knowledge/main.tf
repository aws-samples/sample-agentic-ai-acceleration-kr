# Original uploads for user-created Bedrock Managed Knowledge Bases.
#
# Bedrock ingests each document from its S3 URI rather than inline bytes, so this
# bucket is both the ingestion source and the durable copy: a re-ingest or a
# download after indexing has nothing else to read from.
#
# Objects live under `knowledge/{kb_key}/{doc_id}`, so deleting a knowledge base
# is a prefix delete.

resource "aws_s3_bucket" "knowledge" {
  bucket = "${var.project}-knowledge-${var.region}${var.bucket_suffix}"
}

resource "aws_s3_bucket_public_access_block" "knowledge" {
  bucket                  = aws_s3_bucket.knowledge.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "knowledge" {
  bucket = aws_s3_bucket.knowledge.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "knowledge" {
  bucket = aws_s3_bucket.knowledge.id

  versioning_configuration {
    status = "Enabled"
  }
}

# A place to put documents a knowledge base should *read* rather than receive.
#
# Separate from the upload bucket above because the two are owned differently:
# everything under `knowledge/` was put there by this platform and is deleted with
# its knowledge base, while this one holds content the organisation manages and
# which a knowledge base only borrows — deleting a knowledge base must never touch
# it. Created here, and not left to be made by hand, so that
# `knowledge_source_buckets` names a bucket the stack knows exists and the
# kb-service grant lands on something real.
#
# One bucket with a prefix per knowledge base rather than a bucket each: the grant
# is fixed at deploy time, so a bucket per knowledge base would mean an apply for
# every "Create knowledge base" click.
resource "aws_s3_bucket" "source" {
  count  = var.create_source_bucket ? 1 : 0
  bucket = "${var.project}-kb-source-${var.region}${var.bucket_suffix}"
}

resource "aws_s3_bucket_public_access_block" "source" {
  count                   = var.create_source_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.source[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "source" {
  count  = var.create_source_bucket ? 1 : 0
  bucket = aws_s3_bucket.source[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versioning here is a safety net for someone else's documents, not ours: a sync
# that follows an accidental delete would drop the content from the index too.
resource "aws_s3_bucket_versioning" "source" {
  count  = var.create_source_bucket ? 1 : 0
  bucket = aws_s3_bucket.source[0].id

  versioning_configuration {
    status = "Enabled"
  }
}
