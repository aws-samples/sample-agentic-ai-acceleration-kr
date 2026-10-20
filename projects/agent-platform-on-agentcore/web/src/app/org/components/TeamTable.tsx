"use client";

import { formatMicros } from "@/app/insights/insightsFormat.mjs";
import type { OrgTeam } from "@/lib/org";
import { teamStatusLabel, managedByLabel } from "../orgTable.mjs";
import { budgetLabel, budgetTone, formatBudgetCell } from "../teamBudget.mjs";
import { ToneBadge } from "./ToneBadge";

/** Every team side by side — the "모든 팀" view; a row opens that team. */
export function TeamTable({ teams, onSelect }: { teams: OrgTeam[]; onSelect: (name: string) => void }) {
  // Every team the platform made itself says the same thing; the column only
  // tells teams apart once one was made elsewhere (Terraform, an older stack).
  const showManagedBy = teams.some((t) => t.managed_by !== "platform");
  return (
    <table className="w-full text-xs [&_td]:px-2 [&_th]:px-2">
      <thead>
        <tr className="text-left text-muted-foreground">
          <th className="py-1">팀</th>{showManagedBy && <th>관리</th>}<th>상태</th><th className="text-right">구성원</th><th className="text-right">허용 모델</th><th>정책</th><th className="text-right">30일 비용</th><th className="text-right">오늘 / 일일 경고</th>
        </tr>
      </thead>
      <tbody>
        {teams.map((t) => {
          const s = teamStatusLabel(t);
          const budget = budgetTone(t.today_cost_micros, t.daily_cost_alert_usd);
          const budgetWord = budgetLabel(budget.tone);
          return (
            <tr key={t.name} className="cursor-pointer border-t hover:bg-muted/40" onClick={() => onSelect(t.name)}>
              <td className="py-1.5"><div className="font-medium">{t.label || t.name}</div><div className="text-xxs text-muted-foreground">team:{t.name}</div></td>
              {showManagedBy && <td><ToneBadge label={managedByLabel(t)} tone="muted" /></td>}
              <td><ToneBadge label={s.label} tone={s.tone} /></td>
              <td className="text-right">{t.members}</td>
              <td className="text-right">{t.allowed_models.length === 0 ? "전체" : t.allowed_models.length}</td>
              <td>{t.policy_linked ? "연결" : t.managed_by !== "platform" ? "외부" : "미연결"}</td>
              <td className="text-right">{t.turns_30d > 0 ? formatMicros(t.model_cost_micros_30d) : "—"}</td>
              <td className="text-right whitespace-nowrap">
                {formatBudgetCell(t.today_cost_micros, t.daily_cost_alert_usd)}
                {budgetWord && <span className="ml-1.5"><ToneBadge label={budgetWord} tone={budget.tone} /></span>}
              </td>
            </tr>
          );
        })}
        {teams.length === 0 && <tr><td colSpan={showManagedBy ? 8 : 7} className="py-6 text-center text-muted-foreground">팀이 없습니다. 새 팀을 만드세요.</td></tr>}
      </tbody>
    </table>
  );
}
