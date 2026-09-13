# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ⚠️ 비활성일 때 모두 빈 문자열이다. 그 값이 그대로 helm 의 gatewayProxy.env 로 흘러가고,
#    gateway-proxy 는 빈 `FIREHOSE_STREAM_NAME` 을 "설정되지 않음" 으로 읽어 로거를
#    no-op 으로 만든다(정적 게이트). 즉 이 모듈을 끄면 코드 변경 없이 수집이 멈춘다.
#    null 이 아니라 빈 문자열인 이유: helm `--set` 과 values 병합이 null 을 다루는 방식이
#    경로마다 달라, 빈 문자열이 예측 가능하다.

output "firehose_stream_name" {
  description = "gateway-proxy 의 FIREHOSE_STREAM_NAME env 에 넣을 스트림 이름 (비활성 시 \"\")"
  value       = try(aws_kinesis_firehose_delivery_stream.body_logs[0].name, "")
}

output "firehose_stream_arn" {
  description = "Firehose delivery stream ARN — irsa 모듈의 gateway-proxy write 정책용 (비활성 시 \"\")"
  value       = try(aws_kinesis_firehose_delivery_stream.body_logs[0].arn, "")
}

output "bucket_name" {
  description = "gateway-proxy 의 BODY_LOG_S3_BUCKET env 에 넣을 버킷 이름 (Firehose 레코드 상한 초과분 S3 직행 fallback, 비활성 시 \"\")"
  value       = try(aws_s3_bucket.body_logs[0].bucket, "")
}

output "bucket_arn" {
  description = "S3 버킷 ARN — irsa 모듈의 s3:PutObject fallback 정책용 (비활성 시 \"\")"
  value       = try(aws_s3_bucket.body_logs[0].arn, "")
}
