# LLM Gateway — Deployment

이 폴더는 LLM Gateway의 **프로덕션 배포에 필요한 모든 것**(Helm chart, Terraform, 문서, 스크립트)을 담습니다. 로컬 개발은 리포 루트의 `docker-compose.yml`을 사용하세요.

## 폴더 구조

```
deployment/
├── charts/llm-gateway/    Helm chart (7개 서비스 + 공통 리소스)
├── terraform/                AWS EKS Fargate 인프라 (환경별 VPC/EKS/Aurora/ElastiCache)
├── docs/
│   ├── eks-fargate/          AWS EKS Fargate 설치 가이드 (7 문서)
│   ├── onprem/               고객 on-prem K8s 설치 가이드 (5 문서)
│   ├── runbooks/             장애 대응 런북
│   ├── architecture.md       배포 아키텍처 전체 개요
│   └── secrets-contract.md   Secret 주입 계약 (requirements-document/deployment-secrets.md 참조)
└── scripts/
    ├── bootstrap-tfstate.sh  S3 + DynamoDB tfstate 백엔드 최초 1회 셋업
    ├── install-onprem.sh     고객 on-prem 원클릭 설치
    ├── upgrade.sh            helm upgrade --atomic
    ├── rollback.sh           helm rollback
    ├── smoke-test.sh         배포 후 E2E 검증
    └── package-chart.sh      helm package (고객 private repo용)
```

## 당신은 어느 환경에 배포하려는가?

| 환경 | 가이드 | 소요 시간 (초회) |
|------|-------|---------------|
| **AWS EKS Fargate** (자체 프로덕션) | [docs/eks-fargate/](docs/eks-fargate/) | 2~4시간 |
| **고객 on-prem K8s** | [docs/onprem/](docs/onprem/) | 30분~1시간 (고객이 K8s/DB/Redis 준비 상태라면) |
| **로컬 개발** | 리포 루트 `docker compose up` | 5분 |

## 환경 분리 전략

- **dev**: 기능 검증, 낮은 리소스, 단일 AZ Aurora/Redis
- **prod**: HA, 3+ replica, 다중 AZ Aurora/ElastiCache 클러스터

values 파일로 환경 차이를 흡수합니다. 같은 chart로 4가지 조합 지원:

| values 파일 | 용도 |
|------------|-----|
| `values.yaml` | 공통 기본값 (모든 환경 상속) |
| `values-eks-fargate-dev.yaml` | AWS 개발 환경 |
| `values-eks-fargate-prod.yaml` | AWS 운영 환경 |
| `values-onprem-dev.yaml` | 고객 on-prem 개발/스테이징 |
| `values-onprem-prod.yaml` | 고객 on-prem 운영 |

## 빠른 시작 (EKS Fargate, prod)

```bash
# 1) 최초 1회: tfstate 백엔드 준비
./scripts/bootstrap-tfstate.sh

# 2) 인프라 프로비저닝 (VPC → EKS → Aurora → ElastiCache → IRSA → ALB Controller)
cd terraform/environments/prod
terraform init && terraform plan && terraform apply

# 3) Helm 설치 (terraform 내부에서 helm_release로 자동 실행되거나 수동 호출)
cd ../../../..
helm install llm-gateway ./charts/llm-gateway \
  -f ./charts/llm-gateway/values-eks-fargate-prod.yaml \
  -n llm-gateway --create-namespace

# 4) 스모크 테스트
./scripts/smoke-test.sh --env prod
```

## 빠른 시작 (고객 on-prem)

```bash
# 고객이 K8s 접근 + DB/Redis/SMTP 엔드포인트 준비 완료 가정
kubectl create namespace llm-gateway

# Secret 주입 (DB 비번, JWT 키 등)
kubectl apply -f secrets.yaml -n llm-gateway

# Helm 설치
helm install llm-gateway ./charts/llm-gateway \
  -f ./charts/llm-gateway/values-onprem-prod.yaml \
  -n llm-gateway
```

상세 단계는 [docs/onprem/](docs/onprem/) 참조.

## 품질 게이트 (CI에서 강제)

모든 chart/Terraform 변경은 PR 머지 전 다음을 통과해야 합니다:

| 게이트 | 도구 | 실행 |
|-------|-----|-----|
| Chart lint | `helm lint` | `helm lint charts/llm-gateway -f charts/llm-gateway/values-*.yaml` |
| 렌더 검증 | `helm template` | 4개 values 파일 모두 렌더 성공 |
| K8s 스키마 | `kubeconform` | 렌더 결과를 K8s API 스키마로 검증 |
| 보안 정책 | `conftest` | root 금지, resource limit 강제, NetworkPolicy 강제 |
| Terraform 포맷 | `terraform fmt -check` | 모든 환경/모듈 |
| Terraform 검증 | `terraform validate` | 모든 환경 |

## 버전 관리

- Chart 버전: `charts/llm-gateway/Chart.yaml`의 `version`, SemVer
- 릴리즈: git tag (`v1.0.0`) + GitHub Release에 chart tarball 첨부
- 고객은 `git checkout v1.0.0` 또는 tarball 다운로드

## 관련 문서

- [docs/architecture.md](docs/architecture.md) — 배포 아키텍처 상세
- [docs/secrets-contract.md](docs/secrets-contract.md) — Secret 주입 컨트랙트
- [../requirements-document/deployment-secrets.md](../requirements-document/deployment-secrets.md) — 원본 암호화 키 배포 계약
