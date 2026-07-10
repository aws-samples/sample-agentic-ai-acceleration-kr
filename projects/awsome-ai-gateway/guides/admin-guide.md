# LLM Gateway — 어드민 가이드 (Admin Guide)

이 문서는 LLM Gateway가 배포된 후, **일상 운영**을 담당하는 관리자(Admin)를 위한 가이드입니다.
사용자 관리, 팀 정책 설정, 예산 관리, 모니터링, 사고 대응 절차를 다룹니다.

---

## 목차

1. [역할과 책임](#1-역할과-책임)
2. [Admin UI 접속](#2-admin-ui-접속)
3. [사용자 & 팀 관리](#3-사용자--팀-관리)
4. [모델 관리](#4-모델-관리)
5. [예산 관리](#5-예산-관리)
6. [Rate Limit 관리](#6-rate-limit-관리)
7. [API Key (Virtual Key) 관리](#7-api-key-virtual-key-관리)
8. [대시보드 & 모니터링](#8-대시보드--모니터링)
9. [사고 대응](#9-사고-대응)
10. [정기 점검 체크리스트](#10-정기-점검-체크리스트)
11. [부록 A: Admin API 엔드포인트 레퍼런스](#부록-a-admin-api-엔드포인트-레퍼런스)
12. [부록 B: 자동 프로비저닝 디버깅](#부록-b-자동-프로비저닝-디버깅)
13. [부록 C: STS(AWS SSO) 인증 경로](#부록-c-stsaws-sso-인증-경로)

---

## 1. 역할과 책임

### 1.1 누가 어디서 관리하나

| 데이터 | 관리 위치 | 타이밍 |
|--------|-----------|--------|
| 사용자 신원 (이메일) | **IDP (Cognito 등)** | 입사/퇴사 시 |
| 사용자 → 팀 매핑 | **IDP 그룹** | 팀 이동 시 |
| 팀 자체 (이름, 생성) | **자동** — 첫 사용자 로그인 시 DB 생성 | 자동 |
| 팀별 예산/모델/RL 정책 | **Admin UI** | 팀 추가/변경 시 |
| 사용자 차단 | Admin UI 또는 IDP | 사고 시 |

### 1.2 Admin이 직접 하는 일

- IDP에 사용자 추가 + 그룹 배정
- Admin UI에서 팀 정책 활성화 (예산, Rate Limit, 모델 허용)
- 모니터링 및 이상 감지 대응
- 신규 모델 등록 및 가격 설정

### 1.3 팀 리더(Team Leader) 권한 범위

팀 리더(`TEAM_LEADER` 역할)는 **자기 팀 범위**에서 다음을 수행할 수 있습니다:

| 기능 | 팀 리더 가능 | Admin 전용 |
|------|:---:|:---:|
| 팀원 예산 설정 (PUT /budgets/user/{id}) | ✅ | ✅ |
| 팀 예산 현황 조회 (GET /budgets/summary) | ✅ (본인 팀만) | ✅ (전체) |
| 팀 예산 할당 (PUT /budgets/team/{id}/allocate) | ✅ | ✅ |
| 다운그레이드 정책 조회 (GET /budgets/{scope}/{id}/downgrade) | ✅ (본인 팀만) | ✅ |
| Analytics 조회/내보내기 | ✅ (본인 팀만) | ✅ (전체) |
| 팀 예산 상한 변경 (PUT /budgets/team/{id}) | ❌ | ✅ |
| Rate Limit 설정 | ❌ | ✅ |
| 모델 관리 | ❌ | ✅ |
| 사용자 생성/차단/팀 이동 | ❌ | ✅ |

### 1.4 Admin이 하지 않는 일

- DB에 직접 사용자/팀 INSERT (자동 프로비저닝)
- VK 수동 발급 (CLI가 자동 처리)
- Bedrock/Redis 직접 조작

---

## 2. Admin UI 접속

### 2.1 URL

배포자가 제공한 Admin UI URL로 접속합니다:
```
https://admin-ui.llm-gateway.example.com
```

### 2.2 인증

OIDC 로그인 (Cognito Hosted UI):
1. Admin UI 접속 → "Sign In" 클릭
2. IDP 로그인 페이지로 리디렉션
3. 이메일 + 패스워드 입력
4. Admin UI 대시보드로 복귀

**Admin 권한 조건** (아래 중 하나):
- `ADMIN_EMAILS` 환경변수에 본인 이메일 포함
- `ADMIN_GROUPS` 에 지정된 IDP 그룹 (예: `ClaudeAdmin`) 소속

### 2.3 페이지 구성 (사이드바 메뉴)

| 메뉴 | 기능 | 접근 권한 |
|------|------|-----------|
| 대시보드 | KPI 요약, 모델별 비용 점유율 | 전체 |
| 사용자/팀 | 조직 트리, Cognito 동기화, 팀 리더 지정 | Admin |
| 모델 관리 | 모델 CRUD, 팀별 허용 모델 | Admin |
| 예산 관리 | 예산 설정, 할당, 자동 다운그레이드 | Admin, Team Leader |
| Rate Limits | RPM/TPM/CPM/CPH 설정 (계층형) | Admin |
| API Keys | Virtual Key 목록, 폐기 | Admin |
| 모니터링 | 실시간 에러/지연/상위 사용자 | Admin |

---

## 3. 사용자 & 팀 관리

### 3.1 신규 사용자 등록 흐름

```
[Admin] IDP에 사용자 생성 + 그룹 배정
          ↓
[Admin] 사용자에게 안내 (임시패스워드 + Hosted UI URL + getting-started 문서)
          ↓
[사용자] gateway-cli login → 첫 API 호출
          ↓
[시스템] 자동: User DB 생성 + 팀 매핑 → 429 (budget 미설정)
          ↓
[Admin] Admin UI → Teams → 해당 팀 budget 활성화
          ↓
[사용자] 정상 사용 시작
```

### 3.2 IDP 작업 (Cognito 기준)

**사용자 생성**:
```bash
USER_POOL_ID="ap-northeast-2_xxxxxxx"
NEW_EMAIL="newuser@company.com"
TEAM_GROUP="Claude_Backend"

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$NEW_EMAIL" \
  --user-attributes Name=email,Value="$NEW_EMAIL" Name=email_verified,Value=true \
  --temporary-password 'Temp_Pass-1234!'

aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username "$NEW_EMAIL" \
  --group-name "$TEAM_GROUP"
```

**사용자에게 전달할 정보**:
- 이메일
- 임시 패스워드
- Hosted UI URL: `https://<domain>.auth.ap-northeast-2.amazoncognito.com/login?client_id=...`
- 사용자 시작 가이드 ([user-guide.md](user-guide.md))

### 3.3 Cognito 동기화

Admin UI → Users 페이지 → **"Cognito 동기화"** 버튼 클릭

**전제조건**: 환경변수 `COGNITO_USER_POOL_ID`가 설정되어 있어야 동작합니다. 미설정 시 에러 반환됩니다. (Helm values `adminApi.cognito.userPoolId` 또는 환경변수 `COGNITO_USER_POOL_ID`)

동기화 결과:
- `groups_synced`: 동기화된 그룹 수
- `users_created`: 새로 생성된 사용자
- `users_updated`: 팀 변경 등 업데이트된 사용자
- `users_deactivated`: IDP에서 삭제/비활성된 사용자
- `teams_deleted`: 멤버 0인 팀 삭제

### 3.4 팀 이동

IDP에서 그룹 변경하면 시스템이 자동 처리:
```bash
# IDP 작업
aws cognito-idp admin-remove-user-from-group \
  --user-pool-id "$USER_POOL_ID" --username "$EMAIL" --group-name "Claude_OldTeam"

aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" --username "$EMAIL" --group-name "Claude_NewTeam"
```

→ 사용자가 다음 토큰 갱신 (~1시간) 또는 재로그인 시 자동으로:
- DB 팀 매핑 변경
- 기존 VK 무효화
- 기존 팀의 Budget/RL에서 제외

### 3.5 팀 그룹 명명 규칙

IDP 그룹명은 `OIDC_GROUP_PREFIX` (기본 `Claude_`) 접두사를 가져야 하며, **underscore 개수**로 부서/팀이 결정됩니다:

| 그룹명 | underscore 수 | 결과 |
|--------|---------------|------|
| `Claude_Backend` | 1개 | Default Department 하위 → "Backend" 팀 |
| `Claude_AI-Center_Backend` | 2개 | "AI-Center" 부서 자동 생성 → "Backend" 팀 |
| `ClaudeAdmin` | prefix 불일치 | 팀 매핑 제외 (Admin 전용 그룹) |

**규칙**:
- 팀명/부서명에 underscore(`_`) 사용 금지 — 하이픈(`-`)으로 대체
- underscore 3개 이상 또는 빈 세그먼트 → reject (403)
- `OIDC_REJECT_UNMATCHED_GROUPS: true` (기본값) 시, 매칭 그룹이 없으면 403 반환
- prefix는 Helm values `adminApi.oidc.groupPrefix`로 커스터마이징 가능

### 3.6 조직 구조

Admin UI → Users → **Organization Tree**:
```
Organization
├── Department A
│   ├── Team Backend (5 members)
│   └── Team Frontend (3 members)
└── Department B
    └── Team Data (4 members)
```

---

## 4. 모델 관리

### 4.1 모델 목록 확인

Admin UI → Models 페이지

각 모델 정보:
- **Alias**: 사용자가 요청 시 사용하는 이름 (예: `claude-sonnet`)
- **Provider**: `BEDROCK` / `OPENMODEL` / `BEDROCK_MANTLE`(Cowork) / `BEDROCK_MANTLE_OPENAI`(Codex) 중 하나
- **Provider Model ID**: 실제 백엔드 모델 ID
- **Status**: `ACTIVE` / `INACTIVE`
- **Pricing**: Input/Output/Cache 토큰당 가격

### 4.2 새 모델 등록

Admin UI → Models → **"Create Model"** 버튼

필수 입력:
| 필드 | 설명 | 예시 |
|------|------|------|
| alias | 사용자 요청용 이름 | `claude-opus` |
| provider | 백엔드 유형 | `BEDROCK` |
| provider_model_id | 실제 모델 ID | `anthropic.claude-opus-4-20250514-v1:0` |
| api_format | API 포맷 | `BEDROCK_NATIVE` 또는 `OPENAI_COMPATIBLE` |

가격 설정:
| 필드 | 단위 | 예시 (Claude Sonnet) |
|------|------|------|
| input_price_per_1k_tokens | USD/1K 토큰 | 0.003 |
| output_price_per_1k_tokens | USD/1K 토큰 | 0.015 |
| cache_creation_5m_price_per_1k | USD/1K 토큰 | 0.00375 |
| cache_read_price_per_1k | USD/1K 토큰 | 0.0003 |

### 4.3 모델 활성화/비활성화

Admin UI → Models → 해당 모델 → **Status 토글**

- `INACTIVE` 모델은 사용자 요청 시 404 반환
- 기존 진행 중 요청에는 영향 없음 (완료까지 허용)

### 4.4 팀별 모델 허용 (Whitelist)

Admin UI → Models → **"Team Model Permission"** 패널

설정 방법:
1. 팀 선택
2. 허용할 모델 체크
3. Save

효과:
- Whitelist가 설정된 팀은 **지정된 모델만** 사용 가능
- Whitelist가 비어있으면 (Clear) **모든 ACTIVE 모델** 사용 가능
- 즉시 적용 (Redis 캐시 자동 무효화)

---

## 5. 예산 관리

### 5.1 예산 정책 개요

3가지 정책:
| 정책 | 동작 |
|------|------|
| `HARD_BLOCK` | 예산 100% 도달 시 즉시 차단 (429) |
| `SOFT_WARNING` | 예산 110%까지 허용 후 차단 + 알림 |
| `THROTTLE` | 임계값 도달 시 RPM 자동 감소 (차단 없이 처리량 제한, 기본 50%) |

### 5.2 팀 예산 설정

Admin UI → Budgets → 팀 선택 → **"Edit"**

| 필드 | 설명 | 권장 |
|------|------|------|
| max_budget_usd | 월간 최대 예산 | 처음엔 $100~500으로 시작 |
| policy | 초과 시 동작 | HARD_BLOCK 권장 |
| alert_thresholds | 알림 임계값 (%) | [80, 90, 100] |

**중요**: 새 팀은 budget 미설정 시 **deny by default** (모든 요청 429).

### 5.3 사용자별 예산 설정

Admin UI → Budgets → 사용자 행 → **"Edit"**

- 사용자별 예산은 팀 예산과 **독립적**
- 사용자 예산 삭제 시 팀 예산으로 fallback
- 팀 리더도 소속 팀원의 예산 설정 가능

### 5.4 예산 할당 (Budget Allocation)

팀 총 예산을 팀원에게 분배:

Admin UI → Budgets → 팀 → **"Allocate"** 탭

```
Team Budget: $5,000/월
├── User A: $1,500 allocated
├── User B: $1,500 allocated
├── User C: $1,000 allocated
└── Unallocated: $1,000
```

### 5.5 자동 모델 다운그레이드

**팀 예산** 압박 시 자동으로 저가 모델로 전환:

Admin UI → Budgets → **팀** → **"Auto Downgrade"** 섹션

설정:
```
[ON] 활성화
Rule 1: 예산 80% 도달 시 claude-opus → claude-sonnet
Rule 2: 예산 95% 도달 시 claude-sonnet → claude-haiku
```

- **TEAM scope 전용**: 자동 다운그레이드는 `BudgetScope.TEAM` 정책만 평가합니다.
  USER scope 정책은 DB 에 저장 가능하지만 gateway-proxy 의 다운그레이드
  미들웨어는 사용자가 속한 팀의 정책만 조회 (`downgrade_loader.py`). 개인
  사용자에게 다운그레이드를 적용하려면 해당 사용자가 속한 팀 정책을 사용하세요.
- 1-hop 전환만 허용 (체이닝 불가)
- 사용자에게는 응답 헤더 `X-Downgraded-From`으로 다운그레이드 사실 전달
- 해당 월 종료 시 자동 복원

### 5.6 예산 현황 확인

Admin UI → Budgets → **Summary 테이블**

| 팀/사용자 | Limit | Used | Remaining | Usage % | Alert Level |
|-----------|-------|------|-----------|---------|-------------|
| Backend | $5,000 | $3,200 | $1,800 | 64% | — |
| Frontend | $2,000 | $1,850 | $150 | 92.5% | Warning |

---

## 6. Rate Limit 관리

### 6.1 Rate Limit 계층 구조

```
GLOBAL (모델별)     ← Bedrock 서비스 할당량 보호
  └── TEAM         ← 팀 공정 사용 보장
       └── USER    ← 개별 사용자 제한
```

하위가 상위를 초과할 수 없습니다. 미설정 시 상위 정책을 상속합니다.

### 6.2 Rate Limit 메트릭

| 메트릭 | 의미 | 사용 예 |
|--------|------|---------|
| **RPM** | Requests Per Minute | 분당 요청 수 |
| **TPM** | Tokens Per Minute | 분당 토큰 수 (입출력 합산) |
| **CPM** | Cost Per Minute (USD) | 분당 비용 |
| **CPH** | Cost Per Hour (USD) | 시간당 비용 |

### 6.3 Rate Limit 설정

Admin UI → Rate Limits 페이지

**Tree View**: 계층적으로 상속 관계를 시각화

설정 예:
```
[GLOBAL] claude-opus
  RPM: 60, TPM: 200,000
  ├── [TEAM] Backend
  │   RPM: 30, TPM: 100,000
  │   ├── [USER] user-a
  │   │   RPM: 10, TPM: 50,000
  │   └── [USER] user-b
  │       (상속: RPM: 30, TPM: 100,000)
  └── [TEAM] Frontend
      RPM: 20, TPM: 80,000
```

### 6.4 Rate Limit 동작

사용자가 Rate Limit 초과 시:
- HTTP 429 Too Many Requests 반환
- 응답 헤더:
  - `Retry-After`: 초 단위 대기 시간
  - `X-RateLimit-Scope`: USER / TEAM / GLOBAL
  - `X-RateLimit-Type`: rpm / tpm / cpm / cph
  - `X-RateLimit-Limit`: 한도값
  - `X-RateLimit-Reset`: 리셋 시각

---

## 7. API Key (Virtual Key) 관리

### 7.1 VK 발급 흐름 (자동)

```
사용자 CLI login → OIDC 인증 → admin-api VK 발급 → 로컬 캐시
```

- Admin이 직접 발급할 필요 없음
- 1인 1키 정책: 새 VK 발급 시 이전 키 자동 만료
- TTL: `OIDC_VK_TTL_HOURS` 기본 1시간

### 7.2 VK 목록 확인

Admin UI → Keys 페이지

필터:
- Status: `ACTIVE` / `EXPIRED` / `REVOKED`
- Team / User / Email 검색
- 페이지네이션 (커서 기반)

표시 정보:
| 컬럼 | 설명 |
|------|------|
| Key Prefix | `vk-` + 앞 8자 (마스킹) |
| User | 발급 대상 이메일 |
| Status | ACTIVE / EXPIRED / REVOKED |
| Issued At | 발급 시각 |
| Expires At | 만료 시각 |
| Last Used At | 마지막 사용 시각 |

### 7.3 VK 즉시 폐기 (Revoke)

Admin UI → Keys → 해당 키 → **"Revoke"** 버튼

효과:
- 즉시 Redis에서 삭제
- 다음 요청부터 401 Unauthorized
- 사용자는 `gateway-cli login`으로 새 VK 자동 재발급

### 7.4 팀 전체 강제 재인증

Admin UI → Users → 팀 → **"Force Reauth"** 버튼

효과:
- 해당 팀 모든 멤버의 ACTIVE VK 즉시 REVOKE
- 모든 팀원이 재로그인 필요
- 보안 사고 시 또는 팀 정책 대규모 변경 시 사용

---

## 8. 대시보드 & 모니터링

### 8.1 Dashboard (메인 페이지)

**KPI 카드**:
- 이번 달 총 비용 (USD)
- 예산 소진률 (%)
- 사용자당 비용
- 총 요청 수
- 총 토큰 수
- 활성 키 수
- 활성 모델 수

**모델별 비용 점유율** (도넛 차트):
- 팀별 필터 가능
- 각 모델의 비용 비중 시각화

### 8.2 Monitoring (실시간)

Admin UI → Monitoring 페이지

**Overview (최근 1시간)**:
- 활성 모델 수
- 총 요청 수 / 에러 수 / 에러율 (%)
- 평균 지연시간 / P95 지연시간
- 총 비용

**Model Health 테이블**:
| 모델 | Status | 1h 요청 | Avg 지연 | Error Rate | Last Request |
|------|--------|---------|---------|-----------|-------------|
| claude-opus | ✅ | 150 | 2.3s | 0.7% | 2분 전 |
| claude-sonnet | ✅ | 890 | 1.1s | 0.2% | 30초 전 |
| claude-haiku | ⚠️ | 12 | 5.2s | 8.3% | 15분 전 |

**Top Users (비용 순)**:
- 최근 1시간 상위 사용자
- 요청 수, 토큰 수, 비용, 에러율

**Event Log (최근 24시간)**:
- `ERROR`: 5xx 응답
- `TIMEOUT`: 요청 타임아웃
- `SLOW_REQUEST`: 5초 초과 응답
- 각 이벤트: 시각, user_id, model, 상세

### 8.3 알림

Notification Worker가 자동 발송:
- **80% 예산 도달**: 팀 리더에게 경고
- **90% 예산 도달**: 팀 리더 + Admin에게 경고
- **100% 예산 도달**: 팀 리더 + Admin에게 긴급 알림

> **참고**: 현재 이메일 발송이 mock 모드로 설정되어 있을 수 있습니다. 알림이 실제로 수신되지 않는다면 배포자에게 `notificationWorker.email.provider` 설정 확인을 요청하세요.

---

## 9. 사고 대응

### 9.1 사용자 즉시 차단

**레벨 1 — Gateway만 차단** (빠름):
- Admin UI → Users → 해당 사용자 → **"Deactivate"**
- 효과: 다음 요청부터 403 (VK 캐시 300초 이내 만료)

**레벨 2 — VK까지 즉시 무효화** (즉시):
- Admin UI → Keys → 해당 사용자 키 → **"Revoke"**
- 효과: 진행 중인 세션도 즉시 차단

**레벨 3 — IDP까지 차단** (완전):
```bash
aws cognito-idp admin-disable-user \
  --user-pool-id "$USER_POOL_ID" --username "$EMAIL"
```
- 효과: Refresh Token도 즉시 무효 (Cognito 거부)

### 9.2 팀 전체 차단

Admin UI → Users → 팀 → **"Force Reauth"**

효과: 해당 팀 모든 멤버의 ACTIVE VK 즉시 REVOKE → 전원 재로그인 필요. 추가로 개별 사용자를 Deactivate하여 완전 차단.

### 9.3 서비스 장애 대응

| 상황 | 즉시 조치 |
|------|-----------|
| gateway-proxy 다운 | `kubectl rollout restart deploy/llm-gateway-gateway-proxy -n llm-gateway` |
| admin-api 다운 | `kubectl rollout restart deploy/llm-gateway-admin-api -n llm-gateway` |
| DB 다운 | Aurora 자동 failover (5분 이내). 수동: AWS Console RDS → Failover |
| Redis 다운 | Gateway degradation mode 자동 전환 (in-memory RPM fallback) |
| Cognito 장애 | STS(AWS SSO) 경로로 수동 전환 가능 (부록 C 참조). 기존 발급된 VK는 TTL 내 정상 동작 |

### 9.4 디버깅 명령

> 아래 명령은 긴급 상황에서만 사용하세요. 일반적인 조회는 Admin UI 또는 API (`GET /admin/users?email=...`)를 이용하세요.

```bash
# 특정 사용자 조회
kubectl exec -n llm-gateway deploy/llm-gateway-admin-api -- python -c "
from sqlalchemy import create_engine, text
import os
e = create_engine(os.environ['DATABASE_URL'].replace('+asyncpg',''))
with e.connect() as c:
  r = c.execute(text(\"SELECT id, email, role, team_id, is_active FROM auth.users WHERE email='user@company.com'\"))
  print(r.fetchone())
"

# admin-api 로그 확인
kubectl logs -n llm-gateway deploy/llm-gateway-admin-api --tail=100 -f

# Redis 키 확인
kubectl exec -n llm-gateway deploy/llm-gateway-gateway-proxy -- python -c "
import redis, os
r = redis.from_url(os.environ['REDIS_URL'])
print(r.keys('budget:*'))
"
```

---

## 10. 정기 점검 체크리스트

### 주 1회

- [ ] Admin UI Analytics — 비용 추이 검토
- [ ] 새로 자동 생성된 팀 확인 (budget 미설정 팀 → 활성화)
- [ ] IDP 그룹과 DB 팀 일치 확인
- [ ] 비활성 사용자 정리 (퇴사자)
- [ ] 임계값 알림 설정 확인 (80/90/100%)

### 월 1회

- [ ] 전체 예산 vs 실사용 리뷰
- [ ] 모델별 비용 효율 검토 (ROI)
- [ ] Rate Limit 적정성 검토 (429 빈도)
- [ ] 보안: 미사용 VK 정리, 이상 사용 패턴 확인
- [ ] 시크릿 교체 주기 확인

---

## 부록 A: Admin API 엔드포인트 레퍼런스

모든 Admin UI 작업은 아래 API로도 수행 가능합니다. 자동화/스크립팅이 필요하면 이 엔드포인트를 활용하세요.

**인증**: 모든 요청에 OIDC 토큰을 `Authorization: Bearer` 헤더로 전달합니다.

**curl 예시** (팀 예산 설정):
```bash
curl -X PUT "$ADMIN_API_URL/admin/budgets/team/<team_id>" \
  -H "Authorization: Bearer <OIDC_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "max_budget_usd": 5000,
    "policy": "HARD_BLOCK",
    "alert_thresholds": [80, 90, 100]
  }'
```

### 부서 & 사용자 & 팀

| Method | Path | 설명 |
|--------|------|------|
| POST | `/admin/departments` | 부서 생성 |
| GET | `/admin/users` | 사용자 목록 (pagination) |
| PUT | `/admin/users/{user_id}/team` | 팀 이동 |
| GET | `/admin/users/tree` | 조직 트리 |
| POST | `/admin/users/sync-cognito` | IDP 동기화 |
| GET | `/admin/users/teams` | 전체 팀 목록 (멤버 수 포함) |
| POST | `/admin/teams` | 팀 생성 |
| PUT | `/admin/teams/{team_id}/leader` | 팀 리더 설정 |
| POST | `/admin/teams/{team_id}/force-reauth` | 팀 강제 재인증 |
| GET | `/admin/teams/{team_id}/allowed-models` | 팀 허용 모델 |
| PUT | `/admin/teams/{team_id}/allowed-models` | 팀 모델 설정 |
| DELETE | `/admin/teams/{team_id}/allowed-models` | 팀 모델 제한 해제 |

Request body:
```jsonc
// POST /admin/departments
{ "name": "AI Center", "org_id": null }

// POST /admin/teams
{ "name": "Backend", "department_id": "<dept_uuid>" }

// PUT /admin/users/{user_id}/team
{ "team_id": "<team_uuid>" }

// PUT /admin/teams/{team_id}/leader
{ "user_id": "<user_uuid>" }

// PUT /admin/teams/{team_id}/allowed-models
{ "model_aliases": ["claude-sonnet", "claude-haiku"] }  // 빈 배열 = 전체 허용
```

### 모델

| Method | Path | 설명 |
|--------|------|------|
| GET | `/admin/models` | 모델 목록 |
| POST | `/admin/models` | 모델 생성 |
| PUT | `/admin/models/{alias}` | 모델 수정 |
| PUT | `/admin/models/{alias}/pricing` | 가격 설정 |
| PATCH | `/admin/models/{alias}/status` | 활성화/비활성화 |

Request body:
```jsonc
// POST /admin/models
{
  "alias": "claude-sonnet",
  "provider": "BEDROCK",
  "provider_model_id": "anthropic.claude-sonnet-4-20250514-v1:0",
  "api_format": "BEDROCK_NATIVE",
  "input_price_per_1k_tokens": 0.003,
  "output_price_per_1k_tokens": 0.015,
  "cache_creation_5m_price_per_1k_tokens": 0.00375,
  "cache_read_price_per_1k_tokens": 0.0003
}

// PUT /admin/models/{alias}
{ "provider_model_id": "...", "description": "..." }

// PUT /admin/models/{alias}/pricing
{
  "input_price_per_1k_tokens": 0.003,
  "output_price_per_1k_tokens": 0.015,
  "effective_from": "2026-05-01T00:00:00Z"
}

// PATCH /admin/models/{alias}/status
{ "active": true }
```

### 예산

| Method | Path | 설명 |
|--------|------|------|
| GET | `/admin/budgets/summary` | 예산 요약 |
| PUT | `/admin/budgets/team/{id}` | 팀 예산 설정 |
| PUT | `/admin/budgets/user/{id}` | 사용자 예산 설정 |
| DELETE | `/admin/budgets/user/{id}` | 사용자 예산 삭제 (팀 예산으로 fallback) |
| GET | `/admin/budgets/team/{id}/allocation` | 팀 할당 현황 |
| PUT | `/admin/budgets/team/{id}/allocate` | 팀원 예산 할당 |
| GET | `/admin/budgets/{scope}/{id}/downgrade` | 다운그레이드 정책 조회 |
| PUT | `/admin/budgets/{scope}/{id}/downgrade` | 다운그레이드 정책 설정 |
| DELETE | `/admin/budgets/{scope}/{id}/downgrade` | 다운그레이드 정책 삭제 |

Request body:
```jsonc
// PUT /admin/budgets/team/{id} 또는 /user/{id}
{ "max_budget_usd": 5000, "policy": "HARD_BLOCK", "alert_thresholds": [80, 90, 100] }

// PUT /admin/budgets/team/{id}/allocate
{ "allocations": [
    { "user_id": "<uuid>", "allocated_usd": 1500 },
    { "user_id": "<uuid>", "allocated_usd": 1000 }
] }

// PUT /admin/budgets/{scope}/{id}/downgrade  (scope: TEAM 또는 USER)
{ "enabled": true, "rules": [
    { "from_model_alias": "claude-opus", "to_model_alias": "claude-sonnet", "threshold_pct": 80 }
] }
```

### Rate Limit

| Method | Path | 설명 |
|--------|------|------|
| GET | `/admin/rate-limits/tree` | 계층 트리 |
| PUT | `/admin/rate-limits/user/{user_id}` | 사용자 RL 설정 |
| PUT | `/admin/rate-limits/team/{team_id}` | 팀 RL 설정 |
| PUT | `/admin/rate-limits/global/{model}` | 글로벌 RL 설정 |

Request body:
```jsonc
// PUT /admin/rate-limits/{scope}/{id}  (모든 필드 선택 — null이면 해당 메트릭 미적용)
{ "rpm": 30, "tpm": 100000, "cpm": null, "cph": null }
```

### 키 관리

| Method | Path | 설명 |
|--------|------|------|
| GET | `/admin/keys` | VK 목록 |
| GET | `/admin/keys/count` | VK 개수 |
| DELETE | `/admin/keys/{id}` | VK 폐기 |

### 대시보드 & 모니터링

| Method | Path | 설명 |
|--------|------|------|
| GET | `/admin/dashboard/summary` | KPI 요약 |
| GET | `/admin/dashboard/model-share` | 모델별 비용 점유율 |
| GET | `/admin/monitoring/overview` | 1시간 스냅샷 |
| GET | `/admin/monitoring/models` | 모델 헬스 |
| GET | `/admin/monitoring/events` | 이벤트 로그 |
| GET | `/admin/monitoring/users` | 상위 사용자 |

---

## 부록 B: 자동 프로비저닝 디버깅

### "사용자가 로그인했는데 Admin UI에 안 보임"

```sql
SELECT email, role, team_id, is_active, created_at 
FROM auth.users WHERE email = 'user@company.com';
```
- 행 없음 → OIDC 검증 실패. admin-api 로그 확인
- 행 있는데 안 보임 → Admin UI 캐시 문제. 새로고침

### "사용자가 403 no_matching_team_group 에러"

원인: IDP 그룹명이 `OIDC_GROUP_PREFIX` (기본 `Claude_`) + underscore 규칙에 매칭 안 됨

기본 동작: `OIDC_REJECT_UNMATCHED_GROUPS: true` (기본값)이면 매칭 그룹이 없을 때 **403 거부**됩니다. `false`로 설정하면 Default Team으로 fallback합니다.

확인:
```bash
kubectl exec -n llm-gateway deploy/llm-gateway-admin-api -- \
  env | grep -E "OIDC_GROUP_PREFIX|OIDC_REJECT_UNMATCHED"
```

해결:
- IDP 그룹명을 `Claude_<팀명>` 또는 `Claude_<부서명>_<팀명>` 형식으로 변경
- 또는 Helm values에서 `OIDC_REJECT_UNMATCHED_GROUPS: "false"`로 설정 (Default Team fallback 활성화)

### "사용자가 'Default Team'으로 매핑됨"

원인: `OIDC_REJECT_UNMATCHED_GROUPS: false` 상태에서, IDP 그룹명이 prefix에 매칭 안 됨

확인:
```bash
kubectl exec -n llm-gateway deploy/llm-gateway-admin-api -- \
  env | grep OIDC_GROUP_PREFIX
```

해결: IDP 그룹명을 `Claude_` 접두사 + 올바른 underscore 규칙으로 변경

### "Admin 권한이 안 들어옴"

확인:
```bash
kubectl exec -n llm-gateway deploy/llm-gateway-admin-api -- \
  env | grep -E "ADMIN_EMAILS|ADMIN_GROUPS"
```

해결: Helm values `adminApi.adminBootstrap.emails` 또는 `.groups` 수정 후 `helm upgrade`

---

## 부록 C: STS(AWS SSO) 인증 경로

Gateway는 OIDC 인증 외에 **STS(AWS SSO) 경로**를 dual-mode로 유지하고 있습니다. 이 경로는 OIDC IDP 장애 시 fallback으로 활용하거나, AWS SSO만 사용하는 환경에서 단독으로 사용할 수 있습니다.

### 언제 사용되나

| 상황 | 동작 |
|------|------|
| OIDC 정상 운영 중 | 사용자는 OIDC 경로로 VK 발급 (기본) |
| Cognito/IDP 장애 발생 | 사용자가 `api-key-helper --auth-mode sts`로 전환하여 AWS SSO 경로로 VK 발급 가능 |
| OIDC 미설정 환경 | `OIDC_ISSUER_URL` 미설정 시 api-key-helper가 자동으로 STS 모드 선택 |

### 동작 방식

```
사용자 PC                        admin-api                          AWS STS
─────────                        ─────────                          ───────
aws sso login
    │
api-key-helper (STS mode)
    ├─ aws sts get-caller-identity (세션 확인)
    ├─ SigV4 presigned URL 생성 ──►
    │                              POST /cli/auth/virtual-key
    │                                 ├─ SSRF 방어 (허용 호스트 검증)
    │                                 ├─ presigned URL 전달 ──────────► GetCallerIdentity
    │                                 │                                     │
    │                                 │◄── ARN 반환 ─────────────────────────┘
    │                                 ├─ ALLOWED_IAM_ROLES 검증
    │                                 ├─ User auto-provisioning (신규 시)
    │                                 └─ VK 발급 + Redis 캐시
    │◄── VK 반환 ──────────────────────┘
    │
Claude Code (Bearer vk-xxx)
```

### 관련 환경변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `ALLOWED_STS_REGIONS` | STS URL 허용 리전 (SSRF 방어) | `["ap-northeast-2"]` |
| `ALLOWED_IAM_ROLES` | VK 발급 허용 IAM Role 이름 목록. **빈 값이면 모든 Role 허용** | `[]` (주의) |

### Admin 주의사항

- `ALLOWED_IAM_ROLES`이 빈 배열이면 **모든 AWS SSO 사용자**가 VK를 발급받을 수 있습니다. 운영 환경에서는 반드시 허용 Role을 명시하세요.
- STS 경로로 발급된 VK의 TTL은 `min(rotation_policy, SSO 세션 만료)` — SSO 세션이 보통 8~12시간이므로 OIDC(1시간)보다 긴 VK가 발급됩니다.
- STS 경로의 사용자는 `sso_subject` (ARN)로 식별되며, OIDC 경로의 사용자와 별도 레코드입니다.

### Cognito 장애 시 사용자에게 안내할 절차

OIDC 로그인이 불가한 경우, 사용자에게 AWS SSO를 활용한 아래 임시 방안을 안내하세요:

```bash
# 1. AWS SSO 로그인
aws sso login

# 2. STS 모드로 VK 발급
# 주의: --gateway-url에는 Admin API 주소를 입력 (Gateway Proxy 주소가 아님)
api-key-helper --auth-mode sts --gateway-url https://admin-api.llm-gateway.example.com

# 3. Claude Code 정상 사용 (이미 설정된 ANTHROPIC_BASE_URL 사용)
claude
```

---

## 부록 D: 현재 운영 환경 값 (참고)

> 이 절은 본 deliverable 의 **현재 배포된 두 환경**(AWS 계정 `123456789012`,
> 리전 `ap-northeast-2`)을 기록합니다. 도메인/ACM 적용 전이므로 ALB DNS 직접
> 접근(평문 HTTP)이며, 운영 출시 전에는 HTTPS+도메인으로 교체됩니다.

### D.1 prod (default — 운영 환경)

| 항목 | 값 |
|---|---|
| Cognito User Pool | `ap-northeast-2_XXXXXXXXX` |
| OIDC Issuer | `https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_XXXXXXXXX` |
| OIDC Client ID (PKCE public) | `<COGNITO_APP_CLIENT_ID>` |
| Hosted UI | `https://llm-gateway-prod-vanilla-auth-123456789012.auth.ap-northeast-2.amazoncognito.com/login` |
| Admin UI | `http://<ALB_DNS>` |
| Admin API | `http://<ALB_DNS>` |
| Gateway Proxy | `http://<ALB_DNS>` |
| Aurora cluster | `llm-gateway-prod` (multi-AZ, db.r7g.large) |
| ElastiCache | `<ELASTICACHE_ENDPOINT>` (cluster mode) |
| Bootstrap admin | `admin@example.com` (group `ClaudeAdmin`) |
| Cognito groups (등록됨) | `ClaudeAdmin`, `Claude_AWS-AI-Specialist` |

### D.2 dev (검증/개발 환경)

| 항목 | 값 |
|---|---|
| Cognito User Pool | `ap-northeast-2_XXXXXXXXX` |
| OIDC Issuer | `https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_XXXXXXXXX` |
| OIDC Client ID (PKCE public) | `<COGNITO_APP_CLIENT_ID>` |
| Admin UI | (별도 ALB — `kubectl get ingress -n llm-gateway-dev` 로 확인) |
| Admin API | `http://<ALB_DNS>` |
| Gateway Proxy | `http://<ALB_DNS>` |
| Cognito groups (등록됨) | `ClaudeAdmin`, `Claude_AWS-AI-Specialist` |

> 두 환경 모두 동일 그룹 컨벤션(`ClaudeAdmin`, `Claude_<team>`,
> `Claude_<dept>_<team>`)을 사용하며, dev 도 prod 와 같은 ap-northeast-2
> 리전으로 통일되어 있습니다 (2026-05-18 마이그레이션 완료).
