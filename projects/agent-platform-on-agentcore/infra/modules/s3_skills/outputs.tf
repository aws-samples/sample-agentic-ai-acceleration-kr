output "bucket_name" {
  value = aws_s3_bucket.skills.id
}

output "bucket_arn" {
  value = aws_s3_bucket.skills.arn
}
