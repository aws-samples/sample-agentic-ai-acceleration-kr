// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** ALB 헬스체크용 — 인증 불필요. */

export const dynamic = 'force-dynamic';

export async function GET() {
  return Response.json({ ok: true, service: 'agentic-text-to-sql-admin' }, { status: 200 });
}
