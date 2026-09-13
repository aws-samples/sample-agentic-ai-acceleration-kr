# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 형제 모듈(bedrock-invocation-logging)과 같은 하한. `server_side_encryption`
      # 블록과 `buffering_size`/`buffering_interval`(구 `buffer_size`/`buffer_interval`)
      # 이름을 쓰므로 5.x 초기 버전에서는 렌더되지 않는다.
      version = ">= 5.70"

      # ⚠️ 이 모듈은 형제 모듈과 달리 `configuration_aliases` 를 쓰지 **않는다** —
      #    의도적이다. 여기서 만드는 것은 우리 계정의 일반 리소스(S3/Firehose/IAM)이고,
      #    게이트웨이 파드와 같은 리전에 있어야 한다(파드→Firehose 왕복이 리전을 넘지
      #    않도록). 즉 환경 루트의 기본 provider 를 상속하는 것이 옳다.
      #
      #    형제 모듈이 별칭을 강제하는 이유는 그쪽이 **계정×리전 단위 AWS 설정**이라
      #    리전을 잘못 상속하면 서울 전체의 본문을 수집하게 되기 때문이다. 그 위험이
      #    여기에는 없다.
    }
  }
}
