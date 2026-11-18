// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Dev-login route — MVP authentication bypass.
 *
 * Enabled only when DEV_LOGIN_ENABLED === 'true' **and** APP_ENV is a development
 * environment (lib/devLogin.ts). Anywhere else the route answers 503.
 * Issues a simple base64-encoded dev JWT (NOT cryptographically signed).
 */

import { NextRequest, NextResponse } from 'next/server';
import { devLoginState, DEV_LOGIN_ENVIRONMENTS, type DevLoginState } from '@/lib/devLogin';
import { redirectRelative } from '@/lib/redirect';
import { ROLE_COOKIE_NAME } from '@/lib/roleCookie';
import { UserRole } from '@/types/enums';

const DEV_COOKIE_MAX_AGE = 60 * 60 * 24; // 24 hours in seconds

type DisabledState = Extract<DevLoginState, { enabled: false }>;

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function buildDevToken(role: string): string {
  const payload = {
    user_id: 'dev-admin',
    email: 'admin@dev.local',
    display_name: 'Dev Admin',
    role,
    team_id: null,
    department_id: null,
    issued_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + DEV_COOKIE_MAX_AGE * 1000).toISOString(),
  };

  // dev JWT format: dev.<base64url-payload>.sig  (MVP only — not signed)
  const payloadB64 = Buffer.from(JSON.stringify(payload)).toString('base64url');
  return `dev.${payloadB64}.sig`;
}

/**
 * 꺼져 있을 때의 응답. 예전엔 `new NextResponse(null, { status: 404 })` 였다 —
 * middleware 가 여기로 리다이렉트했으므로 **prod 사용자는 본문 없는 404 에서 끝났고**
 * 브라우저에도 서버 로그에도 단서가 0이었다(A3). 상태코드도 404 는 오답이다:
 * 라우트는 존재하고 정책상 비활성일 뿐이라 503 이 맞다.
 */
const SSO_HINT =
  'Use the SSO entry point at /api/auth/login. ' +
  'If SSO is not configured either, /api/auth/login will name the missing OIDC_* variables.';

/**
 * 왜 꺼져 있는지를 구분해서 말한다. 스위치는 켰는데 환경 때문에 거부된 경우를
 * "DEV_LOGIN_ENABLED 가 꺼져 있다" 고 답하면 운영자는 이미 켠 스위치를 다시 켜려고 한다.
 */
function disabledMessage(state: DisabledState): string {
  if (state.reason === 'environment') {
    return (
      `Dev login is refused: DEV_LOGIN_ENABLED is "true" but APP_ENV="${state.appEnv}" is not a ` +
      `development environment (${DEV_LOGIN_ENVIRONMENTS.join(', ')}). ` +
      SSO_HINT
    );
  }
  return `Dev login is disabled in this environment (DEV_LOGIN_ENABLED != "true"). ${SSO_HINT}`;
}

function disabledReasonHtml(state: DisabledState): string {
  if (state.reason === 'environment') {
    return (
      `<p><code>DEV_LOGIN_ENABLED=true</code> 이지만 <code>APP_ENV=${escapeHtml(state.appEnv)}</code> 는 ` +
      `개발 환경(${DEV_LOGIN_ENVIRONMENTS.join(', ')})이 아니므로 개발용 로그인을 거부합니다.</p>`
    );
  }
  return '<p>이 환경은 <code>DEV_LOGIN_ENABLED != "true"</code> 이므로 개발용 로그인이 비활성입니다.</p>';
}

const disabledHtml = (state: DisabledState): string => `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Dev login disabled — Admin UI</title>
  <style>
    body { font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }
    .card { background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); max-width: 560px; }
    h1 { font-size: 1.25rem; margin: 0 0 1rem; color: #111; }
    p { font-size: 0.875rem; color: #444; line-height: 1.6; }
    code { background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.85rem; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Dev login is disabled</h1>
    ${disabledReasonHtml(state)}
    <p>SSO 진입점: <a href="/api/auth/login">/api/auth/login</a> — OIDC 가 설정돼 있지 않으면 그 페이지가 <strong>비어 있는 환경변수 이름</strong>을 그대로 알려줍니다.</p>
  </div>
</body>
</html>`;

function disabledHtmlResponse(state: DisabledState): NextResponse {
  return new NextResponse(disabledHtml(state), {
    status: 503,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}

const LOGIN_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Dev Login — Admin UI</title>
  <style>
    body { font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }
    .card { background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); width: 320px; }
    h1 { font-size: 1.25rem; margin: 0 0 1.5rem; color: #111; }
    label { display: block; font-size: 0.875rem; font-weight: 500; margin-bottom: 0.375rem; color: #444; }
    select { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.875rem; }
    button { margin-top: 1rem; width: 100%; padding: 0.625rem; background: #2563eb; color: white; border: none; border-radius: 4px; font-size: 0.875rem; font-weight: 500; cursor: pointer; }
    button:hover { background: #1d4ed8; }
    .notice { margin-top: 1rem; font-size: 0.75rem; color: #888; text-align: center; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Dev Login</h1>
    <form method="POST" action="/api/auth/dev-login">
      <label for="role">Role</label>
      <select id="role" name="role">
        <option value="ADMIN">ADMIN</option>
        <option value="TEAM_LEADER">TEAM_LEADER</option>
      </select>
      <button type="submit">Sign in</button>
    </form>
    <p class="notice">Development mode only — not for production use.</p>
  </div>
</body>
</html>`;

export async function GET(): Promise<NextResponse> {
  const state = devLoginState();
  if (!state.enabled) {
    return disabledHtmlResponse(state);
  }

  return new NextResponse(LOGIN_HTML, {
    status: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  const state = devLoginState();
  if (!state.enabled) {
    // POST 는 이 파일의 다른 에러들과 같은 JSON 형태를 유지한다(아래 400 들과 동일 idiom).
    return NextResponse.json(
      { error: disabledMessage(state), sso_entry_point: '/api/auth/login' },
      { status: 503, headers: { 'Cache-Control': 'no-store' } },
    );
  }

  let role: string | null = null;

  try {
    const contentType = request.headers.get('content-type') ?? '';
    if (contentType.includes('application/x-www-form-urlencoded')) {
      const text = await request.text();
      const params = new URLSearchParams(text);
      role = params.get('role');
    } else {
      const body = (await request.json()) as { role?: string };
      role = body.role ?? null;
    }
  } catch {
    return NextResponse.json({ error: 'Invalid request body' }, { status: 400 });
  }

  const validRoles: string[] = [UserRole.ADMIN, UserRole.TEAM_LEADER];
  if (!role || !validRoles.includes(role)) {
    return NextResponse.json(
      { error: `Invalid role. Must be one of: ${validRoles.join(', ')}` },
      { status: 400 }
    );
  }

  const token = buildDevToken(role);

  // 리다이렉트는 상대 경로라 host 가 필요 없다(lib/redirect.ts). proto 는 아래 쿠키의
  // Secure 판정에 쓴다.
  const proto = request.headers.get('x-forwarded-proto') || 'http';
  const redirectResponse = redirectRelative('/');
  redirectResponse.cookies.set('admin_jwt', token, {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: DEV_COOKIE_MAX_AGE,
    // secure: true 로 하면 HTTP 환경에선 브라우저가 쿠키를 저장하지 못해 무한 리다이렉트.
    // NODE_ENV 대신 실제 연결 scheme 을 보는 게 정확 — ALB 가 HTTP 종단이면 'http'.
    secure: proto === 'https',
  });
  // dev 토큰은 role 클레임이 있어 역할 쿠키를 보지 않는다. 앞 세션의 쿠키는 남기지 않는다.
  redirectResponse.cookies.set(ROLE_COOKIE_NAME, '', {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: 0,
    secure: proto === 'https',
  });

  return redirectResponse;
}
