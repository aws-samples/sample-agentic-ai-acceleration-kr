output "role_arn" {
  value = aws_iam_role.harness_execution.arn
}

output "role_name" {
  value = aws_iam_role.harness_execution.name
}
