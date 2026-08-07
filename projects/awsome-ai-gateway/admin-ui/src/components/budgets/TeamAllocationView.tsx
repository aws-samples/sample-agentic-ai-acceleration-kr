'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { TeamBudgetAllocation, AllocationEntry } from '@/types/entities';
import { allocateTeamBudgetAction } from '@/lib/actions/budgets';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { Table, THead, TBody, TFoot, Tr, Th, Td, TEmpty } from '@/components/common/Table';

interface TeamAllocationViewProps {
  teamId: string;
  initialAllocation: TeamBudgetAllocation | null;
}

export function TeamAllocationView({ teamId, initialAllocation }: TeamAllocationViewProps) {
  const t = useTranslations('budgets');
  const tCommon = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();

  const [allocations, setAllocations] = useState<AllocationEntry[]>(
    initialAllocation?.entries ?? []
  );

  const totalBudget = initialAllocation?.total_budget_usd ?? 0;
  const totalAllocated = allocations.reduce((sum, entry) => sum + (entry.allocated_usd || 0), 0);
  const unallocated = totalBudget - totalAllocated;
  const isOverBudget = totalAllocated > totalBudget;

  const handleAllocationChange = (targetId: string, newValue: number) => {
    setAllocations((prev) =>
      prev.map((entry) =>
        entry.target_id === targetId
          ? { ...entry, allocated_usd: isNaN(newValue) ? 0 : newValue }
          : entry
      )
    );
  };

  const handleSave = () => {
    startTransition(async () => {
      const result = await allocateTeamBudgetAction(teamId, allocations);

      if (result.success) {
        toast({
          type: 'success',
          message: t('allocationSaved'),
          auto_dismiss_ms: 3000,
        });
      } else {
        toast({
          type: 'error',
          message: result.error,
          auto_dismiss_ms: 5000,
        });
      }
    });
  };

  if (!initialAllocation) {
    return (
      <p className="text-sm text-muted-foreground">{t('allocationLoadFailed')}</p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 flex items-center justify-between">
        <span className="text-sm font-medium">
          {t('teamTotalBudget', { team: initialAllocation.team_name })}
        </span>
        <span className="text-lg font-bold">${totalBudget.toFixed(2)}</span>
      </div>

      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('colMemberName')}</Th>
              <Th>{t('colAllocated')}</Th>
              <Th numeric>{t('colUsage')}</Th>
            </Tr>
          </THead>
          <TBody>
            {allocations.length === 0 ? (
              <TEmpty colSpan={3}>{t('noMembers')}</TEmpty>
            ) : (
              allocations.map((entry) => (
                <Tr key={entry.target_id}>
                  <Td emphasis>{entry.target_name}</Td>
                  <Td>
                    <div className="flex items-center gap-1">
                      <span className="text-muted-foreground">$</span>
                      <input
                        type="number"
                        min={0}
                        step={0.01}
                        value={entry.allocated_usd}
                        onChange={(e) =>
                          handleAllocationChange(entry.target_id, parseFloat(e.target.value))
                        }
                        className="w-32 rounded-md border border-input bg-background px-2 py-1 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                      />
                    </div>
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    ${entry.used_usd.toFixed(2)}
                  </Td>
                </Tr>
              ))
            )}
          </TBody>
          <TFoot>
            <Tr>
              <Td emphasis colSpan={2}>{t('unallocated')}</Td>
              <Td numeric className={isOverBudget ? 'text-destructive' : 'text-primary'}>
                ${unallocated.toFixed(2)}
              </Td>
            </Tr>
          </TFoot>
        </Table>
      </div>

      {isOverBudget && (
        <p className="text-sm text-destructive" role="alert">
          {t('allocationExceeds', { allocated: totalAllocated.toFixed(2), budget: totalBudget.toFixed(2) })}
        </p>
      )}

      <div className="flex justify-end">
        <SpinnerButton
          onClick={handleSave}
          isLoading={isPending}
          disabled={isOverBudget}
          type="button"
        >
          {tCommon('save')}
        </SpinnerButton>
      </div>
    </div>
  );
}