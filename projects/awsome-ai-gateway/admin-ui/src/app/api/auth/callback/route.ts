// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/auth/callback — OIDC Authorization Code 콜백.
 *
 * /api/auth/login 이 심어 둔 `oidc_state`/`oidc_verifier` httpOnly 쿠키와 짝을 맞춰
 * code 를 token 엔드포인트에서 교환하고, admin-api 가 검증할 토큰을 `admin_jwt` 쿠키에
 * 심은 뒤 `/` 로 보낸다.
 *
 * ⚠️ state 검사가 이 파일의 보안 핵심이다. 검사를 빼면 공격자가 피해자 브라우저에
 *    자기 code 를 밀어 넣어 **피해자를 공격자 계정으로 로그인**시킬 수 있다(로그인 CSRF).
 *    실패 시에는 admin_jwt 를 절대 세팅하지 않고 임시 쿠키를 정리한다.
 *
 * ⚠️ 쿠키에 넣는 토큰: 기본 id_token(사용자 표시용 email/name 클레임을 갖고 있고,
 *    admin-ui/src/lib/auth.ts:44 의 parseJWT 가 그것을 읽는다). provider 가 id_token 을
 *    안 주면 access_token 으로 폴백하고, OIDC_COOKIE_TOKEN=access_token 으로 강제도 된다.
 *    admin-api 는 Helm `adminApi.oidc.adminUiClientId` 가 이 콘솔의 OIDC_CLIENT_ID 와 같으면
 *    그 토큰을 issuer 의 JWKS 로 검증한다(admin-api/src/app/core/auth.py
 *    `_verify_console_sso_token`). 비어 있으면 `auth.admin_jwt_configs` 에 IdP 서명키(PEM)를
 *    직접 등록한 경우에만 통과한다.
 *
 * ⚠️ 쿠키를 굽기 전에 admin-api `GET /admin/me` 로 실효 역할과 팀을 묻고 `admin_role` 쿠키로 기억한다
 *    (lib/identity.ts, lib/roleCookie.ts). IdP 토큰에는 role 클레임이 없어서, 이게 없던 동안 이메일
 *    관리자와 팀 리더는 API 는 통과하는데 모든 화면이 /403 이었다. admin-api 가 거절하면(401/403)
 *    세션을 만들지 않고 고칠 방법을 적은 페이지로 끝낸다.
 *
 * ⚠️ request.url 대신 request.nextUrl / Host 헤더를 쓴다 — dev-login/route.ts:112 와 같은
 *    이유로 컨테이너 안에서 request.url 은 0.0.0.0 으로 풀린다.
 */

import { NextRequest, NextResponse } from 'next/server';
import { redirectRelative } from '@/lib/redirect';
import { parseJWT, isSessionExpired } from '@/lib/auth';
import { fetchIdentity, type IdentityResult } from '@/lib/identity';
import { ROLE_COOKIE_NAME, encodeRoleCookie } from '@/lib/roleCookie';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/** exp/expires_in 을 둘 다 못 읽었을 때만 쓰는 최후 폴백(초). 짧게 둔다. */
const FALLBACK_COOKIE_MAX_AGE = 3600;
/** 쿠키 수명 상한(초). IdP 가 비정상적으로 긴 exp 를 줘도 브라우저에 하루 이상 남기지 않는다. */
const MAX_COOKIE_MAX_AGE = 12 * 60 * 60;
/** 쿠키 수명 하한(초). 시계 오차로 0/음수가 나오면 Set-Cookie 가 즉시 삭제 지시가 된다.
 *
 * ⚠️ 이 값이 무한 리다이렉트를 막아주지는 **않는다.** middleware 의 판정 근거는 Max-Age 가
 *    아니라 토큰의 `exp`(30초 skew, src/lib/auth.ts isSessionExpired) 다 — 이미 만료된
 *    토큰이면 Max-Age 를 크게 줘도 다음 요청에서 쿠키가 지워지고 다시 로그인으로 돈다.
 *    루프를 실제로 막는 건 아래 GET 안의 parseJWT + isSessionExpired 선검사다. */
const MIN_COOKIE_MAX_AGE = 60;

function env(name: string): string {
  return (process.env[name] ?? '').trim();
}

/** login/route.ts 의 schemeAndHost 와 동일 — route.ts 는 핸들러 외 export 를 두지 않는다. */
function schemeAndHost(request: NextRequest): { proto: string; host: string } {
  const host = request.headers.get('host') || 'localhost:3000';
  const rawProto = request.headers.get('x-forwarded-proto') || 'http';
  return { proto: rawProto.split(',')[0].trim() || 'http', host };
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** 길이 비교로 조기 종료하지 않는 상수시간 비교 — state 는 비밀값이다. */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

/** 임시 쿠키를 응답에서 지운다. 실패 경로에서도 반드시 호출한다(재사용 방지). */
function clearTempCookies(res: NextResponse, secure: boolean): void {
  for (const name of ['oidc_state', 'oidc_verifier']) {
    res.cookies.set(name, '', {
      httpOnly: true,
      sameSite: 'lax',
      path: '/',
      maxAge: 0,
      secure,
    });
  }
}

/**
 * 실패를 읽히게 만든다. 여기서 본문 없는 응답을 주면 원래의 A3 증상(단서 0)이 재현된다.
 * `detail` 은 provider 가 준 문자열이라 반드시 escape 한다.
 */
function failure(
  status: number,
  title: string,
  detail: string,
  secure: boolean,
): NextResponse {
  const html = `<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sign-in failed — Admin UI</title>
  <style>
    body { font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }
    .card { background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); max-width: 640px; }
    h1 { font-size: 1.25rem; margin: 0 0 1rem; color: #b91c1c; }
    p { font-size: 0.875rem; color: #444; line-height: 1.6; }
    code { background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.85rem; }
  </style>
</head>
<body>
  <div class="card">
    <h1>${escapeHtml(title)}</h1>
    <p>${escapeHtml(detail)}</p>
    <p><a href="/api/auth/login">다시 로그인 시도</a></p>
  </div>
</body>
</html>`;

  const res = new NextResponse(html, {
    status,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
  clearTempCookies(res, secure);
  return res;
}

/** 서명 검증 없이 payload 만 읽는다(검증은 admin-api 담당). 실패하면 null. */
function readExpSeconds(token: string): number | null {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64.padEnd(b64.length + ((4 - (b64.length % 4)) % 4), '=');
    const payload = JSON.parse(atob(padded)) as { exp?: unknown };
    const exp = payload.exp;
    return typeof exp === 'number' && Number.isFinite(exp) ? exp : null;
  } catch {
    return null;
  }
}

/**
 * 쿠키 Max-Age 를 **토큰 자신의 만료**에서 뽑는다. 하드코딩 상수(dev-login 의 24h)를 쓰면
 * 토큰이 죽은 뒤에도 쿠키가 남아 middleware 는 통과시키고 admin-api 호출만 전부 401 이 되는,
 * 바로 그 원래 증상이 재현된다(admin-ui/src/lib/auth.ts:79 주석 참조).
 *
 * 우선순위: 토큰의 exp → 토큰응답의 expires_in → FALLBACK_COOKIE_MAX_AGE.
 */
function cookieMaxAge(token: string, expiresIn: unknown): number {
  const exp = readExpSeconds(token);
  let seconds: number | null = null;

  if (exp !== null) {
    seconds = exp - Math.floor(Date.now() / 1000);
  } else if (typeof expiresIn === 'number' && Number.isFinite(expiresIn)) {
    seconds = Math.floor(expiresIn);
  } else if (typeof expiresIn === 'string' && /^\d+$/.test(expiresIn)) {
    seconds = Number(expiresIn);
  }

  if (seconds === null) return FALLBACK_COOKIE_MAX_AGE;
  return Math.min(MAX_COOKIE_MAX_AGE, Math.max(MIN_COOKIE_MAX_AGE, seconds));
}

/**
 * admin-api 의 거절을 받은 사람이 할 일로 옮긴다. code 는 admin-api 의 error.message 다
 * (admin-api/src/app/core/auth.py `_resolve_idp_identity`).
 */
function deniedMessage(denied: Extract<IdentityResult, { kind: 'denied' }>): {
  title: string;
  detail: string;
} {
  if (denied.status === 401) {
    return {
      title: 'Admin API rejected the session token',
      detail:
        'admin-api 가 이 콘솔 클라이언트에 발급된 토큰을 인정하지 않습니다. Helm 값 ' +
        'adminApi.oidc.adminUiClientId 가 adminUi.env.OIDC_CLIENT_ID 와 같은지 확인하세요.' +
        (denied.code ? ` (admin-api: ${denied.code})` : ''),
    };
  }
  switch (denied.code) {
    case 'user_not_provisioned':
      return {
        title: 'This account is not registered in the gateway',
        detail:
          '게이트웨이에 아직 이 사용자가 없습니다. gateway-cli login 으로 로그인해 키를 한 번 발급받은 뒤 ' +
          '다시 로그인하세요.',
      };
    case 'user_deactivated':
      return {
        title: 'This account is deactivated',
        detail:
          '게이트웨이에서 이 계정이 비활성화돼 있습니다. IdP 에서 계정을 다시 활성화하고, 관리자에게 ' +
          'Cognito 동기화를 실행해 달라고 요청하세요.',
      };
    case 'required_group_missing':
      return {
        title: 'This account is not in the required group',
        detail:
          '이 계정은 콘솔에 필요한 그룹에 속해 있지 않습니다. 관리자에게 adminApi.oidc.requiredGroup 으로 ' +
          '설정된 그룹에 이 계정을 추가해 달라고 요청하세요.',
      };
    default:
      return {
        title: 'Admin API refused the sign-in',
        detail: `admin-api 가 로그인을 거절했습니다(HTTP 403: ${denied.code || '사유 없음'}).`,
      };
  }
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const { proto, host } = schemeAndHost(request);
  const secure = proto === 'https';
  const params = request.nextUrl.searchParams;

  // ── provider 가 거절한 경우 — 크래시가 아니라 읽히는 페이지 ──
  const providerError = params.get('error');
  if (providerError) {
    return failure(
      400,
      'Identity provider rejected the sign-in',
      `${providerError}: ${params.get('error_description') ?? '(no description)'}`,
      secure,
    );
  }

  // ── state 검사(보안 핵심) ──
  const state = params.get('state');
  const stateCookie = request.cookies.get('oidc_state')?.value;
  if (!state || !stateCookie || !timingSafeEqual(state, stateCookie)) {
    return failure(
      400,
      'Invalid sign-in state',
      'state 파라미터가 쿠키와 일치하지 않습니다. 로그인 요청이 위조됐거나(로그인 CSRF) ' +
        '임시 쿠키가 만료됐습니다(10분). /api/auth/login 에서 처음부터 다시 시작하세요.',
      secure,
    );
  }

  const code = params.get('code');
  if (!code) {
    return failure(400, 'Missing authorization code', 'provider 가 code 를 주지 않았습니다.', secure);
  }

  const verifier = request.cookies.get('oidc_verifier')?.value;
  if (!verifier) {
    return failure(
      400,
      'Missing PKCE verifier',
      'oidc_verifier 쿠키가 없습니다. 임시 쿠키가 만료됐을 가능성이 큽니다(10분).',
      secure,
    );
  }

  const tokenUrl = env('OIDC_TOKEN_URL');
  const clientId = env('OIDC_CLIENT_ID');
  if (!tokenUrl || !clientId) {
    return failure(
      503,
      'SSO is not configured',
      'OIDC_TOKEN_URL / OIDC_CLIENT_ID 가 비어 있습니다. Helm adminUi.env 를 확인하세요.',
      secure,
    );
  }

  const redirectUri = env('OIDC_REDIRECT_URI') || `${proto}://${host}/api/auth/callback`;
  const clientSecret = env('OIDC_CLIENT_SECRET');

  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: redirectUri,
    code_verifier: verifier,
    // public client(secret 없음)는 client_id 를 body 로 보내야 한다. confidential client 에도
    // 붙여 보내는 것은 RFC 6749 §2.3.1 상 무해하다.
    client_id: clientId,
  });

  const headers: Record<string, string> = {
    'Content-Type': 'application/x-www-form-urlencoded',
    Accept: 'application/json',
  };
  if (clientSecret) {
    // confidential client — RFC 6749 가 권장하는 HTTP Basic. Cognito 는 secret 이 붙은
    // app client 에 대해 이 형태를 요구한다.
    headers.Authorization = `Basic ${btoa(`${clientId}:${clientSecret}`)}`;
  }

  let tokenResponse: Response;
  try {
    tokenResponse = await fetch(tokenUrl, {
      method: 'POST',
      headers,
      body: body.toString(),
      cache: 'no-store',
    });
  } catch (e) {
    return failure(
      502,
      'Token endpoint unreachable',
      `OIDC_TOKEN_URL(${tokenUrl}) 에 접속할 수 없습니다: ${e instanceof Error ? e.message : String(e)}`,
      secure,
    );
  }

  if (!tokenResponse.ok) {
    // provider 의 error 코드만 옮긴다. 본문 전체를 그대로 뿌리면 토큰이 섞여 나올 수 있다.
    let providerCode = '(no error field)';
    try {
      const errJson = (await tokenResponse.json()) as { error?: unknown };
      if (typeof errJson.error === 'string') providerCode = errJson.error;
    } catch {
      /* JSON 이 아니면 상태코드만으로 보고한다 */
    }
    return failure(
      502,
      'Token exchange failed',
      `token 엔드포인트가 HTTP ${tokenResponse.status} (${providerCode}) 를 반환했습니다. ` +
        'redirect_uri 등록값과 client 종류(public/confidential)를 확인하세요.',
      secure,
    );
  }

  let tokens: { id_token?: unknown; access_token?: unknown; expires_in?: unknown };
  try {
    tokens = (await tokenResponse.json()) as typeof tokens;
  } catch {
    return failure(502, 'Token response is not JSON', 'token 엔드포인트 응답을 해석할 수 없습니다.', secure);
  }

  const preferAccess = env('OIDC_COOKIE_TOKEN') === 'access_token';
  const idToken = typeof tokens.id_token === 'string' ? tokens.id_token : '';
  const accessToken = typeof tokens.access_token === 'string' ? tokens.access_token : '';
  const cookieToken = preferAccess ? accessToken || idToken : idToken || accessToken;

  if (!cookieToken) {
    return failure(
      502,
      'No usable token returned',
      'token 응답에 id_token/access_token 이 모두 없습니다. scope 에 openid 가 포함됐는지 확인하세요.',
      secure,
    );
  }

  // ⚠️ 굽기 전에 **middleware 가 쓰는 그 파서로** 실제 읽히는지 확인한다.
  //
  // 없으면 이런 무한 루프가 실제로 난다(적대적 검증에서 재현):
  //   `OIDC_COOKIE_TOKEN=access_token` + access_token 이 불투명한 IdP(Okta org 서버),
  //   또는 scope 에 `openid` 가 없어 id_token 이 안 와서 access_token 으로 폴백 →
  //   그 문자열을 admin_jwt 로 굽는다 → 브라우저가 `/` 요청 → middleware 의 parseJWT 가
  //   throw → catch 에서 쿠키 제거 + `/api/auth/login` 리다이렉트 → IdP 세션이 아직
  //   살아 있으니 즉시 콜백으로 되돌아옴 → 다시 같은 쿠키 → **영원히 반복**.
  //   화면에는 아무 진단도 안 나온다 — 이 라우트가 애초에 없애려던 그 막다른 길이다.
  //
  // 그래서 실패는 루프가 아니라 원인이 적힌 페이지로 끝낸다. 검증은 서명 검증이 아니라
  // **형식/클레임 가독성**만 본다(서명은 admin-api 가 OIDC 검증기로 확인한다) — 여기서
  // 막아야 하는 것은 "admin-ui 가 읽을 수 없는 문자열을 쿠키에 넣는 것" 이다.
  let parsedSession: ReturnType<typeof parseJWT>;
  try {
    parsedSession = parseJWT(cookieToken);
  } catch (e) {
    const which = preferAccess ? 'access_token' : 'id_token';
    return failure(
      502,
      'Token is not a readable JWT',
      `${which} 을 admin-ui 가 해석할 수 없습니다(${(e as Error).message}). ` +
        'IdP 가 불투명(opaque) 토큰을 주는 경우입니다 — OIDC_SCOPES 에 openid 를 넣어 ' +
        'id_token 을 받고, OIDC_COOKIE_TOKEN 은 비워 두거나 id_token 으로 두세요. ' +
        '이 검사가 없으면 로그인이 무한 리다이렉트로 빠집니다.',
      secure,
    );
  }

  // ⚠️ 만료 검사도 여기서 한다. middleware 는 exp 를 30초 skew 로 보고 만료면 쿠키를
  //    지우고 로그인으로 되돌린다(src/lib/auth.ts isSessionExpired) — 이미 만료/시계
  //    스큐된 토큰을 굽으면 Max-Age 를 아무리 크게 줘도 다음 요청에서 즉시 지워지며
  //    같은 루프가 된다. (MIN_COOKIE_MAX_AGE 는 이 루프를 막아주지 못한다 — Max-Age 는
  //    middleware 의 판정 근거가 아니다.)
  if (isSessionExpired(parsedSession)) {
    return failure(
      502,
      'Token is already expired',
      '발급된 토큰의 exp 가 이미 지났습니다(서버 시계 불일치일 수 있습니다). ' +
        '그대로 쿠키에 넣으면 다음 요청에서 만료로 판정돼 로그인이 무한 반복됩니다.',
      secure,
    );
  }

  // 역할은 admin-api 에 묻는다 — 쿠키를 굽기 전에. 거절된 사람에게 모든 화면이 /403 이거나
  // 모든 호출이 401 인 세션을 남기지 않는다.
  const identity = await fetchIdentity(cookieToken);
  if (identity.kind === 'denied') {
    const { title, detail } = deniedMessage(identity);
    return failure(403, title, detail, secure);
  }
  if (identity.kind === 'unavailable' && !parsedSession.role) {
    // 토큰이 역할을 주면 오늘처럼 들여보낸다 — 옛 admin-api(404)나 장애가 관리자 그룹 구성원을
    // 잠그면 안 된다. 역할을 모르는 사람은 들여보내도 모든 화면이 /403 이라 여기서 끝낸다.
    return failure(
      503,
      'Could not confirm your role',
      `역할을 확인하려고 admin-api 에 물었지만 답을 받지 못했습니다(${identity.reason}). ` +
        '잠시 뒤 다시 로그인하세요.',
      secure,
    );
  }

  const maxAge = cookieMaxAge(cookieToken, tokens.expires_in);

  // 303 — POST/GET 구분 없이 GET 으로 이동시킨다(logout/route.ts:19 와 같은 이유).
  // 상대 Location — CloudFront/ALB 뒤에서 Host 헤더가 내부 오리진일 수 있다(lib/redirect.ts).
  const res = redirectRelative('/');
  // admin_role 은 admin_jwt 와 수명·속성이 같다 — 토큰보다 오래 남을 이유가 없다.
  const sessionCookie = { httpOnly: true, sameSite: 'lax' as const, path: '/', maxAge, secure };
  res.cookies.set('admin_jwt', cookieToken, sessionCookie);
  if (identity.kind === 'ok') {
    res.cookies.set(
      ROLE_COOKIE_NAME,
      await encodeRoleCookie(identity.role, cookieToken, identity.teamId),
      sessionCookie,
    );
  }
  clearTempCookies(res, secure);
  res.headers.set('Cache-Control', 'no-store');
  return res;
}
