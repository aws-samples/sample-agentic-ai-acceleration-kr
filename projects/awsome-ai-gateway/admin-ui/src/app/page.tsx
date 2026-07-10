// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { adminAPI } from '@/lib/api-client';
import { KPICard } from '@/components/common/KPICard';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { AlertLevel } from '@/types/enums';
import type { ModelListItem } from '@/types/entities';
import {
  DollarSign,
  Key,
  Cpu,
  BarChart3,
  Activity,
  Coins,
  Users,
  CalendarClock,
} from 'lucide-react';
import { Suspense } from 'react';
import {
  fetchDashboardSummary,
  fetchModelShare,
  fetchTeamOptions,
  fetchAnalytics,
  fetchBudgetSummary,
  fetchTopUsers,
  fetchTopTeams,
  fetchAvailablePeriods,
  fetchClientShare,
  type BudgetSummaryItem,
  type ClientShareResponse,
} from '@/lib/actions/dashboard';
import { ModelShareDonutClient } from '@/components/dashboard/ModelShareDonutClient';
import { ClientShareDonutClient } from '@/components/dashboard/ClientShareDonutClient';
import { CostTrendCard } from '@/components/dashboard/CostTrendCard';
import { TopSpendTable, type TopSpendRow } from '@/components/dashboard/TopSpendTable';
import { PeriodSelector } from '@/components/dashboard/PeriodSelector';
import { ClientFilter } from '@/components/dashboard/ClientFilter';

interface BudgetSummaryResponse {
  summary: BudgetSummaryItem[];
}

interface KeyCountResponse {
  count: number;
}

function calcAlertLevel(utilization: number): (typeof AlertLevel)[keyof typeof AlertLevel] {
  if (utilization >= 95) return AlertLevel.CRITICAL;
  if (utilization >= 80) return AlertLevel.WARNING;
  return AlertLevel.NORMAL;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

function fmtUsd2(n: number): string {
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * 일 평균 소비 + (당월이면) 월말 예상.
 * 실데이터: dashboard summary 의 total_cost_usd 를 경과일로 나눔.
 * - 당월: 오늘까지 경과일로 나눠 일평균 → 그 달 총일수 곱해 월말 예상(선형 추정).
 * - 과거월: 그 달 총일수로 나눔(예상 없음, 이미 확정).
 */
function computeDailyAvg(period: string, totalCost: number): {
  dailyAvg: number;
  projection: number | null;
} {
  const [y, m] = period.split('-').map(Number);
  const daysInMonth = new Date(y, m, 0).getDate();
  const now = new Date();
  const isCurrentMonth = y === now.getFullYear() && m === now.getMonth() + 1;
  const elapsedDays = isCurrentMonth ? now.getDate() : daysInMonth;
  const dailyAvg = elapsedDays > 0 ? totalCost / elapsedDays : 0;
  const projection = isCurrentMonth ? dailyAvg * daysInMonth : null;
  return { dailyAvg, projection };
}

async function DashboardKPIs({ period, client }: { period: string; client: string }) {
  const [budgetResult, keysResult, modelsResult, summaryResult] = await Promise.allSettled([
    adminAPI.get<BudgetSummaryResponse>('/admin/budgets/summary', { period }),
    adminAPI.get<KeyCountResponse>('/admin/keys/count', { status: 'ACTIVE' }),
    adminAPI.get<{ items: ModelListItem[] }>('/admin/models'),
    fetchDashboardSummary(period, client),
  ]);

  const budgetData = budgetResult.status === 'fulfilled' ? budgetResult.value : null;
  const keysData = keysResult.status === 'fulfilled' ? keysResult.value : null;
  const modelsData = modelsResult.status === 'fulfilled' ? modelsResult.value : null;
  const summary = summaryResult.status === 'fulfilled' ? summaryResult.value : null;

  // 이번 달 사용량/예산: TEAM 행 + 팀 미소속 USER 행을 합산.
  const summaryItems = budgetData?.summary ?? [];
  const teamItems = summaryItems.filter((i) => i.target_type === 'team');
  const teamlessUsers = summaryItems.filter((i) => i.target_type === 'user' && !i.team_id);
  const aggregateItems = [...teamItems, ...teamlessUsers];
  const totalUsageUsd = aggregateItems.reduce((sum, i) => sum + parseFloat(i.used_usd || '0'), 0);
  const totalLimitUsd = aggregateItems.reduce(
    (sum, i) => sum + (i.limit_usd != null ? parseFloat(i.limit_usd) : 0),
    0,
  );
  const budgetUtilization = totalLimitUsd > 0 ? (totalUsageUsd / totalLimitUsd) * 100 : 0;
  const activeKeys = keysData?.count ?? 0;
  const activeModels = modelsData
    ? (modelsData.items ?? []).filter((m) => m.is_active).length
    : 0;
  const alertLevel = calcAlertLevel(budgetUtilization);

  // 일 평균 / 월말 예상 — summary 의 total_cost_usd 기반 (가짜 없음, 파생값)
  const { dailyAvg, projection } = summary
    ? computeDailyAvg(period, summary.total_cost_usd)
    : { dailyAvg: 0, projection: null };

  return (
    <div className="space-y-6">
      {/* ── 비용 & 예산 ── */}
      <section className="space-y-3">
        <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
          비용 &amp; 예산
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <KPICard
            title="이번 달 사용량"
            value={fmtUsd2(totalUsageUsd)}
            icon={<DollarSign size={18} aria-hidden="true" />}
            description="USD 기준 당월 누적 비용"
          />
          <KPICard
            title="예산 소진율"
            value={`${budgetUtilization.toFixed(1)}%`}
            icon={<BarChart3 size={18} aria-hidden="true" />}
            alertLevel={alertLevel}
            description="전체 예산 대비 사용 비율"
          />
          <KPICard
            title="사용자당 평균비용"
            value={summary ? fmtUsd2(summary.cost_per_user_usd) : '—'}
            icon={<Users size={18} aria-hidden="true" />}
            description={summary ? `이번달 활성 사용자 ${summary.active_users}명 기준` : '집계 실패'}
          />
          <KPICard
            title="일 평균 소비"
            value={summary ? fmtUsd2(dailyAvg) : '—'}
            icon={<CalendarClock size={18} aria-hidden="true" />}
            description={
              projection != null
                ? `월말 예상 ${fmtUsd2(projection)} (선형 추정)`
                : '경과일 기준 일평균'
            }
          />
        </div>
      </section>

      {/* ── 사용량 & 시스템 ── */}
      <section className="space-y-3">
        <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
          사용량 &amp; 시스템
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <KPICard
            title="총 요청 수"
            value={summary ? summary.total_requests.toLocaleString() : '—'}
            icon={<Activity size={18} aria-hidden="true" />}
            description="이번달 SUCCESS 요청"
          />
          <KPICard
            title="총 토큰 수"
            value={summary ? formatTokens(summary.total_tokens) : '—'}
            icon={<Coins size={18} aria-hidden="true" />}
            description="입력+출력+캐시 합계"
          />
          <KPICard
            title="활성 API Keys"
            value={activeKeys}
            icon={<Key size={18} aria-hidden="true" />}
            description="현재 활성 상태인 가상 키 수"
          />
          <KPICard
            title="활성 모델"
            value={activeModels}
            icon={<Cpu size={18} aria-hidden="true" />}
            description="현재 사용 가능한 모델 수"
          />
        </div>
      </section>
    </div>
  );
}

async function TrendAndDistribution({ period, client }: { period: string; client: string }) {
  const [analyticsResult, shareResult, teamsResult] = await Promise.allSettled([
    fetchAnalytics(period, 'team'),
    fetchModelShare(period, 'all', client),
    fetchTeamOptions(),
  ]);

  const analytics =
    analyticsResult.status === 'fulfilled' ? analyticsResult.value : { trends: [] };
  const initialShare =
    shareResult.status === 'fulfilled'
      ? shareResult.value
      : { period, team_id: 'all', total_cost_usd: 0, models: [] };
  const teams = teamsResult.status === 'fulfilled' ? teamsResult.value : [];

  return (
    <section className="space-y-3">
      <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
        추이 &amp; 분포
      </h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1.6fr_1fr]">
        <CostTrendCard trends={analytics.trends ?? []} />
        <ModelShareDonutClient initialData={initialShare} teams={teams} period={period} client={client} />
      </div>
    </section>
  );
}

async function TeamUserRanking({ period, client }: { period: string; client: string }) {
  // §60.9: 팀·사용자 모두 실제 비용(usage_logs SUCCESS+KST) 기준으로 통일 — 예산
  // 설정 여부와 무관히 진짜 top spender 를 보여준다(기존 budgets/summary 소스는
  // 예산설정 대상만 포함해 누락 위험). 예산 소진율(%)은 budgets/summary 에서 보강.
  const [topTeamsResult, topUsersResult, budgetResult] = await Promise.allSettled([
    fetchTopTeams(period, 5, client),
    fetchTopUsers(period, 5, client),
    fetchBudgetSummary(period),
  ]);
  const topTeams = topTeamsResult.status === 'fulfilled' ? topTeamsResult.value : [];
  const topUsers = topUsersResult.status === 'fulfilled' ? topUsersResult.value : [];
  const budgetItems = budgetResult.status === 'fulfilled' ? budgetResult.value : [];

  // 팀명 → 예산 소진율 룩업(예산 설정된 팀만 존재; 없으면 null).
  const teamPctByName = new Map<string, number | null>(
    budgetItems
      .filter((i) => i.target_type === 'team')
      .map((i) => [i.target_name || '', i.usage_pct != null ? parseFloat(i.usage_pct) : null]),
  );

  const teamRows: TopSpendRow[] = topTeams.map((t) => ({
    id: t.name,
    name: t.name,
    usedUsd: t.cost_usd,
    usagePct: teamPctByName.get(t.name) ?? null,
  }));

  // 실제 비용 기준 — usagePct 는 예산 미설정자가 많아 의미 없어 미표시(null).
  const userRows: TopSpendRow[] = topUsers.map((u) => ({
    id: u.email,
    name: u.name || u.email,
    usedUsd: u.cost_usd,
    usagePct: null,
  }));

  return (
    <section className="space-y-3">
      <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
        팀 &amp; 사용자 랭킹
      </h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <TopSpendTable
          title="Top 팀 by 비용"
          subtitle="이번 달 상위 5개 팀 (실제 사용 비용)"
          rows={teamRows}
          accentVar="var(--chart-1)"
        />
        <TopSpendTable
          title="Top 사용자 by 비용"
          subtitle="이번 달 상위 5명 (실제 사용 비용)"
          rows={userRows}
          accentVar="var(--chart-2)"
        />
      </div>
    </section>
  );
}

async function ClientDistribution({ period }: { period: string }) {
  const share = await fetchClientShare(period).catch(() => null);
  const data: ClientShareResponse = share ?? { period, total_cost_usd: 0, clients: [] };
  return (
    <section className="space-y-3">
      <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
        앱별 비용 점유율 (Claude Code · Cowork · Codex)
      </h2>
      <div className="glass glass-hover rounded-apple p-5">
        <ClientShareDonutClient data={data} />
      </div>
    </section>
  );
}

const ALLOWED_CLIENTS = ['all', 'claude-code', 'cowork', 'codex', 'other'];

export default async function DashboardPage({
  searchParams,
}: {
  searchParams: { period?: string; client?: string };
}) {
  // 기간 해석은 한 번만. ?period 가 데이터 있는 월이면 존중, 아니면 latest(데이터
  // 있는 가장 최근 월)로. Next 14.2.29 — searchParams 는 sync 객체(await 금지).
  const { periods, latest } = await fetchAvailablePeriods();
  const requested = searchParams?.period;
  const period = requested && periods.includes(requested) ? requested : latest;

  const rawClient = searchParams?.client;
  const client = rawClient && ALLOWED_CLIENTS.includes(rawClient) ? rawClient : 'all';

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">대시보드</h1>
        <div className="flex flex-wrap items-center gap-2">
          <ClientFilter current={client} />
          <PeriodSelector periods={periods} current={period} />
        </div>
      </div>

      <Suspense
        key={`kpi-${period}-${client}`}
        fallback={
          <div className="space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
              <SkeletonCard count={4} />
            </div>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
              <SkeletonCard count={4} />
            </div>
          </div>
        }
      >
        <DashboardKPIs period={period} client={client} />
      </Suspense>

      <Suspense
        key={`trend-${period}-${client}`}
        fallback={<div className="glass rounded-apple h-72 animate-pulse" />}
      >
        <TrendAndDistribution period={period} client={client} />
      </Suspense>

      <Suspense
        key={`rank-${period}-${client}`}
        fallback={<div className="glass rounded-apple h-64 animate-pulse" />}
      >
        <TeamUserRanking period={period} client={client} />
      </Suspense>

      <Suspense
        key={`client-${period}`}
        fallback={<div className="glass rounded-apple h-64 animate-pulse" />}
      >
        <ClientDistribution period={period} />
      </Suspense>
    </div>
  );
}
