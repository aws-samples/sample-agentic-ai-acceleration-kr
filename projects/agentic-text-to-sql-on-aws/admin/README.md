# Admin — Agentic Text-to-SQL 관리자 패널 (Next.js)

Manager·Admin 페르소나가 semantic 지식을 큐레이션·승인하고, 데이터 소스를 등록·크롤하고,
사용자 권한과 Cedar 정책을 확인하고, 메트릭·트레이스로 디버깅하는 **단일 컨테이너 웹앱**입니다.
web 페이지와 API routes 가 한 Next.js(App Router) 앱에 함께 들어 있으며 ECS Fargate(ARM64) +
전용 ALB 로 호스팅됩니다.

> M4 범위. 인터페이스 계약은 `docs/m2-m3-interface-contract.md` **§8** 이 단일 진실 원천입니다.

## 아키텍처 요약

```
브라우저 (로그인 → AccessToken 을 sessionStorage 보관)
   │  모든 API 호출에 Authorization: Bearer <AccessToken>
   ▼
Next.js API routes (Node 런타임)
   ├─ aws-jwt-verify 로 AccessToken 검증 → cognito:groups 로 인가
   │     · Manager|Admin 아니면 403, 미인증 401
   │     · iam/* 는 Admin 그룹만
   │
   ├─ [쓰기 평면] 사용자 Bearer 토큰을 **그대로** 전달 (On-Behalf-Of)
   │     → Gateway MCP (GATEWAY_URL) → datasource-admin-mcp___<tool>
   │       semantic 엔티티 CRUD·발행, 데이터 소스 등록·테스트·크롤
   │
   └─ [읽기 관리 평면] admin web task role 의 AWS SDK v3 직접 호출
         · cognito-idp   : 사용자·그룹 관리
         · agentcore-control : Cedar 정책 조회(read-only)
         · cloudwatch    : 메트릭 요약 (GetMetricData)
         · cloudwatch-logs : 세션 목록·이벤트 타임라인
```

### 왜 semantic 쓰기를 MCP 로 우회하나 (§8.0)

admin web 은 **DynamoDB 를 직접 쓰지 않습니다.** 모든 쓰기를 Gateway MCP 경유로 통일하면

1. DynamoDB 단일 쓰기 지점이 유지되고(dual-write 금지 원칙),
2. Cedar 가 도구 단위로 Manager/Admin 인가를 강제하며,
3. M3 에서 이월된 **사용자별 JWT On-Behalf-Of** 가 admin 경로에서 실현됩니다.

Cognito 사용자·그룹 관리와 Cedar·CloudWatch 조회는 도구 평면이 아닌 **관리 평면**이므로
MCP 를 거치지 않고 task role 의 SDK 직접 호출로 처리합니다(읽기 위주 · 최소 권한).

### Gateway 도구명 해석

Gateway 는 target 이름을 프리픽스로 붙입니다: `datasource-admin-mcp___put_entity`
(트리플 언더스코어). `src/lib/mcp-client.ts` 는 호출 전에 `tools/list` 를 조회해
**suffix 매칭**으로 실제 이름을 찾고, 실패 시 관례 프리픽스로 폴백합니다(target 개명 내구성).
Cedar 가 미인가 도구를 목록에서 제외하므로 tools/list 가 비어 있으면 권한 부족 신호입니다.

## 화면

| 탭 | 내용 | 권한 |
|---|---|---|
| (로그인) | Cognito USER_PASSWORD_AUTH 로그인 | - |
| Semantic 큐레이션 | 용어·동의어·관계(join)·few-shot 목록(타입·상태 필터), payload JSON 편집, 발행/회수, 버전 표시 | Manager 이상 |
| 승인 큐 | candidate 목록 → 상세(payload) → 승인(발행) | Manager 이상 |
| 데이터 소스 | 등록(id/engine/config), 목록, 연결 테스트, 스키마 크롤 결과 | Manager 이상 |
| 권한 관리 | Cognito 사용자 목록·생성·그룹 지정, Cedar 정책 read-only 뷰 | **Admin 전용** |
| 대시보드 | 메트릭 요약 카드, 최근 세션 목록 → 이벤트 타임라인 | Manager 이상 |

- 그룹이 Manager 면 **권한 관리 탭이 숨겨집니다.** 화면 숨김은 UX 보조이고, 실제 강제는
  서버 route(`requireAdmin`)와 Cedar 가 담당합니다(이중 방어).
- 반려 상태는 별도로 만들지 않습니다 — candidate 로 남겨두는 것이 반려입니다
  (상태 기계를 candidate/published 2개로 유지해 파생 저장소 동기화 규칙을 단순하게 둡니다).

## API 경로 ↔ MCP 도구 / AWS API 매핑

| 경로 | 메서드 | 처리 | 권한 |
|---|---|---|---|
| `/api/health` | GET | `{"ok":true}` (ALB 헬스체크) | 없음 |
| `/api/auth/login` | POST | cognito-idp `InitiateAuth` (USER_PASSWORD_AUTH) | 없음 |
| `/api/semantic/entities` | GET | MCP `list_entities(entity_type?, status?)` | Manager+ |
| `/api/semantic/entities/{type}/{id}` | GET | MCP `get_entity` | Manager+ |
| `/api/semantic/entities/{type}/{id}` | PUT | MCP `put_entity(status=candidate, actor)` | Manager+ |
| `/api/semantic/entities/{type}/{id}/publish` | POST | MCP `publish_entity(actor)` | Manager+ |
| `/api/semantic/entities/{type}/{id}/unpublish` | POST | MCP `unpublish_entity(actor)` | Manager+ |
| `/api/approvals` | GET | MCP `list_entities(status="candidate")` | Manager+ |
| `/api/datasources` | GET | MCP `list_entities(entity_type="datasource")` | Manager+ |
| `/api/datasources` | POST | MCP `register_datasource(actor)` | Manager+ |
| `/api/datasources/{id}/test` | POST | MCP `test_datasource` | Manager+ |
| `/api/datasources/{id}/crawl` | POST | MCP `crawl_schema(actor)` | Manager+ |
| `/api/iam/users` | GET | cognito-idp `ListUsers` + `AdminListGroupsForUser` | **Admin** |
| `/api/iam/users` | POST | cognito-idp `AdminCreateUser` (+ `AdminAddUserToGroup`) | **Admin** |
| `/api/iam/users/{username}/groups` | POST | cognito-idp `AdminAddUserToGroup` / `AdminRemoveUserFromGroup` | **Admin** |
| `/api/iam/groups` | GET | cognito-idp `ListGroups` (그룹 드롭다운 보조) | **Admin** |
| `/api/cedar/policies` | GET | agentcore-control `ListPolicies` (read-only) | Manager+ |
| `/api/metrics/summary` | GET | cloudwatch `GetMetricData` (`AWS/Bedrock-AgentCore`) | Manager+ |
| `/api/traces/sessions` | GET | logs `DescribeLogGroups` + `DescribeLogStreams` | Manager+ |
| `/api/traces/{id}` | GET | logs `GetLogEvents` | Manager+ |

- MCP 호출 시 `actor` 인자에는 JWT 의 `username` 클레임을 실어 **감사 기록**을 남깁니다.
- `/api/traces/{id}` 의 `{id}` 는 `<로그그룹>|<스트림>` 합성 키(URL 인코딩)이며,
  `RUNTIME_LOG_GROUP_PREFIX` 로 시작하지 않는 로그 그룹은 403 으로 차단합니다.

## 환경 변수

`.env.example` 참고. 로컬은 `.env.local` 로 복사해 사용합니다. 값은 배포 산출물
(`base-outputs.json` / `gateway-outputs.json`)에서 가져옵니다.

| 변수 | 설명 |
|---|---|
| `AWS_REGION` | 리전 (기본 `us-west-2`) |
| `COGNITO_USER_POOL_ID` | AccessToken 검증·사용자 관리 대상 사용자 풀 (필수) |
| `COGNITO_CLIENT_ID` | USER_PASSWORD_AUTH 가 켜진 클라이언트 ID — 로그인·토큰 검증 (필수) |
| `GATEWAY_URL` | Gateway MCP 엔드포인트 — semantic 쓰기 OBO 경로 (필수) |
| `POLICY_ENGINE_ID` | Cedar 정책 조회 대상 PolicyEngine ID |
| `RUNTIME_LOG_GROUP_PREFIX` | 트레이스 탐색 대상 프리픽스 (기본 `/aws/bedrock-agentcore/runtimes/`) |
| `ADMIN_MCP_TARGET` | Gateway MCP target 이름 (기본 `datasource-admin-mcp`, 통상 변경 불필요) |

시크릿은 파일에 커밋하지 않습니다 — ECS 는 task definition 환경 변수, 로컬은 `.env.local`.
AWS 자격증명은 ECS 에서 **task role** 을 자동 사용하고, 로컬은 `AWS_PROFILE` 등 표준
credential chain 을 사용합니다.

## 로컬 실행

```bash
# 1) 의존성 설치
npm install

# 2) 환경 변수 준비
cp .env.example .env.local
#   .env.local 에서 COGNITO_*·GATEWAY_URL·POLICY_ENGINE_ID 를 실제 값으로 채웁니다.

# 3) AWS 자격증명 (읽기 관리 평면 호출용)
export AWS_PROFILE=your-profile

# 4) 개발 서버
npm run dev                              # http://localhost:3000
```

기타 스크립트: `npm run build`(프로덕션 빌드) · `npm run lint` · `npm run typecheck` ·
`npm run format`.

로그인에는 Manager 또는 Admin 그룹에 속한 Cognito 사용자가 필요합니다. 신규 사용자는 첫 로그인
시 비밀번호 변경이 필요하며(FORCE_CHANGE_PASSWORD), 그 상태에서는 로그인이 409(추가 인증 필요)로
거부됩니다 — CLI/콘솔로 영구 비밀번호를 설정한 뒤 로그인하세요.

## 주요 파일

| 파일 | 역할 |
|---|---|
| `src/lib/auth.ts` | AccessToken 검증(aws-jwt-verify) + 그룹 인가 (`requireManager`/`requireAdmin`) |
| `src/lib/mcp-client.ts` | Gateway MCP OBO 호출 · 도구명 suffix 매칭 해석 |
| `src/lib/aws-clients.ts` | AWS SDK v3 클라이언트 싱글턴 (읽기 관리 평면) |
| `src/lib/api.ts` | 응답·오류 정규화 래퍼 (`handle`) |
| `src/lib/client.ts` | 브라우저 세션(sessionStorage) · 인증 fetch 헬퍼 |
| `src/lib/types.ts` | 화면·API 공유 도메인 타입·라벨 |
| `src/components/AdminShell.tsx` | 셸 + 탭 라우팅 (Manager 는 iam 탭 숨김) |
| `src/components/CurationView.tsx` | Semantic 큐레이션 (목록·편집·발행) |
| `src/components/ApprovalsView.tsx` | 승인 큐 (candidate → publish) |
| `src/components/DatasourcesView.tsx` | 데이터 소스 등록·테스트·크롤 |
| `src/components/IamView.tsx` | Cognito 사용자·그룹 + Cedar read-only |
| `src/components/DashboardView.tsx` | 메트릭 카드 + 세션 타임라인 |

## Docker / 배포

ARM64 멀티 스테이지 빌드(Next standalone, non-root, 포트 3000) — `ui/` 와 동형입니다.

```bash
# docker 기본, 실패 시 finch 폴백
docker build --platform linux/arm64 -t agentic-t2s-admin .
#   finch build --platform linux/arm64 -t agentic-t2s-admin .

docker run -p 3000:3000 \
  -e AWS_REGION=us-west-2 \
  -e COGNITO_USER_POOL_ID=us-west-2_XXXXXXXXX \
  -e COGNITO_CLIENT_ID=XXXXXXXXXXXX \
  -e GATEWAY_URL=https://...gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp \
  -e POLICY_ENGINE_ID=XXXXXXXXXX \
  agentic-t2s-admin
```

ECS Fargate 에서는 자격증명 대신 **task role**(`agentic-t2sql-admin-web-task-role`)을 사용합니다.
ALB 헬스체크 경로는 `/api/health` 입니다.

## 필요한 task role 권한 (최소 권한)

| 서비스 | 액션 |
|---|---|
| cognito-idp | `InitiateAuth`, `ListUsers`, `ListGroups`, `AdminCreateUser`, `AdminAddUserToGroup`, `AdminRemoveUserFromGroup`, `AdminListGroupsForUser` |
| bedrock-agentcore (control) | `ListPolicies`, `GetPolicy` — **읽기 전용** |
| cloudwatch | `GetMetricData` |
| logs | `DescribeLogGroups`, `DescribeLogStreams`, `GetLogEvents` (런타임 로그 그룹 한정) |

semantic 쓰기·데이터 소스 작업에는 **DynamoDB·Secrets Manager 권한이 필요하지 않습니다** —
사용자 JWT 로 Gateway MCP 를 호출하고, 실제 쓰기는 admin-mcp 런타임 role 이 수행합니다.
