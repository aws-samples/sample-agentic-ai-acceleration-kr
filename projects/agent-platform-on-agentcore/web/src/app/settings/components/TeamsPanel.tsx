"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { BEDROCK_MODELS } from "@/lib/models";
import { fetchTeams, putTeam, type TeamConfig } from "@/lib/settings";

function splitList(value: string): string[] {
  return value.split(",").map((s) => s.trim()).filter(Boolean);
}

/**
 * Per-team settings. The set of teams and each team's execution role come from
 * terraform and are read-only here; an admin edits what the platform decides on
 * top of that: label, the models the team may pick, the harness `allowedTools`
 * patterns its harnesses are composed with, and a daily cost alert.
 */
export function TeamsPanel() {
  const [teams, setTeams] = useState<TeamConfig[] | null>(null);
  const [persisted, setPersisted] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    fetchTeams()
      .then((r) => { setTeams(r.teams); setPersisted(r.persisted); })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));

  useEffect(() => { void load(); }, []);

  if (error) return <p className="text-xs text-destructive">{error}</p>;
  if (!teams) return <p className="text-xs text-muted-foreground">불러오는 중…</p>;
  if (teams.length === 0)
    return <p className="text-xs text-muted-foreground">이 배포에는 팀이 없습니다. terraform 변수 <code>teams</code> 에 팀 이름을 넣고 apply 하세요.</p>;

  return (
    <div className="flex flex-col gap-4">
      {!persisted && (
        <p className="text-xxs text-muted-foreground">환경설정 테이블이 없어 저장되지 않습니다. 표시된 값은 terraform 시드입니다.</p>
      )}
      {teams.map((t) => (
        <TeamRow key={t.name} team={t} onSaved={load} />
      ))}
    </div>
  );
}

function TeamRow({ team, onSaved }: { team: TeamConfig; onSaved: () => void }) {
  const [label, setLabel] = useState(team.label);
  const [models, setModels] = useState<string[]>(team.allowed_models);
  const [tools, setTools] = useState(team.allowed_tools.join(", "));
  const [alert, setAlert] = useState(team.daily_cost_alert_usd?.toString() ?? "");
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setNote(null);
    try {
      await putTeam(team.name, {
        label,
        allowed_models: models,
        allowed_tools: splitList(tools),
        daily_cost_alert_usd: alert.trim() === "" ? null : Number(alert),
      });
      setNote("저장됨");
      onSaved();
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const toggleModel = (id: string) =>
    setModels((cur) => (cur.includes(id) ? cur.filter((m) => m !== id) : [...cur, id]));

  return (
    <div className="rounded-md border p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="text-sm font-semibold">{team.name}</span>
        <code className="text-xxs text-muted-foreground">{team.execution_role_arn || "역할 없음"}</code>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="grid gap-1">
          <Label htmlFor={`label-${team.name}`}>표시 이름</Label>
          <Input id={`label-${team.name}`} value={label} onChange={(e) => setLabel(e.target.value)} />
        </div>
        <div className="grid gap-1">
          <Label htmlFor={`alert-${team.name}`}>일일 비용 경고 (USD)</Label>
          <Input id={`alert-${team.name}`} type="number" min="0" value={alert} onChange={(e) => setAlert(e.target.value)} placeholder="비우면 경고 없음" />
        </div>
        <div className="grid gap-1 sm:col-span-2">
          <Label>허용 모델 (비우면 전역 허용 목록 그대로)</Label>
          <div className="flex flex-wrap gap-2">
            {BEDROCK_MODELS.map((m) => (
              <label key={m.id} className="flex items-center gap-1 text-xs">
                <input type="checkbox" checked={models.includes(m.id)} onChange={() => toggleModel(m.id)} />
                {m.label}
              </label>
            ))}
          </div>
        </div>
        <div className="grid gap-1 sm:col-span-2">
          <Label htmlFor={`tools-${team.name}`}>허용 툴 패턴 (하네스 allowedTools)</Label>
          <Input id={`tools-${team.name}`} value={tools} onChange={(e) => setTools(e.target.value)} placeholder="@builtin, @bap-platform-tools/approve_expense" />
          <p className="text-xxs text-muted-foreground">
            쉼표 구분. <code>*</code> 모두, <code>@builtin</code> 내장 툴 전부, <code>@&lt;서버&gt;/&lt;툴&gt;</code> 특정 툴. 비우면 하네스 조합 시 제한을 걸지 않습니다. 실행 시점 강제는 게이트웨이 Cedar 정책이 맡습니다.
          </p>
        </div>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" onClick={save} disabled={saving}>{saving ? "저장 중…" : "저장"}</Button>
        {note && <span className="text-xxs text-muted-foreground">{note}</span>}
      </div>
    </div>
  );
}
