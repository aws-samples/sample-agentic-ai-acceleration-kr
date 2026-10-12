variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "knowledge_bucket_arn" {
  type        = string
  description = "Bucket Bedrock reads uploaded documents from during ingestion."
}

variable "source_buckets" {
  type    = list(string)
  default = []

  description = <<-EOT
    Existing buckets a user may attach as a knowledge base source, by name.

    Granted to the kb-service role here and offered to users through the server's
    KNOWLEDGE_SOURCE_BUCKETS setting; both come from this one list, so adding a
    bucket is a deploy rather than a runtime IAM change. That is deliberate — the
    alternative is either giving the server iam:PutRolePolicy (a privilege
    escalation path) or granting the role s3:GetObject on every bucket in the
    account.

    Same-account, same-region General Purpose buckets only. A cross-account bucket
    additionally needs a bucket policy naming this role, which this does not write.
  EOT
}
