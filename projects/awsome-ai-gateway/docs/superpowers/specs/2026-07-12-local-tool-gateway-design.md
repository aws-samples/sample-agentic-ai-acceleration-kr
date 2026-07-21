# 로컬 admin-ui에서 배포된 Tool Gateway 연결 (run.sh)

- 날짜: 2026-07-12
- 상태: 설계 승인 대기
- 범위(목표 A): `run.sh`로 띄운 로컬 admin-ui에서 **이미 배포된** AgentCore Tool Gateway의
  **Tool 카탈로그 조회 + Tool 호출**이 동작하게 한다. 가짜 gateway는 만들지 않는다.

## 문제

`run.sh`는 `docker-compose.yml`을 띄우지만, admin-ui 서비스 정의에 Tool Gateway 관련
환경변수가 하나도 없다. 그 결과 브라우저 번들의 `TOOL_GATEWAY_ENABLED` 게이트가 꺼진 채로
빌드되어 대시보드가 "Tool Gateway가 비활성화되어 있습니다"로 표시된다.

원인은 두 가지다.

1. **compose에 env 미주입** — `admin-ui/.env.local`(로컬 `next dev`용)에는 값이 있으나
   compose는 그 파일을 읽지 않는다. `docker-compose.yml`의 admin-ui `environment:`에도 없다.
2. **`NEXT_PUBLIC_*`는 빌드 타임 인라인** — Next.js는 `NEXT_PUBLIC_` 변수를 `next build`
   시점에 브라우저 번들에 인라인한다. Dockerfile builder 스테이지가 이 값을 받지 않으므로,
   compose `environment:`로만 넣어서는 브라우저 게이트에 반영되지 않는다. **build arg가 필요**하다.
   (서버 route의 실제 gateway 호출 · Cognito 토큰 발급은 runtime env로 충분하다.)

## 단일 진실 소스

`deployment/tool-gateway/dashboard.generated.env` (provision_tool_gateway.sh deploy가 생성).
`admin-ui/.env.local`과 gateway ID가 다를 수 있으므로 generated.env를 기준으로 삼는다.

포함 값: `NEXT_PUBLIC_TOOL_GATEWAY_{ENABLED,ID,REGION,URL}`, `TOOL_GATEWAY_ARN`,
`COGNITO_TOOL_TOKEN_ENDPOINT`, `COGNITO_TOOL_M2M_{CLIENT_ID,CLIENT_SECRET,SCOPE}`.

## 데이터 흐름

```
dashboard.generated.env ─(run.sh --tools 가 source)─┐
                                                     ├─ build args → next build → 브라우저 번들에 NEXT_PUBLIC_* 인라인
                                                     └─ runtime env → 서버 route(/api/tools/*)
브라우저 → /api/tools/list, /api/tools/call (self)
                        └→ admin-ui 서버가 Cognito M2M 토큰 발급 → 배포된 AgentCore Gateway(MCP/HTTPS) → Lambda
```

CSP `connect-src`는 무관하다. 브라우저는 self(`/api/tools/*`)만 호출하고, gateway로 나가는
요청은 admin-ui **서버**에서 발생하므로 CSP 대상이 아니다.

## 컴포넌트

### 1. `docker-compose.tools.yml` (신규 override)

admin-ui의 build args + runtime env만 덧씌운다. 모든 값은 `${VAR:-}` 기본 빈 문자열로 두어
generated.env가 없어도 compose 파싱이 깨지지 않게 한다.

- build.args: `NEXT_PUBLIC_TOOL_GATEWAY_{ENABLED,URL,ID,REGION}`
- environment: 위 4개 + `TOOL_GATEWAY_ARN`, `COGNITO_TOOL_TOKEN_ENDPOINT`,
  `COGNITO_TOOL_M2M_{CLIENT_ID,CLIENT_SECRET,SCOPE}`

### 2. `admin-ui/Dockerfile` builder 스테이지에 ARG/ENV 추가

`NEXT_PUBLIC_TOOL_GATEWAY_{ENABLED,URL,ID,REGION}` 4개를 `ARG` → `ENV`로 선언 후 `npm run build`.
ARG가 비면 기존 동작(비활성)과 동일 → gateway 미배포 사용자 무영향.

### 3. `run.sh` 변경

- `cmd_up`에서 `--tools` 플래그 파싱 → `WITH_TOOLS=1`
- `--tools`일 때 `deployment/tool-gateway/dashboard.generated.env`를
  `set -a; source; set +a`로 로드. 파일 없으면 에러 + 안내("먼저 provision_tool_gateway.sh
  deploy 실행") 후 종료.
- `NEXT_PUBLIC_TOOL_GATEWAY_URL`이 비어 있으면 경고만 하고 진행(카탈로그 Unavailable, 크래시 없음).
- compose 호출을 `$DC` 대신 파일 배열로: `--tools`이면 `-f docker-compose.yml -f docker-compose.tools.yml`.
  헬스/ps/logs/down 등 모든 하위 명령이 동일 파일 세트를 쓰도록 `COMPOSE_FILES` 변수로 통일.
- `--build`는 이미 붙어 있어 URL 변경 시 재빌드됨(OK).
- `print_endpoints`에 Tool Gateway 카탈로그 경로(`/tools`) 안내 추가.
- `--help` 텍스트에 `--tools` 추가.

### 4. 보안: `.gitignore`

`deployment/tool-gateway/dashboard.generated.env`는 현재 git-ignore되지 않는다
(Cognito client secret 평문 포함). `.gitignore`에 추가한다. `.env.key`도 함께 확인.

## 에러 처리

| 상황 | 동작 |
|------|------|
| `--tools`인데 generated.env 없음 | 에러 메시지 + 종료(1) |
| generated.env는 있으나 GATEWAY_URL 비어있음 | 경고 후 진행, 대시보드는 Unavailable |
| AWS 자격증명 없어 X-Ray/메트릭 실패 | A 범위 밖, route가 빈 결과 반환(graceful). 손대지 않음 |

## 범위 밖

- 트레이스/메트릭 대시보드(X-Ray/CloudWatch 실데이터)
- Lambda 로컬 실행(배포된 것 사용)
- gateway 자체 프로비저닝(provision_tool_gateway.sh가 담당)

## 검증

1. `provision_tool_gateway.sh deploy` 완료 상태(또는 기존 generated.env) 전제
2. `./run.sh up --tools`
3. admin-ui 로그인 → `/tools` 접속 → Tool 카탈로그 목록 표시 확인
4. 한 개 툴 실행 → 실제 검색 결과 반환 확인
5. `--tools` 없이 `./run.sh up` → 기존처럼 "비활성화" 표시(회귀 없음) 확인
