// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Next.js Middleware — SEC-01 pattern.
 *
 * Applies security headers to all responses and enforces JWT-based
 * authentication for non-API page routes.
 */

import { NextRequest, NextResponse } from 'next/server';
import { parseJWT } from '@/lib/auth';
import { checkPagePermission, isSessionExpired } from '@/lib/auth';

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon\\.ico).*)'],
};

const SECURITY_HEADERS: Record<string, string> = {
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
  'Content-Security-Policy':
    "default-src 'self'; script-src 'self' 'unsafe-eval' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:",
};

function applySecurityHeaders(response: NextResponse): NextResponse {
  for (const [key, value] of Object.entries(SECURITY_HEADERS)) {
    response.headers.set(key, value);
  }
  return response;
}

/**
 * 로그인 화면으로 되돌린다. `clearCookie` 면 남아 있는 admin_jwt 도 지운다.
 *
 * ⚠️ 목적지가 `/api/auth/dev-login` 이 아니라 `/api/auth/login` 인 이유(A3):
 *    dev-login 라우트는 DEV_LOGIN_ENABLED !== 'true' 이면 본문 없는 404 를 준다. prod 는
 *    그 값이 "false"(values-eks-fargate-prod.yaml adminUi.env) 이므로 예전 목적지로는
 *    **prod 브라우저가 307 → 빈 404 에서 끝났다** — ALB 인증도 없어 로그인 경로가 0개였다.
 *    `/api/auth/login` 은 환경을 보고 OIDC authorize / dev 폼 / 읽히는 503 으로 갈라주는
 *    단일 진입점이다(app/api/auth/login/route.ts). 이 경로는 아래 `/api/` 예외에 걸려
 *    미인증으로도 도달 가능하다 — 그래야 무한 리다이렉트가 안 난다.
 */
/**
 * 미들웨어에서 같은 오리진 경로로 보낸다 — **절대 URL** 로 만들어야 한다.
 *
 * ⚠️ 미들웨어에서는 상대 Location(`lib/redirect.ts` 의 redirectRelative)을 쓰면 안 된다.
 *    Next 14 의 미들웨어 어댑터는 응답의 Location 을 `new NextURL(location)` 으로 base 없이
 *    다시 파싱한다. 상대 경로면 거기서 `TypeError: Invalid URL`(ERR_INVALID_URL)이 나고
 *    **로그인 리다이렉트를 타는 모든 페이지가 500** 이 된다(2026-10-14 dev·prod 실측,
 *    next 14.2.29: `/`, `/login` 500 · `/api/health` 200). 라우트 핸들러의 응답은 이
 *    어댑터를 거치지 않으므로 거기서는 redirectRelative 가 그대로 맞다.
 *
 * 0.0.0.0 / ALB 호스트 문제는 생기지 않는다: 어댑터는 요청과 같은 호스트의 URL 만 그대로
 * 두고, 서버가 보내기 전에 같은 오리진 리다이렉트를 상대 경로로 바꾼다. 실측으로 이
 * 방식(`NextResponse.redirect(new URL(..., request.nextUrl))`)을 쓰던 이미지는 ALB 에서
 * `location: /api/auth/dev-login` — 상대 경로 — 를 돌려줬다. 브라우저는 여전히 자신이 요청한
 * URL(CloudFront 도메인) 기준으로 해석한다.
 */
function redirectSameOrigin(request: NextRequest, path: string): NextResponse {
  return NextResponse.redirect(new URL(path, request.nextUrl), 307);
}

function redirectToLogin(request: NextRequest, clearCookie: boolean): NextResponse {
  const redirectResponse = redirectSameOrigin(request, '/api/auth/login');
  if (clearCookie) {
    // 만료/손상된 자격증명은 응답에서 즉시 제거한다 — 안 지우면 다음 요청도 같은 쿠키로
    // 다시 이 분기를 타고, 사용자는 못 쓰는 쿠키를 계속 들고 다닌다.
    redirectResponse.cookies.delete('admin_jwt');
  }
  applySecurityHeaders(redirectResponse);
  return redirectResponse;
}

export async function middleware(request: NextRequest): Promise<NextResponse> {
  const { pathname } = request.nextUrl;

  // Always start with a pass-through response so we can attach headers
  const response = NextResponse.next();
  applySecurityHeaders(response);

  // Public routes — no auth required.
  // '/403' must be public, otherwise an authenticated user without permission
  // for the current path gets redirected to /403, which itself fails the
  // permission check, and bounces back to /403 → ERR_TOO_MANY_REDIRECTS.
  if (
    pathname.startsWith('/api/') ||
    pathname === '/403'
  ) {
    return response;
  }

  const jwtCookie = request.cookies.get('admin_jwt');

  // No JWT present — 환경에 맞는 로그인 진입점(/api/auth/login)으로 보낸다.
  if (!jwtCookie?.value) {
    return redirectToLogin(request, false);
  }

  // JWT present — parse, check expiry, then check permissions
  try {
    const session = parseJWT(jwtCookie.value);

    // ⚠️ 만료 검사. 예전엔 이 분기가 아예 없어서 만료된 admin_jwt 로도 페이지가 열렸고,
    //    서버 컴포넌트의 모든 admin-api 호출만 401 이 됐다 — 화면 전체가 '—' 와
    //    fetchFailed 로 덮이고 로그인으로 돌아갈 방법이 없었다. 만료는 미인증과 같게
    //    취급한다(쿠키 제거 + 로그인 리다이렉트).
    //    isSessionExpired 는 exp 클레임이 없거나 해석 불가면 false 를 준다 — dev 토큰에는
    //    숫자 exp 가 없어서, 여기서 만료로 몰면 dev 로그인이 무한 리다이렉트가 된다.
    if (isSessionExpired(session)) {
      return redirectToLogin(request, true);
    }

    const hasPermission = checkPagePermission(pathname, session.role);

    if (!hasPermission) {
      const redirectResponse = redirectSameOrigin(request, '/403');
      applySecurityHeaders(redirectResponse);
      return redirectResponse;
    }
  } catch {
    // Malformed JWT — treat as unauthenticated (그리고 못 쓰는 쿠키는 지운다)
    return redirectToLogin(request, true);
  }

  return response;
}
