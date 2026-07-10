# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "vpc_id" {
  description = "생성된 VPC ID"
  value       = module.vpc.vpc_id
}

output "vpc_cidr" {
  description = "VPC CIDR block"
  value       = module.vpc.vpc_cidr_block
}

output "private_subnet_ids" {
  description = "워크로드용 private subnet ID 리스트 (Fargate)"
  value       = module.vpc.private_subnets
}

output "public_subnet_ids" {
  description = "Public subnet ID 리스트 (ALB)"
  value       = module.vpc.public_subnets
}

output "database_subnet_group_name" {
  description = "Aurora용 DB subnet group name"
  value       = module.vpc.database_subnet_group_name
}

output "elasticache_subnet_group_name" {
  description = "ElastiCache subnet group name"
  value       = module.vpc.elasticache_subnet_group_name
}

output "private_subnet_cidrs" {
  value = module.vpc.private_subnets_cidr_blocks
}

output "default_security_group_id" {
  value = module.vpc.default_security_group_id
}

output "nat_public_ips" {
  description = "NAT Gateway public IP 리스트 (외부 서비스 방화벽 allow-list용)"
  value       = module.vpc.nat_public_ips
}
