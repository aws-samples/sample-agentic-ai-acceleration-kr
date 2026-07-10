// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// 실시간 RPM 사용량(§60.9) 클라이언트 폴링용 프록시 — RateLimitConfigPanel 이
// 10초마다 호출. admin-api /admin/rate-limits/usage/{scope}/{scope_id} 로 전달.

import { cookies } from 'next/headers';
import { NextRequest, NextResponse } from 'next/server';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET(req: NextRequest) {
  const jwt = cookies().get('admin_jwt')?.value;
  const sp = req.nextUrl.searchParams;
  const scope = sp.get('scope') ?? '';
  const scopeId = sp.get('scope_id') ?? '';
  if (!scope || !scopeId) {
    return NextResponse.json({ available: false, reason: 'missing params' }, { status: 400 });
  }

  const upstream = `${ADMIN_API_URL}/admin/rate-limits/usage/${encodeURIComponent(scope)}/${encodeURIComponent(scopeId)}`;
  try {
    const res = await fetch(upstream, {
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
      },
    });
    if (!res.ok) {
      return NextResponse.json({ available: false, reason: `upstream ${res.status}` }, { status: 200 });
    }
    return NextResponse.json(await res.json());
  } catch {
    // fail-soft: 실시간 조회 실패가 RL 설정 화면을 막지 않게 200 + available:false.
    return NextResponse.json({ available: false, reason: 'fetch error' }, { status: 200 });
  }
}
