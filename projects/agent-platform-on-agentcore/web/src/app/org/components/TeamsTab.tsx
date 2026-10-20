"use client";

import { useCallback, useEffect, useState } from "react";
import { Plus, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatMicros } from "@/app/insights/insightsFormat.mjs";
import { fetchDrift, fetchOrgTeams, orgErrorMessage, type DriftItem, type OrgTeam } from "@/lib/org";
import { isStaleInFlight, teamStatusLabel, managedByLabel } from "../orgTable.mjs";
import { budgetLabel, budgetTone, formatBudgetCell } from "../teamBudget.mjs";
import { DriftBand } from "./DriftBand";
import { TeamCreateDialog } from "./TeamCreateDialog";
import { TeamSheet } from "./TeamSheet";
import { ToneBadge } from "./ToneBadge";

const POLL_MS = 3000;

export function TeamsTab() {
  const [data, setData] = useState<Awaited<ReturnType<typeof fetchOrgTeams>> | null>(null);
  const [drift, setDrift] = useState<DriftItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [teams, d] = await Promise.all([fetchOrgTeams(), fetchDrift().catch(() => ({ items: [] as DriftItem[] }))]);
      setData(teams);
      setDrift(d.items);
      setError(null);
    } catch (e) {
      setError(orgErrorMessage(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // A provisioning or deleting row changes on its own; poll while one is still
  // running. A row stuck past five minutes will not change by itself (the drift
  // band offers its retry), so it does not keep the page polling forever.
  useEffect(() => {
    const now = Date.now();
    if (!data?.teams.some((t) => (t.status === "provisioning" || t.status === "deleting") && !isStaleInFlight(t, now))) return;
    const id = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(id);
  }, [data, load]);

  // Full-page error only before the first load; afterwards keep the tree
  // mounted so a transient poll failure cannot unmount an open sheet/dialog.
  if (!data) {
    return error
      ? <p className="text-xs text-destructive">{error}</p>
      : <p className="text-xs text-muted-foreground">불러오는 중…</p>;
  }
  const current: OrgTeam | null = data.teams.find((t) => t.name === selected) ?? null;
  // Every team the platform made itself says the same thing; the column only
  // tells teams apart once one was made elsewhere (Terraform, an older stack).
  const showManagedBy = data.teams.some((t) => t.managed_by !== "platform");

  return (
    <div className="flex flex-col gap-4">
      {error && <p className="text-xs text-destructive">{error}</p>}
      <DriftBand items={drift} onChanged={() => void load()} />
      <div className="flex items-center justify-between">
        <p className="text-xxs text-muted-foreground">
          팀 {data.teams.length}개
          {!data.provisioner_enabled && " · 서버에 역할 생성 권한(boundary)이 없어 팀을 만들 수 없습니다"}
          {!data.policies_enabled && " · 정책 엔진이 없어 게이트웨이 정책은 미연결입니다"}
        </p>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => void load()}><RefreshCw className="mr-1 size-3" />새로 고침</Button>
          <Button size="sm" onClick={() => setCreating(true)} disabled={!data.provisioner_enabled}><Plus className="mr-1 size-3" />새 팀</Button>
        </div>
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="py-1">팀</th>{showManagedBy && <th>관리</th>}<th>상태</th><th className="text-right">구성원</th><th className="text-right">허용 모델</th><th>정책</th><th className="text-right">30일 비용</th><th className="text-right">오늘 / 일일 경고</th>
          </tr>
        </thead>
        <tbody>
          {data.teams.map((t) => {
            const s = teamStatusLabel(t);
            const budget = budgetTone(t.today_cost_micros, t.daily_cost_alert_usd);
            const budgetWord = budgetLabel(budget.tone);
            return (
              <tr key={t.name} className="cursor-pointer border-t hover:bg-muted/40" onClick={() => setSelected(t.name)}>
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
          {data.teams.length === 0 && <tr><td colSpan={showManagedBy ? 8 : 7} className="py-6 text-center text-muted-foreground">팀이 없습니다. 새 팀을 만드세요.</td></tr>}
        </tbody>
      </table>
      <TeamCreateDialog open={creating} onOpenChange={setCreating} onCreated={() => void load()} policiesEnabled={data.policies_enabled} />
      <TeamSheet team={current} onClose={() => setSelected(null)} onChanged={() => void load()} />
    </div>
  );
}
