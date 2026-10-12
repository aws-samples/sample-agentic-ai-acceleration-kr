"use client";

import { X } from "lucide-react";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { BUILTIN_PATTERN, toggleEntry } from "../orgTable.mjs";

/**
 * The part of `allowed_tools` the gateway list cannot show. The field holds
 * harness patterns (`@builtin`, `@<server>/<tool>`, `*`) next to gateway names;
 * externally written and pre-page teams hold patterns only. `@builtin` (browser, code
 * interpreter, shell, files) gets a switch; every other entry the gateway does
 * not list — a pattern, or a tool since removed from the gateway — is a dashed
 * chip with its raw text, removable so the team stays savable.
 */
export function ToolPatternControls({ id, tools, gatewayTools, onChange }: {
  id: string;
  tools: string[];
  gatewayTools: string[];
  onChange: (tools: string[]) => void;
}) {
  const listed = new Set(gatewayTools);
  const extra = tools.filter((t) => t !== BUILTIN_PATTERN && !listed.has(t));
  return (
    <div className="grid gap-1.5">
      <div className="flex items-center gap-2">
        <Switch
          id={id}
          checked={tools.includes(BUILTIN_PATTERN)}
          onCheckedChange={(on) => onChange(toggleEntry(tools, BUILTIN_PATTERN, on))}
        />
        <Label htmlFor={id} className="text-xxs font-normal">내장 툴 허용 (@builtin)</Label>
      </div>
      {extra.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {extra.map((t) => (
            <span key={t} title="하네스 allowedTools 패턴 (게이트웨이 목록에 없음)"
              className="inline-flex items-center gap-1 rounded-md border border-dashed px-2 py-1 font-mono text-xxs text-muted-foreground">
              {t}
              <button type="button" aria-label={`${t} 제거`} onClick={() => onChange(toggleEntry(tools, t, false))}
                className="hover:text-foreground">
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
