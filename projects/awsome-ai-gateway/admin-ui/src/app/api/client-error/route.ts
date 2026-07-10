// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { NextRequest } from 'next/server';

export async function POST(request: NextRequest) {
  let body: Record<string, unknown> = {};

  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    // Malformed JSON — log what we have
  }

  console.error(
    JSON.stringify({
      level: 'error',
      source: 'client',
      ...body,
      timestamp: new Date().toISOString(),
    })
  );

  return new Response(null, { status: 204 });
}
