"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  CloudDownload,
  RefreshCw,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  EmptyState,
  LoadingState,
  PageBody,
  PageHeader,
} from "@/app/components/PageHeader";
import { NoticeRail, type RailNotice } from "@/app/insights/components/NoticeRail";
import { cn } from "@/lib/utils";
import {
  fetchComposition,
  fetchLayout,
  fetchRecordInsights,
  fetchSummary,
  fetchTelemetry,
  InsightsApiError,
  putLayout,
  type Composition,
  type InsightsSummary,
  type LayoutResponse,
  type RecordInsights,
  type Telemetry,
} from "@/lib/insights";
import {
  POLL_INTERVAL_MS,
  shouldPoll,
  stampLabel,
} from "@/app/insights/refreshPolicy.mjs";
import { reconcileLayout } from "@/app/insights/layoutModel.mjs";
import { costAnomalies, dailyCostPoints } from "@/app/insights/insightsFormat.mjs";
import { createWidgetRegistry } from "@/app/insights/widgetRegistry";
import { WidgetGrid } from "@/app/insights/components/WidgetGrid";
import { WidgetCatalog } from "@/app/insights/components/WidgetCatalog";

const WINDOWS = [7, 30] as const;

interface Widget {
  id: string;
  span: string;
  visible: boolean;
}

export default function InsightsPage() {
  const [days, setDays] = useState<number>(7);
  const [summary, setSummary] = useState<InsightsSummary | null>(null);
  const [composition, setComposition] = useState<Composition | null>(null);
  // Never fetched by the poll or on mount. `GetMetricData` bills per metric
  // requested, and this page used to sweep 279 of them every sixty seconds.
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [vendedLoading, setVendedLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RecordInsights | null>(null);
  const [loading, setLoading] = useState(true);
  const [unconfigured, setUnconfigured] = useState(false);
  // A plain user who reached the page by URL: the server gates the org aggregates
  // on admin, so this is a 403, not an error to retry.
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastAt, setLastAt] = useState<number | null>(null);
  const [now, setNow] = useState<number>(Date.now());
  const [layout, setLayout] = useState<LayoutResponse | null>(null);
  const [persisted, setPersisted] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setSummary(await fetchSummary(days));
      setError(null);
      setUnconfigured(false);
      setForbidden(false);
      setLastAt(Date.now());
    } catch (e) {
      if (e instanceof InsightsApiError && e.status === 501) {
        setUnconfigured(true);
      } else if (e instanceof InsightsApiError && e.status === 403) {
        setForbidden(true);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setLoading(false);
    }
    // Composition needs the harness listing's GetHarness fan-out, so it loads
    // on its own and never holds up the leaderboard.
    try {
      setComposition(await fetchComposition(days));
    } catch {
      setComposition(null);
    }
  }, [days]);

  // A click, and only a click. The server caches the sweep for five minutes, so
  // pressing it again inside that window is free — hence no disabling beyond the
  // in-flight guard.
  const loadTelemetry = useCallback(async () => {
    setVendedLoading(true);
    try {
      setTelemetry(await fetchTelemetry(days));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setVendedLoading(false);
    }
  }, [days]);

  // A window switch invalidates the vended figures: they were fetched for the
  // other window, and silently relabelling them would be the worst of both.
  useEffect(() => {
    setTelemetry(null);
  }, [days]);

  // Load layout once on mount. The server's answer goes through the web
  // reconciler too: the two registries are edited together but deployed as two
  // images, and between the two rollouts (or when one is rolled back) the
  // server can name a widget this build has no renderer for — `rates`, when
  // it moved to Settings. The grid would call `render` on `undefined`.
  useEffect(() => {
    const loadLayout = async () => {
      try {
        const layoutData = await fetchLayout();
        setLayout({ ...reconcileLayout(layoutData), persisted: layoutData.persisted });
      } catch {
        // Fallback to default if layout fetch fails
        const reconciled = reconcileLayout(null);
        setLayout({ ...reconciled, persisted: false });
      }
    };
    void loadLayout();
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let live = true;
    fetchRecordInsights(selected, days)
      .then((value) => live && setDetail(value))
      .catch(() => live && setDetail(null));
    return () => {
      live = false;
    };
  }, [selected, days]);

  // Poll for updates
  useEffect(() => {
    const interval = setInterval(() => {
      setNow(Date.now());
      if (
        shouldPoll({
          visibility: document.visibilityState,
          lastAt,
          now: Date.now(),
          intervalMs: POLL_INTERVAL_MS,
        })
      ) {
        void load();
      }
    }, POLL_INTERVAL_MS);

    const handleVisibilityChange = () => {
      setNow(Date.now());
      if (document.visibilityState === "visible") {
        if (
          shouldPoll({
            visibility: document.visibilityState,
            lastAt,
            now: Date.now(),
            intervalMs: POLL_INTERVAL_MS,
          })
        ) {
          void load();
        }
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [load, lastAt]);

  // Handle layout changes: optimistic update + background save
  const handleLayoutChange = useCallback(
    (newWidgets: Widget[]) => {
      if (!layout) return;

      // Optimistic update
      setLayout({
        version: layout.version,
        widgets: newWidgets,
        persisted: true,
      });

      // Background save
      putLayout({
        version: layout.version,
        widgets: newWidgets,
      })
        .then((result) => {
          setLayout({ ...reconcileLayout(result), persisted: result.persisted });
          setPersisted(result.persisted);
        })
        .catch(() => {
          setPersisted(false);
        });
    },
    [layout],
  );

  // Built as data rather than as seven conditional strips, so the rail can order
  // them by severity and fold the tail. Every condition here is the one that used
  // to render its own full-width Notice.
  const notices: RailNotice[] = [];
  if (unconfigured) {
    notices.push({
      id: "usage-table",
      tone: "info",
      icon: AlertTriangle,
      children:
        "이 환경에는 사용량 테이블(USAGE_TABLE)이 설정되어 있지 않아 턴이 기록되지 않습니다. infra 의 usage 테이블을 배포하고 서버 환경변수를 채우면 이 페이지가 채워집니다.",
    });
  }
  if (error) {
    notices.push({ id: "error", tone: "error", icon: AlertTriangle, children: error });
  }
  // A month partition the server could not read. The figures below are short by
  // whatever it held, and nothing else on the page can show that — this is the one
  // source that used to report itself as good unconditionally.
  if (summary && !summary.sources.usage) {
    notices.push({
      id: "usage-partial",
      tone: "warning",
      icon: AlertTriangle,
      children:
        "사용량 테이블의 일부 파티션을 읽지 못했습니다. 아래 턴·토큰·비용은 읽힌 만큼만 더한 값이라 실제보다 작습니다 — 새로고침하면 대개 복구됩니다.",
    });
  }
  if (telemetry && !telemetry.sources.cloudwatch) {
    notices.push({
      id: "cloudwatch",
      tone: "warning",
      icon: AlertTriangle,
      children:
        "CloudWatch 지표를 읽지 못했습니다. 지연시간·오류율은 비어 있고, 사용량·비용 값은 그대로입니다.",
    });
  }
  // The collector writes today's runtime quantities every few minutes. No item
  // for today means the figure on screen stops at yesterday — said out loud so a
  // flat Runtime tile is not read as an idle fleet.
  if (summary && !summary.sources.collector) {
    notices.push({
      id: "collector-stale",
      tone: "warning",
      icon: AlertTriangle,
      children:
        "수집기가 오늘의 Runtime 수량을 아직 쓰지 않았습니다. Runtime 비용은 마지막으로 수집된 날까지의 값입니다 — 서버의 COLLECTOR_ENABLED 와 CloudWatch 권한을 확인하세요.",
    });
  } else if (summary?.cost.runtime.as_of) {
    const age = Date.now() - new Date(summary.cost.runtime.as_of).getTime();
    if (Number.isFinite(age) && age > 15 * 60 * 1000) {
      notices.push({
        id: "collector-stale",
        tone: "warning",
        icon: AlertTriangle,
        children: `수집기의 마지막 기록이 ${Math.round(age / 60000)}분 전입니다. Runtime 비용이 그 시점에 멈춰 있습니다.`,
      });
    }
  }
  if (summary && summary.cost.rate_card.mismatches.length > 0) {
    notices.push({
      id: "rate-mismatch",
      tone: "error",
      icon: AlertTriangle,
      children: `요율표 ${summary.cost.rate_card.version} 와 AWS 청구서 요율이 ${summary.cost.rate_card.mismatches.length}건 다릅니다 (${summary.cost.rate_card.mismatches
        .map((m) => `${m.family} ${m.routing} ${m.tier}`)
        .join(", ")}). 같은 값이 이틀 연속 청구되면 그 날짜부터의 요율로 자동 반영되고 해당 턴의 비용이 다시 계산됩니다.`,
    });
  }
  if (summary && summary.cost.model.unregistered_models.length > 0) {
    notices.push({
      id: "rate-unregistered",
      tone: "warning",
      icon: AlertTriangle,
      children: `요율표에 없는 모델의 턴 ${summary.cost.model.unpriced_turns.toLocaleString("ko-KR")}개는 모델 비용에서 빠져 있습니다: ${summary.cost.model.unregistered_models.join(", ")}. AWS 청구서에 이 모델이 이틀 연속 같은 요율로 잡히면 자동으로 요율을 등록하고 비용을 채웁니다 (첫 사용 후 2~3일). Settings 의 모델 요율에서 지금 등록할 수도 있습니다.`,
    });
  }
  if (summary && !summary.sources.usage_logs) {
    notices.push({
      id: "usage-logs-off",
      tone: "info",
      icon: AlertTriangle,
      children:
        "런타임 세션 로그(USAGE_LOGS)가 아직 들어오지 않아 사용자별 Runtime 비용은 비어 있습니다. 에이전트별 Runtime 비용은 정상입니다.",
    });
  }
  if (summary && !summary.sources.recon) {
    notices.push({
      id: "billing-lag",
      tone: "info",
      icon: AlertTriangle,
      children:
        "AWS 청구서(Cost Explorer) 스냅샷이 아직 없습니다. 청구는 24~48시간 뒤에 반영되며, 그때부터 이 페이지의 Runtime 비용과 비교합니다.",
    });
  }
  if (summary) {
    const spikes = costAnomalies(dailyCostPoints(summary.daily), { partialDay: summary.partial_day });
    if (spikes.length > 0) {
      notices.push({
        id: "cost-spike",
        tone: "warning",
        icon: AlertTriangle,
        children: `일별 비용(모델+Runtime)이 직전일 중앙값의 2배를 넘은 날이 ${spikes.length}일 있습니다(${spikes
          .map((s) => s.date)
          .join(", ")}). 에이전트별·모델별 비용에서 원인을 확인하세요.`,
      });
    }
  }
  if (!persisted) {
    notices.push({
      id: "layout-local",
      tone: "info",
      icon: AlertTriangle,
      children: "이 브라우저에만 저장됩니다.",
    });
  }

  const widgets: Widget[] = layout?.widgets ?? [];
  const registry = createWidgetRegistry({
    summary,
    detail,
    selected,
    onSelect: setSelected,
    composition,
    telemetry,
    days,
  });

  return (
    <>
      <PageHeader
        icon={BarChart3}
        title="Insights"
        hint="등록된 에이전트의 통계를 확인합니다"
        actions={
          <>
            {WINDOWS.map((window) => (
              <Button
                key={window}
                variant={days === window ? "default" : "ghost"}
                size="sm"
                onClick={() => setDays(window)}
              >
                {window}일
              </Button>
            ))}
            <span className="text-xxs text-muted-foreground">
              {stampLabel(lastAt, now)}
            </span>
            <Button variant="ghost" size="sm" onClick={() => void load()}>
              <RefreshCw className={cn("size-3.5", loading && "animate-spin")} />
            </Button>
            {/* The one control on this page that spends money, so it is a button
                rather than an effect: a metered read must be something a reader
                chose. Its label says which source, not "새로고침", because the
                refresh beside it is free and this is not. */}
            <Button
              variant={telemetry ? "ghost" : "outline"}
              size="sm"
              disabled={vendedLoading}
              onClick={() => void loadTelemetry()}
              title="지연시간·오류율을 CloudWatch 에서 읽습니다. 조회당 요금이 붙고, 5분간은 캐시에서 답합니다. 비용은 수집기가 이미 채웁니다."
            >
              <CloudDownload
                className={cn("size-3.5", vendedLoading && "animate-pulse")}
              />
              CloudWatch
            </Button>
            <WidgetCatalog
              widgets={widgets}
              registry={registry}
              onLayoutChange={handleLayoutChange}
            />
          </>
        }
      />
      <PageBody>
        <NoticeRail notices={notices} />

        {forbidden ? (
          <EmptyState
            icon={BarChart3}
            title="관리자 전용 페이지입니다"
            description="Insights 는 조직 전체 에이전트의 사용량과 비용을 모은 페이지라 관리자만 볼 수 있습니다. 본인 사용량은 프로필에서 확인하세요."
          />
        ) : loading && !summary ? (
          <LoadingState label="집계를 읽고 있습니다" />
        ) : summary && summary.agents.length === 0 ? (
          <EmptyState
            icon={BarChart3}
            title="아직 기록된 턴이 없습니다"
            description="에이전트와 한 번 대화하면 이 페이지가 채워집니다."
          />
        ) : summary && layout ? (
          <WidgetGrid
            widgets={widgets}
            registry={registry}
            onLayoutChange={handleLayoutChange}
          />
        ) : null}
      </PageBody>
    </>
  );
}
