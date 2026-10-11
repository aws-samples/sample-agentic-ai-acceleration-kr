"use client";

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Building2 } from "lucide-react";
import { PageBody, PageHeader } from "@/app/components/PageHeader";
import { useRequireRole } from "@/app/components/AppShell";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TeamsTab } from "./components/TeamsTab";
import { UsersTab } from "./components/UsersTab";
import { AuditTab } from "./components/AuditTab";

/**
 * Organization: teams (provisioned for real), people (the pool, live; Entra
 * as of last login) and what admins did here. Admin-only; `?tab=` selects.
 */
const TABS = ["teams", "users", "audit"] as const;
type Tab = (typeof TABS)[number];

function OrgPageInner() {
  const params = useSearchParams();
  const requested = params.get("tab");
  const tab: Tab = (TABS as readonly string[]).includes(requested ?? "") ? (requested as Tab) : "teams";

  return (
    <>
      <PageHeader icon={Building2} title="Organization" hint="팀과 사용자를 관리합니다. Cognito 는 지금 상태, Entra 는 마지막 로그인 기준입니다." />
      <PageBody>
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
          {/* Native history, not router.replace: the tab is a view of this page, and a
              router navigation that never commits (seen live 2026-10-11) left the
              click dead with the address unchanged. Next syncs useSearchParams. */}
          <Tabs value={tab} onValueChange={(v) => window.history.replaceState(null, "", `/org?tab=${v}`)}>
            <TabsList>
              <TabsTrigger value="teams">팀</TabsTrigger>
              <TabsTrigger value="users">사용자</TabsTrigger>
              <TabsTrigger value="audit">감사 로그</TabsTrigger>
            </TabsList>
            <TabsContent value="teams"><section className="rounded-lg border bg-card p-4 shadow-sm"><TeamsTab /></section></TabsContent>
            <TabsContent value="users"><section className="rounded-lg border bg-card p-4 shadow-sm"><UsersTab /></section></TabsContent>
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
