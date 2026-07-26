// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

export const dynamic = 'force-dynamic';

export async function GET() {
  return Response.json({ status: 'ok', service: 'agentic-text-to-sql-ui' }, { status: 200 });
}
