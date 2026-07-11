// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import {
  CloudWatchClient,
  GetMetricDataCommand,
  type MetricDataQuery,
  type MetricDataResult,
} from '@aws-sdk/client-cloudwatch';
import { AWS_REGION, GATEWAY_ARN, GATEWAY_ID, TIME_RANGES, type TimeRangeKey } from './constants';

const NAMESPACE = 'AWS/Bedrock-AgentCore';

// Common dimension set for gateway-wide aggregates.
const baseDimensions = [
  { Name: 'Resource', Value: GATEWAY_ARN },
  { Name: 'Operation', Value: 'InvokeGateway' },
  { Name: 'Protocol', Value: 'MCP' },
];

const client = new CloudWatchClient({ region: AWS_REGION });

export interface MetricsResponse {
  timeRange: TimeRangeKey;
  period: number;
  invocations: Array<{ timestamp: string; value: number }>;
  latency: Array<{ timestamp: string; p50: number; p90: number; p99: number }>;
  errors: Array<{ timestamp: string; system_errors: number; user_errors: number }>;
  overhead: Array<{ timestamp: string; total: number; target: number; gateway: number }>;
  tools: Array<{
    name: string;
    label: string;
    invocations: number;
    latency: number;
    targetExec: number;
    systemErrors: number;
    userErrors: number;
    errors: number;
    errorRate: number;
    overhead: number;
  }>;
  toolTrend: Array<Record<string, number | string>>;
  toolTrendSeries: Array<{ name: string; label: string }>;
  auth: {
    inboundSuccess: number;
    inboundFailure: number;
    inboundFailureByType: Array<{ exceptionType: string; count: number }>;
    apiKeySuccess: number;
    apiKeyFailure: number;
    apiKeySuccessByProvider: Array<{ provider: string; count: number }>;
    apiKeyFailureByProvider: Array<{ label: string; count: number }>;
  };
  summary: {
    total_invocations: number;
    total_system_errors: number;
    total_user_errors: number;
    total_throttles: number;
    error_rate: number;
    avg_latency: number;
    avg_target_exec: number;
  };
  // 'Disabled' = feature flag off; the API route short-circuits before calling
  // AWS and returns this so the UI can render a dormant state.
  status: 'Complete' | 'NoData' | 'Unavailable' | 'Disabled';
}

function metricQuery(
  id: string,
  metricName: string,
  stat: string,
  period: number
): MetricDataQuery {
  return {
    Id: id,
    MetricStat: {
      Metric: {
        Namespace: NAMESPACE,
        MetricName: metricName,
        Dimensions: baseDimensions,
      },
      Period: period,
      Stat: stat,
    },
    ReturnData: true,
  };
}

// A CloudWatch SEARCH expression query. Lets the gateway's tools be discovered
// dynamically (one CloudWatch series per `Name` dimension) without hardcoding
// the engine list, which changes as targets are added/removed.
function searchQuery(
  id: string,
  schema: string,
  filters: string,
  stat: string,
  period: number
): MetricDataQuery {
  return {
    Id: id,
    Expression: `SEARCH('{${schema}} ${filters}', '${stat}', ${period})`,
    ReturnData: true,
  };
}

// Escape a double quote for safe interpolation inside a SEARCH expression literal.
function q(value: string): string {
  return value.replace(/"/g, '\\"');
}

// Choose a period that yields a reasonable number of buckets for the range.
function periodForRange(minutes: number): number {
  if (minutes <= 60) return 300; // 5 min
  if (minutes <= 360) return 900; // 15 min
  if (minutes <= 1440) return 3600; // 1 hour
  return 21600; // 6 hours
}

// `Name` dimension values look like `serper___web_search`; derive a friendly
// label (engine + tool) for display.
function prettyToolName(name: string): string {
  // The gateway's built-in semantic tool-search (enabled via search_type=SEMANTIC);
  // not a real search engine, so flag it as such.
  if (name === 'x_amz_bedrock_agentcore_search') return '게이트웨이 시맨틱 검색 (내장)';
  const [engine, ...rest] = name.split('___');
  const tool = rest.join('___');
  if (!tool) return name;
  return `${engine} · ${tool.replace(/_/g, ' ')}`;
}

// The CloudWatch series Label for a multi-dimension SEARCH result is the
// space-joined dimension values in the schema's declared order:
//   "<Method> <Name> <Operation> <Protocol> <Resource> <MetricName>"
// e.g. "tools/call serper___web_search InvokeGateway MCP arn:... Invocations".
// The `Name` value is the token immediately before "InvokeGateway" (Operation).
// We can't rely on a "___" heuristic — the built-in semantic-search tool is
// named `x_amz_bedrock_agentcore_search` (no triple underscore), which that
// heuristic missed, splitting it across one row per metric query.
function toolNameFromLabel(label: string | undefined): string {
  if (!label) return 'unknown';
  const tokens = label.split(' ');
  const opIdx = tokens.indexOf('InvokeGateway');
  if (opIdx > 0) return tokens[opIdx - 1];
  // Fallback: the token containing "___" (web_search-style tools).
  return tokens.find((t) => t.includes('___')) ?? label;
}

// First token of a SEARCH label (used for ProviderName / ExceptionType series
// where the relevant dimension value leads the label).
function firstToken(label: string | undefined): string {
  if (!label) return 'unknown';
  return label.split(' ')[0] || label;
}

const sumValues = (r?: MetricDataResult) =>
  (r?.Values ?? []).reduce((a, b) => a + b, 0);
const avgValues = (r?: MetricDataResult) => {
  const v = r?.Values ?? [];
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0;
};

function emptyMetrics(range: TimeRangeKey, period: number = 0, status: 'Unavailable' | 'NoData' = 'Unavailable'): MetricsResponse {
  return {
    timeRange: range,
    period,
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

export async function fetchMetrics(range: TimeRangeKey): Promise<MetricsResponse> {
  // Check if gateway is configured
  if (!GATEWAY_ARN) {
    return emptyMetrics(range, 0, 'Unavailable');
  }

  const timeConfig = TIME_RANGES[range] ?? TIME_RANGES['1h'];
  const period = periodForRange(timeConfig.minutes);

  const endTime = new Date();
  const startTime = new Date(endTime.getTime() - timeConfig.minutes * 60_000);

  const arn = q(GATEWAY_ARN);
  // SEARCH schema (dimension key sets) per metric family, confirmed against
  // `cloudwatch list-metrics` for this gateway.
  const toolSchema = 'AWS/Bedrock-AgentCore,Method,Name,Operation,Protocol,Resource';
  const inboundSchema = 'AWS/Bedrock-AgentCore,ExceptionType,ResourceId';
  const inboundOkSchema = 'AWS/Bedrock-AgentCore,ResourceId';
  const keyOkSchema =
    'AWS/Bedrock-AgentCore,ProviderName,TokenVault,WorkloadIdentity,WorkloadIdentityDirectory';
  const keyFailSchema =
    'AWS/Bedrock-AgentCore,ExceptionType,ProviderName,TokenVault,WorkloadIdentity,WorkloadIdentityDirectory';

  const toolFilter = `Resource="${arn}" Method="tools/call"`;

  const queries: MetricDataQuery[] = [
    // --- Gateway-wide aggregates (existing) ---
    metricQuery('invocations', 'Invocations', 'Sum', period),
    metricQuery('latency_p50', 'Latency', 'p50', period),
    metricQuery('latency_p90', 'Latency', 'p90', period),
    metricQuery('latency_p99', 'Latency', 'p99', period),
    metricQuery('system_errors', 'SystemErrors', 'Sum', period),
    metricQuery('user_errors', 'UserErrors', 'Sum', period),
    metricQuery('throttles', 'Throttles', 'Sum', period),
    // --- Gateway overhead: total Latency vs backend TargetExecutionTime ---
    metricQuery('latency_avg', 'Latency', 'Average', period),
    metricQuery('target_exec_avg', 'TargetExecutionTime', 'Average', period),
    // --- Per-tool breakdown (dynamic discovery via SEARCH) ---
    searchQuery('tool_invocations', toolSchema, `MetricName="Invocations" ${toolFilter}`, 'Sum', period),
    searchQuery('tool_latency', toolSchema, `MetricName="Latency" ${toolFilter}`, 'Average', period),
    searchQuery('tool_target_exec', toolSchema, `MetricName="TargetExecutionTime" ${toolFilter}`, 'Average', period),
    searchQuery('tool_sys_errors', toolSchema, `MetricName="SystemErrors" ${toolFilter}`, 'Sum', period),
    searchQuery('tool_user_errors', toolSchema, `MetricName="UserErrors" ${toolFilter}`, 'Sum', period),
    // --- Inbound (Cognito M2M) authorization health ---
    searchQuery('inbound_ok', inboundOkSchema, `MetricName="InboundAuthorizationSuccess" ResourceId="${arn}"`, 'Sum', period),
    searchQuery('inbound_fail', inboundSchema, `MetricName="InboundAuthorizationFailure" ResourceId="${arn}"`, 'Sum', period),
    // --- Outbound API key / token-vault health (by ProviderName) ---
    searchQuery('apikey_ok', keyOkSchema, `MetricName="ApiKeyFetchSuccess" WorkloadIdentity="${q(GATEWAY_ID)}"`, 'Sum', period),
    searchQuery('apikey_fail', keyFailSchema, `MetricName="ApiKeyFetchFailures" WorkloadIdentity="${q(GATEWAY_ID)}"`, 'Sum', period),
  ];

  try {
    const response = await client.send(
      new GetMetricDataCommand({
        StartTime: startTime,
        EndTime: endTime,
        MetricDataQueries: queries,
        ScanBy: 'TimestampAscending',
      })
    );

    // SEARCH expressions return MANY series sharing one query Id; group them.
    const single = new Map<string, MetricDataResult>();
    const grouped = new Map<string, MetricDataResult[]>();
    for (const result of response.MetricDataResults ?? []) {
      if (!result.Id) continue;
      // Heuristic: SEARCH-backed ids may repeat; collect all, plus keep the
      // first as the singleton view for the simple aggregate queries.
      if (!single.has(result.Id)) single.set(result.Id, result);
      const arr = grouped.get(result.Id) ?? [];
      arr.push(result);
      grouped.set(result.Id, arr);
    }

    const series = (id: string) => single.get(id) ?? { Timestamps: [], Values: [] };

    const buildPointMap = (id: string) => {
      const s = series(id);
      const m = new Map<string, number>();
      (s.Timestamps ?? []).forEach((t, i) => m.set(t.toISOString(), (s.Values ?? [])[i] ?? 0));
      return m;
    };

    // Union of all timestamps across the gateway-wide series, sorted ascending.
    const allTimestamps = new Set<string>();
    for (const id of ['invocations', 'latency_p50', 'system_errors', 'user_errors', 'latency_avg', 'target_exec_avg']) {
      for (const t of series(id).Timestamps ?? []) allTimestamps.add(t.toISOString());
    }
    const timeline = Array.from(allTimestamps).sort();

    const invMap = buildPointMap('invocations');
    const p50Map = buildPointMap('latency_p50');
    const p90Map = buildPointMap('latency_p90');
    const p99Map = buildPointMap('latency_p99');
    const sysMap = buildPointMap('system_errors');
    const userMap = buildPointMap('user_errors');
    const throttleMap = buildPointMap('throttles');
    const latAvgMap = buildPointMap('latency_avg');
    const tgtAvgMap = buildPointMap('target_exec_avg');

    const invocations = timeline.map((ts) => ({ timestamp: ts, value: invMap.get(ts) ?? 0 }));
    const latency = timeline.map((ts) => ({
      timestamp: ts,
      p50: Math.round(p50Map.get(ts) ?? 0),
      p90: Math.round(p90Map.get(ts) ?? 0),
      p99: Math.round(p99Map.get(ts) ?? 0),
    }));
    const errors = timeline.map((ts) => ({
      timestamp: ts,
      system_errors: sysMap.get(ts) ?? 0,
      user_errors: userMap.get(ts) ?? 0,
    }));
    // Gateway overhead = total request latency − backend target execution time.
    const overhead = timeline.map((ts) => {
      const total = Math.round(latAvgMap.get(ts) ?? 0);
      const target = Math.round(tgtAvgMap.get(ts) ?? 0);
      return {
        timestamp: ts,
        total,
        target,
        gateway: Math.max(0, total - target),
      };
    });

    // --- Per-tool breakdown table ---
    const toolStats = new Map<
      string,
      { invocations: number; latency: number; targetExec: number; systemErrors: number; userErrors: number }
    >();
    const ensureTool = (name: string) => {
      if (!toolStats.has(name)) {
        toolStats.set(name, { invocations: 0, latency: 0, targetExec: 0, systemErrors: 0, userErrors: 0 });
      }
      return toolStats.get(name)!;
    };
    for (const r of grouped.get('tool_invocations') ?? []) {
      ensureTool(toolNameFromLabel(r.Label)).invocations = sumValues(r);
    }
    for (const r of grouped.get('tool_latency') ?? []) {
      ensureTool(toolNameFromLabel(r.Label)).latency = Math.round(avgValues(r));
    }
    for (const r of grouped.get('tool_target_exec') ?? []) {
      ensureTool(toolNameFromLabel(r.Label)).targetExec = Math.round(avgValues(r));
    }
    for (const r of grouped.get('tool_sys_errors') ?? []) {
      ensureTool(toolNameFromLabel(r.Label)).systemErrors = sumValues(r);
    }
    for (const r of grouped.get('tool_user_errors') ?? []) {
      ensureTool(toolNameFromLabel(r.Label)).userErrors = sumValues(r);
    }
    const tools = Array.from(toolStats.entries())
      .map(([name, s]) => ({
        name,
        label: prettyToolName(name),
        ...s,
        errors: s.systemErrors + s.userErrors,
        errorRate: s.invocations > 0 ? (s.systemErrors + s.userErrors) / s.invocations : 0,
        overhead: Math.max(0, s.latency - s.targetExec),
      }))
      .sort((a, b) => b.invocations - a.invocations);

    // --- Per-tool invocation trend (time series, for the stacked-area chart) ---
    // The tool_invocations SEARCH already returns one time series per tool; the
    // table above collapses each to a sum. Here we keep the per-bucket values so
    // the UI can show how call volume shifts across tools over time. We surface
    // the busiest few tools individually and fold the long tail into "기타".
    const TOP_TOOL_TREND = 6;
    const topTools = tools.slice(0, TOP_TOOL_TREND);
    const topNames = new Set(topTools.map((t) => t.name));
    const hasOthers = tools.length > TOP_TOOL_TREND;

    const toolInvByName = new Map<string, Map<string, number>>();
    for (const r of grouped.get('tool_invocations') ?? []) {
      const name = toolNameFromLabel(r.Label);
      const m = toolInvByName.get(name) ?? new Map<string, number>();
      (r.Timestamps ?? []).forEach((t, i) => {
        const k = t.toISOString();
        m.set(k, (m.get(k) ?? 0) + ((r.Values ?? [])[i] ?? 0));
      });
      toolInvByName.set(name, m);
    }

    const toolTrend = timeline.map((ts) => {
      const row: Record<string, number | string> = { timestamp: ts };
      let others = 0;
      for (const [name, m] of toolInvByName) {
        const v = m.get(ts) ?? 0;
        if (topNames.has(name)) row[name] = v;
        else others += v;
      }
      // Ensure every charted series has an explicit value (0) at each bucket.
      for (const t of topTools) if (!(t.name in row)) row[t.name] = 0;
      if (hasOthers) row.__others__ = others;
      return row;
    });

    const toolTrendSeries = [
      ...topTools.map((t) => ({ name: t.name, label: t.label })),
      ...(hasOthers ? [{ name: '__others__', label: '기타' }] : []),
    ];

    // --- Auth & API-key health ---
    const inboundOk = (grouped.get('inbound_ok') ?? []).reduce((a, r) => a + sumValues(r), 0);
    const inboundFail = (grouped.get('inbound_fail') ?? []).map((r) => ({
      exceptionType: firstToken(r.Label),
      count: sumValues(r),
    }));
    const inboundFailTotal = inboundFail.reduce((a, b) => a + b.count, 0);

    const apiKeyOk = (grouped.get('apikey_ok') ?? []).map((r) => ({
      provider: firstToken(r.Label),
      count: sumValues(r),
    }));
    const apiKeyFail = (grouped.get('apikey_fail') ?? []).map((r) => {
      // ApiKeyFetchFailures labels lead with ExceptionType then ProviderName,
      // e.g. "AccessDeniedException tavily default <gw> default ApiKeyFetchFailures".
      const [exceptionType, provider] = (r.Label ?? '').split(' ');
      return {
        label: provider ? `${provider} · ${exceptionType}` : (r.Label ?? 'unknown'),
        count: sumValues(r),
      };
    });
    const apiKeyOkTotal = apiKeyOk.reduce((a, b) => a + b.count, 0);
    const apiKeyFailTotal = apiKeyFail.reduce((a, b) => a + b.count, 0);

    const sum = (m: Map<string, number>) => Array.from(m.values()).reduce((a, b) => a + b, 0);
    const totalInvocations = sum(invMap);
    const totalSystemErrors = sum(sysMap);
    const totalUserErrors = sum(userMap);

    // Determine status: NoData if empty, Complete otherwise
    const status = timeline.length === 0 && totalInvocations === 0 ? 'NoData' : 'Complete';

    return {
      timeRange: range,
      period,
      invocations,
      latency,
      errors,
      overhead,
      tools,
      toolTrend,
      toolTrendSeries,
      auth: {
        inboundSuccess: inboundOk,
        inboundFailure: inboundFailTotal,
        inboundFailureByType: inboundFail,
        apiKeySuccess: apiKeyOkTotal,
        apiKeyFailure: apiKeyFailTotal,
        apiKeySuccessByProvider: apiKeyOk,
        apiKeyFailureByProvider: apiKeyFail,
      },
      summary: {
        total_invocations: totalInvocations,
        total_system_errors: totalSystemErrors,
        total_user_errors: totalUserErrors,
        total_throttles: sum(throttleMap),
        error_rate:
          totalInvocations > 0
            ? (totalSystemErrors + totalUserErrors) / totalInvocations
            : 0,
        avg_latency: Math.round(avgValues(single.get('latency_avg'))),
        avg_target_exec: Math.round(avgValues(single.get('target_exec_avg'))),
      },
      status,
    };
  } catch (error) {
    // Handle specific AWS errors gracefully
    const errorName = (error as any)?.name || (error as any)?.__type;
    if (errorName === 'AccessDeniedException' ||
        errorName === 'InvalidRequestException' ||
        errorName === 'ThrottledException' ||
        errorName === 'ThrottlingException') {
      return emptyMetrics(range, period, 'Unavailable');
    }
    // Re-throw unknown errors so the API route can convert to 502
    throw error;
  }
}
