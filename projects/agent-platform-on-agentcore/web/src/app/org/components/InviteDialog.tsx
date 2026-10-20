"use client";

import { useEffect, useRef, useState } from "react";
import { Copy, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { copyText } from "@/lib/utils";
import { inviteUsers, type InviteResult } from "@/lib/org";
import { parseEmails } from "../orgTable.mjs";

/**
 * Invite by email. Cognito mails the temporary password by default; with the
 * switch off the server mints one and this dialog shows it exactly once —
 * it is not stored anywhere, so the admin has to pass it on now.
 */
export function InviteDialog({ open, onOpenChange, teams, initialTeams = [], onInvited }: {
  open: boolean; onOpenChange: (o: boolean) => void; teams: string[]; initialTeams?: string[]; onInvited: () => void;
}) {
  const [text, setText] = useState("");
  const [picked, setPicked] = useState<string[]>(initialTeams);
  const [role, setRole] = useState<"admin" | "user">("user");
  const [sendEmail, setSendEmail] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<InviteResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A response can land after the dialog closed; without this it would set
  // `result` and reopening would show the old temporary passwords.
  const openRef = useRef(open);
  useEffect(() => { openRef.current = open; }, [open]);

  const emails = parseEmails(text);
  const reset = () => { setText(""); setPicked(initialTeams); setRole("user"); setSendEmail(true); setResult(null); setError(null); };

  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const r = await inviteUsers({ emails, teams: picked, role, send_email: sendEmail });
      onInvited();
      if (openRef.current) setResult(r);
    } catch (e) {
      if (openRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => { if (busy) return; if (!o) reset(); onOpenChange(o); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>사용자 초대</DialogTitle>
          <DialogDescription>Cognito 풀에 계정을 만들고 임시 비밀번호를 보냅니다. Entra 사용자는 Entra 에서 초대합니다.</DialogDescription>
        </DialogHeader>
        {result === null ? (
          <div className="grid gap-3">
            <div className="grid gap-1">
              <Label htmlFor="inv-emails">이메일 <span className="text-muted-foreground">(쉼표·줄바꿈 구분, {emails.length}명)</span></Label>
              <Textarea id="inv-emails" rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder="alice@example.com, bob@example.com" />
            </div>
            <div className="grid gap-1">
              <Label>팀</Label>
              <div className="flex flex-wrap gap-1.5">
                {teams.map((t) => (
                  <button key={t} type="button" onClick={() => setPicked(picked.includes(t) ? picked.filter((x) => x !== t) : [...picked, t])}
                    className={`rounded-md border px-2 py-1 text-xxs ${picked.includes(t) ? "border-primary bg-primary/10" : "border-border text-muted-foreground"}`}>{t}</button>
                ))}
                {teams.length === 0 && <span className="text-xxs text-muted-foreground">팀이 없습니다</span>}
              </div>
            </div>
            <div className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-2">
                <Label>역할</Label>
                <select className="h-8 w-40 rounded-md border bg-background pl-2 pr-8 text-xs" value={role} onChange={(e) => setRole(e.target.value as "admin" | "user")}>
                  <option value="user">user</option><option value="admin">admin</option>
                </select>
              </div>
              <div className="flex items-center gap-2">
                <Label htmlFor="inv-mail" className="text-xs">임시 비밀번호 메일 발송</Label>
                <Switch id="inv-mail" checked={sendEmail} onCheckedChange={setSendEmail} />
              </div>
            </div>
            {!sendEmail && <p className="text-xxs text-muted-foreground">메일을 보내지 않으면 임시 비밀번호가 여기 한 번만 표시됩니다. 복사해서 직접 전달하세요.</p>}
            {error && <p className="text-xs text-destructive">{error}</p>}
          </div>
        ) : (
          <div className="grid gap-2 text-xs">
            {result.invited.map((i) => (
              <div key={i.email} className="flex items-center justify-between rounded-md border p-2">
                <span>{i.email} <span className="text-muted-foreground">· {i.role}{i.teams.length ? ` · ${i.teams.join(", ")}` : ""}</span></span>
                {i.temporary_password && (
                  <Button size="sm" variant="outline" onClick={() => void copyText(i.temporary_password!)}>
                    <Copy className="mr-1 size-3" /><code>{i.temporary_password}</code>
                  </Button>
                )}
              </div>
            ))}
            {result.failed.map((f) => <p key={f.email} className="text-destructive">{f.email}: {f.detail}</p>)}
            {result.invited.some((i) => i.temporary_password) && <p className="text-xxs text-muted-foreground">이 창을 닫으면 임시 비밀번호는 다시 볼 수 없습니다.</p>}
          </div>
        )}
        <DialogFooter>
          {result === null ? (
            <>
              <Button variant="outline" onClick={() => { reset(); onOpenChange(false); }} disabled={busy}>취소</Button>
              <Button onClick={submit} disabled={emails.length === 0 || busy}>{busy && <Loader2 className="mr-1 size-3 animate-spin" />}초대</Button>
            </>
          ) : (
            <Button onClick={() => { reset(); onOpenChange(false); }}>닫기</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
