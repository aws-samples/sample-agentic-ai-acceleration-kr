# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type        = string
  description = "Project prefix (예: dslg)"
}

variable "environment" {
  type        = string
  description = "환경 식별자 (dev | prod)"
}

variable "aws_region" {
  type        = string
  description = "AWS region (issuer URL 빌드에 사용)"
}

variable "domain_suffix" {
  type        = string
  description = "Hosted UI 도메인 prefix suffix. 최종 도메인: {project}-{env}-{suffix}.auth.{region}.amazoncognito.com (전 세계 unique 해야 함)"
  default     = "auth"
}

variable "callback_urls" {
  type        = list(string)
  description = "OIDC redirect URI 화이트리스트. gateway-cli 가 사용. 기본: localhost callback (PKCE)"
  default = [
    "http://localhost:8090/callback",
    "http://localhost:8091/callback",
    "http://localhost:8092/callback",
  ]
}

variable "logout_urls" {
  type        = list(string)
  description = "OIDC logout redirect URI 화이트리스트"
  default = [
    "http://localhost:8090/logout",
    "http://localhost:8091/logout",
    "http://localhost:8092/logout",
  ]
}

variable "groups" {
  type        = list(string)
  description = "User groups. 'Claude_<team>' 은 Default Department 하위 팀, 'Claude_<dept>_<team>' 은 dept 자동 생성 후 team 매핑, ClaudeAdmin 은 admin 부트스트랩."
  default     = ["Claude_AI-Center_S/W-Culture-Office", "Claude_test-department_aws-test", "ClaudeAdmin"]
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
