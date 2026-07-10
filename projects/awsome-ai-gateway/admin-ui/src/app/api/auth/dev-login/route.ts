// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Dev-login route — MVP authentication bypass.
 *
 * Disabled in production (DEV_LOGIN_ENABLED !== 'true').
 * Issues a simple base64-encoded dev JWT (NOT cryptographically signed).
 */

import { NextRequest, NextResponse } from 'next/server';
import { UserRole } from '@/types/enums';

const DEV_COOKIE_MAX_AGE = 60 * 60 * 24; // 24 hours in seconds

function isDisabled(): boolean {
  return process.env.DEV_LOGIN_ENABLED !== 'true';
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
  if (isDisabled()) {
    return new NextResponse(null, { status: 404 });
  }

  return new NextResponse(LOGIN_HTML, {
    status: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (isDisabled()) {
    return new NextResponse(null, { status: 404 });
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

  // Build redirect URL from Host header to avoid 0.0.0.0 in Docker
  const host = request.headers.get('host') || 'localhost:3000';
  const proto = request.headers.get('x-forwarded-proto') || 'http';
  const redirectResponse = NextResponse.redirect(`${proto}://${host}/`);
  redirectResponse.cookies.set('admin_jwt', token, {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: DEV_COOKIE_MAX_AGE,
    // secure: true 로 하면 HTTP 환경에선 브라우저가 쿠키를 저장하지 못해 무한 리다이렉트.
    // NODE_ENV 대신 실제 연결 scheme 을 보는 게 정확 — ALB 가 HTTP 종단이면 'http'.
    secure: proto === 'https',
  });

  return redirectResponse;
}
