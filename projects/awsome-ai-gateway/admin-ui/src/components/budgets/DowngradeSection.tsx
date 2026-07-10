'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState } from 'react';
import { AutoDowngradeConfig } from '@/components/budgets/AutoDowngradeConfig';
import type { BudgetSummaryItem, ModelListItem } from '@/types/entities';

interface DowngradeSectionProps {
  teamItems: BudgetSummaryItem[];
  models: ModelListItem[];
}

export function DowngradeSection({ teamItems, models }: DowngradeSectionProps) {
  const [showInactive, setShowInactive] = useState(false);

  const hasInactive = teamItems.some(t => t.is_active === false);
  const visibleTeams = showInactive ? teamItems : teamItems.filter(t => t.is_active !== false);

  return (
    <div>
      <h2 className="text-lg font-semibold mb-3">자동 다운그레이드 설정</h2>
      {hasInactive && (
        <div className="mb-3">
          <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={e => setShowInactive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-gray-300"
            />
            비활성 팀 포함
          </label>
        </div>
      )}
      <div className="space-y-4">
        {visibleTeams.map(team => (
          <AutoDowngradeConfig
            key={team.target_id}
            scopeType="TEAM"
            scopeId={team.target_id}
            scopeName={team.target_name}
            models={models}
          />
        ))}
      </div>
    </div>
  );
}