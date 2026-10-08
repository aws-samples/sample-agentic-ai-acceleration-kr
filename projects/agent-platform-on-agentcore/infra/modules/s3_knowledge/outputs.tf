output "bucket_name" {
  value = aws_s3_bucket.knowledge.id
}

output "bucket_arn" {
  value = aws_s3_bucket.knowledge.arn
}

output "source_bucket_name" {
  description = "Bucket for read-only knowledge base sources, or \"\" when not created."
  value       = var.create_source_bucket ? aws_s3_bucket.source[0].id : ""
}

output "source_bucket_arn" {
  description = "ARN of the source bucket, or \"\" when not created."
  value       = var.create_source_bucket ? aws_s3_bucket.source[0].arn : ""
}
