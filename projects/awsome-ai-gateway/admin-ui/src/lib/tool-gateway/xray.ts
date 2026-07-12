import type { TraceSummary, Segment } from '@aws-sdk/client-xray';
import { XRayClient } from '@aws-sdk/client-xray';
import { AWS_REGION, GATEWAY_ID, TRACE_TIME_RANGES, type TraceTimeRangeKey } from './constants';

// Extract the tool id from an X-Ray span name like
// "AgentCore.Gateway.InvokeTool.you___you_search". Tool ids always contain
// "___" (see the cloudwatch-agentcore-metrics convention), so we pick the
// dot-segment that contains it.
export function extractToolName(spanName: string | undefined): string | null {
  if (!spanName) return null;
  const token = spanName.split('.').find((part) => part.includes('___'));
  return token ?? null;
}

export interface TraceListItem {
  id: string;
  startTime: number; // epoch seconds
  duration: number; // seconds
  tool: string | null;
  httpStatus: number | null;
  httpMethod: string | null;
  hasFault: boolean;
  hasError: boolean;
  hasThrottle: boolean;
}

// X-Ray annotations come back as { key: [{ AnnotationValue: { StringValue } }] }.
function annotationValue(annotations: Record<string, any> | undefined, key: string): string | undefined {
  const entries = annotations?.[key];
  if (!Array.isArray(entries) || entries.length === 0) return undefined;
  return entries[0]?.AnnotationValue?.StringValue;
}

export function normalizeSummary(summary: TraceSummary): TraceListItem {
  const annotations = summary.Annotations as Record<string, any> | undefined;
  const spanName =
    annotationValue(annotations, 'span.name') ?? annotationValue(annotations, 'aws:span.name');
  return {
    id: summary.Id ?? '',
    startTime:
      typeof summary.StartTime === 'number'
        ? summary.StartTime
        : summary.StartTime
          ? new Date(summary.StartTime as any).getTime() / 1000
          : 0,
    duration: summary.Duration ?? 0,
    tool: extractToolName(spanName),
    httpStatus: summary.Http?.HttpStatus ?? null,
    httpMethod: summary.Http?.HttpMethod ?? null,
    hasFault: Boolean(summary.HasFault),
    hasError: Boolean(summary.HasError),
    hasThrottle: Boolean(summary.HasThrottle),
  };
}

export interface Span {
  id: string;
  parentId: string | null;
  name: string;
  kind: string; // SERVER | CLIENT | remote | ''
  startTime: number; // epoch seconds
  endTime: number;
  durationMs: number;
  namespace: string | null;
  httpStatus: number | null;
  error: boolean;
  // Enriched from the segment's `metadata`/`aws` blocks (AgentCore Gateway
  // emits these but the UI previously ignored them).
  tool: string | null; // metadata "tool.name"
  urlPath: string | null; // metadata "url.path" — e.g. tools/call, tools/list
  targetType: string | null; // metadata "target.type" — LAMBDA | MCP | OPENAPI
  targetId: string | null; // metadata "targetId" — which target was routed to
  requestId: string | null; // aws.request.id — joins to the vended logs
  errorType: string | null; // metadata "error_type"
  jsonrpcErrorCode: number | null; // metadata "jsonrpc.error.code"
  latencyMs: number | null; // metadata "latency_ms"
  overheadMs: number | null; // metadata "overhead_latency_ms" — gateway overhead
  execMs: number | null; // metadata "execute_tool_latency_ms" — target execution
}

// X-Ray Segment.Document is a JSON string; parse it defensively.
export function parseSegmentDocument(raw: string | undefined): any | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function spanKind(doc: any): string {
  const ann = doc?.annotations ?? {};
  return ann['aws:span.kind'] ?? ann['span.kind'] ?? (doc?.namespace === 'remote' ? 'remote' : '');
}

// Coerce a metadata value that may be number | numeric-string | undefined.
function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null;
}

function docToSpan(doc: any): Span {
  const start = Number(doc.start_time) || 0;
  const end = Number(doc.end_time) || start;
  const md = doc.metadata ?? {};
  const aws = doc.aws ?? {};
  return {
    id: doc.id ?? '',
    parentId: doc.parent_id ?? null,
    name: doc.name ?? '',
    kind: spanKind(doc),
    startTime: start,
    endTime: end,
    durationMs: Math.max(0, (end - start) * 1000),
    namespace: doc.namespace ?? null,
    httpStatus: doc.http?.response?.status ?? num(md['http.response.status_code']),
    error: Boolean(doc.error || doc.fault) || str(md['error_type']) !== null,
    tool: str(md['tool.name']),
    urlPath: str(md['url.path']),
    targetType: str(md['target.type']),
    targetId: str(md['targetId']),
    requestId: str(aws['request.id']) ?? str(md['request.id']),
    errorType: str(md['error_type']),
    jsonrpcErrorCode: num(md['jsonrpc.error.code']),
    latencyMs: num(md['latency_ms']),
    overheadMs: num(md['overhead_latency_ms']),
    execMs: num(md['execute_tool_latency_ms']),
  };
}

function collectSpans(doc: any, out: Span[], parentId: string | null = null): void {
  if (!doc) return;
  const span = docToSpan(doc);
  // Set parentId from the parent doc if not already set in JSON
  if (parentId !== null && span.parentId === null) {
    span.parentId = parentId;
  }
  out.push(span);
  if (Array.isArray(doc.subsegments)) {
    for (const sub of doc.subsegments) collectSpans(sub, out, doc.id ?? null);
  }
}

export function buildSpanList(segments: Segment[] | undefined): Span[] {
  if (!Array.isArray(segments)) return [];
  const out: Span[] = [];
  for (const seg of segments) {
    const doc = parseSegmentDocument(seg.Document);
    if (doc) collectSpans(doc, out);
  }
  return out;
}

let _client: XRayClient | null = null;
export function getXRayClient(): XRayClient {
  if (!_client) _client = new XRayClient({ region: AWS_REGION });
  return _client;
}

// X-Ray filter expression scoping summaries to this gateway only — the account
// has 2 gateways sharing the namespace, so summaries must be scoped.
export function gatewayFilterExpression(): string {
  return `service("${GATEWAY_ID}")`;
}

// Convert an X-Ray trace id ("1-<8hex>-<24hex>") into the form CloudWatch
// vended logs carry in their `trace_id` field (the 32-hex string with no
// dashes and no version prefix). This is the join key between the waterfall
// (X-Ray) and the request/response + error detail (gateway logs).
export function xrayIdToLogTraceId(traceId: string): string | null {
  const m = /^1-([0-9a-f]{8})-([0-9a-f]{24})$/i.exec(traceId.trim());
  if (!m) return null;
  return (m[1] + m[2]).toLowerCase();
}

// Clamp an arbitrary timeRange query param to a supported trace range (max 24h).
export function clampTraceRange(value: string | null | undefined): TraceTimeRangeKey {
  if (value && value in TRACE_TIME_RANGES) return value as TraceTimeRangeKey;
  return '24h';
}
