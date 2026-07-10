'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Chart as ChartJS,
  ArcElement,
  Tooltip,
  Legend,
} from 'chart.js';
import { Doughnut } from 'react-chartjs-2';
import type { ModelShareResponse, TeamOption } from '@/lib/actions/dashboard';
import { CATEGORICAL_PALETTE } from '@/lib/utils/chartTheme';
import { modelDisplay } from '@/lib/utils/modelLabel';

ChartJS.register(ArcElement, Tooltip, Legend);

// 항목 구분용 다색 카테고리 팔레트 (색맹 안전, 라이트/다크 공통).
const COLORS = CATEGORICAL_PALETTE;

interface Props {
  initialData: ModelShareResponse;
  teams: TeamOption[];
  period: string;
  client?: string;  // 대시보드 앱 필터(?client=) — team 변경 재조회 시에도 유지.
}

export function ModelShareDonutClient({ initialData, teams, period, client }: Props) {
  const [teamId, setTeamId] = useState<string>('all');
  const [data, setData] = useState<ModelShareResponse>(initialData);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isFirstRender = useRef(true);

  useEffect(() => {
    // 첫 렌더는 SSR 로 받은 initialData 를 그대로 쓴다 — fetch 생략.
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ period, team_id: teamId });
    if (client && client !== 'all') params.set('client', client);
    fetch(`/api/dashboard/model-share?${params}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as ModelShareResponse;
      })
      .then((next) => {
        if (!cancelled) setData(next);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : '조회 실패');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [teamId, period, client]);

  const chartData = useMemo(() => {
    return {
      labels: data.models.map((m) => modelDisplay(m.model_alias, m.display_name)),
      datasets: [
        {
          data: data.models.map((m) => m.cost_usd),
          backgroundColor: data.models.map((_, i) => COLORS[i % COLORS.length]),
          // 세그먼트 주변에 선/테두리/gap 일절 없음 (사용자 지시). 조각 직접 맞닿음.
          // 시인성은 (1) 색 자체 + (2) hover 강조 + (3) 가운데 1위 모델 표기로 확보.
          borderWidth: 0,
          spacing: 0,
          // hover 시 해당 조각만 바깥으로 튀어나와 강조 (정적 선 아님).
          hoverOffset: 14,
        },
      ],
    };
  }, [data]);

  const options = useMemo(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      cutout: '62%',
      // hover 시 부드럽게 튀어나오는 애니메이션.
      animation: { animateRotate: true, animateScale: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx: import('chart.js').TooltipItem<'doughnut'>) => {
              const item = data.models[ctx.dataIndex];
              if (!item) return '';
              return `${modelDisplay(item.model_alias, item.display_name)}: $${item.cost_usd.toFixed(4)} (${item.share_pct.toFixed(1)}%)`;
            },
          },
        },
      },
    }),
    [data],
  );

  const isEmpty = data.models.length === 0 || data.total_cost_usd === 0;

  return (
    <div className="glass glass-hover rounded-apple p-5">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <div className="text-sm font-semibold tracking-tight">모델별 비용 점유율</div>
          <div className="text-xs text-muted-foreground">기간 내 모델별 비용 비중</div>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="team-select" className="text-xs text-muted-foreground">
            범위
          </label>
          <select
            id="team-select"
            value={teamId}
            onChange={(e) => setTeamId(e.target.value)}
            className="rounded-apple-sm border border-input bg-background px-2 py-1 text-xs interactive focus:outline-none focus:ring-1 focus:ring-ring"
            disabled={loading}
          >
            <option value="all">전체</option>
            {teams.map((team) => (
              <option key={team.id} value={team.id}>
                {team.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && (
        <p className="text-sm text-destructive mb-2">조회 실패: {error}</p>
      )}

      {isEmpty ? (
        <div className="flex items-center justify-center h-64 text-sm text-muted-foreground">
          이 기간/범위에 사용 기록이 없습니다.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-center">
          <div className="relative h-64">
            <Doughnut data={chartData} options={options} />
            {/* 가운데: 점유율 1위 모델 강조 (models 는 비용 desc 정렬, [0]=1위) */}
            <div className="absolute inset-0 flex flex-col items-center justify-center px-6 text-center pointer-events-none">
              {data.models[0] && (
                <span
                  className="mb-1 h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: COLORS[0] }}
                  aria-hidden="true"
                />
              )}
              <p className="text-2xl font-bold leading-none tracking-tight">
                {data.models[0] ? `${data.models[0].share_pct.toFixed(0)}%` : '—'}
              </p>
              <p className="mt-1 max-w-full truncate text-xs font-medium text-foreground">
                {data.models[0] ? modelDisplay(data.models[0].model_alias, data.models[0].display_name) : ''}
              </p>
              <p className="mt-0.5 text-[10px] text-muted-foreground">
                점유 1위 · 총 ${data.total_cost_usd.toLocaleString('en-US', {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}
              </p>
            </div>
          </div>
          <ul className="space-y-2">
            {data.models.map((m, i) => (
              <li key={m.model_alias} className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2 min-w-0">
                  <span
                    className="w-3 h-3 rounded-full flex-shrink-0"
                    style={{ backgroundColor: COLORS[i % COLORS.length] }}
                  />
                  <span className="font-medium truncate">{modelDisplay(m.model_alias, m.display_name)}</span>
                </div>
                <div className="flex items-center gap-3 text-xs">
                  <span className="tabular-nums">
                    ${m.cost_usd.toFixed(2)}
                  </span>
                  <span className="text-muted-foreground tabular-nums w-12 text-right">
                    {m.share_pct.toFixed(1)}%
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}