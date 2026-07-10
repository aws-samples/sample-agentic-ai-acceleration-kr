// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Logout route — clears the admin_jwt cookie and redirects to '/'.
 *
 * Mirrors the proto/host handling in dev-login/route.ts so the Set-Cookie
 * `secure` flag matches the actual connection scheme (HTTP vs HTTPS) and
 * the redirect URL preserves the original Host header (avoids 0.0.0.0 in
 * containerized envs). The redirect lands on '/' which middleware then
 * sends to '/api/auth/dev-login' since the cookie is gone.
 */

import { NextRequest, NextResponse } from 'next/server';

export async function POST(request: NextRequest): Promise<NextResponse> {
  const host = request.headers.get('host') || 'localhost:3000';
  const proto = request.headers.get('x-forwarded-proto') || 'http';

  const response = NextResponse.redirect(`${proto}://${host}/`, { status: 303 });
  response.cookies.set('admin_jwt', '', {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: 0,
    secure: proto === 'https',
  });
  return response;
}
