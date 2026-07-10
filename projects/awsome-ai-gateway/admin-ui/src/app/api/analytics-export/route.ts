// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET(request: NextRequest) {
  const cookieStore = cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;

  const searchParams = request.nextUrl.searchParams.toString();

  const res = await fetch(`${ADMIN_API_URL}/admin/analytics/export?${searchParams}`, {
    cache: 'no-store',
    headers: {
      ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
    },
  });

  if (!res.ok) {
    return new Response('Export failed', { status: res.status });
  }

  const contentType = res.headers.get('content-type') || 'application/octet-stream';
  const contentDisposition = res.headers.get('content-disposition') || 'attachment';
  const body = await res.blob();

  return new Response(body, {
    headers: {
      'Content-Type': contentType,
      'Content-Disposition': contentDisposition,
    },
  });
}
