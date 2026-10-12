output "role_arn" {
  value = aws_iam_role.harness_execution.arn
}

output "role_name" {
  value = aws_iam_role.harness_execution.name
}

output "boundary_arn" {
  value = var.create_boundary ? aws_iam_policy.boundary[0].arn : ""
}
