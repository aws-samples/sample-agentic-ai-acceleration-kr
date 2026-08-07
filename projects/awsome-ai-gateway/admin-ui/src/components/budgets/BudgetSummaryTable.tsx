'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { Fragment, useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
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

function AlertBadge({ level, labels }: { level: (typeof AlertLevel)[keyof typeof AlertLevel]; labels: Record<string, string> }) {
  const tones: Record<string, BadgeTone> = {
    [AlertLevel.NORMAL]: 'teal',
    [AlertLevel.WARNING]: 'amber',
    [AlertLevel.CRITICAL]: 'pink',
  };
  return <Badge tone={tones[level] ?? 'neutral'}>{labels[level] ?? level}</Badge>;
}

function TypeBadge({ type, labels }: { type: (typeof BudgetScope)[keyof typeof BudgetScope]; labels: Record<string, string> }) {
  return <Badge tone={type === BudgetScope.TEAM ? 'sky' : 'neutral'}>{labels[type] ?? type}</Badge>;
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
  const t = useTranslations('budgets');
  const [selectedItem, setSelectedItem] = useState<DialogTarget | null>(null);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [showInactive, setShowInactive] = useState(false);

  const alertLabels: Record<string, string> = {
    [AlertLevel.NORMAL]: t('alertLevels.NORMAL'),
    [AlertLevel.WARNING]: t('alertLevels.WARNING'),
    [AlertLevel.CRITICAL]: t('alertLevels.CRITICAL'),
  };
  const typeLabels: Record<string, string> = {
    [BudgetScope.TEAM]: t('scope.TEAM'),
    [BudgetScope.USER]: t('scope.USER'),
  };

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
        <TypeBadge type={user.target_type} labels={typeLabels} />
      </Td>
      <Td numeric>
        {user.limit != null ? `$${user.limit.toFixed(2)}` : <span className="text-muted-foreground italic">{t('teamBudgetApplied')}</span>}
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
        <AlertBadge level={user.alert_level} labels={alertLabels} />
      </Td>
      {isAdmin && (
        <Td>
          <button
            onClick={() => handleOpenDialog(user)}
            className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            {t('setBudget')}
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
            {t('includeInactive')}
          </label>
        </div>
      )}
      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('targetName')}</Th>
              <Th>{t('type')}</Th>
              <Th numeric>{t('maxBudget')}</Th>
              <Th numeric>{t('used')}</Th>
              <Th numeric>{t('remainingBudget')}</Th>
              <Th className="min-w-[120px]">{t('usageRate')}</Th>
              <Th>{t('status')}</Th>
              {isAdmin && <Th>{t('actions')}</Th>}
            </Tr>
          </THead>
          <TBody>
            {isEmpty ? (
              <TEmpty colSpan={colCount}>{t('noData')}</TEmpty>
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
                                    ? t('collapse', { name: team.target_name })
                                    : t('expand', { name: team.target_name })
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
                          <TypeBadge type={team.target_type} labels={typeLabels} />
                        </Td>
                        <Td numeric>
                          {team.limit != null ? `$${team.limit.toFixed(2)}` : <span className="text-muted-foreground italic">{t('notSet')}</span>}
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
                          <AlertBadge level={team.alert_level} labels={alertLabels} />
                        </Td>
                        {isAdmin && (
                          <Td>
                            <button
                              onClick={() => handleOpenDialog(team)}
                              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                            >
                              {t('setBudget')}
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
                              aria-label={isOpen ? t('collapseUnassigned') : t('expandUnassigned')}
                              className="flex h-5 w-5 items-center justify-center rounded hover:bg-muted text-muted-foreground"
                            >
                              {isOpen ? (
                                <ChevronDown size={14} />
                              ) : (
                                <ChevronRight size={14} />
                              )}
                            </button>
                            <span className="text-muted-foreground">{t('unassigned')}</span>
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