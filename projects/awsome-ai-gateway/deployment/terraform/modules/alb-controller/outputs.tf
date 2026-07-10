# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "role_arn" {
  value = module.alb_controller_irsa.iam_role_arn
}

output "helm_release_name" {
  value = helm_release.alb_controller.name
}
