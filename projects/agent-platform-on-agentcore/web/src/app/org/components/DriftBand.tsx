"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { driftTitle } from "../orgTable.mjs";
import type { DriftItem } from "@/lib/org";
import { deleteOrgTeam, provisionOrgTeam } from "@/lib/org";

const ACTION_LABEL: Record<string, string> = {
  register: "등록",
  retry: "재시도",
  cleanup: "정리",
  recreate_group: "그룹 재생성",
  retry_delete: "삭제 재시도",
};

/**
 * What the console and the table disagree on. Rendered only when
 * there is something; each line carries the one or two actions the server
 * offers for it. "정리" deletes the row (and anything left of the role/policies).
 * For a team written from outside the server, "정리" drops the row only.
 */
export function DriftBand({ items, onChanged }: { items: DriftItem[]; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (items.length === 0) return null;

  const run = async (item: DriftItem, action: string) => {
    setBusy(`${item.team}:${action}`);
    setError(null);
    try {
      if (action === "register" || action === "retry" || action === "recreate_group") {
        await provisionOrgTeam(item.team);
      } else if (action === "cleanup") {
        if (!window.confirm(`팀 ${item.team} 의 행과 남은 역할·정책을 지웁니다. 계속할까요?`)) return;
        await deleteOrgTeam(item.team);
      } else if (action === "retry_delete") {
        await deleteOrgTeam(item.team);
      }
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
      <div className="text-xs font-semibold">드리프트 {items.length}건</div>
      <p className="mt-0.5 text-xxs text-muted-foreground">
        콘솔·IdP·외부 스택에서 바뀐 것은 감사 로그에 남지 않습니다. 여기서 맞춥니다.
      </p>
      <ul className="mt-2 divide-y divide-border/60">
        {items.map((item) => (
          <li key={`${item.kind}:${item.team}`} className="flex items-start justify-between gap-3 py-2">
            <div>
              <div className="text-xs font-medium">{driftTitle(item)}</div>
              <div className="text-xxs text-muted-foreground">{item.detail}</div>
            </div>
            {item.actions.length > 0 && (
            <div className="flex shrink-0 gap-1">
              {item.actions.map((action) => (
                <Button
                  key={action}
                  size="sm"
                  variant={action === "cleanup" ? "outline" : "default"}
                  disabled={busy !== null}
                  onClick={() => run(item, action)}
                >
                  {busy === `${item.team}:${action}` ? "…" : ACTION_LABEL[action] ?? action}
                </Button>
              ))}
            </div>
            )}
          </li>
        ))}
      </ul>
      {error && <p className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  );
}
