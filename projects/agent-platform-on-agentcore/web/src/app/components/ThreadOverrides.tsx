"use client";

import { useEffect, useRef, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { getCapabilities } from "@/lib/capabilities";
import { modelLabel } from "@/lib/modelLabel.mjs";
import { hasOverrides } from "@/lib/agent-config";
import { getRecordDefaults, type AgentDefaults } from "@/lib/registry";
import { normalizeOverrideDraft } from "@/lib/overrideDefaults.mjs";
import { useChatContext } from "@/providers/ChatProvider";

/** Select cannot hold "", so the agent's own default gets a sentinel value. */
const DEFAULT_MODEL = "__agent_default__";

/**
 * Per-thread model / system-prompt override for the agent a chat is bound to.
 *
 * The server forwards both per turn — InvokeHarness `model`/`systemPrompt` for
 * a harness, the invoke payload for a runtime agent — and the agent definition
 * is untouched. So this is how to try the same agent on a bigger model for one
 * conversation, or steer it with an extra instruction, without recomposing or
 * redeploying it for everyone.
 *
 * The agent's own defaults (GET /api/registry/records/{id}/defaults) are shown
 * as the baseline: the default model item names the actual model, and the
 * prompt box is pre-filled with the agent's prompt so the user edits what runs
 * rather than guessing at it. A runtime agent's prompt is not knowable (it is a
 * constant in the container), and the box says so. Whatever the user leaves
 * equal to the default is not stored — see normalizeOverrideDraft.
 *
 * The model list is the server's allow-list (GET /api/config), fetched when the
 * popover opens so an operator change shows up: offering anything else would
 * only end in a 403. A stored model that has since left the list is still shown,
 * marked, so the user can see why a turn is refused and pick another.
 *
 * The values persist on the thread (metadata.harness_overrides), so the
 * conversation keeps answering the same way when reopened. The dot on the
 * trigger says an override is in force, since the transcript otherwise gives
 * no sign of which model answered.
 */
export function ThreadOverrides({ className }: { className?: string }) {
  const { overrides, setOverrides, overridesSupported, isLoading, agentConfig } =
    useChatContext();
  const recordId = agentConfig?.registryRecordId;
  const [open, setOpen] = useState(false);
  const [modelId, setModelId] = useState(overrides.modelId ?? "");
  const [systemPrompt, setSystemPrompt] = useState(overrides.systemPrompt ?? "");
  const [allowedModels, setAllowedModels] = useState<string[] | null>(null);
  // null = not fetched yet (or no record to ask about); fields null = unknowable.
  const [defaults, setDefaults] = useState<AgentDefaults | null>(null);
  const [defaultsFailed, setDefaultsFailed] = useState(false);

  // What the prompt box shows when the user has typed nothing: the stored
  // override, else the agent's own prompt once it has arrived.
  const defaultPrompt = defaults?.system_prompt ?? "";
  const promptBaseline = overrides.systemPrompt ?? defaultPrompt;
  const promptBaselineRef = useRef(promptBaseline);

  // Re-sync the drafts whenever the popover opens or the stored value changes
  // underneath (thread switched), so it never shows a stale draft. The prompt
  // baseline also moves when the defaults arrive after opening; the draft
  // follows only if the user has not started editing it.
  useEffect(() => {
    setModelId(overrides.modelId ?? "");
    // Read the previous baseline into a local before the updater runs: React
    // may call the updater lazily, after the ref below has already moved on,
    // and comparing against the *new* baseline left the box empty on the live
    // site (the draft "" never matched the prompt that had just arrived).
    const previousBaseline = promptBaselineRef.current;
    promptBaselineRef.current = promptBaseline;
    setSystemPrompt((draft) =>
      draft === previousBaseline || !open ? promptBaseline : draft
    );
  }, [overrides.modelId, overrides.systemPrompt, promptBaseline, open]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    getCapabilities()
      .then((caps) => {
        if (!cancelled) setAllowedModels(caps.allowedModels ?? []);
      })
      .catch(() => {
        if (!cancelled) setAllowedModels([]);
      });
    if (recordId) {
      setDefaultsFailed(false);
      getRecordDefaults(recordId)
        .then((d) => {
          if (!cancelled) setDefaults(d);
        })
        .catch(() => {
          // Shown as "unknown"; the override still works without a baseline.
          if (!cancelled) setDefaultsFailed(true);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [open, recordId]);

  // A different agent means different defaults; drop the old ones at once so
  // the first open after switching never shows another agent's prompt.
  useEffect(() => {
    setDefaults(null);
    setDefaultsFailed(false);
  }, [recordId]);

  if (!overridesSupported) return null;

  const active = hasOverrides(overrides);
  const next = normalizeOverrideDraft({ modelId, systemPrompt }, defaults);
  const dirty =
    (next.modelId ?? "") !== (overrides.modelId ?? "") ||
    (next.systemPrompt ?? "") !== (overrides.systemPrompt ?? "");
  const models = allowedModels ?? [];
  const storedUnlisted =
    !!modelId && allowedModels !== null && !models.includes(modelId);
  const showModel = allowedModels === null || models.length > 0 || storedUnlisted;

  const defaultModelLabel = defaults?.model_id
    ? `에이전트 기본 모델 · ${modelLabel(defaults.model_id)}`
    : "에이전트 기본 모델";
  const promptPlaceholder = !recordId || defaultsFailed
    ? "비우면 에이전트의 프롬프트를 씁니다. 채우면 그것을 대체합니다."
    : defaults === null
      ? "기본 프롬프트를 불러오는 중…"
      : defaults.system_prompt
        ? "비우면 에이전트의 프롬프트를 씁니다."
        : "이 에이전트의 기본 프롬프트는 런타임에 내장되어 조회할 수 없습니다. 비우면 그 프롬프트를, 채우면 이 내용을 씁니다.";

  const apply = () => {
    setOverrides(next);
    setOpen(false);
  };

  const reset = () => {
    setModelId("");
    setSystemPrompt(defaultPrompt);
    setOverrides({});
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "relative flex h-8 items-center gap-1.5 rounded-full border border-border",
            "bg-card px-2.5 text-xs font-medium text-foreground transition-colors hover:bg-accent",
            active && "border-primary/50 text-primary",
            className
          )}
          title={
            active
              ? "이 대화에 모델/프롬프트 덮어쓰기가 적용돼 있습니다"
              : "이 대화만 다른 모델이나 추가 지시로 실행"
          }
          aria-label="Thread overrides"
        >
          <SlidersHorizontal className="h-3.5 w-3.5 flex-shrink-0" />
          {active && (
            <span
              aria-hidden
              className="absolute -right-0.5 -top-0.5 size-2 rounded-full bg-primary ring-2 ring-background"
            />
          )}
        </button>
      </PopoverTrigger>

      <PopoverContent align="end" side="top" className="w-80 space-y-3 p-3">
        <div>
          <p className="text-xs font-semibold">이 대화만 덮어쓰기</p>
          <p className="mt-0.5 text-xxs leading-normal text-muted-foreground">
            에이전트 정의는 그대로 두고, 이 스레드의 턴에만 적용됩니다. 대화
            메모리는 이어집니다. 기본값과 같게 두면 저장되지 않습니다.
          </p>
        </div>

        {showModel && (
          <div className="grid gap-1.5">
            <Label htmlFor="ov-model" className="text-xs">
              Model
            </Label>
            <Select
              value={modelId || DEFAULT_MODEL}
              onValueChange={(v) => setModelId(v === DEFAULT_MODEL ? "" : v)}
            >
              <SelectTrigger id="ov-model" className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={DEFAULT_MODEL} data-default-model={defaults?.model_id ?? ""}>
                  {defaultModelLabel}
                </SelectItem>
                {models.map((id) => (
                  <SelectItem key={id} value={id}>
                    {modelLabel(id)}
                    {id === defaults?.model_id && " · 기본"}
                  </SelectItem>
                ))}
                {storedUnlisted && (
                  <SelectItem value={modelId} data-unlisted-model={modelId}>
                    {modelLabel(modelId)} · 허용 목록에 없음
                  </SelectItem>
                )}
              </SelectContent>
            </Select>
          </div>
        )}

        <div className="grid gap-1.5">
          <Label htmlFor="ov-prompt" className="text-xs">
            System prompt
            {defaults?.system_prompt && !overrides.systemPrompt && (
              <span className="ml-1.5 font-normal text-muted-foreground">
                에이전트 기본값
              </span>
            )}
          </Label>
          <Textarea
            id="ov-prompt"
            rows={6}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder={promptPlaceholder}
            data-default-prompt={defaults?.system_prompt ? "true" : "false"}
            className="max-h-64 text-xs"
          />
        </div>

        <div className="flex items-center justify-between gap-2">
          <Button
            variant="ghost"
            size="sm"
            disabled={!active && !dirty}
            onClick={reset}
          >
            초기화
          </Button>
          <Button size="sm" disabled={!dirty || isLoading} onClick={apply}>
            적용
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
