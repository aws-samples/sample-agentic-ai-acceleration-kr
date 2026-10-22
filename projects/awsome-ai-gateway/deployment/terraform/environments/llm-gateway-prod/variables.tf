# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type    = string
  default = "llm-gateway"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "azs" {
  type    = list(string)
  default = ["ap-northeast-2a", "ap-northeast-2c"] # prod는 multi-AZ (HA)
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16" # prod 환경 — 기존 새 dev(10.30)와 분리
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.1.0/24", "10.40.2.0/24"]
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.101.0/24", "10.40.102.0/24"]
}

variable "database_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.201.0/24", "10.40.202.0/24"]
}

variable "elasticache_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.211.0/24", "10.40.212.0/24"]
}

variable "eks_cluster_version" {
  # 이 값은 **라이브 클러스터와 일치**해야 한다. 기본값 1.36 은 신규 설치용이고, 이 레포의
  # 옛 기본값(1.31)으로 만든 클러스터는 terraform.tfvars 에 라이브 minor 를 고정한다(아래 ⚠️).
  # 여기가 라이브보다 낮으면 apply 가
  #   InvalidParameterException: Unsupported Kubernetes minor version update from 1.36 to 1.35
  # 으로 죽는다. 클러스터 자체는 무사하지만 **prod 스택의 terraform 이 아무것도 못 돌게**
  # 되어 다른 드리프트가 쌓인다(과거 1.30→1.29 동일 사고: docs/eks-fargate/troubleshooting.md
  # "EKS minor version downgrade 금지").
  # 실제로 prod 전체 plan 이 이 다운그레이드를 부비트랩으로 안고 있었다.
  # 아래 validation 은 **형식과 하한(1.31)만** 강제한다. 라이브보다 낮은 값(예: 1.36 클러스터에
  # 1.35)은 막지 못하고 apply 의 위 에러로만 드러나므로, 값을 바꾸기 전에
  # `aws eks describe-cluster --query cluster.version` 으로 라이브를 먼저 읽는다.
  #
  # ⚠️ 옛 주석 "minor version downgrade 불가" 는 **사실이 아니었다**. EKS User Guide
  #    "Downgrade the Kubernetes version for an Amazon EKS cluster" 기준, in-place 업그레이드
  #    **7일 이내**에는 직전 minor 로 롤백할 수 있다(클러스터 ACTIVE + ERROR insight 0 이 조건).
  #    7일이 지나면 정말로 불가하며 새 클러스터 + 워크로드 이관밖에 없다.
  #    단 Fargate 에서는 홉 후 파드를 재생성하면 kubelet skew 가 ERROR insight 로 떠서
  #    롤백이 막히므로, 롤백하려면 새 파드를 지워야 한다 = **계획된 다운타임**이다.
  #    "7일 롤백 가능" 을 "안전하고 공짜" 로 읽지 말 것 — prod 홉은 이 전제로 승인받아야 한다.
  #
  # 정책: **선언값 = 라이브(=이미 적용된 최신)**. 이 파일의 목적은 업그레이드가 아니라
  # 후퇴 방지다. 실제 minor 업그레이드는 별개 승인 사항이며, 그때 쓸 규칙만 아래에 남긴다.
  #
  # 올릴 때의 규칙 (절차·게이트·함정 전체: docs/eks-fargate/09-upgrade-131-to-136.md):
  #   - minor 는 **한 칸씩만**. 다중 점프는 API 가 거부한다(1.31→1.36 = 5회 순차 홉).
  #   - 홉 순서는 ① 컨트롤플레인 minor → ② eks_addon_versions 를 그 버전 기본값으로 →
  #     ③ Fargate 파드 전량 재생성(kubelet 은 파드 재생성 때만 갱신된다).
  #     ②를 ①보다 먼저 하면 kube-proxy 가 apiserver 보다 새 버전이 되어 skew 위반이다.
  #     ①과 ②는 같은 apply 에 넣지 말고 plan/apply 를 두 번으로 나눈다.
  #   - **prod 는 항상 dev 가 같은 홉을 통과한 뒤에** 올린다. 두 루트가 각자
  #     eks_addon_versions 를 갖는 이유가 이 시차를 만들기 위해서다.
  #   - ⚠️ 1.31 은 이미 extended support 다(표준지원 종료 2025-11-26 / extended 종료
  #     2026-11-26). 1.31 클러스터는 **1.32 로 가는 첫 홉에 하드 기한이 있다** — 그 전에
  #     안 올리면 AWS 가 컨트롤플레인을 강제 업그레이드하고 그건 롤백할 수 없다(애드온과
  #     Fargate 파드의 kubelet 은 이전 버전으로 남는다). 이후 홉은 비용(클러스터당 $0.50/hr
  #     extended 프리미엄) 동기라 일정 조정이 가능하다. 1.32·1.33 도 extended 이므로 프리미엄이
  #     실제로 사라지는 건 표준지원 중인 minor 에 도달한 뒤다(EKS 버전 캘린더로 확인).
  #   - AWS 가 이미 자동 업그레이드한 클러스터는 여기(tfvars)에 **라이브 minor** 를 고정하고
  #     그 다음 홉부터 시작한다(09 런북 "이미 AWS 가 자동 업그레이드한 클러스터").
  #   - 1.36 의 표준지원은 2027-08-02 까지다. 그 뒤는 extended support 프리미엄 구간이다.
  #
  # ⚠️ 이 레포의 옛 기본값(1.31)으로 만든 클러스터에 이 기본값을 그대로 apply 하면 안 된다
  #    (1.31→1.36 다중 점프 = InvalidParameterException 으로 스택이 멈춘다). tfvars 로 지금
  #    버전과 그 버전의 애드온을 먼저 고정한 뒤 위 순서로 한 칸씩 올린다. 아래 validation 의
  #    하한이 기본값(1.36)이 아니라 1.31 인 이유가 그 고정을 막지 않기 위해서다.
  # nullable = false: 명시적 `eks_cluster_version = null` 은 **거부가 아니라 아래 기본값으로
  # 폴백**한다(실측 2026-09-07: default 가 있는 변수는 null → default, 에러 없음).
  # 즉 이 키워드의 효과는 "null 이 downstream 으로 전파되지 않는다" 뿐이고, 잘못된 *값* 을
  # 막는 건 아래 validation 이다. default 가 **없는** 변수는 반대로 null 이
  # "required variable may not be set to null" 로 죽는다(modules/eks-fargate/variables.tf).
  type     = string
  default  = "1.36"
  nullable = false

  validation {
    condition = (
      can(regex("^1\\.[0-9]+$", var.eks_cluster_version)) &&
      tonumber(split(".", var.eks_cluster_version)[1]) >= 31
    )
    error_message = "eks_cluster_version 은 1.<minor> 형식이고 1.31 이상이어야 한다. 이 검사는 형식과 하한만 본다 — 라이브보다 낮은 값은 막지 못하고 apply 가 InvalidParameterException 으로 멈춘다. 값은 라이브 minor 와 같거나(고정) 한 칸 위(홉)여야 한다."
  }
}

variable "eks_addon_versions" {
  # 값은 **라이브와 정확히 일치**시켜 둔다(`aws eks describe-addon` 으로 확인). 기본값은
  # 1.36 클러스터용이고, 옛 기본값으로 만든 클러스터는 terraform.tfvars 에 라이브 값을
  # 고정한다. 목적은 애드온 업그레이드가 아니라 "plan 이 no-op" 인 상태를 유지해 의도치 않은
  # 롤을 막는 것이다 — coredns 는 클러스터 DNS 이고 prod 는 replicaCount 3 이라 버전만 바꿔도
  # 실제 파드 롤이다.
  # 모듈이 이 값을 그대로 addon_version 에 넘겨 자동 추종이 없으므로
  # (modules/eks-fargate/variables.tf 의 addon_versions 주석 참고)
  # 애드온은 클러스터 minor 를 올려도 여기 손대지 않으면 그대로 남는다.
  #
  # 1.31 → 1.36 홉의 참고값 (ap-northeast-2, 작성 시점
  # `aws eks describe-addon-versions --kubernetes-version <v>` 의 defaultVersion):
  #   1.31  coredns v1.11.4-eksbuild.60  kube-proxy v1.31.14-eksbuild.36  vpc-cni v1.22.4-eksbuild.3
  #   1.32  coredns v1.11.4-eksbuild.79  kube-proxy v1.32.13-eksbuild.32  vpc-cni 동일
  #   1.33  coredns v1.12.4-eksbuild.57  kube-proxy v1.33.10-eksbuild.29  vpc-cni 동일
  #   1.34  coredns v1.12.4-eksbuild.57  kube-proxy v1.34.6-eksbuild.29   vpc-cni 동일
  #   1.35  coredns v1.13.2-eksbuild.50  kube-proxy v1.35.3-eksbuild.29   vpc-cni 동일
  #   1.36  coredns v1.14.7-eksbuild.10  kube-proxy v1.36.0-eksbuild.25   vpc-cni 동일 (= 아래 default)
  # ⚠️ 애드온 빌드 번호는 계속 올라간다. 홉 시점에 재조회해서 쓸 것(이 표도 시간이 지나면
  #    stale 해진다). coredns 는 1.33·1.35·1.36 에서 마이너가 올라 DNS 파드 롤이 생긴다.
  # ⚠️ 옛 기본값(coredns v1.11.3 / kube-proxy v1.29.7 / vpc-cni v1.18.3)으로 만든 클러스터는
  #    1.31 행부터 적용한다. kube-proxy 1.29 는 1.31 컨트롤플레인보다 2 마이너 뒤라
  #    1.33 홉의 하드 블로커다.
  #    올릴 때는 트래픽 저점 창에서 하고 coredns 파드 3/3 Ready 를 확인할 것.
  # ⚠️ tfvars 의 고정은 라이브 값이 아래 default 와 **정확히 같을 때만** 지운다. 재조회로 더 새
  #    빌드를 적용했다면 고정을 남긴다 — 지우면 plan 이 애드온을 이 default 로 되돌린다.
  # prod 는 항상 dev 가 같은 값으로 먼저 통과한 뒤에 올린다.
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
    coredns    = "v1.14.7-eksbuild.10"
    kube_proxy = "v1.36.0-eksbuild.25"
    vpc_cni    = "v1.22.4-eksbuild.3"
  }
  nullable = false
}

# ─── 클러스터 컨트롤러 Helm chart 버전 (aws-load-balancer-controller / external-secrets) ───
# 모듈 기본값과 같은 값을 루트에 다시 선언하는 이유: 기존 클러스터가 terraform.tfvars 로
# 라이브 버전을 고정할 수 있게 하려는 것이다. 루트 변수가 없으면 레포가 모듈 기본값을 올리는
# 순간 다음 apply 가 그 업그레이드를 끼워 넣는다(eks_cluster_version 과 같은 문제).
# 고정은 라이브 값이 이 기본값과 같아졌을 때만 지운다(docs/eks-fargate/09-upgrade-131-to-136.md).
# prod 는 dev 가 같은 단계를 통과한 뒤에 고정을 푼다 — 두 루트가 각자 이 변수를 갖는 이유다.
# nullable = false: 명시적 null 은 아래 default 로 폴백한다. null 이 모듈로 넘어가면
# helm_release 의 version 이 비어 그 시점의 최신 chart 가 설치되므로 그 경로를 막는다.
variable "alb_controller_chart_version" {
  description = "aws-load-balancer-controller Helm chart 버전(module.alb_controller 의 chart_version). 기본 3.5.0. 옛 기본값 1.8.2 로 만든 클러스터는 tfvars 에 라이브 값을 고정하고, 올릴 때는 v3 CRD 수동 apply 가 먼저다(docs/eks-fargate/09-upgrade-131-to-136.md B 단계)."
  type        = string
  default     = "3.5.0"
  nullable    = false
}

variable "external_secrets_chart_version" {
  description = "external-secrets Helm chart 버전(module.external_secrets 의 chart_version, 정확한 버전만). 기본 2.11.0 은 external-secrets.io/v1 만 serve 하고, 모듈은 chart major >= 1 일 때만 webhook·cert-controller 를 끈다. 옛 기본값 0.10.4 로 만든 클러스터는 tfvars 에 라이브 값을 고정하고 docs/eks-fargate/09-upgrade-131-to-136.md C 단계로 올린다."
  type        = string
  default     = "2.11.0"
  nullable    = false
}

variable "external_secrets_serve_v1beta1" {
  description = "external-secrets chart 의 crds.unsafeServeV1Beta1(module.external_secrets 의 serve_v1beta1). 0.10.x → 2.x 전환 동안에만 true 로 두는 다리이고 평소에는 false 다. chart major 0 에서는 무시된다. 순서: docs/eks-fargate/09-upgrade-131-to-136.md C 단계."
  type        = bool
  default     = false
  nullable    = false
}

variable "aurora_engine_version" {
  type    = string
  default = "16.11"
}

variable "aurora_prod_instance_class" {
  # prod는 provisioned 인스턴스 (모듈이 prod일 때 serverlessv2_scaling_configuration={}로 보내고
  # instance_class를 var.prod_instance_class 그대로 사용 — 따라서 db.serverless 로 두면 모순).
  # 옛 prod에서 사용하던 동일 인스턴스 클래스. 부하 따라 r6g.xlarge 등으로 조정 가능.
  type    = string
  default = "db.r7g.large"
}

variable "elasticache_prod_node_type" {
  # prod는 큰 노드 (옛 prod 동일).
  type    = string
  default = "cache.r7g.large"
}

# ElastiCache prod HA(deepdive Q50 Phase4). 샤드당 replica 수 — 2 면 primary failover
# 시 zero-redundancy 윈도우 제거 + read 스케일(3샤드×3노드=9). 1 이면 기존(6노드).
# **비용 영향(+50% 노드시간)** — 운영 예산 결정 후 tfvars 로 명시. 모듈 기본 1 과 달리
# prod 권장값 2 를 이 env 기본으로 둔다(plan 으로 영향 확인 후 apply).
variable "elasticache_prod_replicas_per_node_group" {
  type    = number
  default = 2
}

# prod 커스텀 cluster 파라미터그룹(maxmemory-policy/reserved-memory) 사용 여부.
# ⚠️ 이 그룹이 박는 값(volatile-lru / reserved 25%)은 AWS 기본
# `default.valkey7.cluster.on` 의 값과 **동일**하다(2026-09-09 ap-northeast-2 실측).
# 그래서 이 토글을 켜고 끄는 것만으로는 **런타임 동작이 바뀌지 않는다** — 옛 설명
# "noeviction OOM-거부 회피" 는 사실과 다르다(default 가 이미 volatile-lru).
# true 의 의미는 "나중에 파라미터그룹 교체 없이 값만 튜닝할 손잡이를 미리 만든다".
# 실제 정책 변경(예: allkeys-lru)은 라이브 캐시 동작 변경이라 별도 승인 필요 —
# 모듈 main.tf 의 `aws_elasticache_parameter_group.prod_cluster` 주석 참고.
variable "elasticache_prod_enable_custom_param_group" {
  type    = bool
  default = true
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
  description = "User groups. Claude_<team> 은 Default Department 하위 팀, Claude_<dept>_<team> 은 dept 자동 생성 후 team 매핑, ClaudeAdmin 은 admin 부트스트랩. prod는 dev 검증에서 결정된 깨끗한 셋만 — 빈 팀 잔재(aws-test, S/W-Culture-Office) 미포함."
  type        = list(string)
  default     = ["Claude_AWS-AI-Specialist", "ClaudeAdmin"]
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
    # Foundation models (실제 추론이 실행되는 리소스) — Anthropic 은 `claude-*` 한 줄로 둔다.
    # 예전엔 세대·계열별로 적었다(opus-4-*, sonnet-5*, haiku-5* …). 이유는 "fable 을 IAM 에서도
    # 막는다" 였는데 0042 가 Fable 을 등록하면서 그 이유가 없어졌고, 그 방식은 신모델마다
    # "IaC 를 안 고치면 조용히 AccessDenied" 를 반복했다(0027 Claude 5, 0041 Haiku 5.5).
    # 벤더(anthropic)·Claude 라인으로는 여전히 한정된다 — openai 등 타 벤더는 아래 별도 줄이다.
    # ⚠️ Fable 은 IAM 만으로 호출되지 않는다: 실행 계정의 요청 리전 데이터 보존 모드가
    #    aws_review 여야 한다(0042 docstring — PUT https://bedrock.<region>.amazonaws.com/data-retention).
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
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
    # ── GPT-6 / GPT-6.1 (migration 0041, 같은 runtime plane) ─────────────────────
    # 버전을 박아 적는다(`gpt-6*` 로 열지 않는다): IAM 은 DB 행과 별개의 두 번째 게이트라,
    # 미래 모델(예: gpt-6.2)이 마이그레이션 없이 열리지 않게 한다. 위 5.6 과 같이
    # 프로파일과 foundation-model 을 **양쪽 다** 적는다.
    "arn:aws:bedrock:*::foundation-model/openai.gpt-6-*",
    "arn:aws:bedrock:*::foundation-model/openai.gpt-6.1-*",
    "arn:aws:bedrock:*:*:inference-profile/us.openai.gpt-6-*",
    "arn:aws:bedrock:*:*:inference-profile/us.openai.gpt-6.1-*",
    "arn:aws:bedrock:*:*:inference-profile/global.openai.gpt-6-*",
    "arn:aws:bedrock:*:*:inference-profile/global.openai.gpt-6.1-*",
    # ⚠️ OpenAI 호환 엔드포인트(/openai/v1/*)는 InvokeModel 을 **project 리소스에도** 검사한다.
    # 실측(2026-10-14, us-east-2, 파드 IRSA): 이 줄이 없으면 gpt-5.6-* 까지 401
    #   "not authorized to perform: bedrock:InvokeModel on resource:
    #    arn:aws:bedrock:us-east-2:<acct>:project/default"
    # 같은 정책 버전으로 2026-09-09 까지는 200 이었다(dev usage_logs 마지막 성공) — 그 뒤 AWS 가 추가한 검사다.
    # Claude(InvokeModel API)와 Mantle 은 이 검사를 받지 않는다(같은 날 실측).
    # 게이트웨이는 project 를 지정하지 않으므로 default 만 허용한다.
    "arn:aws:bedrock:*:*:project/default",
  ]
}

variable "eks_access_entries" {
  type    = any
  default = {}
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
# AgentCore Runtime 자체 (CreateAgentRuntime) 는 image push 후 별도 단계.
# 처음엔 false 로 두고, image 가 빌드/push 가능한 상태가 되면 true 로 enable.
variable "enable_chat_agent" {
  description = "admin-chat-agent 인프라 (ECR + S3 staging + IAM + KMS) 생성 여부"
  type        = bool
  default     = false
}

# ─── 게이트웨이 본문 로깅 sink (Firehose → S3) ───
# AWS 네이티브 invocation logging 과 다른 것이다. 차이:
#   네이티브  = 계정×리전 단위 AWS 설정. Mantle 트래픽을 전혀 잡지 못한다. prod 스택은
#               이것을 terraform 으로 소유하지 않는다.
#   이 sink   = 게이트웨이가 직접 쓴다. Mantle·runtime 두 평면을 모두 덮는다.
# Codex/Cowork 는 Mantle 을 쓰므로, 그 트래픽 본문의 정본은 이쪽밖에 없다.
#
# ⚠️ 이 스위치는 sink 를 **만들** 뿐이고 수집을 시작하지 않는다. 수집에는 관리자 런타임
#    토글(/monitoring, 기본 OFF)이 추가로 필요하다. 본문은 현재 마스킹되지 않으므로 두
#    겹으로 잠가 둔다.
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

variable "body_log_kms_key_arn" {
  description = "본문 로그 버킷 SSE-KMS 키 ARN. 빈 값이면 SSE-S3(AES256). prod 는 고객관리 키를 주는 것을 권장한다"
  type        = string
  default     = ""
}
