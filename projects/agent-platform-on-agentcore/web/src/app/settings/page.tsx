"use client";

import { Suspense, useEffect, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Settings } from "lucide-react";

import { PageBody, PageHeader } from "@/app/components/PageHeader";
import { useRequireRole } from "@/app/components/AppShell";
import { McpInspector } from "@/app/components/McpInspector";
import { TeamsPanel } from "./components/TeamsPanel";
import { RateCardPanel } from "@/app/insights/components/RateCardPanel";
import { PlotStack } from "@/app/insights/components/charts";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  MENU_LABELS,
  fetchNavVisibility,
  putNavVisibility,
  type MenuKey,
  type NavVisibility,
} from "@/lib/settings";

/**
 * Platform settings: the things an admin edits that change what the other
 * pages show. Three tabs, each a thing an admin does rarely and deliberately:
 *
 * - **메뉴**: which sidebar menus plain users see. Admins always see all.
 * - **모델 요율**: the rate card the ledger prices turns with. Started as an
 *   Insights widget; among a page of figures a form reads as one more chart.
 * - **MCP Tools**: the MCP inspector, formerly its own top-level menu.
 *
 * `?tab=` selects the tab, so the old `/mcp` route can land on the inspector.
 *
 * The rate window is fixed at 30 days: this page asks "which models has the
 * platform run, and is each one priced", and a week can miss a model that ran
 * ten days ago and still has unpriced turns on the dashboard.
 */
const RATE_WINDOW_DAYS = 30;
const TABS = ["menus", "rates", "mcp", "teams"] as const;
type Tab = (typeof TABS)[number];

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border bg-card shadow-sm">
      <div className="border-b px-4 py-3">
        <h3 className="text-sm font-semibold">{title}</h3>
        <p className="mt-0.5 text-xxs text-muted-foreground">{hint}</p>
      </div>
      {/* `@container` so the tables inside size against this card, as they did
          inside the Insights widget frame. */}
      <div className="@container p-4">{children}</div>
    </section>
  );
}

/** One switch per menu a plain user can see; saved on every flip. */
function MenuVisibility() {
  const [state, setState] = useState<NavVisibility | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let live = true;
    fetchNavVisibility()
      .then((value) => {
        if (live) setState(value);
      })
      .catch((err) => {
        if (live) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      live = false;
    };
  }, []);

  const toggle = async (key: MenuKey, visible: boolean) => {
    if (!state) return;
    const hidden = visible
      ? state.hidden.filter((entry) => entry !== key)
      : [...state.hidden.filter((entry) => entry !== key), key];
    setSaving(true);
    setError(null);
    try {
      setState(await putNavVisibility(hidden));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  if (error && !state) {
    return <p className="text-xs text-destructive">{error}</p>;
  }
  if (!state) {
    return <p className="text-xs text-muted-foreground">메뉴 설정을 읽고 있습니다…</p>;
  }

  return (
    <div className="space-y-3">
      <ul className="divide-y divide-border rounded-md border border-border">
        {state.menus.map((key) => {
          const visible = !state.hidden.includes(key);
          return (
            <li key={key} className="flex items-center justify-between gap-4 px-3 py-2.5">
              <div>
                <div className="text-sm">{MENU_LABELS[key] ?? key}</div>
                <div className="text-xxs text-muted-foreground">
                  {visible ? "일반 사용자에게 보입니다" : "일반 사용자에게 숨겨져 있습니다"}
                </div>
              </div>
              <Switch
                checked={visible}
                disabled={saving}
                onCheckedChange={(checked) => toggle(key, checked)}
                aria-label={`${MENU_LABELS[key] ?? key} 메뉴 노출`}
              />
            </li>
          );
        })}
      </ul>
      <p className="text-xxs text-muted-foreground">
        Chats 는 홈이라 항상 보이고, Insights 와 Settings 는 관리자 전용이라 여기 없습니다.
        관리자에게는 설정과 무관하게 모든 메뉴가 보입니다. 숨긴 메뉴는 사이드바에서만 사라지고,
        해당 기능의 권한은 그대로입니다.
        {!state.persisted && " · 저장소가 연결되지 않아 이 설정은 저장되지 않습니다."}
      </p>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

function SettingsPageInner() {
  const router = useRouter();
  const params = useSearchParams();
  const requested = params.get("tab");
  const tab: Tab = (TABS as readonly string[]).includes(requested ?? "") ? (requested as Tab) : "menus";

  return (
    <>
      <PageHeader
        icon={Settings}
        title="Settings"
        hint="플랫폼 전체에 적용되는 설정을 관리합니다"
      />
      <PageBody>
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
          <Tabs
            value={tab}
            onValueChange={(value) => router.replace(`/settings?tab=${value}`)}
          >
            <TabsList>
              <TabsTrigger value="menus">메뉴</TabsTrigger>
              <TabsTrigger value="rates">모델 요율</TabsTrigger>
              <TabsTrigger value="mcp">MCP Tools</TabsTrigger>
              <TabsTrigger value="teams">팀</TabsTrigger>
            </TabsList>
            <TabsContent value="menus">
              <Section
                title="메뉴 노출"
                hint="일반 사용자(user 역할)의 사이드바에 어떤 메뉴를 보여줄지 정합니다. 바꾸는 즉시 저장되고, 사용자가 다음에 페이지를 열 때 적용됩니다."
              >
                <MenuVisibility />
              </Section>
            </TabsContent>
            <TabsContent value="rates">
              <Section
                title="모델 요율"
                hint={`최근 ${RATE_WINDOW_DAYS}일에 실행된 모델의 1M 토큰당 요율입니다. 저장하거나 지우면 유효 시작일부터의 턴 비용이 바로 다시 계산되고, Insights 의 모델 비용이 그만큼 움직입니다.`}
              >
                <PlotStack>
                  <RateCardPanel days={RATE_WINDOW_DAYS} />
                </PlotStack>
              </Section>
            </TabsContent>
            <TabsContent value="mcp">
              <Section
                title="MCP Tools"
                hint="MCP 서버에 연결해 도구 목록을 확인하고 하나씩 호출해 봅니다. 게이트웨이나 외부 MCP 서버가 살아 있는지 점검할 때 씁니다."
              >
                <McpInspector />
              </Section>
            </TabsContent>
            <TabsContent value="teams">
              <Section
                title="팀"
                hint="팀은 Cognito 그룹 team:<이름> 과 terraform 의 teams 변수로 정해집니다. 여기서는 팀마다 표시 이름, 허용 모델, 하네스 허용 툴 패턴, 일일 비용 경고를 정합니다. 실행 역할은 terraform 이 만들고 바꿀 수 없습니다."
              >
                <TeamsPanel />
              </Section>
            </TabsContent>
          </Tabs>
        </div>
      </PageBody>
    </>
  );
}

/** Admin-only route: non-admins are redirected home. */
export default function SettingsPage() {
  const { allowed, role } = useRequireRole(["admin"]);
  const router = useRouter();

  useEffect(() => {
    if (role != null && !allowed) {
      router.replace("/");
    }
  }, [allowed, role, router]);

  if (!allowed) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-sm text-muted-foreground">접근 권한이 없습니다.</p>
      </div>
    );
  }

  // `useSearchParams` needs a Suspense boundary for the static prerender.
  return (
    <Suspense
      fallback={
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-muted-foreground">Loading…</p>
        </div>
      }
    >
      <SettingsPageInner />
    </Suspense>
  );
}
