# Agent skill bundles: the store of record, and what the harness fetches.
#
# A skill is a directory (SKILL.md plus optional references/, scripts/, assets/),
# and Agent Registry cannot hold one — its AGENT_SKILLS descriptor is two inline
# strings, and AWS documents the markdown as discovery metadata with "Registry
# does not support storing other agent skill files". The harness skill API, by
# contrast, takes awsSkills / git / s3 / filesystem sources and no inline option.
#
# So uploads land here under skills/<name>/, the registry record carries a pointer
# to that prefix, and the harness is composed against the prefix. Versioning is on
# because replacing a bundle overwrites and prunes objects in place.

resource "aws_s3_bucket" "skills" {
  bucket = "${var.project}-skills-${var.region}${var.bucket_suffix}"
}

resource "aws_s3_bucket_public_access_block" "skills" {
  bucket                  = aws_s3_bucket.skills.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "skills" {
  bucket = aws_s3_bucket.skills.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "skills" {
  bucket = aws_s3_bucket.skills.id

  versioning_configuration {
    status = "Enabled"
  }
}
