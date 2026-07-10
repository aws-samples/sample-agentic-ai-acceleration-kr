// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Top spenders ranking table (teams or users).
 * 실데이터: /admin/budgets/summary 의 BudgetSummaryItem[] 에서
 * target_type 으로 필터 → used_usd 내림차순 정렬 → 상위 N.
 * 데이터가 없으면 빈 상태 문구를 표시(가짜 행 없음).
 */

import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

interface TopSpendRow {
  id: string;
  name: string;
  usedUsd: number;
  usagePct: number | null;
}

interface TopSpendTableProps {
  title: string;
  subtitle?: string;
  rows: TopSpendRow[];
  /** 진행바 색상 (chart 토큰). */
  accentVar?: string;
}

function fmtUsd(n: number): string {
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}

export function TopSpendTable({
  title,
  subtitle,
  rows,
  accentVar = 'var(--chart-1)',
}: TopSpendTableProps) {
  return (
    <div className="glass glass-hover rounded-apple p-5">
      <div className="mb-1 text-sm font-semibold tracking-tight">{title}</div>
      {subtitle && <div className="mb-3 text-xs text-muted-foreground">{subtitle}</div>}

      {rows.length === 0 ? (
        <div className="py-8 text-center text-xs text-muted-foreground">
          이번 기간 집계된 데이터가 없습니다.
        </div>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>이름</Th>
              <Th numeric>비용</Th>
              <Th>예산 소진</Th>
            </Tr>
          </THead>
          <TBody>
            {rows.map((r, i) => (
              <Tr key={r.id}>
                <Td emphasis>
                  <span className="flex items-center gap-2.5">
                    <span
                      className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold tabular-nums ${
                        i === 0
                          ? 'badge-teal'
                          : i === 1
                            ? 'badge-sky'
                            : i === 2
                              ? 'badge-amber'
                              : 'text-muted-foreground'
                      }`}
                    >
                      {i + 1}
                    </span>
                    <span className="truncate">{r.name}</span>
                  </span>
                </Td>
                <Td numeric className="font-semibold">{fmtUsd(r.usedUsd)}</Td>
                <Td>
                  {r.usagePct != null ? (
                    <div className="flex items-center gap-2">
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[--table-progress-track]">
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${Math.min(100, r.usagePct)}%`,
                            background: `hsl(${accentVar})`,
                          }}
                        />
                      </div>
                      <span className="w-9 text-right text-[11px] tabular-nums text-muted-foreground">
                        {r.usagePct.toFixed(0)}%
                      </span>
                    </div>
                  ) : (
                    <span className="text-[11px] text-muted-foreground">예산 미설정</span>
                  )}
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}
    </div>
  );
}

export type { TopSpendRow };
