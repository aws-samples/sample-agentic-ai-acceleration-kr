# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "enabled" {
  description = <<-EOT
    이 sink 를 terraform 이 만들지 여부. **기본 false.**

    ⚠️ 기본값을 false 로 두는 이유: 스트림이 존재하는 것만으로 gateway-proxy 의 정적
       게이트(`firehose_stream_name` 이 설정됐는가)가 통과되어, 이후에는 관리자 토글
       하나만 남는다. 마스킹되지 않은 프롬프트 본문을 다루는 기능이므로 인프라
       단계에서도 명시적 opt-in 이어야 한다. 형제 모듈
       (bedrock-invocation-logging)과 같은 관용구다.
  EOT
  type        = bool
  default     = false
}

variable "project" {
  description = "리소스 이름 접두사(예: llm-gateway)"
  type        = string
}

variable "environment" {
  description = "환경 이름(dev/prod). 버킷·스트림·role 이름에 들어간다"
  type        = string
}

variable "force_destroy" {
  description = <<-EOT
    버킷에 객체가 있어도 삭제를 허용할지.

    ⚠️ 기본 false 다. dev 라도 마찬가지다 — 이 버킷에는 마스킹되지 않은 프롬프트
       본문이 들어 있고, `tofu destroy` 한 번에 사라지게 두면 그 내용을 아는 사람만
       위험을 인지한다. teardown 편의가 실제로 필요한 환경에서만 명시적으로 켠다.
  EOT
  type        = bool
  default     = false
}

variable "kms_key_arn" {
  description = "S3 SSE-KMS 키 ARN. 빈 값이면 SSE-S3(AES256). 본문에 PII 가 들어갈 수 있으므로 prod 는 고객관리 키 권장"
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = <<-EOT
    본문 로그 S3 객체 만료 일수. 형제 모듈의 보존 기간과 같은 축에 맞춘 90 이 기본.

    ⚠️ 0 = 만료 없음. 마스킹되지 않은 본문을 무기한 쌓는 선택이므로, 원본 구현처럼
       그것을 **기본값**으로 두지 않는다. 0 은 의식적으로 지정해야 한다.
  EOT
  type        = number
  default     = 90

  validation {
    condition     = var.log_retention_days >= 0
    error_message = "log_retention_days 는 0(만료 없음) 이상이어야 합니다."
  }
}

variable "buffer_size_mb" {
  description = "Firehose S3 flush 버퍼 크기(MB). 동적 파티셔닝 하한 64MB"
  type        = number
  default     = 64

  validation {
    # 동적 파티셔닝이 켜진 스트림은 64MB 미만을 API 가 거부한다. plan 은 통과하고
    # apply 에서 실패하므로 여기서 먼저 막는다.
    condition     = var.buffer_size_mb >= 64
    error_message = "동적 파티셔닝이 켜진 스트림의 buffering_size 하한은 64MB 입니다."
  }
}

variable "buffer_interval_seconds" {
  description = "Firehose S3 flush 버퍼 인터벌(초). 하한 60s"
  type        = number
  default     = 60

  validation {
    condition     = var.buffer_interval_seconds >= 60
    error_message = "동적 파티셔닝이 켜진 스트림의 buffering_interval 하한은 60초입니다."
  }
}

variable "tags" {
  description = "공통 태그"
  type        = map(string)
  default     = {}
}
