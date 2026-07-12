// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import { NextResponse } from 'next/server';
import { TOOL_GATEWAY_ENABLED } from '@/lib/tool-gateway/constants';
import { listTools } from '@/lib/tool-gateway/mcp';

export const dynamic = 'force-dynamic';

export async function GET() {
  if (!TOOL_GATEWAY_ENABLED) return NextResponse.json({ tools: [], status: 'Disabled' });
  try {
    const tools = await listTools();
    return NextResponse.json({ tools, status: 'Complete' });
  } catch {
    return NextResponse.json({ tools: [], status: 'Unavailable' });
  }
}
