"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";
import {
  listDeployedTargets,
  syncDeployedAgents,
  type DeployedTarget,
} from "@/lib/registry";
import { humanizeStatus } from "@/components/ui/badge";
import { StatusBadge } from "./shared";

interface DeployedAgentsDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSynced: () => void;
}

/**
 * Deployed runtimes, harnesses and gateways, with one-click registration for the
 * ones the registry doesn't know about yet. This is the manual counterpart to
 * deploy.sh's automatic sync — needed for anything deployed outside the platform.
 */
export function DeployedAgentsDialog({
  open,
  onOpenChange,
  onSynced,
}: DeployedAgentsDialogProps) {
  const [targets, setTargets] = useState<DeployedTarget[]>([]);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { targets: list } = await listDeployedTargets();
      setTargets(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setTargets([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      setNotice(null);
      load();
    }
  }, [open, load]);

  const sync = async (only?: DeployedTarget) => {
    setSyncing(true);
    setError(null);
    setNotice(null);
    try {
      const result = await syncDeployedAgents(only ? [only.arn] : []);
      const parts = [`${result.registered.length}건 등록`];
      if (result.failed.length > 0) {
        parts.push(`${result.failed.length}건 실패`);
      }
      setNotice(parts.join(", "));
      if (result.failed.length > 0) {
        setError(result.failed.map((f) => `${f.name}: ${f.error}`).join("\n"));
      }
      await load();
      onSynced();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  // Retired targets are excluded: the server skips them unless named explicitly,
  // so counting them here would overstate what "Register all" does.
  const pending = targets.filter((t) => !t.registered && !t.reason && !t.retired);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Deployed resources</DialogTitle>
          <DialogDescription>
            AgentCore에 배포된 runtime, harness, gateway입니다. Registry에 없는
            항목을 등록하고 승인 요청까지 제출합니다. 에이전트는 A2A 레코드로,
            gateway는 도구 표면이므로 MCP 레코드로 등록되어 Agent Harness에서
            조합할 수 있습니다.
          </DialogDescription>
        </DialogHeader>

        {error && (
          <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/[0.07] px-3 py-2 text-xs text-destructive">
            <AlertTriangle className="mt-px size-3.5 shrink-0" />
            <span className="whitespace-pre-wrap leading-normal">{error}</span>
          </div>
        )}
        {notice && (
          <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs leading-normal text-muted-foreground">
            {notice}
          </p>
        )}

        {/* One scrolling list with internal dividers: the dialog can hold a
            dozen targets, and a stack of separately-bordered boxes made it
            impossible to see where one ended. */}
        <div className="-mx-1 max-h-[52vh] overflow-y-auto px-1">
          {loading ? (
            <div className="py-8 text-center">
              <Loader2 className="mx-auto size-5 animate-spin text-muted-foreground" />
            </div>
          ) : targets.length === 0 ? (
            <p className="py-6 text-center text-xs text-muted-foreground">
              배포된 runtime, harness, gateway가 없습니다.
            </p>
          ) : (
            <div className="divide-y divide-border overflow-hidden rounded-md border border-border">
            {targets.map((target) => (
              <div
                key={target.arn}
                className="flex items-start justify-between gap-3 p-2.5 transition-colors hover:bg-muted/50"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1">
                    <span
                      className="truncate text-xs font-semibold"
                      title={target.arn}
                    >
                      {target.name}
                    </span>
                    <Badge shape="code" variant="outline">
                      {target.kind}
                    </Badge>
                    <StatusBadge status={target.status} />
                    {target.registered ? (
                      <Badge shape="tag" variant="success">
                        Registered
                        {target.record_status
                          ? ` · ${humanizeStatus(target.record_status)}`
                          : ""}
                      </Badge>
                    ) : target.retired ? (
                      <Badge shape="tag" variant="secondary">
                        Deprecated
                      </Badge>
                    ) : (
                      <Badge shape="tag" variant="warning">
                        Not in registry
                      </Badge>
                    )}
                  </div>
                  {target.description && (
                    <p className="mt-1 text-xs leading-normal text-muted-foreground">
                      {target.description}
                    </p>
                  )}
                  {target.reason && (
                    <p className="mt-1 text-xs leading-normal text-warning">
                      {target.reason}
                    </p>
                  )}
                  {target.retired && !target.reason && (
                    <p className="mt-1 text-xs leading-normal text-muted-foreground">
                      이전 레코드가 DEPRECATED 되었습니다. 폐기는 되돌릴 수 없어
                      재등록은 새 레코드를 만듭니다 — 일괄 등록에서는 제외됩니다.
                    </p>
                  )}
                </div>
                {!target.registered && !target.reason && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={syncing}
                    onClick={() => sync(target)}
                  >
                    {target.retired ? "Re-register" : "Register"}
                  </Button>
                )}
              </div>
            ))}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" disabled={loading} onClick={load}>
            <RefreshCw className="size-4" />
            Refresh
          </Button>
          <Button
            size="sm"
            disabled={syncing || pending.length === 0}
            onClick={() => sync()}
          >
            {syncing && <Loader2 className="size-4 animate-spin" />}
            {pending.length > 0
              ? `Register all (${pending.length})`
              : "Nothing to register"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
