// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { NextRequest } from 'next/server';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET(
  _request: NextRequest,
  { params }: { params: { os: string; arch: string } }
) {
  const res = await fetch(`${ADMIN_API_URL}/cli/download/${params.os}/${params.arch}`, {
    cache: 'no-store',
  });

  if (!res.ok) {
    return new Response('Download not available', { status: res.status });
  }

  const blob = await res.blob();
  const ext = params.os === 'windows' ? 'zip' : 'tar.gz';
  const filename = res.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1]
    || `gateway-cli-${params.os}-${params.arch}.${ext}`;
  const mediaType = params.os === 'windows' ? 'application/zip' : 'application/gzip';

  return new Response(blob, {
    headers: {
      'Content-Type': mediaType,
      'Content-Disposition': `attachment; filename="${filename}"`,
    },
  });
}
