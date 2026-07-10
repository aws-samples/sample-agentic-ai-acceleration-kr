'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import type { ModelCostAnalyticsResponse } from '@/lib/actions/analytics-models';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

export function ModelCostDetail({ data }: { data: ModelCostAnalyticsResponse }) {
  const models = data.models;

  return (
    <div className="space-y-6">
      {/* Summary */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">총 비용</p>
          <p className="text-2xl font-bold mt-1 tracking-tight num">${data.total_cost_usd.toFixed(4)}</p>
        </div>
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">활성 모델 수</p>
          <p className="text-2xl font-bold mt-1">{models.length}</p>
        </div>
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">기간</p>
          <p className="text-2xl font-bold mt-1">{data.period}</p>
        </div>
      </div>

      {/* Cost Breakdown Bar */}
      {models.length > 0 && data.total_cost_usd > 0 && (
        <div className="glass glass-hover rounded-apple p-4">
          <h3 className="text-sm font-semibold mb-3">비용 비중</h3>
          <div className="flex h-6 rounded-full overflow-hidden bg-muted">
            {models.map((m, i) => {
              const pct = (m.total_cost_usd / data.total_cost_usd) * 100;
              if (pct < 1) return null;
              const colors = [
                'bg-[#2dd4bf]',
                'bg-[#38bdf8]',
                'bg-[#f472b6]',
                'bg-[#a78bfa]',
                'bg-[#fbbf24]',
                'bg-[#4ade80]',
                'bg-[#fb7185]',
                'bg-[#818cf8]',
              ];
              return (
                <div
                  key={m.model_alias}
                  className={`${colors[i % colors.length]} relative group`}
                  style={{ width: `${pct}%` }}
                  title={`${m.model_alias}: ${pct.toFixed(1)}%`}
                />
              );
            })}
          </div>
          <div className="flex flex-wrap gap-3 mt-2">
            {models.map((m, i) => {
              const colors = [
                'bg-[#2dd4bf]',
                'bg-[#38bdf8]',
                'bg-[#f472b6]',
                'bg-[#a78bfa]',
                'bg-[#fbbf24]',
                'bg-[#4ade80]',
                'bg-[#fb7185]',
                'bg-[#818cf8]',
              ];
              const pct = (m.total_cost_usd / data.total_cost_usd) * 100;
              return (
                <div key={m.model_alias} className="flex items-center gap-1.5 text-xs">
                  <span className={`w-2.5 h-2.5 rounded-full ${colors[i % colors.length]}`} />
                  <span>
                    {m.model_alias} ({pct.toFixed(1)}%)
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Model Table */}
      <div className="glass rounded-apple overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-sm font-semibold">모델별 상세 비용</h3>
        </div>
        {models.length === 0 ? (
          <p className="text-sm text-muted-foreground p-4">이 기간에 사용 기록이 없습니다.</p>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>모델</Th>
                <Th numeric>요청</Th>
                <Th numeric>비용 (USD)</Th>
                <Th numeric>Input 토큰</Th>
                <Th numeric>Output 토큰</Th>
                <Th numeric>$/1K 토큰</Th>
                <Th numeric>평균 지연</Th>
              </Tr>
            </THead>
            <TBody>
              {models.map((m) => (
                <Tr key={m.model_alias}>
                  <Td emphasis className="font-mono mono-id text-xs">{m.model_alias}</Td>
                  <Td numeric>{m.request_count.toLocaleString()}</Td>
                  <Td numeric className="font-semibold">${m.total_cost_usd.toFixed(4)}</Td>
                  <Td numeric>{m.input_tokens.toLocaleString()}</Td>
                  <Td numeric>{m.output_tokens.toLocaleString()}</Td>
                  <Td numeric>${m.cost_per_1k_tokens.toFixed(4)}</Td>
                  <Td numeric>{m.avg_latency_ms}ms</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </div>
    </div>
  );
}