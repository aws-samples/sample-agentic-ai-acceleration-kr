// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import { NextRequest, NextResponse } from 'next/server';
import { GetTraceSummariesCommand } from '@aws-sdk/client-xray';
import { getXRayClient, gatewayFilterExpression, clampTraceRange, normalizeSummary } from '@/lib/tool-gateway/xray';
import { TOOL_GATEWAY_ENABLED, TRACE_TIME_RANGES } from '@/lib/tool-gateway/constants';

export const dynamic = 'force-dynamic';

export async function GET(request: NextRequest) {
  if (!TOOL_GATEWAY_ENABLED) return NextResponse.json({ traces: [], count: 0, status: 'Disabled' });

  try {
    const range = clampTraceRange(request.nextUrl.searchParams.get('timeRange'));
    const minutes = TRACE_TIME_RANGES[range].minutes;
    const endTime = new Date();
    const startTime = new Date(endTime.getTime() - minutes * 60_000);
    const resp = await getXRayClient().send(new GetTraceSummariesCommand({
      StartTime: startTime, EndTime: endTime,
      TimeRangeType: 'Event',
      FilterExpression: gatewayFilterExpression(),
    }));
    const traces = (resp.TraceSummaries ?? []).map(normalizeSummary);
    traces.sort((a, b) => b.startTime - a.startTime);
    return NextResponse.json({ traces, count: traces.length, status: traces.length ? 'Complete' : 'NoData' });
  } catch (error) {
    const name = error instanceof Error ? error.name : '';
    if (name === 'InvalidRequestException' || name === 'AccessDeniedException' || name === 'ThrottledException') {
      return NextResponse.json({ traces: [], count: 0, status: 'Unavailable', note: `X-Ray traces unavailable: ${error instanceof Error ? error.message : String(error)}` });
    }
    console.error('Failed to query X-Ray trace summaries:', error);
    return NextResponse.json({ error: 'Failed to query traces', details: error instanceof Error ? error.message : String(error) }, { status: 502 });
  }
}
