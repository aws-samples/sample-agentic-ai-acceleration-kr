"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Plus, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/AuthProvider";
import {
  fetchDrift, fetchOrgTeams, fetchOrgUsers, orgErrorMessage,
  type DriftItem, type OrgTeamsResponse, type OrgUser,
} from "@/lib/org";
import { isStaleInFlight, teamStatusLabel, viewFromParams, viewQuery } from "../orgTable.mjs";
import { budgetLabel, budgetTone } from "../teamBudget.mjs";
import { DriftBand } from "./DriftBand";
import { TeamCreateDialog } from "./TeamCreateDialog";
import { TeamDetail } from "./TeamDetail";
import { TeamTable } from "./TeamTable";
import { ToneBadge } from "./ToneBadge";
import { UserList } from "./UserList";
import { UserSheet } from "./UserSheet";

const POLL_MS = 3000;
// Past this many teams the rail gets a filter box; below it the list is the search.
const TEAM_FILTER_AT = 8;

type OrgView = ReturnType<typeof viewFromParams>;

/**
 * Teams and people in one place: the rail lists every team plus the two views
 * no single team owns (everyone, and the people no team claims), and the pane
 * beside it shows the pick. People belong to several teams or none, so they
 * are not only reachable through a team.
 */
export function OrgDirectory({ initial }: { initial: OrgView }) {
  const { user: me } = useAuth();
  const [teams, setTeams] = useState<OrgTeamsResponse | null>(null);
  const [users, setUsers] = useState<OrgUser[] | null>(null);
  const [drift, setDrift] = useState<DriftItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<OrgView>(initial);
  const [person, setPerson] = useState<OrgUser | null>(null);
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("");
  const [memberReload, setMemberReload] = useState(0);

  const loadTeams = useCallback(async () => {
    try {
      const [t, d] = await Promise.all([fetchOrgTeams(), fetchDrift().catch(() => ({ items: [] as DriftItem[] }))]);
      setTeams(t);
      setDrift(d.items);
      setError(null);
    } catch (e) {
      setError(orgErrorMessage(e));
    }
  }, []);
  const loadUsers = useCallback(async (refresh = false) => {
    try {
      setUsers((await fetchOrgUsers({ refresh })).users);
    } catch (e) {
      setError(orgErrorMessage(e));
    }
  }, []);
  useEffect(() => { void loadTeams(); void loadUsers(); }, [loadTeams, loadUsers]);

  // A provisioning or deleting team changes on its own; poll while one is still
  // running. A row stuck past five minutes will not change by itself (the drift
  // band offers its retry), so it does not keep the page polling forever.
  useEffect(() => {
    const now = Date.now();
    if (!teams?.teams.some((t) => (t.status === "provisioning" || t.status === "deleting") && !isStaleInFlight(t, now))) return;
    const id = setInterval(() => void loadTeams(), POLL_MS);
    return () => clearInterval(id);
  }, [teams, loadTeams]);

  // Native history, as the page tabs do: the pick is a view of this page.
  const go = (next: OrgView) => {
    setView(next);
    window.history.replaceState(null, "", `/org${viewQuery(next)}`);
  };

  const teamNames = useMemo(() => (teams?.teams ?? []).map((t) => t.name), [teams]);
  const showProvider = new Set((users ?? []).map((u) => u.provider)).size > 1;
  const teamless = users ? users.filter((u) => u.teams.length === 0).length : "…";
  const shownTeams = (teams?.teams ?? []).filter((t) => {
    const needle = filter.trim().toLowerCase();
    return !needle || t.name.includes(needle) || (t.label || "").toLowerCase().includes(needle);
  });
  const current = view.kind === "team" ? teams?.teams.find((t) => t.name === view.name) ?? null : null;
  const openPerson = person ? users?.find((u) => u.sub === person.sub) ?? person : null;

  // Full-page error only before the first load; afterwards keep the tree
  // mounted so a transient poll failure cannot unmount an open sheet. People
  // are not waited for: the pool read is the slow one (seconds at 1,000), and
  // the team views need it only for the member figures.
  if (!teams) {
    return error
      ? <p className="text-xs text-destructive">{error}</p>
      : <p className="text-xs text-muted-foreground">불러오는 중…</p>;
  }

  const railItem = (key: string, active: boolean, onClick: () => void, label: string, right: ReactNode) => (
    <button key={key} type="button" onClick={onClick}
      className={cn("flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-xs",
        active ? "bg-muted font-medium" : "hover:bg-muted/50")}>
      <span className="truncate">{label}</span>
      <span className="flex shrink-0 items-center gap-1 text-xxs text-muted-foreground">{right}</span>
    </button>
  );

  return (
    <div className="flex flex-col gap-3">
      {error && <p className="text-xs text-destructive">{error}</p>}
      <DriftBand items={drift} onChanged={() => void loadTeams()} />
      {(!teams.provisioner_enabled || !teams.policies_enabled) && (
        <p className="text-xxs text-muted-foreground">
          {!teams.provisioner_enabled && "서버에 역할 생성 권한(boundary)이 없어 팀을 만들 수 없습니다. "}
          {!teams.policies_enabled && "정책 엔진이 없어 게이트웨이 정책은 미연결입니다."}
        </p>
      )}
      <div className="grid grid-cols-[220px_minmax(0,1fr)] gap-4">
        <nav className="flex flex-col gap-0.5 rounded-lg border bg-card p-2 shadow-sm">
          {railItem("teams", view.kind === "teams", () => go({ kind: "teams" }), "모든 팀", teams.teams.length)}
          {railItem("users", view.kind === "users", () => go({ kind: "users" }), "모든 사용자", users ? users.length : "…")}
          {railItem("noteam", view.kind === "noteam", () => go({ kind: "noteam" }), "팀 없음", teamless)}
          <div className="my-1.5 border-t" />
          {teams.teams.length > TEAM_FILTER_AT && (
            <Input className="mb-1 h-7 text-xxs" placeholder="팀 찾기" value={filter} onChange={(e) => setFilter(e.target.value)} />
          )}
          <div className="max-h-[calc(100vh-22rem)] overflow-auto">
          {shownTeams.map((t) => {
            const status = teamStatusLabel(t);
            // The rail flags only what needs a look: a team mid-change, or one near or over its alert.
            const budget = budgetTone(t.today_cost_micros, t.daily_cost_alert_usd).tone;
            const budgetWord = budget === "warn" || budget === "error" ? budgetLabel(budget) : null;
            return railItem(t.name, view.kind === "team" && view.name === t.name, () => go({ kind: "team", name: t.name }), t.label || t.name, (
              <>
                {t.status !== "ready" && <ToneBadge label={status.label} tone={status.tone} />}
                {budgetWord && <ToneBadge label={budgetWord} tone={budget} />}
                {t.members}
              </>
            ));
          })}
          </div>
          <Button size="sm" variant="ghost" className="mt-1 justify-start text-xs" disabled={!teams.provisioner_enabled} onClick={() => setCreating(true)}>
            <Plus className="mr-1 size-3" />새 팀
          </Button>
        </nav>

        <section className="min-w-0 rounded-lg border bg-card p-4 shadow-sm">
          {view.kind === "teams" && (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold">모든 팀 <span className="font-normal text-muted-foreground">{teams.teams.length}</span></h3>
                <Button size="sm" variant="outline" onClick={() => void loadTeams()}><RefreshCw className="mr-1 size-3" />새로 고침</Button>
              </div>
              <TeamTable teams={teams.teams} onSelect={(name) => go({ kind: "team", name })} />
            </div>
          )}
          {(view.kind === "users" || view.kind === "noteam") && (
            <div className="flex flex-col gap-3">
              <h3 className="text-sm font-semibold">{view.kind === "users" ? "모든 사용자" : "팀 없음"}</h3>
              {view.kind === "noteam" && <p className="text-xxs text-muted-foreground">어느 팀에도 속하지 않은 사람입니다. 이들의 턴은 Insights 에서 미귀속으로 집계됩니다.</p>}
              {!users ? <p className="text-xs text-muted-foreground">불러오는 중…</p> : <UserList key={view.kind} users={users} teams={teamNames} scope={view.kind === "users" ? "all" : "none"}
                showProvider={showProvider} onOpen={setPerson} onRefresh={() => void loadUsers(true)} />}
            </div>
          )}
          {view.kind === "team" && (current ? (
            <TeamDetail key={current.name} team={current} users={users ?? []} showProvider={showProvider} reloadKey={memberReload}
              onOpenUser={setPerson}
              onChanged={() => { void loadTeams(); void loadUsers(true); }}
              onDeleted={() => go({ kind: "teams" })} />
          ) : (
            <p className="text-xs text-muted-foreground">팀 {view.name} 이 없습니다.</p>
          ))}
        </section>
      </div>

      <TeamCreateDialog open={creating} onOpenChange={setCreating} onCreated={() => void loadTeams()} policiesEnabled={teams.policies_enabled} />
      <UserSheet user={openPerson} teams={teamNames} showProvider={showProvider} selfSub={me?.sub ?? ""}
        onClose={() => setPerson(null)}
        onChanged={() => { void loadUsers(true); void loadTeams(); setMemberReload((n) => n + 1); }} />
    </div>
  );
}
