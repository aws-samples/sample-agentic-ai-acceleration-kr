output "repository_urls" {
  description = "Map of repo name to repository URL"
  value       = { for k, v in aws_ecr_repository.this : k => v.repository_url }
}
