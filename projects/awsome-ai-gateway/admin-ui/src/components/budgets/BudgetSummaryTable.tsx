'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { Fragment, useMemo, useState } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';
import type { BudgetSummaryItem } from '@/types/entities';
import { AlertLevel, BudgetScope } from '@/types/enums';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { SetBudgetDialog } from './SetBudgetDialog';

interface BudgetSummaryTableProps {
  items: BudgetSummaryItem[];
  isAdmin: boolean;
}

type DialogTarget = {
  id: string;
  name: string;
  type: (typeof BudgetScope)[keyof typeof BudgetScope];
  currentLimit: number;
  parentLimit?: number;
};

const UNASSIGNED_KEY = '__unassigned__';

function AlertBadge({ level }: { level: (typeof AlertLevel)[keyof typeof AlertLevel] }) {
  const tones: Record<string, BadgeTone> = {
    [AlertLevel.NORMAL]: 'teal',
    [AlertLevel.WARNING]: 'amber',
    [AlertLevel.CRITICAL]: 'pink',
  };
  const labels: Record<string, string> = {
    [AlertLevel.NORMAL]: '정상',
    [AlertLevel.WARNING]: '경고',
    [AlertLevel.CRITICAL]: '위험',
  };
  return <Badge tone={tones[level] ?? 'neutral'}>{labels[level] ?? level}</Badge>;
}

function TypeBadge({ type }: { type: (typeof BudgetScope)[keyof typeof BudgetScope] }) {
  const label = type === BudgetScope.TEAM ? '팀' : '사용자';
  return <Badge tone={type === BudgetScope.TEAM ? 'sky' : 'neutral'}>{label}</Badge>;
}

function UsageBar({
  pct,
  level,
}: {
  pct: number;
  level: (typeof AlertLevel)[keyof typeof AlertLevel];
}) {
  // 임계 기반 시맨틱색(테마 토큰 — 다크/라이트 자동): 정상 teal / 경고 amber / 위험 destructive.
  const colorMap: Record<string, string> = {
    [AlertLevel.NORMAL]: 'hsl(var(--chart-1))',
    [AlertLevel.WARNING]: 'hsl(38 92% 50%)',
    [AlertLevel.CRITICAL]: 'hsl(var(--destructive))',
  };
  const color = colorMap[level] ?? 'hsl(var(--muted-foreground))';
  return (
    <div className="w-full h-1.5 rounded-full overflow-hidden bg-[--table-progress-track]">
      <div
        className="h-full rounded-full"
        style={{ width: `${Math.min(pct, 100)}%`, background: color }}
      />
    </div>
  );
}

export function BudgetSummaryTable({ items, isAdmin }: BudgetSummaryTableProps) {
  const [selectedItem, setSelectedItem] = useState<DialogTarget | null>(null);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [showInactive, setShowInactive] = useState(false);

  const hasInactive = items.some(i => i.is_active === false);
  const filteredItems = showInactive ? items : items.filter(i => i.is_active !== false);

  const { teamRows, usersByTeam, unassignedUsers } = useMemo(() => {
    const teams = filteredItems.filter((i) => i.target_type === BudgetScope.TEAM);
    const users = filteredItems.filter((i) => i.target_type === BudgetScope.USER);
    const grouped: Record<string, BudgetSummaryItem[]> = {};
    const orphans: BudgetSummaryItem[] = [];
    for (const u of users) {
      if (u.team_id) {
        (grouped[u.team_id] ??= []).push(u);
      } else {
        orphans.push(u);
      }
    }
    return { teamRows: teams, usersByTeam: grouped, unassignedUsers: orphans };
  }, [filteredItems]);

  const handleOpenDialog = (item: BudgetSummaryItem) => {
    setSelectedItem({
      id: item.target_id,
      name: item.target_name,
      type: item.target_type,
      currentLimit: item.limit ?? 0,
    });
    setIsDialogOpen(true);
  };

  const handleCloseDialog = () => {
    setIsDialogOpen(false);
    setSelectedItem(null);
  };

  const toggle = (key: string) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const colCount = isAdmin ? 8 : 7;
  const isEmpty = teamRows.length === 0 && unassignedUsers.length === 0;

  const renderUserRow = (user: BudgetSummaryItem) => (
    <Tr key={user.target_id} className="bg-muted/10">
      <Td emphasis>
        <div className="flex items-center gap-2 pl-10">
          <span className="text-muted-foreground" aria-hidden="true">
            └
          </span>
          {user.target_name}
        </div>
      </Td>
      <Td>
        <TypeBadge type={user.target_type} />
      </Td>
      <Td numeric>
        {user.limit != null ? `$${user.limit.toFixed(2)}` : <span className="text-muted-foreground italic">팀 예산 적용</span>}
      </Td>
      <Td numeric>${user.used.toFixed(2)}</Td>
      <Td numeric>
        {user.remaining != null ? `$${user.remaining.toFixed(2)}` : <span className="text-muted-foreground italic">-</span>}
      </Td>
      <Td>
        <div className="flex items-center gap-2">
          {user.usage_pct != null ? (
            <>
              <UsageBar pct={user.usage_pct} level={user.alert_level} />
              <span className="w-12 text-right text-[11px] tabular-nums text-muted-foreground whitespace-nowrap">
                {user.usage_pct.toFixed(1)}%
              </span>
            </>
          ) : (
            <span className="text-xs text-muted-foreground italic">-</span>
          )}
        </div>
      </Td>
      <Td>
        <AlertBadge level={user.alert_level} />
      </Td>
      {isAdmin && (
        <Td>
          <button
            onClick={() => handleOpenDialog(user)}
            className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            예산 설정
          </button>
        </Td>
      )}
    </Tr>
  );

  return (
    <>
      {hasInactive && (
        <div className="flex items-center gap-2 mb-3">
          <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={e => setShowInactive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-border"
            />
            비활성 팀/유저 포함
          </label>
        </div>
      )}
      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>대상명</Th>
              <Th>타입</Th>
              <Th numeric>최대 예산</Th>
              <Th numeric>사용량</Th>
              <Th numeric>남은 예산</Th>
              <Th className="min-w-[120px]">사용률</Th>
              <Th>상태</Th>
              {isAdmin && <Th>액션</Th>}
            </Tr>
          </THead>
          <TBody>
            {isEmpty ? (
              <TEmpty colSpan={colCount}>예산 데이터가 없습니다.</TEmpty>
            ) : (
              <>
                {teamRows.map((team) => {
                  const members = usersByTeam[team.target_id] ?? [];
                  const isOpen = expanded[team.target_id] ?? false;
                  const hasMembers = members.length > 0;
                  return (
                    <Fragment key={team.target_id}>
                      <Tr>
                        <Td emphasis>
                          <div className="flex items-center gap-2">
                            <button
                              type="button"
                              onClick={() => hasMembers && toggle(team.target_id)}
                              disabled={!hasMembers}
                              aria-expanded={hasMembers ? isOpen : undefined}
                              aria-label={
                                hasMembers
                                  ? isOpen
                                    ? `${team.target_name} 접기`
                                    : `${team.target_name} 펼치기`
                                  : undefined
                              }
                              className={`flex h-5 w-5 items-center justify-center rounded ${
                                hasMembers
                                  ? 'hover:bg-muted text-muted-foreground'
                                  : 'text-transparent cursor-default'
                              }`}
                            >
                              {isOpen ? (
                                <ChevronDown size={14} />
                              ) : (
                                <ChevronRight size={14} />
                              )}
                            </button>
                            <span>{team.target_name}</span>
                            {hasMembers && (
                              <span className="text-xs text-muted-foreground">
                                ({members.length})
                              </span>
                            )}
                          </div>
                        </Td>
                        <Td>
                          <TypeBadge type={team.target_type} />
                        </Td>
                        <Td numeric>
                          {team.limit != null ? `$${team.limit.toFixed(2)}` : <span className="text-muted-foreground italic">미설정</span>}
                        </Td>
                        <Td numeric>${team.used.toFixed(2)}</Td>
                        <Td numeric>
                          {team.remaining != null ? `$${team.remaining.toFixed(2)}` : <span className="text-muted-foreground italic">-</span>}
                        </Td>
                        <Td>
                          <div className="flex items-center gap-2">
                            {team.usage_pct != null ? (
                              <>
                                <UsageBar pct={team.usage_pct} level={team.alert_level} />
                                <span className="w-12 text-right text-[11px] tabular-nums text-muted-foreground whitespace-nowrap">
                                  {team.usage_pct.toFixed(1)}%
                                </span>
                              </>
                            ) : (
                              <span className="text-xs text-muted-foreground italic">-</span>
                            )}
                          </div>
                        </Td>
                        <Td>
                          <AlertBadge level={team.alert_level} />
                        </Td>
                        {isAdmin && (
                          <Td>
                            <button
                              onClick={() => handleOpenDialog(team)}
                              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                            >
                              예산 설정
                            </button>
                          </Td>
                        )}
                      </Tr>
                      {isOpen && members.map(renderUserRow)}
                    </Fragment>
                  );
                })}

                {unassignedUsers.length > 0 && (() => {
                  const isOpen = expanded[UNASSIGNED_KEY] ?? false;
                  return (
                    <Fragment key={UNASSIGNED_KEY}>
                      <Tr>
                        <Td emphasis colSpan={colCount}>
                          <div className="flex items-center gap-2">
                            <button
                              type="button"
                              onClick={() => toggle(UNASSIGNED_KEY)}
                              aria-expanded={isOpen}
                              aria-label={isOpen ? '팀 미배정 접기' : '팀 미배정 펼치기'}
                              className="flex h-5 w-5 items-center justify-center rounded hover:bg-muted text-muted-foreground"
                            >
                              {isOpen ? (
                                <ChevronDown size={14} />
                              ) : (
                                <ChevronRight size={14} />
                              )}
                            </button>
                            <span className="text-muted-foreground">팀 미배정</span>
                            <span className="text-xs text-muted-foreground">
                              ({unassignedUsers.length})
                            </span>
                          </div>
                        </Td>
                      </Tr>
                      {isOpen && unassignedUsers.map(renderUserRow)}
                    </Fragment>
                  );
                })()}
              </>
            )}
          </TBody>
        </Table>
      </div>

      <SetBudgetDialog
        key={selectedItem?.id ?? 'none'}
        isOpen={isDialogOpen}
        onClose={handleCloseDialog}
        target={selectedItem}
      />
    </>
  );
}