// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { NextResponse } from 'next/server';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET() {
  const cookieStore = cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;

  const res = await fetch(`${ADMIN_API_URL}/admin/users/teams`, {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
    },
  });

  if (!res.ok) {
    return NextResponse.json({ error: '팀 목록 조회 실패' }, { status: res.status });
  }

  const data = (await res.json()) as { items?: Array<{ id: string; name: string }> };
  const teams = (data.items ?? []).map((t) => ({ id: t.id, name: t.name }));
  return NextResponse.json(teams);
}
