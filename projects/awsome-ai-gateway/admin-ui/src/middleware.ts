// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Next.js Middleware — SEC-01 pattern.
 *
 * Applies security headers to all responses and enforces JWT-based
 * authentication for non-API page routes.
 */

import { NextRequest, NextResponse } from 'next/server';
import { parseJWT } from '@/lib/auth';
import { checkPagePermission } from '@/lib/auth';

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
    pathname.startsWith('/cli') ||
    pathname === '/403'
  ) {
    return response;
  }

  const jwtCookie = request.cookies.get('admin_jwt');

  // No JWT present — redirect to dev-login (MVP) or 401
  if (!jwtCookie?.value) {
    // Use nextUrl (preserves original host header) instead of request.url (may resolve to 0.0.0.0 in Docker)
    const devLoginUrl = request.nextUrl.clone();
    devLoginUrl.pathname = '/api/auth/dev-login';
    const redirectResponse = NextResponse.redirect(devLoginUrl);
    applySecurityHeaders(redirectResponse);
    return redirectResponse;
  }

  // JWT present — parse and check permissions
  try {
    const session = parseJWT(jwtCookie.value);

    const hasPermission = checkPagePermission(pathname, session.role);

    if (!hasPermission) {
      const forbiddenUrl = request.nextUrl.clone();
      forbiddenUrl.pathname = '/403';
      const redirectResponse = NextResponse.redirect(forbiddenUrl);
      applySecurityHeaders(redirectResponse);
      return redirectResponse;
    }
  } catch {
    // Malformed JWT — treat as unauthenticated
    const devLoginUrl = request.nextUrl.clone();
    devLoginUrl.pathname = '/api/auth/dev-login';
    const redirectResponse = NextResponse.redirect(devLoginUrl);
    applySecurityHeaders(redirectResponse);
    return redirectResponse;
  }

  return response;
}
