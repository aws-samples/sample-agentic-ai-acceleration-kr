'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import type { MyUsageResponse } from '@/lib/actions/my';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

export function MyUsageDashboard({ data }: { data: MyUsageResponse }) {
  const totalCost = data.daily_usage.reduce((sum, d) => sum + d.cost_usd, 0);
  const totalRequests = data.daily_usage.reduce((sum, d) => sum + d.requests, 0);
  const totalTokens = data.daily_usage.reduce((sum, d) => sum + d.tokens, 0);

  return (
    <div className="space-y-6">
      {/* Summary Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">총 비용</p>
          <p className="text-2xl font-bold mt-1">${totalCost.toFixed(4)}</p>
        </div>
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">총 요청 수</p>
          <p className="text-2xl font-bold mt-1">{totalRequests.toLocaleString()}</p>
        </div>
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">총 토큰</p>
          <p className="text-2xl font-bold mt-1">{totalTokens.toLocaleString()}</p>
        </div>
      </div>

      {/* Daily Usage Table */}
      <div className="glass rounded-apple overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-sm font-semibold">일별 사용량</h3>
        </div>
        {data.daily_usage.length === 0 ? (
          <p className="text-sm text-muted-foreground p-4">이 기간의 사용 기록이 없습니다.</p>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>날짜</Th>
                <Th numeric>비용 (USD)</Th>
                <Th numeric>요청</Th>
                <Th numeric>토큰</Th>
              </Tr>
            </THead>
            <TBody>
              {data.daily_usage.map((row) => (
                <Tr key={row.date}>
                  <Td className="num">{row.date}</Td>
                  <Td numeric>${row.cost_usd.toFixed(4)}</Td>
                  <Td numeric>{row.requests.toLocaleString()}</Td>
                  <Td numeric>{row.tokens.toLocaleString()}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </div>

      {/* By Model Table */}
      <div className="glass rounded-apple overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-sm font-semibold tracking-tight">모델별 사용량</h3>
        </div>
        {data.by_model.length === 0 ? (
          <p className="text-sm text-muted-foreground p-4">이 기간의 모델별 기록이 없습니다.</p>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>모델</Th>
                <Th numeric>비용 (USD)</Th>
                <Th numeric>요청</Th>
                <Th numeric>토큰</Th>
              </Tr>
            </THead>
            <TBody>
              {data.by_model.map((row) => (
                <Tr key={row.model_alias}>
                  <Td emphasis className="font-mono mono-id text-xs">{row.model_alias}</Td>
                  <Td numeric>${row.cost_usd.toFixed(4)}</Td>
                  <Td numeric>{row.requests.toLocaleString()}</Td>
                  <Td numeric>{row.tokens.toLocaleString()}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </div>
    </div>
  );
}