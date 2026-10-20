"use client";

import { useMemo, useState } from "react";
import { RefreshCw, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatMicros } from "@/app/insights/insightsFormat.mjs";
import type { OrgUser } from "@/lib/org";
import { filterUsers, providerLabel, sortUsers, userStatusLabel } from "../orgTable.mjs";
import { InviteDialog } from "./InviteDialog";
import { SortTh } from "./SortHeader";
import { useUserSort, type UserSortKey } from "./useUserSort";
import { ToneBadge } from "./ToneBadge";

/**
 * People, searchable and sortable. `scope="none"` is the people no team claims
 * (admins, newcomers), whose turns Insights reports as unattributed.
 */
export function UserList({ users, teams, scope, showProvider, onOpen, onRefresh }: {
  users: OrgUser[];
  teams: string[];
  scope: "all" | "none";
  showProvider: boolean;
  onOpen: (user: OrgUser) => void;
  onRefresh: () => void;
}) {
  const [q, setQ] = useState("");
  const [team, setTeam] = useState("");
  const [role, setRole] = useState("");
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("");
  const { sort, toggle } = useUserSort();
  const [inviting, setInviting] = useState(false);

  const scoped = useMemo(() => (scope === "none" ? users.filter((u) => u.teams.length === 0) : users), [users, scope]);
  const rows: OrgUser[] = useMemo(
    () => sortUsers(filterUsers(scoped, { q, team: scope === "all" ? team : "", role, provider, status }), sort.key, sort.dir),
    [scoped, scope, q, team, role, provider, status, sort],
  );
  const header = (key: UserSortKey, label: string, right = false) => <SortTh label={label} sortKey={key} sort={sort} onSort={toggle} right={right} />;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Input className="h-8 w-56 text-xs" placeholder="이메일·이름 검색" value={q} onChange={(e) => setQ(e.target.value)} />
        {scope === "all" && (
          <select className="h-8 rounded-md border bg-background pl-2 pr-8 text-xs" value={team} onChange={(e) => setTeam(e.target.value)}>
            <option value="">모든 팀</option>{teams.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
        <select className="h-8 rounded-md border bg-background pl-2 pr-8 text-xs" value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="">모든 역할</option><option value="admin">admin</option><option value="user">user</option>
        </select>
        {showProvider && (
          <select className="h-8 rounded-md border bg-background pl-2 pr-8 text-xs" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="">모든 공급자</option><option value="cognito">Cognito</option><option value="entra">Entra</option>
          </select>
        )}
        <select className="h-8 rounded-md border bg-background pl-2 pr-8 text-xs" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">모든 상태</option><option value="CONFIRMED">활성</option><option value="FORCE_CHANGE_PASSWORD">임시 비밀번호</option><option value="disabled">비활성</option>
        </select>
        <span className="ml-auto text-xxs text-muted-foreground">{rows.length} / {scoped.length}명{showProvider && " · Cognito 는 지금, Entra 는 마지막 로그인 기준"}</span>
        <Button size="sm" variant="outline" onClick={onRefresh}><RefreshCw className="mr-1 size-3" />새로 고침</Button>
        <Button size="sm" onClick={() => setInviting(true)}><UserPlus className="mr-1 size-3" />초대</Button>
      </div>
      {/* The table scrolls inside the pane, header pinned: at 1,000 people the page
          itself grew to tens of thousands of pixels. */}
      <div className="max-h-[calc(100vh-18rem)] min-h-64 overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 z-[1] bg-card shadow-[inset_0_-1px_0_hsl(var(--border))]">
          <tr className="text-left text-muted-foreground">
            {header("email", "사용자")}{showProvider && <th>공급자</th>}{header("role", "역할")}{scope === "all" && header("teams", "팀")}{header("status", "상태")}{header("last_login", "마지막 로그인")}{header("turns_30d", "30일 턴", true)}{header("model_cost_micros_30d", "30일 비용", true)}
          </tr>
        </thead>
        <tbody>
          {rows.map((u) => {
            const s = userStatusLabel(u);
            return (
              <tr key={u.sub} className="cursor-pointer border-t hover:bg-muted/40" onClick={() => onOpen(u)}>
                <td className="py-1.5"><div className="font-medium">{u.email || u.sub}</div>{u.name && <div className="text-xxs text-muted-foreground">{u.name}</div>}</td>
                {showProvider && <td><ToneBadge label={providerLabel(u.provider)} tone="muted" /></td>}
                <td>{u.role}</td>
                {scope === "all" && <td className="text-muted-foreground">{u.teams.join(", ") || "—"}</td>}
                <td><ToneBadge label={s.label} tone={s.tone} /></td>
                <td className="text-muted-foreground">{u.last_login ? u.last_login.slice(0, 10) : "—"}</td>
                <td className="text-right">{u.turns_30d}</td>
                <td className="text-right">{u.turns_30d > 0 ? formatMicros(u.model_cost_micros_30d) : "—"}</td>
              </tr>
            );
          })}
          {rows.length === 0 && <tr><td colSpan={6 + (showProvider ? 1 : 0) + (scope === "all" ? 1 : 0)} className="py-6 text-center text-muted-foreground">조건에 맞는 사용자가 없습니다.</td></tr>}
        </tbody>
      </table>
      </div>
      <InviteDialog open={inviting} onOpenChange={setInviting} teams={teams} onInvited={onRefresh} />
    </div>
  );
}
