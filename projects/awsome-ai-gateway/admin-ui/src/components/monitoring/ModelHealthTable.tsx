'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import type { MonitoringModelsResponse } from '@/lib/actions/monitoring';
import { Badge } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

// 에러율 강조 — 테마 토큰(다크/라이트 자동). 임계: ≥10% 위험, ≥5% 경고.
function errorColor(pct: number) {
  if (pct >= 10) return 'text-destructive font-semibold';
  if (pct >= 5) return 'text-amber-600 dark:text-amber-400';
  return '';
}

export function ModelHealthTable({ data }: { data: MonitoringModelsResponse }) {
  if (data.models.length === 0) {
    return (
      <div className="glass rounded-apple p-6">
        <p className="text-sm text-muted-foreground">최근 1시간 동안 모델 트래픽이 없습니다.</p>
      </div>
    );
  }

  return (
    <div className="glass rounded-apple overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold tracking-tight">모델별 상태 (최근 1시간)</h3>
      </div>
      <Table>
        <THead>
          <Tr>
            <Th>모델</Th>
            <Th>상태</Th>
            <Th numeric>요청</Th>
            <Th numeric>평균 지연</Th>
            <Th numeric>에러율</Th>
            <Th numeric>마지막 요청</Th>
          </Tr>
        </THead>
        <TBody>
          {data.models.map((m) => (
            <Tr key={m.alias}>
              <Td emphasis className="font-mono mono-id text-xs">{m.alias}</Td>
              <Td>
                <Badge tone={m.status === 'ACTIVE' ? 'teal' : 'neutral'}>{m.status}</Badge>
              </Td>
              <Td numeric>{m.last_1h_requests.toLocaleString()}</Td>
              <Td numeric>{m.avg_latency_ms}ms</Td>
              <Td numeric className={errorColor(m.error_rate_pct)}>
                {m.error_rate_pct}%
              </Td>
              <Td numeric className="text-muted-foreground">
                {m.last_request_at
                  ? new Date(m.last_request_at).toLocaleTimeString('ko-KR')
                  : '-'}
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}