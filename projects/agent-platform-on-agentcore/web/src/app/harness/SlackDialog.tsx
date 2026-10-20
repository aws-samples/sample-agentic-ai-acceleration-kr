"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, CircleAlert, CircleDashed, Copy, ExternalLink, Loader2, X } from "lucide-react";
import { Badge, StatusDot } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  createSlackIntegration,
  deleteSlackIntegration,
  getSlackIntegration,
  getSlackManifest,
  SlackApiError,
  testSlackTokens,
  type SlackCheck,
  type SlackDeletionStep,
  type SlackIntegration,
  type SlackManifest,
} from "@/lib/slack";
import { deletionProgress, slackBadge, tokensLookValid } from "./slackStatus.mjs";

type View = "loading" | "setup" | "linked" | "confirm-delete" | "deleted";

/**
 * Link one Slack app to one Harness, show it, unlink it.
 *
 * Tokens live in this component's state only while being typed: they are
 * cleared on save, on close, and on every view change, and the server never
 * sends them back.
 */
export function SlackDialog({
  harnessId,
  harnessName,
  open,
  onOpenChange,
  onChanged,
}: {
  harnessId: string;
  harnessName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged: () => void;
}) {
  const [view, setView] = useState<View>("loading");
  const [integration, setIntegration] = useState<SlackIntegration | null>(null);
  const [manifest, setManifest] = useState<SlackManifest | null>(null);
  const [format, setFormat] = useState<"yaml" | "json">("yaml");
  const [copied, setCopied] = useState(false);
  const [botToken, setBotToken] = useState("");
  const [appToken, setAppToken] = useState("");
  const [checks, setChecks] = useState<SlackCheck[] | null>(null);
  const [checksOk, setChecksOk] = useState(false);
  const [busy, setBusy] = useState<"test" | "save" | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [steps, setSteps] = useState<SlackDeletionStep[] | null>(null);

  const forgetTokens = () => {
    setBotToken("");
    setAppToken("");
    setChecks(null);
    setChecksOk(false);
  };

  const load = useCallback(async () => {
    setView("loading");
    setError(null);
    try {
      const current = await getSlackIntegration(harnessId);
      setIntegration(current);
      setSteps(current?.deletion?.steps ?? null);
      if (current) {
        setView("linked");
      } else {
        setManifest(await getSlackManifest(harnessId));
        setView("setup");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setView("setup");
    }
  }, [harnessId]);

  useEffect(() => {
    if (open) {
      forgetTokens();
      setSteps(null);
      void load();
    } else {
      forgetTokens();
    }
  }, [open, load]);

  const copyManifest = async () => {
    if (!manifest) return;
    await navigator.clipboard.writeText(format === "yaml" ? manifest.yaml : manifest.json);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const runTest = async () => {
    setBusy("test");
    setError(null);
    try {
      const result = await testSlackTokens(harnessId, botToken.trim(), appToken.trim());
      setChecks(result.checks);
      setChecksOk(result.ok);
    } catch (e) {
      setChecks(e instanceof SlackApiError ? e.checks : null);
      setChecksOk(false);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    setBusy("save");
    setError(null);
    try {
      const created = await createSlackIntegration(harnessId, botToken.trim(), appToken.trim());
      forgetTokens();
      setIntegration(created);
      setView("linked");
      onChanged();
    } catch (e) {
      if (e instanceof SlackApiError && e.checks.length > 0) {
        setChecks(e.checks);
        setChecksOk(false);
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    setBusy("delete");
    setError(null);
    try {
      const result = await deleteSlackIntegration(harnessId);
      setSteps(result.steps);
      setIntegration(null);
      setView("deleted");
      onChanged();
    } catch (e) {
      if (e instanceof SlackApiError && e.steps.length > 0) {
        setSteps(e.steps);
        setIntegration((prev) => (prev ? { ...prev, status: "deleting" } : prev));
        setView("linked");
        onChanged();
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const badge = slackBadge(integration);
  const deleting = integration?.status === "deleting";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Slack 연동 · {harnessName}</DialogTitle>
          <DialogDescription>
            Slack 앱 하나를 이 Harness에 연결합니다. 사용자는 봇에게 1:1 DM을 보내 대화하고, 웹과 같은 권한·사용량 규칙이 적용됩니다.
          </DialogDescription>
        </DialogHeader>

        {view === "loading" && (
          <div className="flex items-center gap-2 py-6 text-xs text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> 연동 정보를 읽는 중…
          </div>
        )}

        {view === "setup" && (
          <div className="grid gap-4">
            <section className="grid gap-2">
              <h3 className="text-xs font-semibold">1. Slack 앱 만들기</h3>
              <ol className="list-decimal space-y-1 pl-4 text-xs leading-normal text-muted-foreground">
                <li>
                  아래 버튼으로 manifest가 채워진 앱 생성 화면을 열거나, manifest를 복사해 <span className="font-medium text-foreground">Create New App → From a manifest</span>에 붙여 넣습니다.
                </li>
                <li>
                  <span className="font-medium text-foreground">Install to Workspace</span> 후 OAuth &amp; Permissions의 <span className="font-mono">xoxb-</span> Bot Token을 복사합니다.
                </li>
                <li>
                  Basic Information → App-Level Tokens에서 <span className="font-mono">connections:write</span> scope로 토큰을 만들고 <span className="font-mono">xapp-</span> 토큰을 복사합니다.
                </li>
              </ol>
              {manifest && (
                <div className="grid gap-2">
                  <div className="flex items-center gap-1">
                    {(["yaml", "json"] as const).map((f) => (
                      <button
                        key={f}
                        type="button"
                        onClick={() => setFormat(f)}
                        className={`rounded-md border px-2 py-0.5 text-xxs uppercase ${format === f ? "border-primary bg-primary/10" : "border-border text-muted-foreground"}`}
                      >
                        {f}
                      </button>
                    ))}
                    <span className="flex-1" />
                    <Button size="sm" variant="outline" onClick={copyManifest}>
                      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                      {copied ? "복사됨" : "복사"}
                    </Button>
                    <Button size="sm" variant="outline" asChild>
                      <a href={manifest.create_url} target="_blank" rel="noreferrer">
                        <ExternalLink className="size-3.5" /> Slack에서 열기
                      </a>
                    </Button>
                  </div>
                  <pre className="max-h-48 overflow-auto rounded-md border border-border bg-muted p-2 font-mono text-xxs leading-normal">
                    {format === "yaml" ? manifest.yaml : manifest.json}
                  </pre>
                </div>
              )}
            </section>

            <section className="grid gap-2">
              <h3 className="text-xs font-semibold">2. 토큰 입력</h3>
              <div className="grid gap-1">
                <Label htmlFor="slack-bot">Bot Token</Label>
                <Input
                  id="slack-bot"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="xoxb-…"
                  value={botToken}
                  onChange={(e) => {
                    setBotToken(e.target.value);
                    setChecks(null);
                    setChecksOk(false);
                  }}
                />
              </div>
              <div className="grid gap-1">
                <Label htmlFor="slack-app">App-Level Token</Label>
                <Input
                  id="slack-app"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="xapp-…"
                  value={appToken}
                  onChange={(e) => {
                    setAppToken(e.target.value);
                    setChecks(null);
                    setChecksOk(false);
                  }}
                />
              </div>
              <p className="text-xxs leading-normal text-muted-foreground">
                토큰은 검사 후 AWS Secrets Manager에만 저장되고, 저장한 뒤에는 다시 표시하지 않습니다. 토큰을 바꾸려면 연동을 삭제하고 다시 연결합니다.
              </p>
              {checks && checks.length > 0 && (
                <ul className="grid gap-1 rounded-md border border-border p-2">
                  {checks.map((c) => (
                    <li key={c.id} className="flex items-start gap-1.5 text-xs leading-normal">
                      {c.ok ? (
                        <Check className="mt-0.5 size-3.5 shrink-0 text-success" />
                      ) : (
                        <X className="mt-0.5 size-3.5 shrink-0 text-destructive" />
                      )}
                      <span className={c.ok ? "text-muted-foreground" : "text-destructive"}>{c.message}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            {error && <p className="text-xs text-destructive">{error}</p>}
          </div>
        )}

        {view === "linked" && integration && (
          <div className="grid gap-3">
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
              <dt className="text-muted-foreground">상태</dt>
              <dd>
                {badge && (
                  <Badge shape="chip" variant={badge.variant}>
                    <StatusDot variant={badge.variant} />
                    {badge.label}
                  </Badge>
                )}
              </dd>
              <dt className="text-muted-foreground">워크스페이스</dt>
              <dd>
                {integration.team_url ? (
                  <a className="underline-offset-2 hover:underline" href={integration.team_url} target="_blank" rel="noreferrer">
                    {integration.team_name ?? integration.team_id}
                  </a>
                ) : (
                  integration.team_name ?? integration.team_id
                )}
              </dd>
              <dt className="text-muted-foreground">봇</dt>
              <dd>
                {integration.bot_name ? `@${integration.bot_name}` : integration.bot_user_id}
                {integration.app_id && <span className="ml-1.5 font-mono text-xxs text-muted-foreground">{integration.app_id}</span>}
              </dd>
              {integration.agent_name && (
                <>
                  <dt className="text-muted-foreground">에이전트</dt>
                  <dd>{integration.agent_name}</dd>
                </>
              )}
              {integration.connection.last_event_at && (
                <>
                  <dt className="text-muted-foreground">마지막 이벤트</dt>
                  <dd className="font-mono text-xxs">{integration.connection.last_event_at}</dd>
                </>
              )}
              {integration.connection.last_error && !deleting && (
                <>
                  <dt className="text-muted-foreground">최근 오류</dt>
                  <dd className="font-mono text-xxs text-destructive">{integration.connection.last_error}</dd>
                </>
              )}
              {integration.created_at && (
                <>
                  <dt className="text-muted-foreground">연결</dt>
                  <dd className="text-muted-foreground">
                    {integration.created_at}
                    {integration.created_by ? ` · ${integration.created_by}` : ""}
                  </dd>
                </>
              )}
            </dl>
            {deleting && (
              <div className="grid gap-1.5 rounded-md border border-destructive/30 p-2">
                <p className="text-xs font-medium text-destructive">연동 삭제가 끝나지 않았습니다. 다시 시도해 주세요.</p>
                <DeletionSteps steps={steps} />
              </div>
            )}
            {integration.connection.state === "auth_error" && !deleting && (
              <p className="flex items-start gap-1.5 text-xs leading-normal text-destructive">
                <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
                Slack이 토큰을 거부했습니다. 앱이 삭제·재설치되었거나 토큰이 폐기된 경우입니다. 연동을 삭제하고 새 토큰으로 다시 연결해 주세요.
              </p>
            )}
            {error && <p className="text-xs text-destructive">{error}</p>}
          </div>
        )}

        {view === "confirm-delete" && (
          <div className="grid gap-2 text-xs leading-normal">
            <p>이 Harness의 Slack 연동을 삭제합니다.</p>
            <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
              <li>Slack 연결을 끊고, 진행 중인 답변은 중단합니다.</li>
              <li>Bot Token을 폐기하고 저장된 토큰을 지웁니다. 이후 봇은 응답하지 않습니다.</li>
              <li>DM 대화 기록은 사용자의 웹 대화 목록에 그대로 남습니다.</li>
            </ul>
            {error && <p className="text-destructive">{error}</p>}
          </div>
        )}

        {view === "deleted" && (
          <div className="grid gap-2 text-xs leading-normal">
            <p className="font-medium">Slack 연동을 삭제했습니다.</p>
            <DeletionSteps steps={steps} />
            <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
              <li>
                Slack 앱 자체는 남아 있습니다. 필요 없으면{" "}
                <a className="underline underline-offset-2" href="https://api.slack.com/apps" target="_blank" rel="noreferrer">
                  api.slack.com/apps
                </a>
                에서 앱을 직접 삭제해 주세요.
              </li>
              <li>같은 앱을 다시 쓰려면 워크스페이스에 재설치하고 새 Bot Token·App-Level Token을 발급해 연결합니다.</li>
              <li>Slack에 남은 DM 메시지는 별도로 삭제해야 합니다.</li>
            </ul>
          </div>
        )}

        <DialogFooter>
          {view === "setup" && (
            <>
              <Button variant="outline" onClick={runTest} disabled={busy !== null || !tokensLookValid(botToken, appToken)}>
                {busy === "test" && <Loader2 className="size-3.5 animate-spin" />}
                검사
              </Button>
              <Button onClick={save} disabled={busy !== null || !checksOk}>
                {busy === "save" && <Loader2 className="size-3.5 animate-spin" />}
                저장
              </Button>
            </>
          )}
          {view === "linked" && (
            <Button
              variant={deleting ? "default" : "outline"}
              className={deleting ? "" : "text-destructive hover:text-destructive"}
              disabled={busy !== null}
              onClick={() => (deleting ? remove() : (setError(null), setView("confirm-delete")))}
            >
              {busy === "delete" && <Loader2 className="size-3.5 animate-spin" />}
              {deleting ? "삭제 다시 시도" : "연동 삭제"}
            </Button>
          )}
          {view === "confirm-delete" && (
            <>
              <Button variant="outline" disabled={busy !== null} onClick={() => setView("linked")}>
                취소
              </Button>
              <Button variant="destructive" disabled={busy !== null} onClick={remove}>
                {busy === "delete" && <Loader2 className="size-3.5 animate-spin" />}
                삭제
              </Button>
            </>
          )}
          {view === "deleted" && <Button onClick={() => onOpenChange(false)}>닫기</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeletionSteps({ steps }: { steps: SlackDeletionStep[] | null }) {
  if (!steps) return null;
  return (
    <ul className="grid gap-1">
      {deletionProgress(steps).map((s) => (
        <li key={s.step} className="flex items-center gap-1.5 text-xs">
          {s.state === "done" ? (
            <Check className="size-3.5 text-success" />
          ) : s.state === "failed" ? (
            <X className="size-3.5 text-destructive" />
          ) : (
            <CircleDashed className="size-3.5 text-muted-foreground" />
          )}
          <span className={s.state === "failed" ? "text-destructive" : s.state === "pending" ? "text-muted-foreground" : ""}>
            {s.label}
            {s.error ? ` (${s.error})` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}
