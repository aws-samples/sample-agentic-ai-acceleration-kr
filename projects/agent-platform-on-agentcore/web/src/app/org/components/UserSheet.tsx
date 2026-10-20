"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { deleteOrgUser, resetUserPassword, updateOrgUser, type OrgUser } from "@/lib/org";
import { providerLabel, userStatusLabel } from "../orgTable.mjs";
import { ToneBadge } from "./ToneBadge";
import { UserUsage } from "./UserUsage";

/**
 * One person. Cognito: role, teams, enabled, reset, delete (delete asks for
 * the email to be typed). Entra: read-only with the group ids the token
 * carried, because the server cannot write to Entra.
 */
export function UserSheet({ user, teams, selfSub, showProvider, onClose, onChanged }: {
  user: OrgUser | null; teams: string[]; selfSub: string; showProvider: boolean; onClose: () => void; onChanged: () => void;
}) {
  const [role, setRole] = useState<"admin" | "user">("user");
  const [picked, setPicked] = useState<string[]>([]);
  const [confirmEmail, setConfirmEmail] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Keyed on the sub, not the object: the refetch after a successful action
  // hands us a fresh `user` for the same person, and resetting then would
  // erase the "저장됨" note and the admin's in-progress edits.
  useEffect(() => {
    if (user) { setRole(user.role); setPicked(user.teams); setConfirmEmail(""); setError(null); }
    else setNote(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.sub]);
  if (!user) return null;
  const cognito = user.provider === "cognito";
  const isSelf = user.sub === selfSub;
  const status = userStatusLabel(user);

  // onSuccess runs only when fn resolved, so a failed delete leaves the sheet open.
  const run = async (key: string, fn: () => Promise<unknown>, done?: string, onSuccess?: () => void) => {
    setBusy(key); setNote(null); setError(null);
    try { await fn(); onChanged(); if (done) setNote(done); onSuccess?.(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(null); }
  };

  return (
    <Sheet open={user !== null} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-[480px] overflow-y-auto sm:max-w-[480px]">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">{user.email || user.sub} <ToneBadge label={status.label} tone={status.tone} /></SheetTitle>
          <SheetDescription>
            {[showProvider && providerLabel(user.provider), user.name, user.last_login && `마지막 로그인 ${user.last_login.slice(0, 10)}`].filter(Boolean).join(" · ") || "—"}
          </SheetDescription>
        </SheetHeader>

        <section className="mt-4">
          <h4 className="text-xs font-semibold">최근 30일 사용량</h4>
          <UserUsage sub={user.sub} />
        </section>

        {!cognito ? (
          <section className="mt-4 grid gap-2 text-xs">
            <p className="text-muted-foreground">Entra 사용자의 역할·팀·상태는 Entra ID 에서 바꿉니다. 아래는 마지막 로그인 토큰이 가진 그룹 ID 입니다. 팀 탭에서 이 ID 를 팀에 매핑하면 팀원이 됩니다.</p>
            <div>소속 팀: {user.teams.length ? user.teams.join(", ") : "—"}</div>
            <ul className="rounded-md border p-2 font-mono text-xxs">{user.groups.length ? user.groups.map((g) => <li key={g}>{g}</li>) : <li>—</li>}</ul>
          </section>
        ) : (
          <>
            <section className="mt-4 grid gap-3">
              <div className="flex items-center gap-2">
                <Label>역할</Label>
                <select className="h-8 w-40 rounded-md border bg-background pl-2 pr-8 text-xs" value={role} onChange={(e) => setRole(e.target.value as "admin" | "user")}>
                  <option value="user">user</option><option value="admin">admin</option>
                </select>
                {isSelf && role === "user" && user.role === "admin" && <span className="text-xxs text-muted-foreground">자기 역할은 다른 관리자가 내려야 합니다</span>}
              </div>
              <div className="grid gap-1">
                <Label>팀</Label>
                <div className="flex flex-wrap gap-1.5">
                  {teams.map((t) => (
                    <button key={t} type="button" onClick={() => setPicked(picked.includes(t) ? picked.filter((x) => x !== t) : [...picked, t])}
                      className={`rounded-md border px-2 py-1 text-xxs ${picked.includes(t) ? "border-primary bg-primary/10" : "border-border text-muted-foreground"}`}>{t}</button>
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button size="sm" disabled={busy !== null} onClick={() => run("save", () => updateOrgUser(user.sub, { role, teams: picked }), "저장됨")}>
                  {busy === "save" && <Loader2 className="mr-1 size-3 animate-spin" />}저장
                </Button>
                {note && <span className="text-xxs text-muted-foreground">{note}</span>}
                {error && <span className="text-xxs text-destructive">{error}</span>}
              </div>
            </section>

            <section className="mt-6 grid gap-2 border-t pt-4">
              <h4 className="text-xs font-semibold">계정</h4>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" disabled={busy !== null || isSelf}
                  onClick={() => run("enabled", () => updateOrgUser(user.sub, { enabled: !user.enabled }), user.enabled ? "비활성화됨" : "복구됨")}>
                  {user.enabled ? "비활성화" : "복구"}
                </Button>
                <Button size="sm" variant="outline" disabled={busy !== null}
                  onClick={() => run("reset", () => resetUserPassword(user.sub), "다음 로그인에 비밀번호를 다시 정하게 됩니다")}>
                  비밀번호 초기화
                </Button>
              </div>
              {isSelf && <p className="text-xxs text-muted-foreground">자기 자신은 비활성화·삭제할 수 없습니다.</p>}
            </section>

            {!isSelf && (
              <section className="mt-6 grid gap-2 border-t pt-4">
                <h4 className="text-xs font-semibold text-destructive">삭제</h4>
                <p className="text-xxs text-muted-foreground">계정을 풀에서 지웁니다. 이 사람의 스레드·사용량 기록은 남습니다. 확인을 위해 이메일을 입력하세요.</p>
                <Input value={confirmEmail} onChange={(e) => setConfirmEmail(e.target.value)} placeholder={user.email} />
                <Button size="sm" variant="destructive" disabled={busy !== null || confirmEmail.trim() === "" || confirmEmail.trim().toLowerCase() !== user.email.toLowerCase()}
                  onClick={() => run("delete", () => deleteOrgUser(user.sub), undefined, onClose)}>
                  사용자 삭제
                </Button>
              </section>
            )}
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
