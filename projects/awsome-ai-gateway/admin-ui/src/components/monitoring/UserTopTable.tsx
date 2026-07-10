'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import type { MonitoringUsersResponse } from '@/lib/actions/monitoring';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

// 에러율 강조 — 테마 토큰(다크/라이트 자동). 임계: ≥10% 위험, ≥5% 경고.
function errorColor(pct: number) {
  if (pct >= 10) return 'text-destructive font-semibold';
  if (pct >= 5) return 'text-amber-600 dark:text-amber-400';
  return '';
}

export function UserTopTable({ data }: { data: MonitoringUsersResponse }) {
  if (data.users.length === 0) {
    return (
      <div className="glass rounded-apple p-6">
        <p className="text-sm text-muted-foreground">최근 1시간 동안 사용자 트래픽이 없습니다.</p>
      </div>
    );
  }

  return (
    <div className="glass rounded-apple overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold tracking-tight">사용자 Top {data.users.length} (최근 1시간, 비용 기준)</h3>
      </div>
      <Table>
        <THead>
          <Tr>
            <Th>사용자</Th>
            <Th numeric>요청</Th>
            <Th numeric>토큰</Th>
            <Th numeric>비용 (USD)</Th>
            <Th numeric>에러율</Th>
            <Th numeric>마지막 요청</Th>
          </Tr>
        </THead>
        <TBody>
          {data.users.map((u) => (
            <Tr key={u.user_id}>
              <Td>
                <div className="font-medium">{u.display_name}</div>
                <div className="text-xs text-muted-foreground">{u.email}</div>
              </Td>
              <Td numeric>{u.requests.toLocaleString()}</Td>
              <Td numeric>{u.tokens.toLocaleString()}</Td>
              <Td numeric emphasis>${u.cost_usd.toFixed(4)}</Td>
              <Td numeric className={errorColor(u.error_rate_pct)}>
                {u.error_rate_pct}%
              </Td>
              <Td numeric className="text-muted-foreground">
                {u.last_request_at
                  ? new Date(u.last_request_at).toLocaleTimeString('ko-KR')
                  : '-'}
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}