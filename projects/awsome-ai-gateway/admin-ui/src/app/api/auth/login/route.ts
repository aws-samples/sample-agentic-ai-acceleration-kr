// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/auth/login — **유일한 로그인 진입점**. 환경에 따라 세 갈래로 갈린다.
 *
 * 왜 이 라우트가 새로 필요했나 (A3):
 *   middleware.ts 의 redirectToLogin 이 예전엔 `/api/auth/dev-login` 을 직접 가리켰다.
 *   그런데 그 라우트는 DEV_LOGIN_ENABLED !== 'true' 이면 `new NextResponse(null, {status: 404})`
 *   를 돌려준다. prod 는 values-eks-fargate-prod.yaml 에서 adminUi.env.DEV_LOGIN_ENABLED="false"
 *   이므로, **prod 브라우저는 / 로 들어오면 307 → 본문 없는 404 에서 끝난다**. 그 앞에
 *   ALB 인증도 없다(deployment/ 전체에 authenticate-oidc/authenticate-cognito 어노테이션 0건,
 *   templates/common/ingress.yaml 은 healthcheck 어노테이션만 붙인다). 즉 prod 에는
 *   로그인 경로가 **아예 없었다** — admin_jwt 쿠키를 세팅하는 코드가 트리 전체에서
 *   dev-login/route.ts 한 곳뿐이다(`grep -rn "admin_jwt" admin-ui/src`).
 *
 * 분기:
 *   1) OIDC 필수 3개(OIDC_CLIENT_ID / OIDC_AUTHORIZE_URL / OIDC_TOKEN_URL)가 모두 채워짐
 *      → provider 의 authorize 엔드포인트로 302 (response_type=code + state + PKCE S256)
 *   2) OIDC 변수가 하나도 없고 DEV_LOGIN_ENABLED=true → dev 폼으로 넘긴다 (dev 무변경)
 *   3) 그 외(둘 다 없음 / OIDC 반쪽 설정) → **읽을 수 있는 503**. 어떤 env 가 비었는지
 *      이름으로 찍는다. 본문 없는 404 로는 절대 끝내지 않는다.
 *
 * ⚠️ 안전 속성: 새 env 를 하나도 안 넣으면 (1)(3) 은 발동하지 않고 (2) 만 남는다 —
 *    즉 dev 는 오늘과 완전히 동일하게 동작한다. tests/unit/authLoginFlow.test.ts 가 못 박는다.
 *
 * ⚠️ env 이름은 admin-api 규약(`OIDC_*`, admin-api/src/app/core/config.py:72-96)을 그대로
 *    따른다. "OIDC_ISSUER_URL 이 비면 OIDC 비활성" 이라는 admin-api 의 관용구를 여기서는
 *    OIDC_CLIENT_ID 가 맡는다(브라우저 코드 플로우에는 client_id 가 필수).
 *
 * ⚠️ authorize/token URL 을 issuer 에서 유도하지 않는 이유: provider 마다 경로가 다르고,
 *    Cognito 는 **호스트 자체가 다르다**. issuer 는 https://cognito-idp.<region>.amazonaws.com/<poolId>
 *    인데 authorize 는 hosted UI 도메인 https://<domain>.auth.<region>.amazoncognito.com/oauth2/authorize
 *    이다. Keycloak 은 <issuer>/protocol/openid-connect/auth. 유도 규칙을 하나 고르면 나머지가
 *    조용히 깨지므로 두 URL 은 명시 입력만 받는다.
 */

import { NextRequest, NextResponse } from 'next/server';
import { redirectRelative } from '@/lib/redirect';

// 로그인 진입점 — 캐시/정적최적화 금지. 여기서 만든 state/PKCE 가 캐시되면 모든 사용자가
// 같은 state·verifier 를 쓰게 되어 CSRF 보호가 무력화된다.
export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/** state/verifier 임시 쿠키 TTL(초). authorize 왕복 한 번만 버티면 된다. */
const TEMP_COOKIE_MAX_AGE = 600;

/** 채워져야 OIDC 가 켜지는 env. 하나라도 비면 (3) 분기에서 이름을 그대로 노출한다. */
const OIDC_REQUIRED_VARS = ['OIDC_CLIENT_ID', 'OIDC_AUTHORIZE_URL', 'OIDC_TOKEN_URL'] as const;

/** 있으면 쓰고 없으면 기본값으로 도는 env. "OIDC 를 건드렸는지" 판정에는 포함한다. */
const OIDC_OPTIONAL_VARS = [
  'OIDC_CLIENT_SECRET',
  'OIDC_REDIRECT_URI',
  'OIDC_SCOPES',
  'OIDC_COOKIE_TOKEN',
] as const;

const DEFAULT_SCOPES = 'openid email profile';

function env(name: string): string {
  return (process.env[name] ?? '').trim();
}

/**
 * 실제 연결 scheme. dev-login/route.ts:113 · logout/route.ts:18 과 같은 이유로
 * request.url 이 아니라 헤더를 본다 — 컨테이너 안에서 request.url 은 0.0.0.0 으로 풀려
 * 원래 호스트를 잃는다.
 *
 * ⚠️ 여기만 한 가지 더 한다: 프록시가 체인이면 x-forwarded-proto 가 "https,http" 처럼
 *    콤마 목록으로 온다. 그대로 쓰면 proto === 'https' 비교가 실패해 (a) Secure 플래그가
 *    빠지고 (b) redirect_uri 가 http:// 로 조립돼 IdP 의 정확일치 검사에서 튕긴다.
 *    첫 토큰만 취한다.
 */
function schemeAndHost(request: NextRequest): { proto: string; host: string } {
  const host = request.headers.get('host') || 'localhost:3000';
  const rawProto = request.headers.get('x-forwarded-proto') || 'http';
  return { proto: rawProto.split(',')[0].trim() || 'http', host };
}

function b64url(bytes: Uint8Array): string {
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** RFC 7636 code_verifier / state — 32바이트 CSPRNG → base64url 43자. */
function randomToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return b64url(bytes);
}

/** code_challenge = base64url(SHA-256(verifier)) — S256 만 쓴다(plain 금지). */
async function s256(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return b64url(new Uint8Array(digest));
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * 설정이 안 된 상태를 **읽히게** 만든다. 원래 증상(본문 없는 404)이 다시 나오면
 * 운영자는 브라우저 개발자도구에서조차 아무 단서를 못 얻는다.
 */
function notConfiguredResponse(missing: readonly string[], devHint: boolean): NextResponse {
  const items = missing.map((v) => `<li><code>${escapeHtml(v)}</code></li>`).join('');
  const devBlock = devHint
    ? `<p>DEV_LOGIN_ENABLED=true 이므로 개발용 로그인은 <a href="/api/auth/dev-login">/api/auth/dev-login</a> 에서 쓸 수 있습니다.</p>`
    : `<p>개발용 로그인(<code>DEV_LOGIN_ENABLED</code>)도 꺼져 있습니다. 위 변수를 채우는 것이 유일한 로그인 경로입니다.</p>`;

  const html = `<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>SSO not configured — Admin UI</title>
  <style>
    body { font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }
    .card { background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); max-width: 640px; }
    h1 { font-size: 1.25rem; margin: 0 0 1rem; color: #111; }
    code { background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.85rem; }
    ul { margin: 0.5rem 0 1rem; }
    p { font-size: 0.875rem; color: #444; line-height: 1.6; }
  </style>
</head>
<body>
  <div class="card">
    <h1>SSO (OIDC) is not configured</h1>
    <p>이 배포에는 로그인 경로가 설정되지 않았습니다. admin-ui 컨테이너에 다음 환경변수를 채우면 SSO 로그인이 활성화됩니다 (Helm: <code>adminUi.env</code>).</p>
    <ul>${items}</ul>
    ${devBlock}
    <p>Cognito 라면 <code>OIDC_AUTHORIZE_URL</code>/<code>OIDC_TOKEN_URL</code> 은 issuer 가 아니라 hosted UI 도메인(<code>https://&lt;domain&gt;.auth.&lt;region&gt;.amazoncognito.com/oauth2/{authorize,token}</code>) 입니다.</p>
  </div>
</body>
</html>`;

  return new NextResponse(html, {
    status: 503,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const missing = OIDC_REQUIRED_VARS.filter((v) => !env(v));
  const devLoginEnabled = process.env.DEV_LOGIN_ENABLED === 'true';

  // (2) OIDC 를 아예 안 건드렸고 dev-login 이 켜져 있으면 dev 폼으로. 오늘과 동일한 동작.
  if (missing.length === OIDC_REQUIRED_VARS.length && devLoginEnabled) {
    const touchedOptional = OIDC_OPTIONAL_VARS.some((v) => env(v));
    if (!touchedOptional) {
      // 같은 오리진 → 상대 Location. nextUrl 은 컨테이너에서 0.0.0.0 으로 푼다(lib/redirect.ts).
      // 307 — GET→GET 이라 메서드 보존이 자연스럽다(기존 동작과 동일).
      const res = redirectRelative('/api/auth/dev-login', { status: 307 });
      res.headers.set('Cache-Control', 'no-store');
      return res;
    }
  }

  // (3) 반쪽 설정 또는 아무 설정 없음 → 어떤 변수가 비었는지 이름으로 알려준다.
  //     반쪽 설정에서 dev 폼으로 조용히 흘리면 운영자는 SSO 가 왜 안 붙는지 영원히 모른다.
  if (missing.length > 0) {
    return notConfiguredResponse(missing, devLoginEnabled);
  }

  // (1) 정상 경로 — Authorization Code + PKCE(S256).
  const { proto, host } = schemeAndHost(request);
  const redirectUri = env('OIDC_REDIRECT_URI') || `${proto}://${host}/api/auth/callback`;

  const state = randomToken();
  const verifier = randomToken();
  const challenge = await s256(verifier);

  let authorizeUrl: URL;
  try {
    authorizeUrl = new URL(env('OIDC_AUTHORIZE_URL'));
  } catch {
    // 값이 있는데 URL 이 아니면 그것도 설정 오류다 — 크래시(500)가 아니라 읽히는 503.
    return notConfiguredResponse(['OIDC_AUTHORIZE_URL (유효한 절대 URL 이 아님)'], devLoginEnabled);
  }

  authorizeUrl.searchParams.set('response_type', 'code');
  authorizeUrl.searchParams.set('client_id', env('OIDC_CLIENT_ID'));
  authorizeUrl.searchParams.set('redirect_uri', redirectUri);
  authorizeUrl.searchParams.set('scope', env('OIDC_SCOPES') || DEFAULT_SCOPES);
  authorizeUrl.searchParams.set('state', state);
  authorizeUrl.searchParams.set('code_challenge', challenge);
  authorizeUrl.searchParams.set('code_challenge_method', 'S256');

  // 302 — authorize 로의 이동은 GET 이며 재사용될 값이 아니다.
  const res = NextResponse.redirect(authorizeUrl.toString(), { status: 302 });

  // state/verifier 는 httpOnly 로만 보관한다. SameSite=Lax 가 필수 — Strict 로 두면
  // IdP 에서 돌아오는 크로스사이트 리다이렉트에 쿠키가 실리지 않아 콜백이 100% 실패한다.
  const cookieOpts = {
    httpOnly: true,
    sameSite: 'lax' as const,
    path: '/',
    maxAge: TEMP_COOKIE_MAX_AGE,
    // dev-login/route.ts:118 과 동일 판단 — HTTP 종단(ALB 평문)에서 Secure 를 붙이면
    // 브라우저가 쿠키를 저장하지 못해 콜백이 state 불일치로 죽는다.
    secure: proto === 'https',
  };
  res.cookies.set('oidc_state', state, cookieOpts);
  res.cookies.set('oidc_verifier', verifier, cookieOpts);
  res.headers.set('Cache-Control', 'no-store');

  return res;
}
