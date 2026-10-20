"use client";

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Building2 } from "lucide-react";
import { PageBody, PageHeader } from "@/app/components/PageHeader";
import { useRequireRole } from "@/app/components/AppShell";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { OrgDirectory } from "./components/OrgDirectory";
import { viewFromParams } from "./orgTable.mjs";
import { AuditTab } from "./components/AuditTab";

/**
 * Organization: teams (provisioned for real) and the people in them (the pool,
 * live; Entra as of last login) in one directory, and what admins did here.
 * Admin-only; `?tab=audit` opens the log, `?team=` / `?view=` pick in the
 * directory (the old `?tab=users` lands on 모든 사용자).
 */
type Tab = "teams" | "audit";

function OrgPageInner() {
  const params = useSearchParams();
  const requested = params.get("tab");
  const tab: Tab = requested === "audit" ? "audit" : "teams";

  return (
    <>
      <PageHeader icon={Building2} title="Organization" hint="팀과 사용자를 관리합니다." />
      <PageBody>
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
          {/* Native history, not router.replace: the tab is a view of this page, and a
              router navigation that never commits (seen live 2026-10-11) left the
              click dead with the address unchanged. Next syncs useSearchParams. */}
          <Tabs value={tab} onValueChange={(v) => window.history.replaceState(null, "", v === "audit" ? "/org?tab=audit" : "/org")}>
            <TabsList>
              <TabsTrigger value="teams">팀·사용자</TabsTrigger>
              <TabsTrigger value="audit">감사 로그</TabsTrigger>
            </TabsList>
            <TabsContent value="teams"><OrgDirectory initial={viewFromParams(new URLSearchParams(params.toString()))} /></TabsContent>
            <TabsContent value="audit"><section className="rounded-lg border bg-card p-4 shadow-sm"><AuditTab /></section></TabsContent>
          </Tabs>
        </div>
      </PageBody>
    </>
  );
}

export default function OrgPage() {
  const { allowed, role } = useRequireRole(["admin"]);
  const router = useRouter();
  useEffect(() => { if (role != null && !allowed) router.replace("/"); }, [allowed, role, router]);
  if (!allowed) return <div className="flex flex-1 items-center justify-center"><p className="text-sm text-muted-foreground">접근 권한이 없습니다.</p></div>;
  return (
    <Suspense fallback={<div className="flex flex-1 items-center justify-center"><p className="text-sm text-muted-foreground">Loading…</p></div>}>
      <OrgPageInner />
    </Suspense>
  );
}
