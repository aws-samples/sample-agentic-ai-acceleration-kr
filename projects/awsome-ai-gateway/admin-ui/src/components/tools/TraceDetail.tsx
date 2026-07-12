'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import {
  parseJavaMap,
  extractToolName,
  extractArguments,
  extractToolError,
  extractLatencyMs,
  extractResponseText,
  prettyPrintBody,
} from '@/lib/tool-gateway/log-parse';

// Span/log shapes mirror the /api/tools/traces/[id] response.
export interface Span {
  id: string;
  parentId: string | null;
  name: string;
  kind: string;
  startTime: number;
  endTime: number;
  durationMs: number;
  namespace: string | null;
  httpStatus: number | null;
  error: boolean;
  tool: string | null;
  urlPath: string | null;
  targetType: string | null;
  targetId: string | null;
  requestId: string | null;
  errorType: string | null;
  jsonrpcErrorCode: number | null;
  latencyMs: number | null;
  overheadMs: number | null;
  execMs: number | null;
}

export interface GatewayLogEntry {
  timestamp: string;
  spanId: string | null;
  isError: boolean;
  log: string | null;
  requestBody: string | null;
  responseBody: string | null;
  errorMessage: string | null;
}

// Renders the latency decomposition. Unlike a span waterfall (near-identical
// for every tool call), this answers "was the time spent in the gateway or in
// the target?" from the root span's metadata:
//   overhead_latency_ms → gateway (auth, routing, marshalling)
//   execute_tool_latency_ms → downstream target
//   latency_ms → total end-to-end
function LatencyBreakdown({ spans, traceId }: { spans: Span[]; traceId: string }) {
  if (spans.length === 0) {
    return (
      <div className="border rounded-lg p-4 bg-card">
        <p className="font-mono text-sm">{traceId}</p>
        <p className="text-xs text-muted-foreground mt-1">지연 분해</p>
        <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">
          스팬 정보를 찾을 수 없습니다.
        </div>
      </div>
    );
  }

  const root = spans.find((s) => s.kind === 'SERVER') ?? spans[0];
  const total = root.latencyMs ?? root.durationMs;
  const gateway = root.overheadMs;
  const target = root.execMs;

  const hasSplit = gateway != null && target != null && total > 0;
  const other = hasSplit ? Math.max(0, total - gateway! - target!) : 0;
  const segments = hasSplit
    ? [
        { label: '게이트웨이', ms: gateway!, bar: 'bg-violet-500', dot: 'bg-violet-500' },
        { label: '타깃 실행', ms: target!, bar: 'bg-sky-500', dot: 'bg-sky-500' },
        ...(other > 0
          ? [{ label: '기타/네트워크', ms: other, bar: 'bg-muted-foreground/40', dot: 'bg-muted-foreground/40' }]
          : []),
      ]
    : [];

  const toolSpans = spans.filter((s) => s.kind !== 'SERVER');

  return (
    <div className="border rounded-lg p-4 bg-card space-y-4">
      <div>
        <p className="font-mono text-sm">{traceId}</p>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span>지연 분해</span>
          {root.urlPath && <span className="font-mono">{root.urlPath}</span>}
          {root.tool && <span className="font-mono text-foreground">{root.tool}</span>}
          {root.requestId && <span className="font-mono text-muted-foreground/70">req {root.requestId}</span>}
        </div>
      </div>

      <div className="flex items-baseline gap-2">
        <span className="font-mono text-2xl font-semibold tabular-nums">{total.toFixed(0)}</span>
        <span className="text-sm text-muted-foreground">ms 총 지연</span>
      </div>

      {hasSplit ? (
        <>
          <div className="flex h-3 w-full overflow-hidden rounded-full bg-muted/30">
            {segments.map((seg) => (
              <div
                key={seg.label}
                className={seg.bar}
                style={{ width: `${(seg.ms / total) * 100}%` }}
                title={`${seg.label} ${seg.ms.toFixed(0)} ms`}
              />
            ))}
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1.5">
            {segments.map((seg) => (
              <div key={seg.label} className="flex items-center gap-2">
                <span className={`h-2.5 w-2.5 rounded-sm ${seg.dot}`} />
                <span className="text-xs text-muted-foreground">{seg.label}</span>
                <span className="font-mono text-xs tabular-nums">{seg.ms.toFixed(0)} ms</span>
                <span className="font-mono text-[11px] tabular-nums text-muted-foreground/70">
                  {((seg.ms / total) * 100).toFixed(0)}%
                </span>
              </div>
            ))}
          </div>
        </>
      ) : (
        <p className="text-xs text-muted-foreground">
          이 트레이스에는 지연 분해 메타데이터(overhead/exec)가 없어 총 지연만 표시합니다.
        </p>
      )}

      {toolSpans.length > 0 && (
        <div className="space-y-1.5 border-t border-border/50 pt-3">
          {toolSpans.map((span) => {
            const chips = [
              span.targetType,
              span.targetId,
              span.httpStatus != null ? `HTTP ${span.httpStatus}` : null,
              span.errorType,
              span.jsonrpcErrorCode != null ? `rpc ${span.jsonrpcErrorCode}` : null,
            ].filter(Boolean) as string[];
            return (
              <div key={span.id} className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <span className="truncate font-mono text-xs" title={span.name}>
                    {span.tool ?? span.name}
                  </span>
                  {chips.map((c) => (
                    <span
                      key={c}
                      className={`rounded px-1 py-0.5 text-[10px] font-mono ${
                        span.error ? 'bg-red-500/10 text-red-500' : 'bg-muted/60 text-muted-foreground'
                      }`}
                    >
                      {c}
                    </span>
                  ))}
                </div>
                <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
                  {span.durationMs.toFixed(0)} ms
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function summarizeRequest(requestBody: string): {
  method: string | null;
  tool: string | null;
  query: string | null;
} {
  const method = (parseJavaMap(requestBody).method as string | undefined) ?? null;
  const tool = method === 'tools/call' ? extractToolName(requestBody) : null;
  const args = extractArguments(requestBody);
  const query = typeof args.query === 'string' ? args.query : null;
  return { method, tool, query };
}

function summarizeResponse(responseBody: string): {
  resultCount: number | null;
  engine: string | null;
  latencyMs: number | null;
  bytes: number;
} {
  const text = extractResponseText(responseBody);
  const latencyMs = extractLatencyMs(responseBody);
  let resultCount: number | null = null;
  let engine: string | null = null;
  if (text) {
    try {
      const obj = JSON.parse(text) as { results?: unknown[]; engine?: string };
      if (Array.isArray(obj.results)) resultCount = obj.results.length;
      if (typeof obj.engine === 'string') engine = obj.engine;
    } catch {
      // result text wasn't clean JSON — leave summary fields null.
    }
  }
  return { resultCount, engine, latencyMs, bytes: (text ?? responseBody).length };
}

function RawFold({ label, body }: { label: string; body: string }) {
  return (
    <details className="group mt-1">
      <summary className="cursor-pointer select-none text-[10px] font-medium uppercase tracking-wider text-muted-foreground/60 hover:text-muted-foreground">
        {label} 원본 보기
      </summary>
      <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded bg-muted/40 p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
        {prettyPrintBody(body)}
      </pre>
    </details>
  );
}

// The gateway vended logs joined to the selected trace, as a stepped timeline:
// each step shows elapsed time since request start, a distilled req/res summary,
// and any error inlined at the step where it occurred. Raw bodies are foldable.
function LogTimeline({ logs }: { logs: GatewayLogEntry[] }) {
  if (logs.length === 0) {
    return (
      <div className="border rounded-lg p-4 bg-card">
        <p className="text-sm font-semibold">요청 로그</p>
        <p className="text-xs text-muted-foreground mt-1">
          이 트레이스에 연결된 게이트웨이 로그를 찾지 못했습니다 (인덱싱 지연일 수 있음).
        </p>
      </div>
    );
  }

  const t0 = logs[0].timestamp ? Date.parse(logs[0].timestamp) : NaN;

  return (
    <div className="border rounded-lg p-4 bg-card space-y-3">
      <div>
        <p className="text-sm font-semibold">요청 로그</p>
        <p className="text-xs text-muted-foreground">trace_id로 조인된 게이트웨이 요청·응답·오류 타임라인</p>
      </div>
      <ol className="space-y-3">
        {logs.map((l, i) => {
          const ts = l.timestamp ? Date.parse(l.timestamp) : NaN;
          const elapsed = Number.isFinite(ts) && Number.isFinite(t0) ? ts - t0 : null;
          const prevTs = i > 0 && logs[i - 1].timestamp ? Date.parse(logs[i - 1].timestamp) : NaN;
          const delta = Number.isFinite(ts) && Number.isFinite(prevTs) ? ts - prevTs : null;
          const slow = delta != null && delta >= 500;

          const req = l.requestBody ? summarizeRequest(l.requestBody) : null;
          const res = l.responseBody ? summarizeResponse(l.responseBody) : null;
          const toolErr = l.responseBody ? extractToolError(l.responseBody) : null;
          const inlineError = l.errorMessage ?? (l.isError ? l.log : null) ?? toolErr;

          return (
            <li key={i} className="relative border-l-2 border-border/60 pl-4">
              <span
                className={`absolute -left-[5px] top-1.5 h-2 w-2 rounded-full ring-2 ring-background ${
                  inlineError ? 'bg-red-500' : 'bg-emerald-500'
                }`}
              />
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                {elapsed != null && (
                  <span className="font-mono text-xs tabular-nums">+{elapsed} ms</span>
                )}
                {delta != null && delta > 0 && (
                  <span
                    className={`font-mono text-[10px] tabular-nums ${
                      slow ? 'text-amber-500' : 'text-muted-foreground/60'
                    }`}
                  >
                    Δ{delta} ms{slow ? ' ←' : ''}
                  </span>
                )}
                <span className="text-xs text-muted-foreground">{l.log ?? '—'}</span>
              </div>

              {req && (
                <div className="mt-1 flex flex-wrap items-center gap-1.5">
                  <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-sky-600">
                    REQ
                  </span>
                  {req.method && <span className="font-mono text-xs text-muted-foreground">{req.method}</span>}
                  {req.tool && <span className="font-mono text-xs">{req.tool}</span>}
                  {req.query != null && (
                    <span className="truncate font-mono text-xs text-muted-foreground" title={req.query}>
                      query: &quot;{req.query}&quot;
                    </span>
                  )}
                </div>
              )}
              {l.requestBody && <RawFold label="요청" body={l.requestBody} />}

              {res && (
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  <span className="rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-violet-600">
                    RES
                  </span>
                  {res.resultCount != null && <span className="font-mono text-xs">{res.resultCount} results</span>}
                  {res.engine && <span className="font-mono text-xs text-muted-foreground">{res.engine}</span>}
                  {res.latencyMs != null && (
                    <span className="font-mono text-xs text-muted-foreground">{res.latencyMs} ms</span>
                  )}
                  <span className="font-mono text-[11px] text-muted-foreground/60">
                    {res.bytes.toLocaleString()} chars
                  </span>
                </div>
              )}
              {l.responseBody && <RawFold label="응답" body={l.responseBody} />}

              {inlineError && (
                <div className="mt-1.5 rounded-md border border-red-500/30 bg-red-500/10 px-2.5 py-1.5">
                  <p className="break-words font-mono text-xs text-red-600">{inlineError}</p>
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function TraceDetail({
  traceId,
  spans,
  logs,
  loading,
}: {
  traceId: string;
  spans: Span[];
  logs: GatewayLogEntry[];
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="border rounded-lg p-4 bg-card flex items-center justify-center h-32 text-sm text-muted-foreground">
        상세 정보를 불러오는 중…
      </div>
    );
  }
  return (
    <div className="space-y-6">
      <LatencyBreakdown spans={spans} traceId={traceId} />
      <LogTimeline logs={logs} />
    </div>
  );
}
