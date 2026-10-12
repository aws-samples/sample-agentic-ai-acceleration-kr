output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "server_service_dns" {
  description = "Internal DNS for the server service"
  value       = "server.${var.namespace_name}"
}

# CloudWatch's ApplicationELB dimensions take the ARN suffix, not the ARN.
output "alb_arn_suffix" {
  value = aws_lb.this.arn_suffix
}

output "target_group_arn_suffix" {
  value = aws_lb_target_group.web.arn_suffix
}
