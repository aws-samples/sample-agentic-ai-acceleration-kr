'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useState } from 'react';
import { TRACE_TIME_RANGES, type TraceTimeRangeKey } from '@/lib/tool-gateway/constants';

interface TraceListItem {
  id: string;
  startTime: number;
  duration: number;
  tool: string | null;
  httpStatus: number | null;
  httpMethod: string | null;
  hasFault: boolean;
  hasError: boolean;
  hasThrottle: boolean;
}

interface TraceResponse {
  traces: TraceListItem[];
  count: number;
  status: 'Complete' | 'Unavailable' | 'Disabled';
  note?: string;
}

export function TraceTable() {
  const [timeRange, setTimeRange] = useState<TraceTimeRangeKey>('24h');
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [data, setData] = useState<TraceResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchTraces = async () => {
      try {
        setLoading(true);
        setError(null);
        const response = await fetch(`/api/tools/traces?timeRange=${timeRange}`);
        if (!response.ok) {
          throw new Error(`API error: ${response.status}`);
        }
        const result = (await response.json()) as TraceResponse;
        setData(result);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    };

    fetchTraces();
  }, [timeRange]);

  const formatTime = (epochSec: number): string => {
    return new Date(epochSec * 1000).toLocaleString('ko-KR');
  };

  const getStatusBadge = (trace: TraceListItem): { label: string; color: string } => {
    if (trace.hasFault) {
      return { label: '오류', color: 'bg-red-100 text-red-800' };
    }
    if (trace.hasError) {
      return { label: '에러', color: 'bg-red-100 text-red-800' };
    }
    if (trace.hasThrottle) {
      return { label: '스로틀', color: 'bg-yellow-100 text-yellow-800' };
    }
    return { label: '정상', color: 'bg-green-100 text-green-800' };
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        불러오는 중…
      </div>
    );
  }

  if (data?.status === 'Disabled') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        Tool Gateway가 비활성화되어 있습니다.
      </div>
    );
  }

  if (data?.status === 'Unavailable') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        Tool Gateway에 연결할 수 없습니다.
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-destructive">
        오류: {error}
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        데이터를 불러올 수 없습니다.
      </div>
    );
  }

  // Filter traces based on errorsOnly
  const filteredTraces = errorsOnly
    ? data.traces.filter((trace) => trace.hasFault || trace.hasError || trace.hasThrottle)
    : data.traces;

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="flex flex-col sm:flex-row gap-4 items-start sm:items-center justify-between">
        <div className="flex gap-2">
          {Object.entries(TRACE_TIME_RANGES).map(([key, config]) => (
            <button
              key={key}
              onClick={() => setTimeRange(key as TraceTimeRangeKey)}
              className={`px-3 py-1 text-sm rounded font-medium transition ${
                timeRange === key
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-muted text-muted-foreground hover:bg-muted/80'
              }`}
            >
              {config.label}
            </button>
          ))}
        </div>

        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={errorsOnly}
            onChange={(e) => setErrorsOnly(e.target.checked)}
            className="w-4 h-4 rounded border-gray-300"
          />
          <span className="text-sm font-medium">오류만 보기</span>
        </label>
      </div>

      {/* Traces Table */}
      <div className="border rounded-lg bg-card overflow-x-auto">
        {filteredTraces.length > 0 ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="text-left py-3 px-4 font-semibold">시간</th>
                <th className="text-left py-3 px-4 font-semibold">Tool</th>
                <th className="text-right py-3 px-4 font-semibold">소요 시간(ms)</th>
                <th className="text-left py-3 px-4 font-semibold">HTTP</th>
                <th className="text-left py-3 px-4 font-semibold">상태</th>
              </tr>
            </thead>
            <tbody>
              {filteredTraces.map((trace) => {
                const status = getStatusBadge(trace);
                return (
                  <tr key={trace.id} className="border-b hover:bg-muted/30">
                    <td className="py-3 px-4 text-xs font-mono">
                      {formatTime(trace.startTime)}
                    </td>
                    <td className="py-3 px-4">
                      {trace.tool ? (
                        <span className="font-mono">{trace.tool}</span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="text-right py-3 px-4">
                      {(trace.duration * 1000).toFixed(0)}
                    </td>
                    <td className="py-3 px-4">
                      {trace.httpMethod && trace.httpStatus ? (
                        <span className="font-mono text-xs">
                          {trace.httpMethod} {trace.httpStatus}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      <span
                        className={`inline-block px-2 py-1 rounded text-xs font-semibold ${status.color}`}
                      >
                        {status.label}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="flex items-center justify-center h-32 text-sm text-muted-foreground">
            {errorsOnly ? '오류가 없습니다.' : '데이터가 없습니다.'}
          </div>
        )}
      </div>

      {/* Count Info */}
      <div className="text-xs text-muted-foreground">
        표시된 항목: {filteredTraces.length} / 전체: {data.count}
      </div>
    </div>
  );
}
