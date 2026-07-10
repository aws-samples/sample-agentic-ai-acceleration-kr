// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { NextRequest, NextResponse } from 'next/server';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET(req: NextRequest) {
  const cookieStore = cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;

  const sp = req.nextUrl.searchParams;
  const period = sp.get('period') ?? '';

  const upstream = new URL(`${ADMIN_API_URL}/admin/dashboard/client-share`);
  if (period) upstream.searchParams.set('period', period);

  const res = await fetch(upstream.toString(), {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
    },
  });

  if (!res.ok) {
    return NextResponse.json({ error: '앱별 비용 점유율 조회 실패' }, { status: res.status });
  }
  const data = await res.json();
  return NextResponse.json(data);
}
