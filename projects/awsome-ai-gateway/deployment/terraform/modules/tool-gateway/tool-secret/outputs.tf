output "secret_arn" {
  value       = aws_secretsmanager_secret.this.arn
  description = "ARN of the Secrets Manager secret container."
}
