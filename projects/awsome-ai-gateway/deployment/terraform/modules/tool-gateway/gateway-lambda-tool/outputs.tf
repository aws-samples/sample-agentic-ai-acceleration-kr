output "function_arn" {
  value       = aws_lambda_function.this.arn
  description = "Lambda function ARN"
}

output "function_name" {
  value       = aws_lambda_function.this.function_name
  description = "Lambda function name"
}

output "role_arn" {
  value       = aws_iam_role.lambda.arn
  description = "IAM role ARN for Lambda execution"
}
