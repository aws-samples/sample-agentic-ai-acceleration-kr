# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type    = string
  default = "llm-gateway"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "azs" {
  type    = list(string)
  default = ["ap-northeast-2a", "ap-northeast-2c"] # dev는 2 AZ (비용)
}

variable "vpc_cidr" {
  type    = string
  default = "10.30.0.0/16" # 신규 vanilla 환경 — 기존 llm-gateway-dev(10.20)와 분리
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.1.0/24", "10.30.2.0/24"]
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.101.0/24", "10.30.102.0/24"]
}

variable "database_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.201.0/24", "10.30.202.0/24"]
}

variable "elasticache_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.211.0/24", "10.30.212.0/24"]
}

variable "eks_cluster_version" {
  # 이 값은 **라이브 클러스터와 일치**해야 한다(2026-09-04 실측: dev·prod 모두 1.31).
  # 여기가 라이브보다 낮으면 apply 가
  #   InvalidParameterException: Unsupported Kubernetes minor version update from 1.31 to 1.30
  # 으로 죽는다. 클러스터 자체는 무사하지만 **이 스택의 terraform 이 아무것도 못 돌게**
  # 되어 다른 드리프트가 쌓인다(과거 1.30→1.29 동일 사고: docs/eks-fargate/troubleshooting.md:205).
  # 그래서 아래 validation 으로 다운그레이드 커밋 자체를 막는다.
  #
  # ⚠️ 옛 주석 "minor version downgrade 불가" 는 **사실이 아니었다**. EKS User Guide
  #    "Downgrade the Kubernetes version for an Amazon EKS cluster" 기준, in-place 업그레이드
  #    **7일 이내**에는 직전 minor 로 롤백할 수 있다(클러스터 ACTIVE + ERROR insight 0 이 조건).
  #    7일이 지나면 정말로 불가하며 새 클러스터 + 워크로드 이관밖에 없다.
  #    단 Fargate 에서는 홉 후 파드를 재생성하면 kubelet skew 가 ERROR insight 로 떠서
  #    롤백이 막히므로, 롤백하려면 새 파드를 지워야 한다 = **계획된 다운타임**이다.
  #    "7일 롤백 가능" 을 "안전하고 공짜" 로 읽지 말 것.
  #
  # 정책: **선언값 = 라이브(=이미 적용된 최신)**. 이 파일의 목적은 업그레이드가 아니라
  # 후퇴 방지다. 실제 minor 업그레이드는 별개 승인 사항이며, 그때 쓸 규칙만 아래에 남긴다.
  #
  # 나중에 올릴 때의 규칙 (기록용, 최신 릴리스는 1.36):
  #   - minor 는 **한 칸씩만**. 다중 점프는 API 가 거부한다(1.31→1.36 = 5회 순차 홉).
  #   - 홉 순서는 ① 컨트롤플레인 minor → ② eks_addon_versions 를 그 버전 기본값으로 →
  #     ③ Fargate 파드 전량 재생성(kubelet 은 파드 재생성 때만 갱신된다).
  #     ②를 ①보다 먼저 하면 kube-proxy 가 apiserver 보다 새 버전이 되어 skew 위반이다.
  #   - 1.31 은 이미 extended support 다(표준지원 종료 2025-11-26 / extended 종료
  #     2026-11-26). 즉 **1.32 로 가는 첫 홉만 하드 기한이 있고**(그 전에 안 올리면 AWS 가
  #     강제 업그레이드하며 그건 롤백 불가), 이후 홉은 비용(클러스터당 $0.50/hr extended
  #     프리미엄) 동기라 일정 조정이 가능하다. 1.32·1.33 도 extended 이므로 프리미엄이
  #     실제로 사라지는 건 1.34+ 이다.
  # nullable = false: 명시적 `eks_cluster_version = null` 은 **거부가 아니라 아래 기본값으로
  # 폴백**한다(실측 2026-09-07: default 가 있는 변수는 null → default, 에러 없음).
  # 즉 이 키워드의 효과는 "null 이 downstream 으로 전파되지 않는다" 뿐이고, 잘못된 *값* 을
  # 막는 건 아래 validation 이다. default 가 **없는** 변수는 반대로 null 이
  # "required variable may not be set to null" 로 죽는다(modules/eks-fargate/variables.tf).
  type     = string
  default  = "1.31"
  nullable = false

  validation {
    condition = (
      can(regex("^1\\.[0-9]+$", var.eks_cluster_version)) &&
      tonumber(split(".", var.eks_cluster_version)[1]) >= 31
    )
    error_message = "라이브 EKS 가 1.31 이므로 1.31 미만은 apply 시 InvalidParameterException 이다(다운그레이드 커밋 방지 가드). 형식은 1.<minor>."
  }
}

variable "eks_addon_versions" {
  # 값은 **라이브와 정확히 일치**시켜 둔다(aws eks describe-addon 실측 2026-09-04:
  # dev·prod 3개 애드온 모두 아래와 동일, 전부 ACTIVE). 목적은 애드온 업그레이드가
  # 아니라 "plan 이 no-op" 인 상태를 유지해 의도치 않은 롤을 막는 것이다 —
  # coredns 는 클러스터 DNS 라 버전만 바꿔도 실제 파드 롤이 발생한다.
  # 모듈이 이 값을 그대로 addon_version 에 넘겨 자동 추종이 없으므로
  # (modules/eks-fargate/variables.tf 의 addon_versions 주석 참고)
  # 애드온은 클러스터 minor 를 올려도 여기 손대지 않으면 그대로 남는다.
  #
  # 나중에 minor 홉을 할 때 쓸 버전 표 (실측, ap-northeast-2 / vpc-cni 는 1.31~1.36 동일):
  #   1.32  coredns v1.11.4-eksbuild.51  kube-proxy v1.32.13-eksbuild.24  vpc-cni v1.22.4-eksbuild.3
  #   1.33  coredns v1.12.4-eksbuild.29  kube-proxy v1.33.10-eksbuild.21
  #   1.34  coredns v1.12.4-eksbuild.29  kube-proxy v1.34.6-eksbuild.21
  #   1.35  coredns v1.13.2-eksbuild.21  kube-proxy v1.35.3-eksbuild.21
  #   1.36  coredns v1.14.3-eksbuild.14  kube-proxy v1.36.0-eksbuild.17
  # ⚠️ 현재 핀이 각 버전에서 아직 제공되는지: coredns·vpc-cni 는 1.34 까지 ✓ / 1.35 ✗,
  #    kube-proxy 는 1.32 까지 ✓ / 1.33 ✗ (그 버전에서는 apply 가
  #    InvalidParameterException 으로 막히므로 홉 전에 이 블록을 먼저 올려야 한다).
  # 환경별로 따로 있는 이유: dev 를 먼저 올려 검증한 뒤 prod 를 따라가게 하려면
  # 두 루트가 독립적으로 움직일 수 있어야 한다(모듈 기본값 공유로는 불가능).
  type = object({
    coredns    = string
    kube_proxy = string
    vpc_cni    = string
  })
  # nullable = false: 명시적 `eks_addon_versions = null` 은 **아래 default 로 폴백**한다
  # (실측: default 가 있으면 null → default, 거부가 아니다). 그 덕에 모듈의
  # `var.addon_versions.coredns` 가 "Attempt to get attribute from null value" 로
  # 죽는 경로가 막힌다 — 거부가 아니라 폴백으로 막는 것이다.
  default = {
    coredns    = "v1.11.3-eksbuild.1"
    kube_proxy = "v1.29.7-eksbuild.2"
    vpc_cni    = "v1.18.3-eksbuild.1"
  }
  nullable = false
}

variable "aurora_engine_version" {
  type    = string
  default = "16.11"
}

variable "aurora_prod_instance_class" {
  # dev는 Serverless v2 로 자동 대체 (module 내부 로직)
  type    = string
  default = "db.serverless"
}

variable "elasticache_prod_node_type" {
  # dev는 cache.t4g.small 로 자동 대체 (module 내부 로직)
  type    = string
  default = "cache.t4g.small"
}

# dev ElastiCache 노드 수(deepdive Q50 Phase4). 1=단일노드(기본·기존, replica/failover
# 없음·저비용). 2 로 올리면 primary+replica → failover/Multi-AZ 자동 활성(prod 전
# failover 드릴 가능, 노드 ~2배 비용). 평소엔 1 유지 권장, 테스트 시 tfvars 로 2.
variable "elasticache_dev_num_cache_clusters" {
  type    = number
  default = 1
}

variable "enable_rds_proxy" {
  description = "Aurora 앞단에 RDS Proxy 배치 (connection pool). dev/prod 모두 기본 true 로 통일 — gateway user 인증을 Terraform 이 자동 관리하므로 Proxy 기본 사용 가능. 꼭 필요한 경우만 `-var enable_rds_proxy=false` 로 내릴 것."
  type        = bool
  default     = true
}

variable "application_namespace" {
  type    = string
  default = "llm-gateway"
}

# ─── Cognito (OIDC IDP) ───
variable "cognito_domain_suffix" {
  description = "Hosted UI 도메인 suffix. 최종: {project}-{env}-{suffix}.auth.{region}.amazoncognito.com (전 세계 unique). 빈 값이면 account_id 로 자동 생성(vanilla-auth-<account_id>) — 신규 계정 이식 시 별도 지정 불필요."
  type        = string
  default     = ""
}

variable "cognito_callback_urls" {
  description = "OIDC redirect URI 화이트리스트. gateway-cli 의 PKCE callback 용. 사용자 PC 의 localhost 포트."
  type        = list(string)
  default = [
    "http://localhost:8090/callback",
    "http://localhost:8091/callback",
    "http://localhost:8092/callback",
  ]
}

variable "cognito_logout_urls" {
  description = "OIDC logout redirect URI"
  type        = list(string)
  default = [
    "http://localhost:8090/logout",
    "http://localhost:8091/logout",
    "http://localhost:8092/logout",
  ]
}

variable "cognito_groups" {
  description = "User groups. Claude_<team> 은 Default Department 하위 팀, Claude_<dept>_<team> 은 dept 자동 생성 후 team 매핑, ClaudeAdmin 은 admin 부트스트랩."
  type        = list(string)
  default     = ["Claude_AI-Center_S/W-Culture-Office", "Claude_test-department_aws-test", "ClaudeAdmin"]
}

variable "bedrock_allowed_model_arns" {
  # 애플리케이션은 `global.anthropic.*` (cross-region inference profile) 로 호출.
  # IAM 은 inference-profile + 호출될 foundation-model 양쪽에 InvokeModel 허용 필요.
  #
  # ⚠️ **거부는 "이름을 안 쓴 쪽"에서 난다.** 프로파일 패턴(`global.anthropic.claude-*`)은
  # 세대에 무관하게 매칭되므로, foundation-model 줄만 구세대에 고정돼 있으면 신모델이
  # 조용히 AccessDenied 가 된다 — 프로파일은 통과했는데 그 프로파일이 가리키는
  # foundation-model 이 막힌 것이다. 실측(2026-08-19, federation token 으로 이 목록을
  # 그대로 세션 정책에 넣어 실호출):
  #     claude-4-* 만 있던 목록  → global.anthropic.claude-opus-5   AccessDenied
  #                                  (resource: foundation-model/anthropic.claude-opus-5)
  #     아래 5·6행 추가 후        → opus-5 / sonnet-5 둘 다 200 OK
  # 그래서 **신모델 등록 마이그레이션(0027 등)을 넣을 때 이 목록도 같이 늘려야 한다.**
  # DB 에 alias 를 넣는 것만으로는 호출되지 않는다.
  type = list(string)
  default = [
    # Foundation models (실제 추론이 실행되는 리소스)
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-*",
    # Claude 5 (migration 0027). 세대 전체를 `claude-*-5*` 로 열지 않고 모델별로 적는 이유는
    # fable-5 를 IAM 에서도 계속 막아 두기 위해서다(0027 이 의도적으로 미등록 — apne2
    # 프로파일 부재). 실측으로 fable-5 는 이 목록에서 AccessDenied 유지됨을 확인했다.
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5*",
    # Global cross-region inference profiles (application 이 호출하는 엔트리포인트)
    "arn:aws:bedrock:*::inference-profile/global.anthropic.claude-*",
    "arn:aws:bedrock:*:*:inference-profile/global.anthropic.claude-*",
    # APAC cross-region inference profile (예비, ap-northeast-2 전용)
    "arn:aws:bedrock:ap-northeast-2::inference-profile/apac.anthropic.claude-*",
    # ── GPT-5.6 표준 bedrock-runtime plane (migration 0031/0032) ──────────────
    # Mantle(`bedrock-mantle:*`, 아래 irsa 모듈의 별도 statement)과 달리 이 plane 은
    # 일반 `bedrock:InvokeModel` 이라 이 목록의 통제를 받는다. 위 Claude-5 사고와 **똑같이**
    # 프로파일과 foundation-model 을 **양쪽 다** 적어야 한다.
    #
    # 실측(2026-09-03, 859/us-east-2, `aws bedrock get-inference-profile`):
    #   us.openai.gpt-5.6-terra     → foundation-model/openai.gpt-5.6-terra
    #                                 in us-east-1 · us-east-2 · us-west-2  (3개 멤버 리전)
    #   global.openai.gpt-5.6-terra → arn:aws:bedrock:::foundation-model/... + us-east-2
    #   ap-northeast-2 에는 `global.` 프로파일만 존재하고 `us.` 는 **없다**.
    # foundation-model 줄의 리전을 `*` 로 두는 이유: `us.` 프로파일이 어느 멤버 리전에서
    # 실행될지 호출자가 고르지 못한다(라우팅은 Bedrock 이 한다). 리전을 좁히면 그 순간
    # 조용한 AccessDenied 가 된다 — 프로파일은 통과했는데 실행 리전이 막히는 형태.
    "arn:aws:bedrock:*::foundation-model/openai.gpt-5.6-*",
    "arn:aws:bedrock:*:*:inference-profile/us.openai.gpt-5.6-*",
    "arn:aws:bedrock:*:*:inference-profile/global.openai.gpt-5.6-*",
  ]
}

variable "eks_access_entries" {
  type    = any
  default = {}
}

variable "cowork_role_arn" {
  # Cowork cross-account Mantle role (905, Tokyo Opus 4.8). gateway-proxy AssumeRole into it.
  # Must match model.routing_profiles.account_role_arn for client=cowork (migration 0009).
  # The 905 role's trust must allow this env's gateway-proxy IRSA + sts:ExternalId=cowork-bedrock.
  type    = string
  default = "arn:aws:iam::222233334444:role/llm-gateway-cowork-bedrock"
}

variable "claude_code_374_role_arn" {
  # Claude Code cross-account Bedrock NATIVE role (333). gateway-proxy AssumeRole into it,
  # builds a 374 bedrock-runtime client (boto3 invoke_model). Must match
  # model.routing_profiles.account_role_arn for client=claude-code (migration 0022).
  # The 374 role trust must allow this env's gateway-proxy IRSA + sts:ExternalId=claude-code-bedrock.
  type    = string
  default = "arn:aws:iam::333344445555:role/llm-gateway-claude-code-bedrock"
}

variable "mantle_regions" {
  # gateway-proxy IRSA 가 in-account Bedrock Mantle(bedrock-mantle:*) 를 호출할 수 있는 리전.
  # 기본값 = 라이브와 동일(ap-northeast-1 Claude Code Opus 4.8 / us-east-2 Codex GPT-5.5).
  # 다른 리전 배포는 tfvars 에서 덮어쓴다. nullable=false — 명시적 null 은 아래 default 로
  # 폴백하므로(실측: 거부가 아니라 폴백) 모듈의 for 표현식이 "Iteration over null value" 로
  # 죽는 경로가 막힌다. 빈 리스트 `[]` 는 모듈 쪽 validation 이 plan 단계에서 거부한다.
  description = "in-account Bedrock Mantle 호출 허용 리전 목록"
  type        = list(string)
  nullable    = false
  default     = ["ap-northeast-1", "us-east-2"]
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ─── admin-chat-agent (Phase 1 부트스트랩 — 활성 시 ECR/S3/IAM 만 생성) ───
variable "enable_chat_agent" {
  description = "admin-chat-agent 인프라 (ECR + S3 staging + IAM + KMS) 생성 여부"
  type        = bool
  default     = false
}

# ─── admin-chat-agent BI tool Lambdas (query_db / get_schema) ───
# enable_chat_agent=true 가 선행 조건 (같은 모듈에 추가됨).
variable "enable_chat_db_tools" {
  description = "admin-chat-agent 의 query_db/get_schema Lambda + reader secret + SG 생성 여부"
  type        = bool
  default     = false
}

# ─── Bedrock model-invocation logging (GPT-5.6 runtime plane 본문 감사) ───
# ⚠️ 이 스위치를 켜면 `bedrock_invocation_log_region` 에서 일어나는 **계정 전체의**
#    Bedrock 호출 요청/응답 본문이 수집된다(모델별/주체별 스코프는 존재하지 않는다).
#
# ⚠️ **소유자 충돌 주의.** 리전당 로깅 설정은 싱글턴이라
#    `deployment/scripts/provision_bedrock_invocation_logging.py` 와 이 모듈이 서로를
#    덮어쓴다. dev us-east-2 는 현재 그 스크립트로 프로비저닝돼 있고(live 캡처 검증에
#    사용된 그 설정), 그래서 default 가 false 다. terraform 으로 소유권을 옮기려면 켜기
#    전에 기존 리소스를 import 해야 한다 — 이름 규칙이 스크립트와 동일하게 맞춰져 있어
#    import 자체는 안전하다:
#
#      tofu import 'module.bedrock_invocation_logging.aws_cloudwatch_log_group.invocations[0]' /aws/bedrock/modelinvocations
#      tofu import 'module.bedrock_invocation_logging.aws_iam_role.delivery[0]'                llm-gateway-dev-bedrock-invlog-us-east-2
#      tofu import 'module.bedrock_invocation_logging.aws_s3_bucket.large_bodies[0]'           llm-gateway-dev-bedrock-invlogs-<account>-us-east-2
#      tofu import 'module.bedrock_invocation_logging.aws_bedrock_model_invocation_logging_configuration.this[0]' us-east-2
#
#    import 없이 켜면 terraform 이 같은 이름으로 create 를 시도해 EntityAlreadyExists /
#    BucketAlreadyOwnedByYou 로 실패한다(파괴적이지는 않지만 apply 가 멈춘다).
variable "enable_bedrock_invocation_logging" {
  description = "us-east-2 Bedrock invocation logging(계정 전체 본문 수집)을 terraform 이 소유할지 여부. 위 주석의 import 절차를 읽고 켤 것"
  type        = bool
  default     = false
}

variable "bedrock_invocation_log_region" {
  description = "invocation log 가 쌓이는 리전 = 모델이 실행되는 리전. GPT-5.6 CRIS 는 us-east-2. ap-northeast-2 는 모듈이 거부한다(서울 Claude 본문 수집 방지)"
  type        = string
  default     = "us-east-2"
}

variable "bedrock_invocation_log_retention_days" {
  description = "log group 보존일. 프롬프트 원문이 들어 있으므로 무기한(0) 은 명시적 선택이어야 한다"
  type        = number
  default     = 30
}

# ─── 게이트웨이 자체 본문 로깅 sink (Firehose → S3) ───
# 위 AWS 네이티브 로깅과 **다른 것**이다. 차이:
#   네이티브  = 계정×리전 단위 AWS 설정. Mantle 트래픽을 전혀 잡지 못한다.
#   이 sink   = 게이트웨이가 직접 쓴다. Mantle·runtime 두 평면을 모두 덮는다.
# Codex/Cowork 는 Mantle 을 쓰므로, 그 트래픽 본문의 정본은 이쪽밖에 없다.
#
# ⚠️ 이 스위치는 sink 를 **만들** 뿐이고 수집을 시작하지 않는다. 수집에는 관리자 런타임
#    토글(/monitoring, 기본 OFF)이 추가로 필요하고, 켜는 조작은 audit.audit_logs 에
#    불변 행으로 남는다. 본문은 현재 마스킹되지 않으므로 두 겹으로 잠가 둔다.
variable "enable_body_logging" {
  description = "요청/응답 본문 로깅 sink(S3 + Firehose + IAM)를 만들지 여부. 만들기만 하며 수집은 관리자 토글이 별도로 켠다"
  type        = bool
  default     = false
}

variable "body_log_retention_days" {
  description = "본문 로그 S3 객체 만료일. 마스킹되지 않은 프롬프트가 들어 있으므로 무기한(0) 은 명시적 선택이어야 한다"
  type        = number
  default     = 90
}
