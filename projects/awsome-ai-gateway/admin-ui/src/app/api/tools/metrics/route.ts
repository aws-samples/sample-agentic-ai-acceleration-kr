// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import { NextRequest, NextResponse } from 'next/server';
import { TOOL_GATEWAY_ENABLED, TIME_RANGES, type TimeRangeKey } from '@/lib/tool-gateway/constants';
import { fetchMetrics, type MetricsResponse } from '@/lib/tool-gateway/cloudwatch';

export const dynamic = 'force-dynamic';

// Helper to return empty metrics payload for disabled or error states.
// UI treats non-Complete status as empty, so minimal payload is sufficient.
function emptyMetricsResponse(
  range: TimeRangeKey,
  status: 'Disabled' | 'Unavailable',
): MetricsResponse {
  return {
    timeRange: range,
    period: 0,
    invocations: [],
    latency: [],
    errors: [],
    overhead: [],
    tools: [],
    toolTrend: [],
    toolTrendSeries: [],
    auth: {
      inboundSuccess: 0,
      inboundFailure: 0,
      inboundFailureByType: [],
      apiKeySuccess: 0,
      apiKeyFailure: 0,
      apiKeySuccessByProvider: [],
      apiKeyFailureByProvider: [],
    },
    summary: {
      total_invocations: 0,
      total_system_errors: 0,
      total_user_errors: 0,
      total_throttles: 0,
      error_rate: 0,
      avg_latency: 0,
      avg_target_exec: 0,
    },
    status,
  };
}

export async function GET(request: NextRequest) {
  // Parse and clamp timeRange parameter
  const param = request.nextUrl.searchParams.get('timeRange');
  const range: TimeRangeKey = (param && param in TIME_RANGES ? param : '24h') as TimeRangeKey;

  // Guard: if feature flag is off, return disabled response
  if (!TOOL_GATEWAY_ENABLED) {
    return NextResponse.json(emptyMetricsResponse(range, 'Disabled'));
  }

  // Fetch metrics; fetchMetrics already handles known AWS errors and returns Unavailable,
  // so we only catch rethrown unknown errors.
  try {
    const data = await fetchMetrics(range);
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(emptyMetricsResponse(range, 'Unavailable'));
  }
}
