// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { NextRequest, NextResponse } from 'next/server';
import { BatchGetTracesCommand } from '@aws-sdk/client-xray';
import { getXRayClient, buildSpanList, xrayIdToLogTraceId } from '@/lib/tool-gateway/xray';
import { fetchLogsForTrace } from '@/lib/tool-gateway/gateway-logs';
import { TOOL_GATEWAY_ENABLED } from '@/lib/tool-gateway/constants';

export const dynamic = 'force-dynamic';

export async function GET(
  _request: NextRequest,
  { params }: { params: { id: string } }
) {
  const { id } = params;

  if (!TOOL_GATEWAY_ENABLED) {
    return NextResponse.json({ traceId: id, spans: [], logs: [], status: 'Disabled' });
  }

  try {
    const resp = await getXRayClient().send(new BatchGetTracesCommand({ TraceIds: [id] }));
    const trace = resp.Traces?.[0];
    if (!trace) {
      return NextResponse.json({ traceId: id, spans: [], logs: [], status: 'NotFound' });
    }
    const spans = buildSpanList(trace.Segments);
    spans.sort((a, b) => a.startTime - b.startTime);

    // Join gateway vended logs by trace_id so the detail view can show
    // request/response bodies and the real error message (X-Ray spans only
    // carry error_type/codes). The log trace_id is the X-Ray id with the
    // version prefix and dashes stripped.
    const logTraceId = xrayIdToLogTraceId(id);
    const logs = logTraceId ? await fetchLogsForTrace(logTraceId).catch(() => []) : [];

    return NextResponse.json({ traceId: id, spans, logs, status: 'Complete' });
  } catch (error) {
    const name = error instanceof Error ? error.name : '';
    if (
      name === 'InvalidRequestException' ||
      name === 'AccessDeniedException' ||
      name === 'ThrottledException'
    ) {
      return NextResponse.json({
        traceId: id,
        spans: [],
        logs: [],
        status: 'Unavailable',
        note: error instanceof Error ? error.message : String(error),
      });
    }
    console.error('Failed to fetch X-Ray trace:', error);
    return NextResponse.json(
      { error: 'Failed to fetch trace', details: error instanceof Error ? error.message : String(error) },
      { status: 502 }
    );
  }
}
