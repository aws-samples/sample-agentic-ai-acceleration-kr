# Secret 주입 계약

이 문서는 LLM Gateway가 **무엇이 Secret인지**, **어디서 어떻게 읽는지**, **로테이션은 누가 책임지는지**를 정의합니다. 원본 암호화 키 배포 계약(생성/교체 방식)은 [`../../requirements-document/deployment-secrets.md`](../../requirements-document/deployment-secrets.md)를 참조하세요.

## 1. Secret 인벤토리

| 이름 | 용도 | 소비 서비스 | 필수/선택 | 생성 방법 |
|------|-----|-----------|---------|---------|
| `virtual_key_encryption_key` | Virtual Key AES-256-GCM DEK (64-char hex) | admin-api, gateway-proxy | **필수** | `openssl rand -hex 32` |
| `nextauth_secret` | NextAuth.js 세션 서명 | admin-ui | **필수** | `openssl rand -hex 32` |
| `jwt_jwks_cache_key` | JWKS 캐시 HMAC (위변조 방지) | admin-api, gateway-proxy | **필수** | `openssl rand -hex 32` |
| DB `password` (gateway 유저) | Aurora/PostgreSQL 연결 비번 | admin-api, gateway-proxy, cost-recorder-worker, scheduler, migration | **필수** | Aurora `manage_master_user_password=true`가 Secrets Manager에 자동 저장 |
| DB `notification_worker_password` | notification_worker_user 비번 (권한 분리) | notification-worker | **on-prem 선택** (EKS는 단일 gateway 유저 사용) | 운영자가 수동 생성 (마이그레이션 후) |
| Redis `password` (AUTH 토큰) | ElastiCache / 사내 Redis 인증 | 전 서비스 (worker 포함) | **선택** | ElastiCache는 Terraform이 `random_password`로 자동 생성 |
| SMTP `username` / `password` | 사내 SMTP 인증 (on-prem) | notification-worker | **on-prem 선택** | 고객 메일팀이 제공 |
| TLS `tls.crt` / `tls.key` | Ingress 인증서 (on-prem) | Ingress Controller | **on-prem 필수 if TLS on** | 고객 발급 or Let's Encrypt/cert-manager |
| Image pull `docker-password` | 사내 private registry 인증 | Pod imagePullSecrets | **on-prem 선택** | 고객 registry 운영팀이 제공 |

## 2. 주입 모드 (두 가지)

### Mode A: ExternalSecrets Operator (AWS EKS 권장)

```
  AWS Secrets Manager
  ┌─────────────────────────────────┐
  │ /llm-gateway/prod/app        │ ← virtual_key_encryption_key 등
  │ /llm-gateway/prod/db         │ ← password, notification_worker_password
  │ /llm-gateway/prod/redis      │ ← password
  └─────────────┬───────────────────┘
                │ IRSA 권한으로 읽기
                ▼
  ExternalSecrets Operator (Pod)
                │
                │ 자동 동기화 (기본 1시간마다)
                ▼
  K8s Secret (namespace: llm-gateway)
                │
                │ envFrom / valueFrom.secretKeyRef
                ▼
  애플리케이션 Pod (gateway-proxy 등)
```

**활성화**: `values-eks-fargate-*.yaml` 에서 `externalSecrets.enabled: true`.

**장점**:
- 비번 교체 시 Secrets Manager만 수정 — K8s는 자동 갱신
- git에 비번이 절대 들어가지 않음
- 감사 로그 (CloudTrail)

**단점**:
- ESO 추가 설치·관리 필요 (Terraform이 처리)
- 외부 의존성 (Secrets Manager 장애 시 ESO 복구 불가, 하지만 기존 K8s Secret은 유지됨)

### Mode B: K8s Secret 직접 (on-prem 권장)

```
  운영자가 직접 생성
  $ kubectl create secret generic llm-gateway-app \
      --from-literal=virtual_key_encryption_key=$(openssl rand -hex 32) \
      --from-literal=nextauth_secret=$(openssl rand -hex 32) \
      ...
                │
                ▼
  K8s Secret (namespace: llm-gateway)
                │
                ▼
  애플리케이션 Pod
```

**활성화**: `values-onprem-*.yaml` 에서 `externalSecrets.enabled: false` (기본값).

**장점**:
- 외부 의존성 없음 — 사내 K8s만 있으면 동작
- 고객 내부 정책에 맞춰 Vault/sealed-secrets 등으로 자유롭게 확장

**단점**:
- 비번 교체 시 수동 `kubectl` 재실행 + Pod 재시작 (`checksum/secret` annotation으로 자동 재시작 가능)
- 비번을 git에 커밋하지 않도록 운영 절차 필요

## 3. Secret ↔ values.yaml 매핑

Helm chart `values.yaml` 에서 Secret 참조는 이렇게 생겼습니다:

```yaml
database:
  external:
    passwordSecretName: "llm-gateway-db"       # K8s Secret 이름
    passwordSecretKey: "password"                  # 그 Secret 안의 key
    notificationWorkerPasswordSecretKey: "notification_worker_password"

redis:
  external:
    passwordSecretName: "llm-gateway-redis"    # 비어있으면 AUTH 미사용
    passwordSecretKey: "password"

auth:
  virtualKey:
    encryptionKeySecretName: "llm-gateway-app"
    encryptionKeySecretKey: "virtual_key_encryption_key"
```

즉 chart는 **"이 이름의 Secret을 namespace에서 찾아 그 key를 읽겠다"**는 계약만 정의하고, **실제 Secret 객체 생성은 ESO 또는 운영자 책임**입니다.

## 4. 사전 생성 체크리스트

### AWS EKS (ESO 사용)

- [ ] Terraform apply 완료 → Aurora/ElastiCache가 Secrets Manager에 자동 저장된 비번 확인
- [ ] `/llm-gateway/{env}/app` Secret 생성:
  ```bash
  aws secretsmanager create-secret --name /llm-gateway/prod/app \
    --secret-string "{
      \"virtual_key_encryption_key\": \"$(openssl rand -hex 32)\",
      \"nextauth_secret\": \"$(openssl rand -hex 32)\",
      \"jwt_jwks_cache_key\": \"$(openssl rand -hex 32)\"
    }"
  ```
- [ ] `/llm-gateway/{env}/db` Secret 생성 (Aurora 마스터 비번을 gateway 유저용으로 사용 or 별도 유저 생성 후 그 비번):
  ```bash
  # 1) Aurora 마스터 유저로 접속해 gateway / notification_worker_user 생성 (migration이 처리)
  # 2) 각 유저 비번을 /llm-gateway/prod/db 에 저장:
  aws secretsmanager create-secret --name /llm-gateway/prod/db \
    --secret-string "{
      \"password\": \"<gateway 유저 비번>\",
      \"notification_worker_password\": \"<nw 유저 비번>\"
    }"
  ```
- [ ] `/llm-gateway/{env}/redis` Secret 생성 (ElastiCache AUTH token 복제):
  ```bash
  REDIS_AUTH=$(aws secretsmanager get-secret-value \
    --secret-id /llm-gateway/prod/redis/auth_token \
    --query SecretString --output text)
  aws secretsmanager create-secret --name /llm-gateway/prod/redis \
    --secret-string "{\"password\":\"${REDIS_AUTH}\"}"
  ```
- [ ] `helm install` 실행 → ExternalSecret 자동 동기화 확인:
  ```bash
  kubectl get externalsecret -n llm-gateway
  kubectl get secret -n llm-gateway
  ```

### On-prem K8s

- [ ] 사내 DB 팀으로부터 `gateway` / `notification_worker_user` 비번 받기
- [ ] 사내 Redis 팀으로부터 AUTH 토큰 받기 (사용하면)
- [ ] 사내 SMTP 팀으로부터 credentials 받기
- [ ] TLS 인증서 발급 (내부 CA or Let's Encrypt)
- [ ] Private registry 인증 정보 받기
- [ ] Secret 일괄 생성:
  ```bash
  kubectl create namespace llm-gateway

  kubectl create secret generic llm-gateway-db -n llm-gateway \
    --from-literal=password='<gateway 비번>' \
    --from-literal=notification_worker_password='<nw 비번>'

  kubectl create secret generic llm-gateway-app -n llm-gateway \
    --from-literal=virtual_key_encryption_key="$(openssl rand -hex 32)" \
    --from-literal=nextauth_secret="$(openssl rand -hex 32)" \
    --from-literal=jwt_jwks_cache_key="$(openssl rand -hex 32)"

  kubectl create secret generic llm-gateway-redis -n llm-gateway \
    --from-literal=password='<redis AUTH>'

  kubectl create secret generic llm-gateway-smtp -n llm-gateway \
    --from-literal=username='<SMTP user>' \
    --from-literal=password='<SMTP pass>'

  kubectl create secret tls llm-gateway-tls-gateway -n llm-gateway \
    --cert=./gateway.crt --key=./gateway.key
  # admin-ui / admin-api 각각 반복

  kubectl create secret docker-registry harbor-registry -n llm-gateway \
    --docker-server=harbor.customer.internal \
    --docker-username=<user> \
    --docker-password=<pass>
  ```

## 5. 로테이션 정책

| Secret | 로테이션 권장 주기 | 방법 |
|-------|-----------------|-----|
| `virtual_key_encryption_key` | 1년 1회 | `extraKeys={v0: ...}` 다버전 복호화 지원 — 이전 키를 v0에 남기고 v1으로 교체. 자세히: [deployment-secrets.md](../../requirements-document/deployment-secrets.md) |
| `nextauth_secret` | 6개월 1회 | 교체 시 모든 관리자 재로그인 필요 |
| `jwt_jwks_cache_key` | 1년 1회 | 교체 시 서비스 재시작으로 캐시 초기화 |
| DB `password` | 90일 1회 | Aurora `manage_master_user_password` 자동 로테이션 활용 or 수동 |
| Redis `password` | 90일 1회 | ElastiCache `auth_token_update_strategy: ROTATE` — 2개 토큰 공존 기간 → 무중단 교체 |
| TLS 인증서 | 만료 30일 전 | cert-manager 자동 갱신 권장 |
| SMTP 비번 | 고객 정책 따름 | 수동 |

**로테이션 후 Pod 재시작**: K8s Secret 값이 바뀌어도 Pod는 **자동 재시작 안 함**. 우리 chart는 Deployment 에 `checksum/secret` annotation을 주입해 helm upgrade 시 자동 재시작되도록 처리함. 다만 ESO 동기화로 Secret이 바뀌면 Pod 재시작은 별도 트리거 필요 — `stakater/reloader` 같은 애드온 or 수동 `kubectl rollout restart`.

## 6. Secret 유출 사고 대응

1. **즉시 무효화**:
   - DB 비번 → `ALTER USER ... WITH PASSWORD ...` + Secrets Manager 업데이트
   - Redis AUTH → ElastiCache Console 또는 Terraform 재적용
   - VK encryption key → `extraKeys.v0`에 구 키 추가, 새 키로 재암호화 job 실행
2. **감사 로그 검토**: CloudTrail (`secretsmanager:GetSecretValue`) / EKS audit log / Aurora `audit.audit_logs`
3. **영향 범위 확정**: 해당 Secret을 참조하는 Pod/User/VK 리스트 확보
4. **사후 조치**: 접근 권한 최소화 재검토, IRSA Role 점검, NetworkPolicy 재확인

## 7. 절대 하면 안 되는 것

- ❌ values.yaml 에 Secret 평문 직접 쓰기
- ❌ Secret 값을 ConfigMap에 넣기 (ConfigMap은 etcd에 평문 저장될 수 있음)
- ❌ Secret 값을 Pod env 로그에 출력 (우리 코드는 `pydantic SecretStr` 사용하므로 `repr` 자동 마스킹됨)
- ❌ git commit 할 Helm values 파일에 실제 Secret ARN 외의 값 남기기 (ARN 자체는 ID일 뿐 비밀 아님)
- ❌ Slack / issue tracker에 Secret 내용 붙여넣기

---

## 참고

- [deployment-secrets.md](../../requirements-document/deployment-secrets.md) — 원본 암호화 키 배포 계약 (AES-256-GCM v1 prefix, legacy 호환, 로테이션 시나리오)
- [architecture.md](./architecture.md) — 전체 배포 아키텍처
