"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Loader2, Trash2, UserMinus, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { BEDROCK_MODELS } from "@/lib/models";
import {
  addTeamMember, deleteOrgTeam, fetchGatewayTools, fetchOrgUsers, fetchTeamMembers, fetchTeamRecords,
  removeTeamMember, updateOrgTeam, type OrgTeam, type OrgUser, type TeamRecord,
} from "@/lib/org";
import { isToolPattern, providerLabel, shortTool, teamStatusLabel, managedByLabel } from "../orgTable.mjs";
import { budgetLabel, budgetTone, formatBudgetCell } from "../teamBudget.mjs";
import { ToolPatternControls } from "./ToolPatternControls";
import { ToneBadge } from "./ToneBadge";
import { TeamUsage } from "./TeamUsage";
import { TypeMarker } from "@/app/registry/components/types";

/**
 * One team: settings (saved → role/policy reapplied), live members, approved
 * records, and delete — disabled with the reason while members or records
 * remain. A team written from outside the server (managed_by other than
 * "platform") keeps its own role/policy: the form says so, and delete drops the row only.
 */
export function TeamSheet({ team, onClose, onChanged }: { team: OrgTeam | null; onClose: () => void; onChanged: () => void }) {
  const [label, setLabel] = useState("");
  const [description, setDescription] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [alert, setAlert] = useState("");
  const [known, setKnown] = useState<{ tools: string[]; shared: string[]; conditioned: string[] } | null>(null);
  const [members, setMembers] = useState<OrgUser[] | null>(null);
  const [records, setRecords] = useState<TeamRecord[] | null>(null);
  const [candidates, setCandidates] = useState<OrgUser[]>([]);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-seed the form only when a different team is opened. Keying on the whole
  // object would wipe in-progress edits on every 3s status poll.
  const teamName = team?.name ?? null;
  useEffect(() => {
    if (!team) return;
    setLabel(team.label); setDescription(team.description); setModels(team.allowed_models);
    setTools(team.allowed_tools); setAlert(team.daily_cost_alert_usd?.toString() ?? "");
    setNote(null); setError(null); setMembers(null); setRecords(null);
    // On a failed read keep the team's gateway names pickable; its patterns are ToolPatternControls'.
    fetchGatewayTools().then(setKnown).catch(() => setKnown({ tools: team.allowed_tools.filter((t) => !isToolPattern(t)), shared: [], conditioned: [] }));
    setLoadFailed(false);
    fetchTeamMembers(team.name).then((r) => setMembers(r.members)).catch(() => { setMembers(null); setLoadFailed(true); });
    fetchTeamRecords(team.name).then((r) => setRecords(r.records)).catch(() => { setRecords(null); setLoadFailed(true); });
    fetchOrgUsers({ provider: "cognito" }).then((r) => setCandidates(r.users)).catch(() => setCandidates([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [teamName]);

  if (!team) return null;
  const external = team.managed_by !== "platform";
  const status = teamStatusLabel(team);

  const run = async (key: string, fn: () => Promise<unknown>, after?: () => void) => {
    setBusy(key); setNote(null); setError(null);
    try { await fn(); after?.(); onChanged(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(null); }
  };

  const reloadMembers = () => fetchTeamMembers(team.name).then((r) => setMembers(r.members)).catch((e) => setError(e instanceof Error ? e.message : String(e)));
  const save = () => run("save", () => updateOrgTeam(team.name, {
    label, description, allowed_models: models, allowed_tools: tools,
    daily_cost_alert_usd: alert.trim() === "" ? null : Number(alert),
  }), () => setNote("저장됨"));

  // null members/records means "unknown" (loading or failed): never allow delete on unknown.
  const transitioning = team.status === "provisioning" || team.status === "deleting";
  const known2 = members !== null && records !== null;
  const canDelete = !transitioning && known2 && members.length === 0 && records.length === 0;
  const deleteReason = transitioning ? "프로비저닝/삭제가 끝난 뒤 지울 수 있습니다"
    : loadFailed && !known2 ? "구성원·레코드를 확인하지 못했습니다"
    : (members?.length ?? 0) > 0 ? "구성원이 남아 있습니다"
    : (records?.length ?? 0) > 0 ? "승인 레코드가 남아 있습니다" : null;
  const toggle = (list: string[], set: (v: string[]) => void, id: string) => set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);
  const notMembers = candidates.filter((c) => !c.teams.includes(team.name));
  // Shared and conditioned tools are shown but never selectable: the server refuses both.
  const unselectable = new Set([...(known?.shared ?? []), ...(known?.conditioned ?? [])]);
  const budget = budgetTone(team.today_cost_micros, team.daily_cost_alert_usd);
  const budgetWord = budgetLabel(budget.tone);

  return (
    <Sheet open={team !== null} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-[560px] overflow-y-auto sm:max-w-[560px]">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">{team.label || team.name} <ToneBadge label={status.label} tone={status.tone} /></SheetTitle>
          <SheetDescription>
            team:{team.name}{external && ` · ${managedByLabel(team)} 관리`} · 오늘 {formatBudgetCell(team.today_cost_micros, team.daily_cost_alert_usd)}
            {budgetWord && <span className="ml-1.5"><ToneBadge label={budgetWord} tone={budget.tone} /></span>}
          </SheetDescription>
        </SheetHeader>

        {team.status === "error" && team.error && (
          <p className="mt-3 rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs">
            {team.error.step} 단계 실패: {team.error.message}
          </p>
        )}

        <section className="mt-4">
          <h4 className="text-xs font-semibold">최근 30일 사용량</h4>
          <TeamUsage team={team.name} />
        </section>

        <section className="mt-6 grid gap-3">
          <h4 className="text-xs font-semibold">설정</h4>
          {external && <p className="text-xxs text-muted-foreground">실행 역할과 게이트웨이 정책은 외부({team.managed_by})에서 만들어져 여기서 바꾼 허용 모델·툴은 하네스 allowedTools 에만 반영됩니다. 삭제는 이 행만 지웁니다.</p>}
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1"><Label>표시 이름</Label><Input value={label} onChange={(e) => setLabel(e.target.value)} /></div>
            <div className="grid gap-1"><Label>일일 비용 경고 (USD)</Label><Input type="number" min="0" step="0.5" value={alert} onChange={(e) => setAlert(e.target.value)} /></div>
          </div>
          <div className="grid gap-1"><Label>설명</Label><Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} /></div>
          <div className="grid gap-1">
            <Label>허용 모델 <span className="text-muted-foreground">(비우면 제한 없음)</span></Label>
            <div className="flex flex-wrap gap-1.5">
              {BEDROCK_MODELS.map((m) => (
                <button key={m.id} type="button" onClick={() => toggle(models, setModels, m.id)}
                  className={`rounded-md border px-2 py-1 text-xxs ${models.includes(m.id) ? "border-primary bg-primary/10" : "border-border text-muted-foreground"}`}>{m.label}</button>
              ))}
            </div>
          </div>
          <div className="grid gap-1">
            <Label>허용 툴</Label>
            <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
              {(known?.tools ?? []).filter((t) => !unselectable.has(t)).map((t) => (
                <button key={t} type="button" title={t} onClick={() => toggle(tools, setTools, t)}
                  className={`rounded-md border px-2 py-1 text-xxs ${tools.includes(t) ? "border-primary bg-primary/10" : "border-border text-muted-foreground"}`}>{shortTool(t)}</button>
              ))}
              {(known?.shared ?? []).map((t) => <span key={t} title={t} className="rounded-md border border-dashed px-2 py-1 text-xxs text-muted-foreground">{shortTool(t)} · 공유</span>)}
              {(known?.conditioned ?? []).map((t) => <span key={t} title={t} className="rounded-md border border-dashed px-2 py-1 text-xxs text-muted-foreground">{shortTool(t)} · 조건 정책</span>)}
            </div>
            {known !== null && (
              <ToolPatternControls id="ts-builtin" tools={tools} gatewayTools={(known.tools).filter((t) => !unselectable.has(t))} onChange={setTools} />
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={save} disabled={busy !== null || transitioning}>{busy === "save" && <Loader2 className="mr-1 size-3 animate-spin" />}저장</Button>
            {note && <span className="text-xxs text-muted-foreground">{note}</span>}
            {error && <span className="text-xxs text-destructive">{error}</span>}
          </div>
        </section>

        <section className="mt-6">
          <h4 className="text-xs font-semibold">구성원 {members ? members.length : ""}</h4>
          <p className="text-xxs text-muted-foreground">Cognito 는 지금 그룹 그대로, Entra 는 마지막 로그인 기준입니다.</p>
          {members === null ? <p className="mt-2 text-xxs text-muted-foreground">{loadFailed ? "불러오지 못했습니다" : "불러오는 중…"}</p> : members.length === 0 ? <p className="mt-2 text-xxs text-muted-foreground">—</p> : (
            <ul className="mt-2 divide-y divide-border/60 text-xs">
              {members.map((m) => (
                <li key={m.sub} className="flex items-center justify-between py-1.5">
                  <span>{m.email || m.sub} <span className="text-muted-foreground">· {providerLabel(m.provider)}{m.provider !== "cognito" && " · 마지막 로그인 기준"}</span></span>
                  {m.provider === "cognito" && (
                    <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => run(`rm:${m.sub}`, () => removeTeamMember(team.name, m.sub), () => void reloadMembers())}>
                      <UserMinus className="size-3" />
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          <div className="mt-2 flex items-center gap-2">
            <select className="h-8 flex-1 rounded-md border bg-background pl-2 pr-8 text-xs" value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">Cognito 사용자 선택…</option>
              {notMembers.map((c) => <option key={c.sub} value={c.sub}>{c.email}</option>)}
            </select>
            <Button size="sm" variant="outline" disabled={!pick || busy !== null} onClick={() => run("add", () => addTeamMember(team.name, pick), () => { setPick(""); void reloadMembers(); })}>
              <UserPlus className="mr-1 size-3" />추가
            </Button>
          </div>
        </section>

        <section className="mt-6">
          <h4 className="text-xs font-semibold">이 팀의 승인 레코드 {records ? records.length : ""}</h4>
          <p className="text-xxs text-muted-foreground">Registry 에서 레코드의 team 메타데이터를 이 팀 이름으로 지정하고 승인하면 여기에 나타납니다.</p>
          {records === null ? <p className="mt-1 text-xxs text-muted-foreground">{loadFailed ? "불러오지 못했습니다" : "불러오는 중…"}</p> : records.length === 0 ? <p className="mt-1 text-xxs text-muted-foreground">—</p> : (
            <ul className="mt-1 space-y-1 text-xs">
              {records.map((r) => (
                <li key={r.record_id}>
                  <Link href={`/registry?record=${encodeURIComponent(r.record_id)}`} className="flex items-center gap-2 hover:underline">
                    <TypeMarker type={r.descriptor_type} />
                    {r.name || r.record_id}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="mt-6 border-t pt-4">
          <Button variant="destructive" size="sm" disabled={!canDelete || busy !== null}
            onClick={() => { if (window.confirm(`팀 ${team.name} 과 그 역할·정책·그룹을 지웁니다. 계속할까요?`)) void run("delete", () => deleteOrgTeam(team.name), onClose); }}>
            <Trash2 className="mr-1 size-3" />팀 삭제
          </Button>
          {deleteReason && <span className="ml-2 text-xxs text-muted-foreground">{deleteReason}</span>}
        </section>
      </SheetContent>
    </Sheet>
  );
}
