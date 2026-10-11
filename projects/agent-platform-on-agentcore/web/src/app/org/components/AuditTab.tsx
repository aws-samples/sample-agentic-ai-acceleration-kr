"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchAudit, orgErrorMessage, type AuditEntry } from "@/lib/org";
import { auditActionLabel } from "../orgTable.mjs";
import { ToneBadge } from "./ToneBadge";

function diff(entry: AuditEntry): string {
  const b = entry.before && typeof entry.before === "object" ? (entry.before as Record<string, unknown>) : null;
  const a = entry.after && typeof entry.after === "object" ? (entry.after as Record<string, unknown>) : null;
  if (!a && !b) return entry.message || "—";
  const keys = Array.from(new Set([...(b ? Object.keys(b) : []), ...(a ? Object.keys(a) : [])]));
  return keys.map((k) => {
    const from = b && k in b ? JSON.stringify(b[k]) : null;
    const to = a && k in a ? JSON.stringify(a[k]) : null;
    return from !== null && to !== null && from !== to ? `${k}: ${from} → ${to}` : `${k}: ${to ?? from}`;
  }).join(" · ");
}

export function AuditTab() {
  const today = new Date();
  const monthAgo = new Date(today.getTime() - 30 * 86400000);
  const [from, setFrom] = useState(monthAgo.toISOString().slice(0, 10));
  const [to, setTo] = useState(today.toISOString().slice(0, 10));
  const [actor, setActor] = useState("");
  const [target, setTarget] = useState("");
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchAudit({ from: `${from}T00:00:00+00:00`, to: `${to}T23:59:59+00:00`, actor, target })
      .then((r) => { setEntries(r.entries); setError(null); })
      .catch((e) => setError(orgErrorMessage(e)));
  }, [from, to, actor, target]);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="flex flex-col gap-3">
      <p className="rounded-md border bg-muted/30 p-2 text-xxs text-muted-foreground">
        이 페이지에서 한 변경만 기록됩니다. Cognito 콘솔이나 Entra ID 에서 한 변경은 여기 없으며, 팀 탭의 드리프트 점검으로 드러납니다.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Input type="date" className="h-8 w-36 text-xs" value={from} onChange={(e) => setFrom(e.target.value)} />
        <span className="text-xxs text-muted-foreground">~</span>
        <Input type="date" className="h-8 w-36 text-xs" value={to} onChange={(e) => setTo(e.target.value)} />
        <Input className="h-8 w-44 text-xs" placeholder="행위자 sub" value={actor} onChange={(e) => setActor(e.target.value)} />
        <Input className="h-8 w-44 text-xs" placeholder="대상(팀 이름·이메일)" value={target} onChange={(e) => setTarget(e.target.value)} />
        <Button size="sm" variant="outline" onClick={load}>조회</Button>
      </div>
      {error && entries !== null && <p className="text-xs text-destructive">{error}</p>}
      {entries === null ? (
        error ? <p className="text-xs text-destructive">{error}</p> : <p className="text-xs text-muted-foreground">불러오는 중…</p>
      ) : (
        <table className="w-full text-xs">
          <thead><tr className="text-left text-muted-foreground"><th className="py-1">시각</th><th>행위자</th><th>행위</th><th>대상</th><th>변경</th><th>결과</th></tr></thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id} className="border-t align-top">
                <td className="py-1.5 whitespace-nowrap text-muted-foreground">{e.at.replace("T", " ").slice(0, 19)}</td>
                <td>{e.actor_email || e.actor_sub}</td>
                <td>{auditActionLabel(e.action)}</td>
                <td className="font-medium">{e.target}</td>
                <td className="max-w-md break-all text-muted-foreground">{diff(e)}</td>
                <td><ToneBadge label={e.result === "ok" ? "성공" : "실패"} tone={e.result === "ok" ? "ok" : "error"} /></td>
              </tr>
            ))}
            {entries.length === 0 && <tr><td colSpan={6} className="py-6 text-center text-muted-foreground">기간 안에 기록이 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  );
}
