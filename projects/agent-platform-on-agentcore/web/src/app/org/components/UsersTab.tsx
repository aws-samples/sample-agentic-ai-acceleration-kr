"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatMicros } from "@/app/insights/insightsFormat.mjs";
import { useAuth } from "@/providers/AuthProvider";
import { fetchOrgTeams, fetchOrgUsers, orgErrorMessage, type OrgUser } from "@/lib/org";
import { filterUsers, providerLabel, sortUsers, userStatusLabel } from "../orgTable.mjs";
import { InviteDialog } from "./InviteDialog";
import { ToneBadge } from "./ToneBadge";
import { UserSheet } from "./UserSheet";

type SortKey = "email" | "turns_30d" | "model_cost_micros_30d";

export function UsersTab() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<OrgUser[] | null>(null);
  const [teams, setTeams] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [team, setTeam] = useState("");
  const [role, setRole] = useState("");
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "email", dir: "asc" });
  const [inviting, setInviting] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async (refresh = false) => {
    try {
      const [u, t] = await Promise.all([fetchOrgUsers({ refresh }), fetchOrgTeams()]);
      setUsers(u.users);
      setTeams(t.teams.map((x) => x.name));
      setError(null);
    } catch (e) {
      setError(orgErrorMessage(e));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const rows: OrgUser[] = useMemo(() => sortUsers(filterUsers(users ?? [], { q, team, role, provider, status }), sort.key, sort.dir), [users, q, team, role, provider, status, sort]);
  const current = users?.find((u) => u.sub === selected) ?? null;
  // One sign-in provider says nothing per row; the column, its filter and the
  // freshness note only appear once both Cognito and Entra users exist.
  const showProvider = new Set((users ?? []).map((u) => u.provider)).size > 1;
  const header = (key: SortKey, label: string, right = false) => (
    <th className={`cursor-pointer select-none ${right ? "text-right" : ""}`} onClick={() => setSort((s) => ({ key, dir: s.key === key && s.dir === "desc" ? "asc" : "desc" }))}>
      {label}{sort.key === key ? (sort.dir === "desc" ? " ↓" : " ↑") : ""}
    </th>
  );

  // Only a failed first load replaces the page; a later refresh failure must not unmount an open sheet.
  if (!users) return error ? <p className="text-xs text-destructive">{error}</p> : <p className="text-xs text-muted-foreground">불러오는 중…</p>;

  return (
    <div className="flex flex-col gap-3">
      {error && <p className="text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Input className="h-8 w-56 text-xs" placeholder="이메일·이름 검색" value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="h-8 rounded-md border bg-background pl-2 pr-8 text-xs" value={team} onChange={(e) => setTeam(e.target.value)}>
          <option value="">모든 팀</option>{teams.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
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
        <span className="ml-auto text-xxs text-muted-foreground">{rows.length} / {users.length}명{showProvider && " · Cognito 는 지금, Entra 는 마지막 로그인 기준"}</span>
        <Button size="sm" variant="outline" onClick={() => void load(true)}><RefreshCw className="mr-1 size-3" />새로 고침</Button>
        <Button size="sm" onClick={() => setInviting(true)}><UserPlus className="mr-1 size-3" />초대</Button>
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-muted-foreground">
            {header("email", "사용자")}{showProvider && <th>공급자</th>}<th>역할</th><th>팀</th><th>상태</th><th>마지막 로그인</th>{header("turns_30d", "30일 턴", true)}{header("model_cost_micros_30d", "30일 비용", true)}
          </tr>
        </thead>
        <tbody>
          {rows.map((u) => {
            const s = userStatusLabel(u);
            return (
              <tr key={u.sub} className="cursor-pointer border-t hover:bg-muted/40" onClick={() => setSelected(u.sub)}>
                <td className="py-1.5"><div className="font-medium">{u.email || u.sub}</div>{u.name && <div className="text-xxs text-muted-foreground">{u.name}</div>}</td>
                {showProvider && <td><ToneBadge label={providerLabel(u.provider)} tone="muted" /></td>}
                <td>{u.role}</td>
                <td className="text-muted-foreground">{u.teams.join(", ") || "—"}</td>
                <td><ToneBadge label={s.label} tone={s.tone} /></td>
                <td className="text-muted-foreground">{u.last_login ? u.last_login.slice(0, 10) : "—"}</td>
                <td className="text-right">{u.turns_30d}</td>
                <td className="text-right">{u.turns_30d > 0 ? formatMicros(u.model_cost_micros_30d) : "—"}</td>
              </tr>
            );
          })}
          {rows.length === 0 && <tr><td colSpan={showProvider ? 8 : 7} className="py-6 text-center text-muted-foreground">조건에 맞는 사용자가 없습니다.</td></tr>}
        </tbody>
      </table>
      <InviteDialog open={inviting} onOpenChange={setInviting} teams={teams} onInvited={() => void load(true)} />
      <UserSheet user={current} teams={teams} showProvider={showProvider} selfSub={me?.sub ?? ""} onClose={() => setSelected(null)} onChanged={() => void load(true)} />
    </div>
  );
}
