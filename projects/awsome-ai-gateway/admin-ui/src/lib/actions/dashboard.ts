'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface DashboardSummary {
  period: string;
  total_requests: number;
  total_tokens: number;
  total_cost_usd: number;
  active_users: number;
  cost_per_user_usd: number;
}

export interface ModelShareItem {
  model_alias: string;
  display_name?: string | null;
  cost_usd: number;
  share_pct: number;
}

export interface ModelShareResponse {
  period: string;
  team_id: string;
  total_cost_usd: number;
  models: ModelShareItem[];
}

export interface ClientShareItem {
  client: string;        // 'claude-code' | 'cowork' | 'codex' | 'other'
  cost_usd: number;
  share_pct: number;
  call_count: number;
  web_search_count: number;   // server-side AgentCore web searches (attribution metric)
}

export interface ClientShareResponse {
  period: string;
  total_cost_usd: number;
  clients: ClientShareItem[];
}

export interface TeamOption {
  id: string;
  name: string;
}

export interface AvailablePeriods {
  periods: string[]; // newest-first, e.g. ['2026-06','2026-05', ...]
  latest: string; // periods[0], or current calendar month if empty
}

/**
 * 사용량 데이터가 있는 월 목록 + 기본 월(latest).
 * 백엔드 /admin/dashboard/periods 가 데이터 있는 월만 최신순 반환.
 * 빈 DB / 엔드포인트 미배포(404) 시 현재 달력월로 graceful fallback.
 */
export async function fetchAvailablePeriods(): Promise<AvailablePeriods> {
  const now = new Date();
  const ym = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  const thisMonth = ym(now);
  const lastMonth = ym(new Date(now.getFullYear(), now.getMonth() - 1, 1));
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ periods: string[] }>('/admin/dashboard/periods'),
    );
    const withData = (res.periods ?? []).filter(Boolean);
    // 이번 달·지난 달은 데이터가 0이어도 선택기 고정 버튼이라 항상 포함.
    // 그 외 데이터 있는 과거 월은 드롭다운에. dedup 후 최신순.
    const periods = Array.from(new Set([thisMonth, lastMonth, ...withData])).sort().reverse();
    return {
      // 기본(latest)은 "데이터 있는 가장 최근 월" — 첫 화면이 비지 않도록.
      // 데이터가 전혀 없으면 이번 달.
      latest: withData.slice().sort().reverse()[0] ?? thisMonth,
      periods,
    };
  } catch (err) {
    // 백엔드 미배포(404)/장애를 빈 DB 와 구분해 로그로 남김 — 둘 다 graceful
    // fallback(이번/지난 달)이지만, 침묵으로 outage 가 가려지지 않도록 기록.
    console.error('[fetchAvailablePeriods] /admin/dashboard/periods 실패 — 현재월로 fallback:', err);
    return { periods: [thisMonth, lastMonth], latest: thisMonth };
  }
}

export async function fetchDashboardSummary(period?: string, client?: string): Promise<DashboardSummary> {
  const params: Record<string, string> = {};
  if (period) params.period = period;
  if (client && client !== 'all') params.client = client;
  return withRetry(() =>
    adminAPI.get<DashboardSummary>(
      '/admin/dashboard/summary',
      Object.keys(params).length ? params : undefined,
    ),
  );
}

export async function fetchModelShare(
  period?: string,
  teamId?: string,
  client?: string,
): Promise<ModelShareResponse> {
  const params: Record<string, string> = {};
  if (period) params.period = period;
  if (teamId) params.team_id = teamId;
  if (client && client !== 'all') params.client = client;
  return withRetry(() =>
    adminAPI.get<ModelShareResponse>(
      '/admin/dashboard/model-share',
      Object.keys(params).length ? params : undefined,
    ),
  );
}

export async function fetchClientShare(period?: string): Promise<ClientShareResponse> {
  return withRetry(() =>
    adminAPI.get<ClientShareResponse>(
      '/admin/dashboard/client-share',
      period ? { period } : undefined,
    ),
  );
}

export async function fetchTeamOptions(): Promise<TeamOption[]> {
  const res = await withRetry(() =>
    adminAPI.get<{ items: Array<{ id: string; name: string }> }>('/admin/users/teams'),
  );
  return res.items.map((t) => ({ id: t.id, name: t.name }));
}

// ── Analytics (cost trend + breakdowns) — /admin/analytics ──
// trends: 일별 비용/요청, by_team/by_model: 분해. group_by 로 user 분해도 가능.

export interface TrendItem {
  date: string;
  cost_usd: number;
  requests: number;
}

export interface TeamBreakdown {
  team: string;
  team_id: string;
  cost_usd: number;
  active_users: number;
}

export interface AnalyticsResponse {
  period: string;
  currency: string;
  by_team: TeamBreakdown[];
  trends: TrendItem[];
}

/** 비용 추이(trends) + 팀별 분해(by_team) 를 한 번에. group_by=team. */
export async function fetchAnalytics(
  period: string,
  groupBy: 'team' | 'user' | 'model' = 'team',
): Promise<AnalyticsResponse> {
  return withRetry(() =>
    adminAPI.get<AnalyticsResponse>('/admin/analytics', { period, group_by: groupBy, scope: 'all' }),
  );
}

// ── Budget summary (Top 팀/사용자 by 비용) — /admin/budgets/summary ──

export interface BudgetSummaryItem {
  target_type: string; // 'team' | 'user'
  target_id: string;
  target_name: string | null;
  team_id: string | null;
  used_usd: string;
  limit_usd: string | null;
  usage_pct: string | null;
}

export async function fetchBudgetSummary(period: string): Promise<BudgetSummaryItem[]> {
  const res = await withRetry(() =>
    adminAPI.get<{ summary: BudgetSummaryItem[] }>('/admin/budgets/summary', { period }),
  );
  return res.summary ?? [];
}

// 실제 비용 기준 상위 사용자(§60.8) — usage_logs SUCCESS+KST 집계. 기존 'Top 사용자
// by 비용' 위젯이 budgets/summary(예산설정자만)를 써 헤비유저를 누락하던 버그 수정.
export interface TopUserItem {
  name: string;
  email: string;
  cost_usd: number;
  call_count: number;
}

export async function fetchTopUsers(period?: string, limit = 5, client?: string): Promise<TopUserItem[]> {
  const params: Record<string, string> = { limit: String(limit) };
  if (period) params.period = period;
  if (client && client !== 'all') params.client = client;
  const res = await withRetry(() =>
    adminAPI.get<{ users: TopUserItem[] }>('/admin/dashboard/top-users', params),
  );
  return res.users ?? [];
}

// 실제 비용 기준 상위 팀(§60.9) — usage_logs.team_id 직접 집계(SUCCESS+KST). top-users 와
// 동형으로, 예산 미설정 팀도 포함(기존 budgets/summary 소스는 예산설정 팀만 누락 위험).
export interface TopTeamItem {
  name: string;
  cost_usd: number;
  call_count: number;
}

export async function fetchTopTeams(period?: string, limit = 5, client?: string): Promise<TopTeamItem[]> {
  const params: Record<string, string> = { limit: String(limit) };
  if (period) params.period = period;
  if (client && client !== 'all') params.client = client;
  const res = await withRetry(() =>
    adminAPI.get<{ teams: TopTeamItem[] }>('/admin/dashboard/top-teams', params),
  );
  return res.teams ?? [];
}