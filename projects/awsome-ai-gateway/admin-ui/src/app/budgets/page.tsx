// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { adminAPI } from '@/lib/api-client';
import { parseJWT } from '@/lib/auth';
import type { BudgetSummaryItem, ModelListItem, TeamBudgetAllocation } from '@/types/entities';
import { BudgetSummaryTable } from '@/components/budgets/BudgetSummaryTable';
import { TeamAllocationView } from '@/components/budgets/TeamAllocationView';
import { DowngradeSection } from '@/components/budgets/DowngradeSection';
import { RegisterScreenContext } from '@/components/chat/RegisterScreenContext';

export default async function BudgetsPage() {
  const cookieStore = cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;
  const session = jwt ? parseJWT(jwt) : null;
  const isAdmin = session?.role === 'ADMIN';

  const now = new Date();
  const period = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;

  interface RawBudgetItem {
    target_type: string;
    target_id: string;
    target_name: string | null;
    team_id?: string | null;
    is_active?: boolean;
    limit_usd: string | null;
    used_usd: string;
    remaining_usd: string | null;
    usage_pct: string | null;
  }

  const raw = await adminAPI
    .get<{ summary: RawBudgetItem[] }>('/admin/budgets/summary', { period })
    .catch(() => ({ summary: [] as RawBudgetItem[] }));

  const rawItems = Array.isArray(raw) ? raw : (raw as { summary: RawBudgetItem[] }).summary ?? [];

  const items: BudgetSummaryItem[] = rawItems.map((r) => {
    const pct = r.usage_pct != null ? parseFloat(r.usage_pct) || 0 : null;
    return {
      target_id: r.target_id,
      target_type: r.target_type.toUpperCase() as BudgetSummaryItem['target_type'],
      target_name: r.target_name ?? r.target_id,
      team_id: r.team_id ?? null,
      is_active: r.is_active ?? true,
      limit: r.limit_usd != null ? parseFloat(r.limit_usd) || 0 : null,
      used: parseFloat(r.used_usd) || 0,
      remaining: r.remaining_usd != null ? parseFloat(r.remaining_usd) || 0 : null,
      usage_pct: pct,
      alert_level: pct != null ? (pct >= 100 ? 'CRITICAL' : pct >= 80 ? 'WARNING' : 'NORMAL') : 'NORMAL',
    } as BudgetSummaryItem;
  });

  let teamAllocation: TeamBudgetAllocation | null = null;
  if (!isAdmin && session?.team_id) {
    teamAllocation = await adminAPI
      .get<TeamBudgetAllocation>(`/admin/budgets/team/${session.team_id}/allocation`)
      .catch(() => null);
  }

  interface APIModelItem {
    alias: string;
    provider: string;
    provider_model_id: string;
    endpoint_url: string | null;
    status: string;
    description: string | null;
    display_name: string | null;
    current_pricing: {
      input_price_per_1k_tokens: string;
      output_price_per_1k_tokens: string;
      cache_creation_5m_price_per_1k_tokens?: string;
      cache_creation_1h_price_per_1k_tokens?: string;
      cache_read_price_per_1k_tokens?: string;
    } | null;
  }

  const modelsRes = await adminAPI
    .get<{ items: APIModelItem[] }>('/admin/models')
    .catch(() => ({ items: [] as APIModelItem[] }));
  const models: ModelListItem[] = (modelsRes.items ?? []).map(m => {
    const p = m.current_pricing;
    return {
      alias: m.alias,
      provider: m.provider,
      model_id: m.provider_model_id,
      endpoint_url: m.endpoint_url ?? null,
      is_active: m.status === 'ACTIVE',
      input_price_per_1k: p ? parseFloat(p.input_price_per_1k_tokens) : 0,
      output_price_per_1k: p ? parseFloat(p.output_price_per_1k_tokens) : 0,
      cache_creation_5m_price_per_1k: p?.cache_creation_5m_price_per_1k_tokens
        ? parseFloat(p.cache_creation_5m_price_per_1k_tokens)
        : 0,
      cache_creation_1h_price_per_1k: p?.cache_creation_1h_price_per_1k_tokens
        ? parseFloat(p.cache_creation_1h_price_per_1k_tokens)
        : 0,
      cache_read_price_per_1k: p?.cache_read_price_per_1k_tokens
        ? parseFloat(p.cache_read_price_per_1k_tokens)
        : 0,
      max_tokens: 0,
      context_window: 0,
      description: m.description,
      display_name: m.display_name,
    };
  });

  const teamItems = items.filter(i => i.target_type === 'TEAM');

  // 퀵챗 화면 컨텍스트용 집계 — 개별 target_name(개인 예산은 사람 이름일 수 있음)은
  // 절대 동봉하지 않고, 건수/합계/경보 레벨 분포만. PII 없음.
  const userItems = items.filter(i => i.target_type === 'USER');
  const totalLimit = items.reduce((acc, b) => acc + (b.limit ?? 0), 0);
  const totalUsed = items.reduce((acc, b) => acc + b.used, 0);
  const alertCounts = items.reduce(
    (acc, b) => {
      acc[b.alert_level] = (acc[b.alert_level] ?? 0) + 1;
      return acc;
    },
    {} as Record<string, number>,
  );
  const contextData = isAdmin
    ? {
        팀예산수: teamItems.length,
        개인예산수: userItems.length,
        총한도USD: totalLimit,
        총사용USD: totalUsed,
        경보_위험: alertCounts.CRITICAL ?? 0,
        경보_경고: alertCounts.WARNING ?? 0,
        경보_정상: alertCounts.NORMAL ?? 0,
      }
    : { 뷰: '내 팀 예산 배정' };

  return (
    <div className="space-y-6">
      {/* 퀵챗 화면 컨텍스트 등록(렌더 null) — 예산 집계만(건수/합계/경보 분포).
          개별 사용자·팀 이름은 미동봉(PII). agent 는 query_db 로 상세 재조회 가능. */}
      <RegisterScreenContext page="예산 관리" period={`${period} 월간`} data={contextData} />

      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">예산 관리</h1>
      </div>

      {isAdmin ? (
        <BudgetSummaryTable items={items} isAdmin={isAdmin} />
      ) : session?.team_id ? (
        <TeamAllocationView
          teamId={session.team_id}
          initialAllocation={teamAllocation}
        />
      ) : (
        <p className="text-sm text-muted-foreground">배정된 팀이 없습니다.</p>
      )}

      {isAdmin && teamItems.length > 0 && (
        <DowngradeSection teamItems={teamItems} models={models} />
      )}
    </div>
  );
}
