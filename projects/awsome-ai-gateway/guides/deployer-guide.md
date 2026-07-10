# LLM Gateway — 배포자 가이드 (Deployer Guide)

이 문서는 LLM Gateway를 AWS EKS Fargate에 **처음 배포**하고 **운영 유지**하는 인프라 엔지니어를 위한 가이드입니다.

---

## 목차

1. [시스템 아키텍처 개요](#1-시스템-아키텍처-개요)
2. [사전 준비사항](#2-사전-준비사항)
3. [AWS EKS Fargate 배포](#3-aws-eks-fargate-배포)
4. [시크릿 관리](#4-시크릿-관리)
5. [Helm Chart 구성](#5-helm-chart-구성)
6. [Observability (모니터링)](#6-observability-모니터링)
7. [부록 A: 파일 구조 빠른 참조](#부록-a-파일-구조-빠른-참조)
8. [부록 B: 배포 계층 구조](#부록-b-배포-계층-구조)
9. [부록 C: Pod 레벨 데이터 흐름](#부록-c-pod-레벨-데이터-흐름)
10. [부록 D: 보안 경계](#부록-d-보안-경계)

---

## 1. 시스템 아키텍처 개요

### 1.1 서비스 구성 (7개 컴포넌트)

| 서비스 | 역할 | 포트 | Prod 레플리카 |
|--------|------|------|---------------|
| **gateway-proxy** | 사용자 API 진입점. VK 인증, 라우팅, Rate Limit, Bedrock/vLLM 호출 | 8000 | 3+ (HPA) |
| **admin-api** | 관리 REST API. VK 발급, 예산/Rate Limit/모델 CRUD | 8080 | 3+ (HPA) |
| **admin-ui** | Next.js 14 관리 대시보드 | 3000 | 2+ (HPA) |
| **scheduler** | ROI 집계, VK 만료 정리 (싱글톤 크론) | — | 1 |
| **notification-worker** | Redis pub/sub → 이메일/내부 API 알림 | — | 2+ (HPA) |
| **cost-recorder-worker** | Redis Stream → Aurora 배치 기록 (비동기 비용 처리) | — | 3+ (HPA) |
| **migration** | Alembic DB 마이그레이션 (pre-install/pre-upgrade Hook Job) | — | 1 (Job) |

### 1.2 외부 의존성

| 구성요소 | 역할 |
|----------|------|
| PostgreSQL | Aurora PostgreSQL 16.4 |
| Redis | ElastiCache Valkey (Cluster Mode) |
| Email | 내부 메일 API / SES |
| Secrets | AWS Secrets Manager + ESO |
| LLM | AWS Bedrock (Claude) |
| OIDC IdP | AWS Cognito |
| Container Registry | ECR |

### 1.3 네트워크 다이어그램 (AWS EKS)

```
          ┌──────────────────────────────┐
          │  ALB (자동 생성 DNS)          │
          │  *.elb.amazonaws.com         │
          ├──────────┬─────────┬─────────┤
          │ gateway  │admin-ui │admin-api│
          └────┬─────┴────┬────┴────┬────┘
               │          │         │
   ═══════════════ VPC (10.10.0.0/16) ═══════════════
               │          │         │
          ├── Public Subnets  (/24 × 3)  ── ALB 전용
          ├── Private Subnets (/24 × 3)  ── Fargate Pods (NAT 경유 외부 접근)
          ├── Database Subnets (/24 × 3) ── Aurora (인터넷 불가)
          └── ElastiCache Subnets (/24 × 3) ── Redis (인터넷 불가)
                    │                    │
               Aurora HA             ElastiCache
            (Multi-AZ)           (Cluster Mode)
                    │
          Secrets Manager (KMS 암호화)
```

### 1.4 요청 흐름

```
User HTTP → ALB → gateway-proxy Pod
                    │ (미들웨어 파이프라인)
                    ├─ 1. VK 인증:   Redis cache → (miss) → DB
                    ├─ 2. Rate Limit: Redis Lua (USER/TEAM/GLOBAL)
                    ├─ 3. Budget:    Redis Lua (HARD_BLOCK 시 429)
                    ├─ 4. Router:    model alias → provider 매핑
                    ├─ 5. Provider:  Bedrock (IRSA) 또는 vLLM (httpx)
                    ├─ 6. Stream:    SSE pass-through → 클라이언트
                    └─ 7. Cost:      Redis Stream XADD (비동기)
                                       ↓
                    cost-recorder-worker → Aurora (batch INSERT)
```

---

## 2. 사전 준비사항

### 2.1 필수 도구

| 도구 | 최소 버전 |
|------|-----------|
| AWS CLI | v2.x |
| Terraform | v1.9+ |
| kubectl | v1.29+ |
| Helm | v3.14+ |
| Docker 또는 Finch | 최신 |
| jq | 최신 |

### 2.2 AWS 계정 준비

- IAM 사용자/역할에 필요한 권한: VPC, EKS, RDS, ElastiCache, Secrets Manager, ECR, Cognito, IAM Role 생성
- Bedrock 모델 액세스 승인: AWS Console → Bedrock → Model access → Claude 모델 활성화
- (선택) Bedrock Invocation Logging 활성화: AWS Console → Bedrock → Settings → Model invocation logging → S3/CloudWatch 대상 설정. 활성화하면 모든 Bedrock 호출의 입출력을 감사 로그로 기록할 수 있습니다. [설정 가이드](https://docs.aws.amazon.com/ko_kr/bedrock/latest/userguide/model-invocation-logging.html)

### 2.3 네트워크 요구사항

| 방향 | 프로토콜 | 목적 |
|------|----------|------|
| Pod → Internet (NAT) | HTTPS 443 | Bedrock API, ECR pull, Cognito |
| ALB → Pod | HTTP 8000/8080/3000 | 서비스 통신 |
| Pod → Aurora | TCP 5432 | 데이터베이스 |
| Pod → Redis | TCP 6379 | 캐시/스트림 |
| Internet → ALB | HTTP 80 | 사용자 접근 |

---

## 3. AWS EKS Fargate 배포

단계별 상세 실행 절차는 [`../deployment/docs/eks-fargate/`](../deployment/docs/eks-fargate/)에 있습니다. 아래는 전체 흐름 요약입니다.

| 단계 | 상세 문서 | 요약 | 소요 시간 |
|------|-----------|------|-----------|
| 1 | [01-prerequisites.md](../deployment/docs/eks-fargate/01-prerequisites.md) | tfstate 백엔드 부트스트랩 (S3+DynamoDB) | 5분 |
| 2 | [02-terraform-apply.md](../deployment/docs/eks-fargate/02-terraform-apply.md) | VPC·EKS·Aurora·ElastiCache 프로비저닝 | 45~60분 |
| 3 | [03-secrets.md](../deployment/docs/eks-fargate/03-secrets.md) | Secrets Manager에 앱/DB/Redis 시크릿 등록 | 15분 |
| 4 | [04-helm-install.md](../deployment/docs/eks-fargate/04-helm-install.md) | Docker 빌드 + ECR 푸시 + Helm install | 15분 |
| 5 | [05-smoke-test.md](../deployment/docs/eks-fargate/05-smoke-test.md) | Pod Ready, Health, E2E 검증 | 10분 |
| 6 | [06-upgrade-rollback.md](../deployment/docs/eks-fargate/06-upgrade-rollback.md) | Helm upgrade / rollback 절차 | — |
| 7 | [07-cognito-onboarding.md](../deployment/docs/eks-fargate/07-cognito-onboarding.md) | Cognito 사용자 생성, 그룹 매핑 | 10분 |

트러블슈팅: [troubleshooting.md](../deployment/docs/eks-fargate/troubleshooting.md) (36개 이슈 + 해결법)

### Terraform 생성 리소스 요약

| 모듈 | 리소스 |
|------|--------|
| vpc | VPC, 서브넷(4종×3AZ), NAT Gateway, 라우트 테이블 |
| eks-fargate | EKS 클러스터, Fargate Profile 3개, OIDC Provider |
| aurora-postgresql | RDS 클러스터, 인스턴스, RDS Proxy(prod), 보안 그룹 |
| elasticache-valkey | ElastiCache Cluster Mode, 복제 그룹 |
| irsa | IAM 역할 3개 (gateway-proxy, admin-api, external-secrets) |
| alb-controller | ALB Controller Helm + IRSA |
| external-secrets | ESO Helm + IRSA + KMS 복호화 권한 |
| cognito | User Pool, 도메인, 클라이언트, 그룹 |

### Prod vs Dev 주요 차이

| 리소스 | Dev | Prod |
|--------|-----|------|
| Aurora | Serverless v2 (0.5~4.0 ACU) | r7g.2xlarge × 2 |
| ElastiCache | 단일 노드 | r7g.large × 6 (Cluster) |
| NAT Gateway | 1개 | 3개 (AZ별) |

### Cognito 그룹 명명 규칙

그룹명은 `OIDC_GROUP_PREFIX` (기본 `Claude_`) 접두사 + underscore 개수로 팀/부서가 결정됩니다:

| 그룹명 패턴 | underscore 수 | 결과 |
|-------------|---------------|------|
| `Claude_Backend` | 1개 | Default Department → "Backend" 팀 |
| `Claude_AI-Center_Backend` | 2개 | "AI-Center" 부서 자동 생성 → "Backend" 팀 |
| `ClaudeAdmin` | prefix 불일치 | 팀 매핑 제외 (Admin 전용 그룹) |

- 팀명/부서명에 underscore 사용 금지 (하이픈 `-` 사용)
- underscore 3개 이상은 reject
- `OIDC_REJECT_UNMATCHED_GROUPS: true` (기본값) 시, 매칭 그룹 없으면 403

### DEV_LOGIN_ENABLED 주의사항

> `values-eks-fargate-prod.yaml`에 `DEV_LOGIN_ENABLED: "true"`가 남아있을 수 있습니다. 이 설정이 활성화되면 OIDC 인증 없이 dev-login 우회 접근이 가능합니다. **운영 환경에서는 SSO 연동 완료 후 반드시 `"false"`로 변경**하세요.

---

## 4. 시크릿 관리

> 시크릿 주입 계약, 로테이션 상세, 유출 대응 절차는 [`../deployment/docs/secrets-contract.md`](../deployment/docs/secrets-contract.md)를 참조하세요.

### 4.1 시크릿 목록

| 시크릿 이름 | 용도 | 소비자 | 교체 주기 |
|-------------|------|--------|-----------|
| `virtual_key_encryption_key` | VK AES-256-GCM 암호화 DEK | gateway-proxy, admin-api | 1년 |
| `nextauth_secret` | Admin UI 세션 서명 | admin-ui | 6개월 |
| `jwt_jwks_cache_key` | JWT 검증 캐시 | gateway-proxy, admin-api | 1년 |
| DB `password` | Aurora 접근 | 전체 서비스, migration | 90일 |
| Redis `password` | ElastiCache AUTH | 전체 서비스 | 90일 |

### 4.2 주입 방식 (External Secrets Operator)

```
Secrets Manager → ESO (refreshInterval 폴링) → K8s Secret → Pod (Volume/Env)
```

> 폴링 주기(`refreshInterval`)는 환경별로 다릅니다: **dev `1h` / prod `30m`** (`values-eks-fargate-{dev,prod}.yaml`).

### 4.3 시크릿 교체 절차

```bash
# 1) Secrets Manager 업데이트 (AWS)
aws secretsmanager update-secret \
  --secret-id "/llm-gateway/prod/app" \
  --secret-string '{"virtual_key_encryption_key":"<NEW_KEY>", ...}'

# 2) ESO가 refreshInterval(dev 1h / prod 30m) 내 자동 동기화
# 또는 즉시 강제 동기화:
kubectl annotate externalsecret llm-gateway-app \
  -n llm-gateway \
  force-sync=$(date +%s) --overwrite

# 3) Pod 자동 재시작 (reloader annotation 설정 시)
```

### 4.4 RDS master 비번 드리프트 원천 차단

Aurora `ManageMasterUserPassword=on` 환경에서는 RDS가 master 비번을 **자동 로테이션**하며,
그 값을 `rds!cluster-<uuid>` 형태의 별도 Secrets Manager 시크릿에 보관합니다. 이 경우 app
시크릿(`<prefix><env>/db`)의 `master_password`를 그대로 두면 RDS 로테이션과 값이 어긋나
(드리프트) migration Job이 인증 실패할 수 있습니다.

`values.yaml`의 `database.external`에서 master 비번을 RDS 관리 시크릿으로 직접 가리켜
드리프트를 원천 차단하세요:

```yaml
database:
  external:
    masterPasswordRemoteKey: "rds!cluster-<uuid>"   # 이름만 (suffix 불필요)
    masterPasswordRemoteProperty: "password"
```

- 빈 값이면 하위호환으로 `<prefix><env>/db` 시크릿의 `master_password` 프로퍼티를 사용합니다.
- `rds!cluster-<uuid>`를 참조하려면 **ESO IRSA 정책에 `rds!cluster-*` read 권한**이 필요합니다 (irsa 모듈에 추가됨).

---

## 5. Helm Chart 구성

### 5.1 Values 파일 계층

```
values.yaml                               ← 공통 기본값 (모든 환경)
├── values-eks-fargate-dev.yaml           ← EKS Dev 오버라이드
├── values-eks-fargate-prod.yaml          ← EKS Prod 오버라이드 (HA, HPA, IRSA)
├── values-eks-fargate-prod-loadtest.yaml ← EKS Prod 부하테스트용 오버라이드
├── values-onprem-dev.yaml                ← On-Prem Dev 오버라이드
└── values-onprem-prod.yaml               ← On-Prem Prod 오버라이드
```

> ⚠️ **prod 배포 시 서비스별 `image.tag`를 반드시 명시 핀할 것.**
> `values-eks-fargate-prod.yaml`에는 현재 어떤 서비스도 `image.tag`가 핀되어 있지
> 않습니다(dev values는 `1.0.51-resilience` 등 서비스별로 명시 핀). 또한
> `install-eks.sh`는 `--set`으로 `global.imageRegistry`만 주입하고 **image.tag는 주입하지
> 않습니다**. 태그를 비워둔 채 배포하면 전 서비스가 `Chart.yaml`의 `appVersion`으로
> 폴백되어, 의도치 않은 버전으로 다운그레이드되거나 migration(alembic head)이 해당
> 이미지에서 미지원되어 배포가 실패할 수 있습니다. prod values의 각 서비스
> `image.tag`를 배포 대상 버전으로 명시하세요.

### 5.2 주요 설정 섹션

**Database**:
```yaml
database:
  host: ""           # Aurora endpoint
  port: 5432
  name: gateway
  user: gateway
  poolSize: 20       # Prod 권장 (기본값: 10)
  maxOverflow: 30    # Prod 권장 (기본값: 20)
  sslMode: require   # Aurora는 require 필수
  statementCacheSize: 0  # RDS Proxy 사용 시 0, 직접 연결 시 100
```

**Redis**:
```yaml
redis:
  host: ""
  port: 6379
  tls: true
  clusterMode: true  # Prod 권장
  poolSize: 150      # Pod당
```

**인증**:
```yaml
auth:
  jwt:
    issuerUrl: ""         # Cognito/OIDC issuer
    jwksUri: ""           # JWKS endpoint
    cacheTtl: 3600
  virtualKey:
    encryptionKeySecret: llm-gateway-app
    # VK TTL: OIDC_VK_TTL_HOURS (기본 1시간, 짧게 유지 권장)
```

**HPA (Prod)**:
```yaml
gatewayProxy:
  hpa:
    enabled: true
    minReplicas: 3
    maxReplicas: 30
    targetCPUUtilizationPercentage: 65
```

**Ingress (ALB)**:
```yaml
ingress:
  className: alb
  annotations:
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/idle-timeout: "600"
    alb.ingress.kubernetes.io/certificate-arn: "<ACM_ARN>"
```

---

## 6. Observability (모니터링)

### 6.1 스택 구성

| 컴포넌트 | 역할 |
|----------|------|
| kube-prometheus-stack | Prometheus + Grafana + kube-state-metrics |
| prometheus-adapter | Fargate 환경 HPA 메트릭 제공 (metrics.k8s.io 대체) |
| OTel Collector | 애플리케이션 트레이스 수집 (OTLP → Prometheus) |

### 6.2 Fargate 특수사항

EKS Fargate에서는 metrics-server가 webhook authorization에 의해 차단됩니다. 해결:

```
Prometheus (cAdvisor 스크래핑) → prometheus-adapter → metrics.k8s.io API → HPA
```

### 6.3 Observability 스택 설치

전체 스택은 `deployment/observability/install.sh`로 일괄 설치됩니다:

```bash
cd deployment/observability
bash install.sh
```

내부적으로 3단계 실행:
1. `kube-prometheus-stack/install.sh` — Prometheus + Grafana (Helm)
2. `prometheus-adapter/install.sh` — metrics.k8s.io API 서빙 (Helm)
3. `otel-collector/install.sh` — OTel Collector (Helm)

> metrics-server가 이미 설치되어 있으면 APIService 소유권 충돌이 발생합니다. prometheus-adapter 설치 전에 `helm uninstall metrics-server -n kube-system` 실행 필요.

### 6.4 Grafana 대시보드

배포 시 자동으로 아래 대시보드가 프로비저닝됩니다:
- Gateway 운영 메트릭 (요청량, 지연시간, 에러율)
- 모델별 비용 추이
- Pod 리소스 사용량
- Redis/Aurora 커넥션 상태

### 6.5 알림 설정

Notification Worker가 예산 임계값 도달 시 자동 알림:
- 80% / 90% / 100% 단계별 이메일
- 팀 리더 + 관리자에게 발송

> **현재 상태**: dev/prod 모두 `email.provider: "mock"` (이메일 미발송). 실제 알림을 활성화하려면 values 파일에서 `notificationWorker.email.provider`를 `"internal_api"`로 변경하고 `internalApi.url`에 내부 메일 API 주소를 설정하세요.

### 6.6 OTel (OpenTelemetry) 구현 범위

gateway-proxy에 OTel SDK가 통합되어 있으며, OTel Collector 배포 매니페스트가 준비되어 있습니다. 현재 **metrics 파이프라인만 정상 동작**합니다.

| 파이프라인 | 상태 | 경로 |
|-----------|------|------|
| **Metrics** | 운영 가능 | gateway-proxy → OTel Collector → Prometheus remote-write → Grafana |
| Traces | 수집 구조만 구성 | gateway-proxy → OTel Collector → `debug` exporter (저장소 미연동) |
| Logs | 수집 구조만 구성 | gateway-proxy → OTel Collector → `debug` exporter (저장소 미연동) |

Traces/Logs를 실제 운영에 활용하려면 Tempo(traces) 또는 Loki(logs)와 같은 백엔드를 추가 배포하고, OTel Collector ConfigMap의 `exporters` 섹션을 교체해야 합니다.

---

---

## 부록 A: 파일 구조 빠른 참조

```
deployment/
├── scripts/
│   ├── bootstrap-tfstate.sh    ← Step 1: State 백엔드
│   ├── install-eks.sh          ← Step 4: Helm 설치
│   └── smoke-test.sh           ← Step 5: 검증
├── terraform/
│   ├── environments/
│   │   ├── llm-gateway-dev/    ← Dev 환경 변수
│   │   └── llm-gateway-prod/   ← Prod 환경 변수
│   └── modules/                ← 재사용 모듈 8개
├── charts/llm-gateway/
│   ├── Chart.yaml              ← 차트 메타 (appVersion: 1.0.43-rebrand)
│   ├── values.yaml             ← 기본값
│   └── values-*.yaml           ← 환경별 오버라이드
├── docs/
│   ├── secrets-contract.md     ← 시크릿 계약
│   └── eks-fargate/            ← EKS 배포 단계별 문서 + troubleshooting.md
└── observability/              ← Prometheus, Grafana 설정
```

> 아키텍처 상세는 레포 루트의 `ARCHITECTURE.md`를 참조하세요 (`deployment/docs/`에는 별도 architecture.md가 없습니다).

---

## 부록 B: 배포 계층 구조

배포는 4개 계층으로 분리됩니다. 각 계층은 독립적으로 변경·배포 가능합니다.

```
┌──────────────────────────────────────────────────────────────┐
│ 계층 1. 소스코드                                               │
│  - gateway-proxy/, admin-api/, admin-ui/, 워커 등             │
│  - git repository                                            │
└──────────────────────────────────────────────────────────────┘
                              ↓ docker build
┌──────────────────────────────────────────────────────────────┐
│ 계층 2. 컨테이너 이미지                                        │
│  - ECR (7개 이미지 × 버전 tag)                                │
└──────────────────────────────────────────────────────────────┘
                              ↓ Helm pull
┌──────────────────────────────────────────────────────────────┐
│ 계층 3. 애플리케이션 배포 (Kubernetes)                         │
│  - Helm chart: deployment/charts/llm-gateway/              │
└──────────────────────────────────────────────────────────────┘
                              ↓ runs on
┌──────────────────────────────────────────────────────────────┐
│ 계층 4. 인프라 (AWS)                                          │
│  - Terraform modules (VPC/EKS/Aurora/ElastiCache/...)        │
└──────────────────────────────────────────────────────────────┘
```

- 코드만 변경 → 계층 2 재빌드 → 계층 3에서 이미지 태그 업데이트
- 인프라 변경 → 계층 4만 (Terraform), 애플리케이션 무관

---

## 부록 C: Pod 레벨 데이터 흐름

```
┌─────────────────────────────────────────────────────────┐
│  gateway-proxy Pod                                       │
│  ┌──────────────────┐    ┌─────────────────────────┐    │
│  │ FastAPI          │◄──►│ OTel sidecar (opt)      │    │
│  │ (uvicorn worker) │    │ :4317 → Collector       │    │
│  └──────────────────┘    └─────────────────────────┘    │
│         │                                                │
│         ├─ Aurora (asyncpg connection pool)              │
│         ├─ Redis (redis.asyncio pool)                    │
│         └─ Bedrock (boto3 + IRSA)                        │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  cost-recorder-worker Pod                               │
│  ┌──────────────────┐                                    │
│  │ worker loop      │                                    │
│  │ ◆ XREADGROUP     │── Redis Stream (cost:stream)       │
│  │ ◆ 배치 INSERT    │── Aurora (usage_logs)              │
│  │ ◆ XACK           │── Redis Stream                     │
│  └──────────────────┘                                    │
│  consumer = pod.metadata.name (재시작 시 pending 승계)   │
└─────────────────────────────────────────────────────────┘
```

---

## 부록 D: 보안 경계

| 경계 | 방어 메커니즘 |
|------|-------------|
| 인터넷 → 클러스터 | ALB TLS 1.3 + ACM 인증서 + WAF (옵션) |
| 클러스터 외부 → Pod | NetworkPolicy (Ingress Controller 네임스페이스에서만 허용) |
| Pod ↔ Pod | NetworkPolicy (admin-ui → admin-api만 허용, 워커는 egress-only) |
| Pod → DB/Redis | VPC Security Group (EKS private subnet CIDR만) + TLS in-transit |
| Pod → AWS 서비스 | IRSA (Role별 최소 권한 — Bedrock/STS 분리) |
| Pod 내부 | non-root user, readOnlyRootFilesystem, drop ALL capabilities, seccomp RuntimeDefault |
| Secret 저장 | Secrets Manager(KMS 암호화) → ESO → K8s Secret(etcd KMS) |
| At-rest | Aurora/ElastiCache 스토리지 암호화 |
| Audit | Aurora `audit.audit_logs` 테이블 + EKS audit log |

---

## 참고: On-Premises 배포

이 문서는 AWS EKS Fargate 환경을 기준으로 작성되었습니다. On-Prem Kubernetes 환경을 위한 Helm values(`values-onprem-*.yaml`)와 설치 스크립트(`install-onprem.sh`)가 별도로 제공되지만, 이는 배포 참조용 가이드이며 실제 고객 인프라에서의 동작은 환경에 따라 다를 수 있습니다. On-Prem 배포 시에는 해당 스크립트를 기반으로 자체 환경에 맞는 검증을 직접 수행해야 합니다.

---

## 부록 E: 현재 deliverable 의 배포 환경 (참고)

> 이 절은 본 deliverable 의 **현재 배포된 두 환경 스냅샷**입니다. 고객 인프라
> 적용 시 동일 절차를 따르되 계정/도메인/IAM 만 교체하세요.

### E.1 공통

| 항목 | 값 |
|---|---|
| AWS 계정 | `123456789012` |
| 리전 | `ap-northeast-2` (서울) |
| Terraform 워크스페이스 | `deployment/terraform/environments/llm-gateway-{dev,prod}/` |
| Helm chart | `deployment/charts/llm-gateway/` |
| Helm values | `values-eks-fargate-{dev,prod}.yaml` |
| ECR registry | `123456789012.dkr.ecr.ap-northeast-2.amazonaws.com` |
| Cognito 그룹 컨벤션 | `ClaudeAdmin`, `Claude_<team>`, `Claude_<dept>_<team>` |

### E.2 prod (production traffic 받는 환경)

| 항목 | 값 |
|---|---|
| VPC CIDR | `10.40.0.0/16` (multi-AZ: ap-northeast-2a/c) |
| EKS cluster | `llm-gateway-prod` (1.30) |
| Aurora | `llm-gateway-prod`, db.r7g.large, multi-AZ, RDS Proxy on |
| ElastiCache | `llm-gateway-prod`, cache.r7g.large, **cluster mode on** |
| Cognito User Pool | `ap-northeast-2_XXXXXXXXX` |
| OIDC Issuer | `https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_XXXXXXXXX` |
| OIDC public client | `<COGNITO_APP_CLIENT_ID>` (PKCE, no secret) |
| Hosted UI | `llm-gateway-prod-vanilla-auth-123456789012.auth.ap-northeast-2.amazoncognito.com` |
| Gateway Proxy ALB | `http://<ALB_DNS>` |
| Admin API ALB | `http://<ALB_DNS>` |
| Admin UI ALB | `http://<ALB_DNS>` |
| Replicas (HPA) | gateway-proxy 3-30 / admin-api 3-10 / admin-ui 2-6 / cost-recorder 3-6 / notification 2-6 |
| Bootstrap admin | `admin@example.com` |

### E.3 dev (검증/개발)

| 항목 | 값 |
|---|---|
| VPC CIDR | `10.30.0.0/16` (single AZ) |
| EKS cluster | `llm-gateway-dev` (1.30) |
| Aurora | `llm-gateway-dev`, t-class, single-AZ, RDS Proxy on |
| ElastiCache | `llm-gateway-dev`, single node (cluster mode off) |
| Cognito User Pool | `ap-northeast-2_XXXXXXXXX` (2026-05-18 us-east-1 → ap-northeast-2 마이그레이션 완료) |
| OIDC Issuer | `https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_XXXXXXXXX` |
| OIDC public client | `<COGNITO_APP_CLIENT_ID>` (PKCE, no secret) |
| Gateway Proxy ALB | `http://<ALB_DNS>` |
| Admin API ALB | `http://<ALB_DNS>` |
| Replicas | gateway-proxy 1-3 / admin-api 1 / admin-ui 1 / cost-recorder 1 / notification 1 |

### E.4 현재 알려진 임시 설정 (고객 인프라 적용 시 반드시 교체)

| 항목 | 현재 | 고객 환경에서 |
|---|---|---|
| Ingress | HTTP(80), 도메인 없음 (방식 A) | HTTPS(443) + ACM 인증서 + Route53/외부 DNS (방식 B) |
| `DEV_LOGIN_ENABLED` (admin-api/admin-ui) | `"true"` (SSO 교체 전 우회) | **`"false"`** — 고객 SSO 연동 후 |
| `notificationWorkerUser` | `gateway` (단일 user 공유) | 별도 role + 최소 권한 GRANT 권장 |
| `observability.otel.mode` | `in-cluster` + dummy endpoint | OTel Collector 배포 후 endpoint 교체 (Pod 재시작 불필요 — env only) |
| Bedrock 모델 | `claude-sonnet-4`, `claude-haiku-4`, `claude-opus-4` | 고객 정책에 맞춰 `bedrockAllowedModels` / `bedrock_allowed_model_arns` 갱신 |

---

## 부록 F: 자사 계정 부트스트랩 체크리스트

다른 계정에서 본 deliverable 을 처음 적용하려면 아래 순서로 진행. 우리
환경의 하드코딩된 값(계정 ID, Cognito pool, ALB DNS 등)을 본인 환경 값으로
교체해야 합니다.

### F.1 사전 준비 (1회)

```bash
# 1) 본 repo 클론
git clone <repo-url> && cd <repo>

# 2) AWS CLI 인증 + 본인 계정 확인
aws sts get-caller-identity
# Account: <YOUR_ACCOUNT_ID>

# 3) tfstate bucket + DynamoDB lock table 부트스트랩
./deployment/scripts/bootstrap-tfstate.sh
# → S3 bucket "llm-gateway-vanilla-tfstate-<YOUR_ACCOUNT_ID>" 와
#   DynamoDB table "llm-gateway-vanilla-tflock" 생성
```

### F.2 Terraform 변수 채우기

`deployment/terraform/environments/llm-gateway-{dev,prod}/` 양쪽에서:

> 저장소에는 각 환경별 `terraform.tfvars.example`만 커밋되어 있고, 실제
> `terraform.tfvars`는 gitignored(추적 제외)입니다. 따라서 dev·prod **양쪽 모두**
> example을 복사해 새로 작성해야 합니다.

```bash
cp terraform.tfvars.example terraform.tfvars
# terraform.tfvars 안에서 교체:
#   - cognito_domain_suffix : "vanilla-auth-CHANGE_ACCOUNT_ID" → 본인 계정 번호
#   - eks_access_entries.*.principal_arn : "ACCOUNT_ID:role/YOUR_ROLE" → 본인 IAM
#   - cognito_groups : 본인 회사 그룹 컨벤션 (Claude_<team> 패턴)
#   - bedrock_allowed_model_arns : 자기 회사 정책 모델 ARN
#   - tags : CostCenter / Owner 본인 조직
```

### F.3 Terraform Apply

```bash
cd deployment/terraform/environments/llm-gateway-dev    # 또는 -prod

# partial backend config 으로 init
terraform init \
  -backend-config="bucket=llm-gateway-vanilla-tfstate-<YOUR_ACCOUNT_ID>" \
  -backend-config="dynamodb_table=llm-gateway-vanilla-tflock"

terraform plan
terraform apply
# → VPC, EKS, Aurora, ElastiCache, Cognito, IRSA, ALB Controller, ESO 모두 생성

# outputs 저장 (helm install 에 필요)
terraform output -json > /tmp/tf-outputs.json
```

### F.4 Helm values 갱신 (`# CHANGE_ME` 마커)

`deployment/charts/llm-gateway/values-eks-fargate-{dev,prod}.yaml` 안에서
`# CHANGE_ME` 주석 옆 값을 위 terraform output 으로 교체:

| key | terraform output |
|---|---|
| `global.imageRegistry` | `ecr_registry` |
| `database.external.host` | `rds_proxy_endpoint` |
| `redis.external.host` | `elasticache_primary_endpoint` (dev) / `elasticache_configuration_endpoint` (prod) |
| `gatewayProxy.serviceAccount.annotations.eks.amazonaws.com/role-arn` | `gateway_proxy_role_arn` |
| `adminApi.serviceAccount.annotations.eks.amazonaws.com/role-arn` | `admin_api_role_arn` |
| `adminApi.env.COGNITO_USER_POOL_ID` | `cognito_user_pool_id` |
| `adminApi.oidc.issuerUrl` | `cognito_issuer_url` |
| `adminApi.adminBootstrap.emails[0]` | 본인 운영자 이메일 |

### F.5 Helm Install

```bash
ENV=dev    # 또는 prod
./deployment/scripts/install-eks.sh $ENV

# 또는 수동:
helm install llm-gateway ./deployment/charts/llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml \
  -n llm-gateway --create-namespace --timeout 15m
```

### F.6 Cognito 사용자 등록

```bash
POOL_ID=$(terraform -chdir=deployment/terraform/environments/llm-gateway-$ENV \
  output -raw cognito_user_pool_id)

aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" \
  --username admin@example.com \
  --user-attributes Name=email,Value=admin@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" \
  --username admin@example.com --password '<temp-pw>' --permanent
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username admin@example.com --group-name ClaudeAdmin
```

### F.7 Smoke Test

```bash
./deployment/scripts/smoke-test.sh $ENV
# → ALB health, VK 발급, Bedrock InvokeModel 까지 검증
```

### F.8 자주 마주칠 함정

| 증상 | 원인 / 해결 |
|---|---|
| `terraform init` 실패 — bucket 권한 없음 | bootstrap-tfstate.sh 안 돌렸거나 S3 bucket 이름이 backend-config 와 다름 |
| `helm install` 후 `ImagePullBackOff` | `imageRegistry` 가 본인 ECR 가 아님. 또는 image 가 push 안 됨 |
| Pod 가 `redirect_uri_mismatch` | Cognito callback URL 에 `http://localhost:8090/callback` 미등록 (terraform `cognito_callback_urls`) |
| `unknown kid` (admin-api 401) | `oidc.issuerUrl` 또는 `COGNITO_USER_POOL_ID` 가 본인 pool 가 아님 |
| `429 budget_exceeded` | 첫 사용자라 팀 budget 미설정. admin-ui 에서 팀 budget 활성화 |
