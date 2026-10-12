"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { BEDROCK_MODELS } from "@/lib/models";
import { createOrgTeam, fetchGatewayTools } from "@/lib/org";
import { shortTool } from "../orgTable.mjs";
import { ToolPatternControls } from "./ToolPatternControls";

const NAME_RE = /^[a-z0-9-]{2,32}$/;

/**
 * New team. Tools are picked from the gateway's real list, never typed: one
 * unknown action fails the whole Cedar policy, so the server refuses strings
 * it cannot find and this dialog never produces them. The one harness pattern
 * a new team needs, `@builtin`, is a switch (ToolPatternControls).
 */
export function TeamCreateDialog({ open, onOpenChange, onCreated, policiesEnabled }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
  policiesEnabled: boolean;
}) {
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [description, setDescription] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [alert, setAlert] = useState("");
  const [known, setKnown] = useState<{ tools: string[]; shared: string[]; conditioned: string[] } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    fetchGatewayTools().then(setKnown).catch((e) => {
      setKnown({ tools: [], shared: [], conditioned: [] });
      setError(e instanceof Error ? e.message : String(e));
    });
  }, [open]);

  const toggle = (list: string[], set: (v: string[]) => void, id: string) =>
    set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);

  const valid = NAME_RE.test(name) && !["shared", "admin", "user"].includes(name);

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      await createOrgTeam({
        name,
        label: label || undefined,
        description: description || undefined,
        allowed_models: models,
        allowed_tools: tools,
        daily_cost_alert_usd: alert.trim() === "" ? null : Number(alert),
      });
      onCreated();
      onOpenChange(false);
      setName(""); setLabel(""); setDescription(""); setModels([]); setTools([]); setAlert("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  // Shared and conditioned tools are shown but never selectable: the server refuses both.
  const unselectable = new Set([...(known?.shared ?? []), ...(known?.conditioned ?? [])]);
  const selectable = (known?.tools ?? []).filter((t) => !unselectable.has(t));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>새 팀</DialogTitle>
          <DialogDescription>
            Cognito 그룹 team:&lt;이름&gt;, 하네스 실행 역할, 게이트웨이 정책이 함께 만들어집니다. 준비까지 수십 초가 걸립니다.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid gap-1">
            <Label htmlFor="t-name">이름</Label>
            <Input id="t-name" value={name} onChange={(e) => setName(e.target.value.toLowerCase())} placeholder="fin-ops" />
            <p className="text-xxs text-muted-foreground">
              소문자·숫자·하이픈 2~32자. shared·admin·user 는 쓸 수 없습니다. {name && !valid && <span className="text-destructive">형식이 맞지 않습니다.</span>}
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1">
              <Label htmlFor="t-label">표시 이름</Label>
              <Input id="t-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="재무운영" />
            </div>
            <div className="grid gap-1">
              <Label htmlFor="t-alert">일일 비용 경고 (USD)</Label>
              <Input id="t-alert" type="number" min="0" step="0.5" value={alert} onChange={(e) => setAlert(e.target.value)} />
            </div>
          </div>
          <div className="grid gap-1">
            <Label htmlFor="t-desc">설명</Label>
            <Textarea id="t-desc" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div className="grid gap-1">
            <Label>허용 모델 <span className="text-muted-foreground">(비우면 제한 없음)</span></Label>
            <div className="flex flex-wrap gap-1.5">
              {BEDROCK_MODELS.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => toggle(models, setModels, m.id)}
                  className={`rounded-md border px-2 py-1 text-xxs ${models.includes(m.id) ? "border-primary bg-primary/10" : "border-border text-muted-foreground"}`}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>
          <div className="grid gap-1">
            <Label>
              허용 툴{" "}
              <span className="text-muted-foreground">
                {policiesEnabled ? "(하네스 allowedTools 와 게이트웨이 정책에 함께 적용)" : "(이 배포에는 정책 엔진이 없어 하네스 allowedTools 에만 적용)"}
              </span>
            </Label>
            {known === null ? (
              <p className="text-xxs text-muted-foreground">게이트웨이 툴 목록을 읽는 중…</p>
            ) : (
              <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
                {selectable.map((t) => (
                  <button
                    key={t}
                    type="button"
                    title={t}
                    onClick={() => toggle(tools, setTools, t)}
                    className={`rounded-md border px-2 py-1 text-xxs ${tools.includes(t) ? "border-primary bg-primary/10" : "border-border text-muted-foreground"}`}
                  >
                    {shortTool(t)}
                  </button>
                ))}
                {known.shared.map((t) => (
                  <span key={t} title={t} className="rounded-md border border-dashed px-2 py-1 text-xxs text-muted-foreground">
                    {shortTool(t)} · 공유
                  </span>
                ))}
                {known.conditioned.map((t) => (
                  <span key={t} title={t} className="rounded-md border border-dashed px-2 py-1 text-xxs text-muted-foreground">
                    {shortTool(t)} · 조건 정책
                  </span>
                ))}
              </div>
            )}
            <ToolPatternControls id="t-builtin" tools={tools} gatewayTools={selectable} onChange={setTools} />
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>취소</Button>
          <Button onClick={submit} disabled={!valid || saving}>
            {saving && <Loader2 className="mr-1 size-3 animate-spin" />}만들기
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
