# web (Next.js 프론트엔드)

agent-platform의 프론트엔드입니다. 채팅 UI와 Registry / Harness / Knowledge /
Insights / Settings 페이지, 로그인 화면(Cognito 비밀번호 · Microsoft Entra ID)을
제공합니다. 전체 구성·아키텍처는 루트 [`README.md`](../README.md) 참고.

- Next.js 16 (App Router, Turbopack), React 19, TypeScript
- Radix UI + Tailwind, SWR
- 포트 3000 (MCP Apps 샌드박스 출처는 별도 dev 서버 3001)

## 실행

루트의 `run.sh`가 backend·frontend·샌드박스를 한 번에 띄웁니다 (권장).

```bash
cd .. && ./run.sh            # backend(:8000) + frontend(:3000) + 샌드박스(:3001)
cd .. && ./run.sh frontend   # frontend만
```

프론트만 직접 띄우려면:

```bash
cd web
yarn install
yarn dev                     # http://localhost:3000
```

## 라우트

| 경로 | 내용 |
| --- | --- |
| `/` | 채팅. `?threadId=` 로 스레드를 연다. 에이전트 전환 메뉴 맨 위에는 서버가 허용한 모델별 **기본 채팅** 행이 있다(`/api/config` 의 `basicChat.models`, 로직은 `lib/basicChat.mjs`) |
| `/registry`, `/harness`, `/knowledge` | 일반 사용자 메뉴. admin 이 Settings 에서 숨길 수 있다 |
| `/insights`, `/settings` | admin. Settings 는 `?tab=menus\|rates\|mcp` 세 탭 |
| `/mcp` | `/settings?tab=mcp` 로 리다이렉트(옛 북마크용) |
| `/auth/callback` | OIDC(Entra ID) 로그인 콜백 |
| `/client-config`, `/sandbox` | Route Handler. 런타임 샌드박스 출처 조회, MCP Apps 샌드박스 문서(CSP 헤더 포함) |
| `/api/*`, `/threads/*` | 백엔드 프록시(아래) |

## 로그인

로그인 화면은 `GET /api/auth/config` 가 돌려주는 공급자 목록대로 그립니다(요청이 실패하면
비밀번호 폼만). 두 방식은 토큰이 다릅니다.

- **Cognito**: 서버가 `USER_PASSWORD_AUTH` 를 대행하고, access token 이 API bearer 입니다.
- **OIDC(Entra ID)**: 브라우저가 Authorization Code + PKCE 를 직접 수행합니다(`lib/oidc.ts`,
  순수 로직은 `lib/oidcCore.mjs`). discovery → authorize → `/auth/callback` 에서 code 교환 →
  `POST /api/auth/session` 으로 역할을 받습니다. **id_token** 이 bearer 이고, 저장된 토큰에
  `providerId` 가 붙어 리프레시가 같은 토큰 엔드포인트로 갑니다.

역할 판정은 서버가 하고 브라우저는 응답을 그대로 그립니다. 프론트의 역할 게이트는 UI
편의일 뿐입니다.

## 백엔드 프록시

브라우저는 `http://localhost:3000`만 열면 됩니다. 프론트가 `/api/*`, `/threads/*`를
백엔드(FastAPI)로 프록시합니다.

- 프록시는 **Route Handler**에서 업스트림 응답 본문을 그대로 흘려보냅니다.
- `next.config`의 `rewrites()`도 `middleware`도 쓰지 않습니다. rewrite 목적지는
  빌드 시점에 고정되므로 ECS가 런타임에 주입하는 `BACKEND_ORIGIN`이 무시되고,
  middleware의 rewrite는 SSE 본문을 버퍼링해 스트리밍이 끊깁니다. Route Handler만이
  런타임 origin과 무버퍼 스트리밍을 동시에 만족합니다.

## 환경 변수

| 변수 | 용도 | 기본값 |
| --- | --- | --- |
| `BACKEND_ORIGIN` | 프록시가 향하는 백엔드 origin (서버 사이드, ECS가 런타임 주입) | `http://localhost:8000` |
| `SANDBOX_ORIGIN` | MCP Apps 샌드박스 출처. 런타임에 결정되므로 브라우저가 `/client-config`에서 조회 | 없음 |
| `NEXT_PUBLIC_API_URL` | 클라이언트 fetch의 API base. 비우면 same-origin(프록시 경유) | `""` |
| `NEXT_PUBLIC_AUTH_DISABLED` | `true`면 로그인 화면을 건너뛰고 local admin 세션으로 렌더. 서버 `AUTH_ENFORCED=false`와 짝. 빌드 시 인라인되므로 배포 빌드엔 절대 금지 | 없음 |

배포 환경에서는 프록시가 same-origin으로 동작하므로 `NEXT_PUBLIC_API_URL`은 보통
비워 둡니다. 백엔드가 다른 호스트에 있을 때만 지정합니다.

## MCP Apps 샌드박스 출처

MCP Apps(SEP-1865)는 앱 HTML을 **다른 출처의 iframe**에서 렌더링해야 합니다(spec MUST).
로컬에서는 `run.sh`가 `SANDBOX_PORT`(기본 3001)로 두 번째 dev 서버를 띄웁니다 —
포트가 다르면 브라우저 출처가 다릅니다. CSP는 앱마다 요청 시점에 계산되어 HTTP
헤더로 나갑니다.

## 검증

```bash
yarn tsc --noEmit            # 타입 검사
yarn lint                    # eslint (Next 16 에는 `next lint` 가 없어 eslint 를 직접 부릅니다)
find src -name '*.test.mjs' -print0 | xargs -0 node --test   # 순수 로직 (node 내장 테스트)
```

> 포매터는 쓰지 않습니다. 커밋된 스타일은 손으로 맞춘 것이라 prettier 를 돌리면
> 되돌아갑니다. 검증은 `tsc` + `eslint` + 위 테스트로만 합니다.

## 배포 후 주의

- 인증서 없이 올린 ALB 는 평문 HTTP 라 secure-context 전용 API(`crypto.randomUUID`,
  `navigator.clipboard`)가 없습니다. 대체 헬퍼(`randomId()` / `copyText()`)를 씁니다.
  Entra ID 로그인(PKCE 의 `crypto.subtle`)은 HTTPS 에서만 동작합니다.
- 인증이 필요한 라우트는 `<img src>`에 직접 넣지 않습니다 (배포에서 401).
