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

variable "admin_ui_callback_urls" {
  type        = list(string)
  description = "admin-ui(브라우저 SSO) 의 OIDC redirect URI. 비어 있으면 admin-ui 용 app client 를 만들지 않는다(기본). 예: [\"https://admin.example.com/api/auth/callback\"]. admin-ui 의 콜백 경로는 /api/auth/callback 이다."
  default     = []

  validation {
    # Cognito 는 localhost 가 아니면 https 만 받는다. apply 중간에 InvalidParameterException 으로
    # 죽는 대신 plan 에서 알린다. 호스트 자리에는 이름과 포트만 받는다 — `user@host` 형태를
    # 허용하면 `https://admin.example.com@other.example/` 처럼 눈에 보이는 이름과 실제 호스트가
    # 다른 값이 통과한다.
    condition = alltrue([
      for u in var.admin_ui_callback_urls : can(regex("^(https://[A-Za-z0-9.-]+(:[0-9]+)?|http://localhost(:[0-9]+)?)/", u))
    ])
    error_message = "admin_ui_callback_urls 의 각 값은 https:// 로 시작해야 합니다(localhost 만 http 허용). admin-ui 를 평문 HTTP 로만 노출하고 있다면 먼저 HTTPS 를 붙이십시오."
  }
}

variable "admin_ui_logout_urls" {
  type        = list(string)
  description = "admin-ui 로그아웃 뒤 돌아올 URI 화이트리스트. 비워도 된다."
  default     = []
}

variable "admin_ui_token_validity_hours" {
  type        = number
  description = "admin-ui app client 가 받는 id/access token 의 수명(시간). admin-ui 는 이 토큰을 그대로 세션 쿠키로 쓰므로 **관리 콘솔 세션 길이**가 된다. 그룹에서 뺀 관리자의 권한은 이 시간이 지나야 사라진다 — 짧게 둔다."
  default     = 1

  validation {
    # 상한 12 는 admin-ui 가 쿠키에 거는 상한과 같다(callback/route.ts MAX_COOKIE_MAX_AGE).
    condition     = var.admin_ui_token_validity_hours >= 1 && var.admin_ui_token_validity_hours <= 12 && floor(var.admin_ui_token_validity_hours) == var.admin_ui_token_validity_hours
    error_message = "admin_ui_token_validity_hours 는 1 이상 12 이하의 정수여야 합니다."
  }
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
