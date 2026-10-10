"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Badge,
  StatusDot,
  humanizeStatus,
  statusVariant,
} from "@/components/ui/badge";
import {
  EmptyState,
  LoadingState,
  Notice,
  PageBody,
  PageHeader,
} from "@/app/components/PageHeader";
import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { BEDROCK_MODELS } from "@/lib/models";
import { useAuth } from "@/providers/AuthProvider";
import { fetchTeams } from "@/lib/settings";
import {
  AlertTriangle,
  Blocks,
  BookOpen,
  Bot,
  Check,
  Cpu,
  Library,
  Loader2,
  Pencil,
  Sparkles,
  Terminal,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { useAppShell, useRequireRole } from "@/app/components/AppShell";
import {
  composeHarness,
  deleteHarness,
  getHarness,
  getHarnessCatalog,
  listHarnesses,
  suggestHarnessName,
  updateHarness,
  HARNESS_NAME_PATTERN,
  type ComposableRecord,
  type HarnessCatalog,
  type HarnessSummary,
  type TruncationSettings,
} from "@/lib/harness";
import { compositionFromHarness } from "./harnessComposition.mjs";
import {
  listDeployedTargets,
  syncDeployedAgents,
  type DeployedTarget,
} from "@/lib/registry";
import {
  listKnowledgeBases,
  type KnowledgeBaseRecord,
} from "@/lib/knowledge";
import { getCapabilities } from "@/lib/capabilities";

/**
 * A knowledge base that can actually be attached.
 *
 * `gateway_arn` is null until provisioning reaches its last step, and it is the
 * only thing the composer sends — narrowing the type here keeps every use below
 * from needing a non-null assertion.
 */
type AttachableKnowledgeBase = KnowledgeBaseRecord & { gateway_arn: string };

const BUILTIN_LABELS: Record<string, { label: string; description: string }> = {
  agentcore_browser: {
    label: "Browser",
    description: "Managed web browsing and automation",
  },
  agentcore_code_interpreter: {
    label: "Code Interpreter",
    description: "Sandboxed Python / JS execution",
  },
};

/**
 * A selectable row; disabled rows explain why they can't be composed.
 *
 * Rows in a group share one bordered container (see `SelectRowGroup`) rather than
 * each drawing its own outline — a list of eight separately-bordered rows read
 * as eight cards instead of one list.
 */
function SelectRow({
  selected,
  disabled,
  title,
  subtitle,
  onToggle,
}: {
  selected: boolean;
  disabled?: boolean;
  title: string;
  subtitle?: string | null;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onToggle}
      aria-pressed={selected}
      className={cn(
        "flex w-full items-start gap-2.5 px-3 py-2 text-left",
        "transition-colors duration-150 ease-snap",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring/40",
        disabled
          ? "cursor-not-allowed opacity-50"
          : selected
            ? "bg-primary-tint"
            : "hover:bg-muted"
      )}
    >
      <span
        className={cn(
          "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-sm border transition-colors",
          selected
            ? "border-primary bg-primary text-primary-foreground"
            : "border-input bg-background"
        )}
      >
        {selected && <Check className="size-3" strokeWidth={3} />}
      </span>
      <span className="min-w-0 flex-1">
        <span
          className={cn(
            "block truncate text-xs",
            selected ? "font-semibold text-primary" : "font-medium"
          )}
        >
          {title}
        </span>
        {subtitle && (
          <span className="mt-0.5 block text-xs leading-normal text-muted-foreground">
            {subtitle}
          </span>
        )}
      </span>
    </button>
  );
}

/** Shared container for a run of SelectRows. */
function SelectRowGroup({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "divide-y divide-border overflow-hidden rounded-md border border-border",
        className
      )}
    >
      {children}
    </div>
  );
}

/** Lists longer than this get a filter input; the registry keeps growing but
 *  the section shouldn't. */
const FILTER_THRESHOLD = 8;

type SelectListItem = {
  key: string;
  title: string;
  subtitle?: string | null;
  disabled?: boolean;
};

/**
 * A SelectRowGroup that stays usable as the registry grows: the list scrolls
 * past ~5 rows instead of stretching the page, and long lists get a text
 * filter. Selection lives in the parent so filtering never drops state.
 */
function SelectList({
  items,
  selectedKeys,
  onToggle,
}: {
  items: SelectListItem[];
  selectedKeys: string[];
  onToggle: (key: string) => void;
}) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const visible = q
    ? items.filter((item) =>
        `${item.title} ${item.subtitle ?? ""}`.toLowerCase().includes(q)
      )
    : items;

  return (
    <div className="space-y-1.5">
      {items.length > FILTER_THRESHOLD && (
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="이름이나 설명으로 검색"
          className="h-8 text-xs"
        />
      )}
      {visible.length === 0 ? (
        <p className="px-1 text-xs text-muted-foreground">
          검색과 일치하는 항목이 없습니다.
        </p>
      ) : (
        <SelectRowGroup className="max-h-60 overflow-y-auto">
          {visible.map((item) => (
            <SelectRow
              key={item.key}
              selected={selectedKeys.includes(item.key)}
              disabled={item.disabled}
              title={item.title}
              subtitle={item.subtitle}
              onToggle={() => onToggle(item.key)}
            />
          ))}
        </SelectRowGroup>
      )}
    </div>
  );
}

function Section({
  icon: Icon,
  title,
  hint,
  selectedCount,
  children,
}: {
  icon: typeof Bot;
  title: string;
  hint?: string;
  selectedCount?: number;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <Icon className="size-3.5 text-muted-foreground" />
        <Label className="caps-label-xs text-foreground">{title}</Label>
        {selectedCount != null && selectedCount > 0 && (
          <Badge shape="count" variant="secondary">
            {selectedCount}
          </Badge>
        )}
      </div>
      {hint && (
        <p className="text-xs leading-normal text-muted-foreground">{hint}</p>
      )}
      {children}
    </div>
  );
}

/** Group the shared Bedrock catalog by provider, plus any id the API returned
 *  that isn't in the catalog so an existing default is never silently dropped. */
function modelGroups(extraId?: string): Array<{
  provider: string;
  models: Array<{ id: string; label: string }>;
}> {
  const groups = new Map<string, Array<{ id: string; label: string }>>();
  for (const model of BEDROCK_MODELS) {
    const list = groups.get(model.provider) ?? [];
    list.push({ id: model.id, label: model.label });
    groups.set(model.provider, list);
  }
  if (extraId && !BEDROCK_MODELS.some((m) => m.id === extraId)) {
    groups.set("Other", [{ id: extraId, label: extraId }]);
  }
  return Array.from(groups, ([provider, models]) => ({ provider, models }));
}

function toggle(list: string[], value: string): string[] {
  return list.includes(value)
    ? list.filter((v) => v !== value)
    : [...list, value];
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Poll until the harness leaves CREATING / UPDATING.
 *
 * POST and PUT /api/harnesses answer as soon as AWS accepts the call — holding
 * the response for the full provisioning (~2min, both ways) ran into the
 * Next.js proxy's 30s timeout, which answers 500 itself. Each poll is a
 * sub-second request.
 */
async function waitForHarness(harnessId: string): Promise<HarnessSummary> {
  for (let i = 0; i < 60; i++) {
    const summary = await getHarness(harnessId);
    if (summary.status !== "CREATING" && summary.status !== "UPDATING") {
      return summary;
    }
    await sleep(3000);
  }
  throw new Error("Harness 작업이 3분 내에 끝나지 않았습니다. 목록에서 상태를 확인하세요.");
}

const TRUNCATION_STRATEGIES: Array<{
  value: TruncationSettings["strategy"];
  label: string;
  hint: string;
}> = [
  {
    value: "sliding_window",
    label: "Sliding window",
    hint: "최근 N개 메시지만 유지 (AWS 기본 150)",
  },
  {
    value: "summarization",
    label: "Summarization",
    hint: "오래된 턴을 요약으로 압축",
  },
  { value: "none", label: "None", hint: "잘라내지 않음" },
];

/** "" or a non-number leaves the field unset; the server applies its default. */
function optionalInt(value: string): number | undefined {
  const n = parseInt(value, 10);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

function splitList(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((v) => v.trim())
    .filter(Boolean);
}

/** Poll until the server's background registration puts the harness in the registry. */
async function waitForRegistration(
  harnessArn: string
): Promise<DeployedTarget | null> {
  for (let i = 0; i < 20; i++) {
    const { targets } = await listDeployedTargets();
    const target = targets.find((t) => t.arn === harnessArn) ?? null;
    if (target?.registered) return target;
    await sleep(3000);
  }
  return null;
}

export default function HarnessPage() {
  const router = useRouter();
  const { config, saveConfig } = useAppShell();
  // Composing is open to everyone; deleting a harness and bulk registry sync are
  // not (see `server/routes/harness.py`). Only those two controls are gated, so
  // the page itself renders for any signed-in user.
  const { allowed: isAdmin } = useRequireRole(["admin"]);
  const { user } = useAuth();
  const myTeams = user?.teams ?? [];
  const [teamOptions, setTeamOptions] = useState<string[]>(myTeams);
  const [team, setTeam] = useState<string>(myTeams.length === 1 ? myTeams[0] : "");
  useEffect(() => {
    if (!isAdmin) return;
    fetchTeams()
      .then((r) => setTeamOptions(r.teams.map((t) => t.name)))
      .catch(() => setTeamOptions(myTeams));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin]);

  const [catalog, setCatalog] = useState<HarnessCatalog | null>(null);
  const [harnesses, setHarnesses] = useState<HarnessSummary[]>([]);
  // Registry status per harness ARN, so a harness that failed to register is
  // visible here rather than only in the transient warning banner.
  const [registryByArn, setRegistryByArn] = useState<Record<string, DeployedTarget>>({});
  const [registering, setRegistering] = useState<string | null>(null);
  const [registryEnabled, setRegistryEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Which phase of the create flow is running, for the submit button label.
  const [savingPhase, setSavingPhase] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [modelId, setModelId] = useState("");
  const [mcpIds, setMcpIds] = useState<string[]>([]);
  const [skillIds, setSkillIds] = useState<string[]>([]);
  // Bucket skills, selected by S3 uri: the registry-off skill picker (registry-on
  // uses skillIds / the registry records instead).
  const [skillBucketUris, setSkillBucketUris] = useState<string[]>([]);
  const [builtins, setBuiltins] = useState<string[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<AttachableKnowledgeBase[]>([]);
  const [gatewayArns, setGatewayArns] = useState<string[]>([]);
  // AWS toolkit skill globs are not offered in the UI any more, but a harness
  // composed when they were still has them; carried through an edit unchanged.
  const [awsSkillPaths, setAwsSkillPaths] = useState<string[]>([]);

  // Advanced options. Strings, so an emptied field means "server default".
  const [maxTokens, setMaxTokens] = useState("");
  const [maxIterations, setMaxIterations] = useState("");
  const [timeoutSeconds, setTimeoutSeconds] = useState("");
  const [allowedTools, setAllowedTools] = useState("");
  const [truncationStrategy, setTruncationStrategy] =
    useState<TruncationSettings["strategy"]>("sliding_window");
  const [truncationCount, setTruncationCount] = useState("");
  const [memoryExpiryDays, setMemoryExpiryDays] = useState("");

  // The harness being edited, or null when composing a new one. Editing reuses
  // the compose form: same fields, but the name is fixed (the API cannot
  // rename) and the description is left to the registry record.
  const [editing, setEditing] = useState<HarnessSummary | null>(null);
  const [loadingEdit, setLoadingEdit] = useState<string | null>(null);
  // Sources on the harness that no catalogue row explains any more. Saving
  // replaces the tool list, so these would be lost — say so before it happens.
  const [unmatched, setUnmatched] = useState<string[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [caps, cat, list, deployed, knowledge] = await Promise.all([
        getCapabilities(),
        getHarnessCatalog(),
        listHarnesses().catch(() => [] as HarnessSummary[]),
        // Registry annotation is supplementary; the page works without it.
        listDeployedTargets().catch(() => ({
          targets: [] as DeployedTarget[],
          unregistered: 0,
        })),
        // Knowledge bases are optional too: the feature may not be provisioned,
        // in which case the section below simply has nothing to offer.
        listKnowledgeBases("READY").catch(() => [] as KnowledgeBaseRecord[]),
      ]);
      setRegistryEnabled(caps.registryEnabled);
      setCatalog(cat);
      setHarnesses(list);
      setKnowledgeBases(
        knowledge.filter((kb): kb is AttachableKnowledgeBase => !!kb.gateway_arn)
      );
      setRegistryByArn(
        Object.fromEntries(
          deployed.targets
            .filter((t) => t.kind === "harness")
            .map((t) => [t.arn, t])
        )
      );
      setModelId((prev) => prev || cat.default_model_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const resetForm = useCallback(() => {
    setEditing(null);
    setUnmatched([]);
    setName("");
    setDescription("");
    setSystemPrompt("");
    setModelId(catalog?.default_model_id ?? "");
    setMcpIds([]);
    setSkillIds([]);
    setSkillBucketUris([]);
    setBuiltins([]);
    setGatewayArns([]);
    setAwsSkillPaths([]);
    setMaxTokens("");
    setMaxIterations("");
    setTimeoutSeconds("");
    setAllowedTools("");
    setTruncationStrategy("sliding_window");
    setTruncationCount("");
    setMemoryExpiryDays("");
  }, [catalog?.default_model_id]);

  /** Load a harness into the form. The listing row lacks the detail fields, so
   *  this fetches the full record first. */
  const startEdit = async (harness: HarnessSummary) => {
    if (!catalog) return;
    setLoadingEdit(harness.harness_id);
    setError(null);
    setNotice(null);
    try {
      const detail = await getHarness(harness.harness_id);
      const composition = compositionFromHarness(detail, catalog, knowledgeBases);
      setEditing(detail);
      setName(detail.harness_name);
      setDescription("");
      setSystemPrompt(detail.system_prompt ?? "");
      setModelId(detail.model_id ?? catalog.default_model_id);
      setMcpIds(composition.mcpIds);
      setSkillIds(composition.skillIds);
      setSkillBucketUris(composition.skillBucketUris);
      setBuiltins(composition.builtins);
      setGatewayArns(composition.gatewayArns);
      setAwsSkillPaths(composition.awsSkillPaths);
      setMaxTokens(detail.max_tokens ? String(detail.max_tokens) : "");
      setMaxIterations(detail.max_iterations ? String(detail.max_iterations) : "");
      setTimeoutSeconds(detail.timeout_seconds ? String(detail.timeout_seconds) : "");
      setAllowedTools((detail.allowed_tools ?? []).join(", "));
      setTruncationStrategy(detail.truncation?.strategy ?? "sliding_window");
      setTruncationCount(
        detail.truncation?.messages_count ? String(detail.truncation.messages_count) : ""
      );
      setMemoryExpiryDays(
        detail.memory?.event_expiry_days ? String(detail.memory.event_expiry_days) : ""
      );
      setUnmatched([
        ...composition.unmatched.mcpUrls.map((url) => `MCP ${url}`),
        ...composition.unmatched.skillUris.map((uri) => `Skill ${uri}`),
      ]);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingEdit(null);
    }
  };

  const nameValid = editing ? true : HARNESS_NAME_PATTERN.test(name.trim());

  const truncation: TruncationSettings = {
    strategy: truncationStrategy,
    messages_count:
      truncationStrategy === "sliding_window" ? optionalInt(truncationCount) : undefined,
  };

  const save = async () => {
    if (!editing) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      // Every selection field goes, empty or not: UpdateHarness replaces the
      // tool and skill lists, so what is ticked is what the harness will have.
      // Model and cap always go together — the API requires modelId whenever
      // the model block is sent, and the cap lives inside that block.
      await updateHarness(editing.harness_id, {
        system_prompt: systemPrompt.trim(),
        model_id: modelId.trim() || undefined,
        max_tokens: optionalInt(maxTokens),
        mcp_record_ids: mcpIds,
        skill_record_ids: skillIds,
        skill_bucket_uris: skillBucketUris,
        aws_skill_paths: awsSkillPaths,
        builtin_tools: builtins,
        gateway_arns: gatewayArns,
        allowed_tools: splitList(allowedTools),
        max_iterations: optionalInt(maxIterations),
        timeout_seconds: optionalInt(timeoutSeconds),
        truncation,
      });

      setSavingPhase("새 버전을 배포하는 중…");
      const harness = await waitForHarness(editing.harness_id);
      if (harness.status !== "READY") {
        setError(
          harness.failure_reason ||
            `Harness 수정이 실패했습니다 (status: ${harness.status ?? "unknown"}).`
        );
      } else {
        setNotice(
          `${harness.harness_name} 을 수정했습니다 (version ${harness.version ?? "?"}). ` +
            "이 에이전트에 묶인 대화는 다음 턴부터 새 정의를 씁니다."
        );
        resetForm();
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
      setSavingPhase(null);
    }
  };

  const submit = async () => {
    if (editing) return save();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      // Answers as soon as CreateHarness is accepted; provisioning and registry
      // registration continue on the server. The phases below are polls.
      const result = await composeHarness({
        name: name.trim(),
        team: team || undefined,
        description: description.trim() || undefined,
        system_prompt: systemPrompt.trim() || undefined,
        model_id: modelId.trim() || undefined,
        mcp_record_ids: mcpIds,
        skill_record_ids: skillIds,
        skill_bucket_uris: skillBucketUris,
        builtin_tools: builtins,
        gateway_arns: gatewayArns,
        allowed_tools: splitList(allowedTools).length ? splitList(allowedTools) : undefined,
        max_tokens: optionalInt(maxTokens),
        max_iterations: optionalInt(maxIterations),
        timeout_seconds: optionalInt(timeoutSeconds),
        truncation,
        memory_event_expiry_days: optionalInt(memoryExpiryDays),
      });

      setSavingPhase("에이전트를 프로비저닝하는 중…");
      const harness = await waitForHarness(result.harness.harness_id);
      if (harness.status !== "READY") {
        setError(
          harness.failure_reason ||
            `Harness 생성이 실패했습니다 (status: ${harness.status ?? "unknown"}).`
        );
        await load();
        return;
      }

      setSavingPhase("Registry에 등록하는 중…");
      const target = await waitForRegistration(harness.harness_arn);
      if (!target?.record_id) {
        setNotice(
          "Harness는 생성됐지만 Registry 등록이 아직 끝나지 않았습니다. " +
            "잠시 후 목록에서 등록 상태를 확인하세요."
        );
        await load();
        return;
      }

      // Bind chat to the new agent straight away — that's the point of composing.
      saveConfig({
        ...(config ?? {}),
        selectedAgent: {
          recordId: target.record_id,
          name: target.record_name ?? harness.harness_name,
          description: description.trim() || undefined,
          harnessArn: harness.harness_arn,
          agentRuntimeArn: harness.runtime_arn ?? undefined,
        },
      });
      router.push("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
      setSavingPhase(null);
    }
  };

  const removeHarness = async (harnessId: string) => {
    setError(null);
    setNotice(null);
    try {
      const { deprecated_records: deprecated } = await deleteHarness(harnessId);
      if (deprecated.length > 0) {
        setNotice(
          `Harness를 삭제하고 연결된 Registry 레코드 ${deprecated.length}건을 DEPRECATED 처리했습니다.`
        );
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  /** Register a harness whose automatic registration failed or was never run. */
  const registerHarness = async (harness: HarnessSummary) => {
    setRegistering(harness.harness_arn);
    setError(null);
    setNotice(null);
    try {
      const result = await syncDeployedAgents([harness.harness_arn]);
      if (result.failed.length > 0) {
        setError(result.failed.map((f) => `${f.name}: ${f.error}`).join("\n"));
      } else if (result.registered.length > 0) {
        setNotice(`${harness.harness_name}을 Registry에 등록했습니다.`);
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRegistering(null);
    }
  };

  const composableCount = (records: ComposableRecord[]) =>
    records.filter((r) => r.composable).length;

  return (
    <>
      <PageHeader
        icon={Blocks}
        title="Agent Harness"
        hint="Registry 구성요소를 조합해 에이전트를 만들고, 만든 뒤에도 고칩니다"
        actions={
          <Button variant="outline" size="sm" onClick={() => router.push("/registry")}>
            <Library className="size-3.5" />
            Registry
          </Button>
        }
      />

      <PageBody>
        {error && (
          <Notice tone="error" icon={AlertTriangle}>
            {error}
          </Notice>
        )}

        {notice && (
          <Notice tone="warning" icon={AlertTriangle}>
            {notice}
          </Notice>
        )}

        {loading ? (
          <LoadingState label="카탈로그를 불러오는 중…" />
        ) : !catalog?.configured ? (
          <EmptyState
            icon={AlertTriangle}
            title="Harness 실행 역할이 설정되지 않았습니다"
            description={
              <>
                <code>infra/modules/iam</code>을 배포하고{" "}
                <code>HARNESS_EXECUTION_ROLE_ARN</code>을 서버에 설정하세요.
              </>
            }
          />
        ) : (
          <div className="grid gap-3 lg:grid-cols-3">
            <div className="space-y-3 lg:col-span-2">
              {editing && (
                <Notice tone="warning" icon={Pencil}>
                  <span className="flex flex-wrap items-center justify-between gap-2">
                    <span>
                      <strong>{editing.harness_name}</strong> 을 수정하고 있습니다
                      {editing.version ? ` (현재 version ${editing.version})` : ""}.
                      저장하면 새 버전이 만들어지고 DEFAULT 엔드포인트가 그쪽으로 옮겨집니다.
                    </span>
                    <Button variant="outline" size="sm" onClick={resetForm}>
                      <X className="size-3.5" />
                      취소
                    </Button>
                  </span>
                </Notice>
              )}

              {editing && unmatched.length > 0 && (
                <Notice tone="error" icon={AlertTriangle}>
                  다음 구성요소는 카탈로그에서 찾을 수 없어 저장 시 제거됩니다:
                  <ul className="mt-1 list-disc pl-4 font-mono text-xxs">
                    {unmatched.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </Notice>
              )}

              <Card>
                <CardHeader>
                  <CardTitle>Identity</CardTitle>
                  <CardDescription>
                    {editing
                      ? "이름은 바꿀 수 없고, 설명은 Registry 레코드에서 관리합니다."
                      : "조합한 에이전트는 Registry의 Agents 탭에 등록됩니다."}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="grid gap-2">
                    <Label htmlFor="h-name">Name</Label>
                    <Input
                      id="h-name"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="research_agent"
                      disabled={!!editing}
                    />
                    {!editing && name.trim() && !nameValid && (
                      <p className="text-xs text-destructive">
                        영문자로 시작하고 영문, 숫자, <code>_</code> 만 40자까지
                        사용할 수 있습니다 (하이픈·점 불가).
                        {suggestHarnessName(name) && (
                          <>
                            {" "}
                            <button
                              type="button"
                              className="underline"
                              onClick={() => setName(suggestHarnessName(name))}
                            >
                              {suggestHarnessName(name)} 로 변경
                            </button>
                          </>
                        )}
                      </p>
                    )}
                  </div>

                  {!editing && (
                    <div className="grid gap-2">
                      <Label htmlFor="h-desc">Description</Label>
                      <Input
                        id="h-desc"
                        value={description}
                        onChange={(e) => setDescription(e.target.value)}
                      />
                    </div>
                  )}

                  <div className="grid gap-2">
                    <Label htmlFor="h-model">Model</Label>
                    <Select value={modelId} onValueChange={setModelId}>
                      <SelectTrigger id="h-model">
                        <SelectValue placeholder="Select model" />
                      </SelectTrigger>
                      <SelectContent>
                        {modelGroups(catalog.default_model_id).map((group) => (
                          <SelectGroup key={group.provider}>
                            <SelectLabel>{group.provider}</SelectLabel>
                            {group.models.map((model) => (
                              <SelectItem key={model.id} value={model.id}>
                                {model.label}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className="truncate text-xs text-muted-foreground">
                      {modelId}
                    </p>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="h-prompt">System prompt</Label>
                    <Textarea
                      id="h-prompt"
                      rows={4}
                      value={systemPrompt}
                      onChange={(e) => setSystemPrompt(e.target.value)}
                      placeholder="You are a helpful assistant."
                    />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Tools & skills</CardTitle>
                  <CardDescription>
                    Registry에 등록된 MCP 서버와 스킬을 조합합니다. shell과
                    file_operations는 기본 제공됩니다.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  {registryEnabled && (
                    <Section
                      icon={Wrench}
                      title={`MCP servers (${composableCount(catalog.mcp_servers)} available)`}
                      hint="엔드포인트가 등록된 레코드만 선택할 수 있습니다."
                      selectedCount={mcpIds.length}
                    >
                      {catalog.mcp_servers.length === 0 ? (
                        <p className="text-xs text-muted-foreground">
                          등록된 MCP 레코드가 없습니다.
                        </p>
                      ) : (
                        <SelectList
                          items={catalog.mcp_servers.map((record) => ({
                            key: record.record_id,
                            title: record.name,
                            subtitle: record.reason || record.description,
                            disabled: !record.composable,
                          }))}
                          selectedKeys={mcpIds}
                          onToggle={(key) => setMcpIds((prev) => toggle(prev, key))}
                        />
                      )}
                    </Section>
                  )}

                  {registryEnabled && (
                    <Section
                      icon={Sparkles}
                      title={`Registry skills (${composableCount(catalog.skills)} available)`}
                      hint="선택한 SKILL.md는 harness가 읽을 수 있도록 S3에 게시됩니다."
                      selectedCount={skillIds.length}
                    >
                      {catalog.skills.length === 0 ? (
                        <p className="text-xs text-muted-foreground">
                          등록된 스킬 레코드가 없습니다.
                        </p>
                      ) : (
                        <SelectList
                          items={catalog.skills.map((record) => ({
                            key: record.record_id,
                            title: record.name,
                            subtitle: record.reason || record.description,
                            disabled: !record.composable,
                          }))}
                          selectedKeys={skillIds}
                          onToggle={(key) => setSkillIds((prev) => toggle(prev, key))}
                        />
                      )}
                    </Section>
                  )}

                  {/* The registry-off skill source. With the registry on it is
                      normally hidden (records are the source) — except while
                      editing a harness that already carries bucket skills, which
                      would otherwise be preserved invisibly with no way to
                      remove them. */}
                  {(!registryEnabled || skillBucketUris.length > 0) && (
                    <Section
                      icon={Sparkles}
                      title={`Skills (${catalog.bucket_skills.length} available)`}
                      hint={
                        registryEnabled
                          ? "이 harness 에 직접 붙어 있는 업로드 번들입니다."
                          : "스킬 버킷에 업로드된 번들을 harness 에 붙입니다."
                      }
                      selectedCount={skillBucketUris.length}
                    >
                      {catalog.bucket_skills.length === 0 ? (
                        <p className="text-xs text-muted-foreground">
                          버킷에 업로드된 스킬이 없습니다.
                        </p>
                      ) : (
                        <SelectList
                          items={catalog.bucket_skills.map((skill) => ({
                            key: skill.uri,
                            title: skill.name,
                            subtitle: skill.description,
                          }))}
                          selectedKeys={skillBucketUris}
                          onToggle={(key) =>
                            setSkillBucketUris((prev) => toggle(prev, key))
                          }
                        />
                      )}
                    </Section>
                  )}

                  <Section
                    icon={BookOpen}
                    title={`Knowledge bases (${knowledgeBases.length} available)`}
                    hint="선택한 Knowledge Base는 Retrieve MCP 도구로 붙습니다. READY 상태만 표시됩니다."
                    selectedCount={gatewayArns.length}
                  >
                    {knowledgeBases.length === 0 ? (
                      <p className="text-xs text-muted-foreground">
                        사용할 수 있는 Knowledge Base가 없습니다.{" "}
                        <button
                          type="button"
                          className="font-medium text-primary underline underline-offset-2"
                          onClick={() => router.push("/knowledge")}
                        >
                          Knowledge 페이지에서 만들기
                        </button>
                      </p>
                    ) : (
                      <SelectList
                        items={knowledgeBases.map((kb) => ({
                          key: kb.gateway_arn,
                          title: kb.shared ? `${kb.name} (공용)` : kb.name,
                          subtitle: kb.description,
                        }))}
                        selectedKeys={gatewayArns}
                        onToggle={(key) =>
                          setGatewayArns((prev) => toggle(prev, key))
                        }
                      />
                    )}
                  </Section>

                  {/* Gateways attached by ARN that are not knowledge bases
                      (e.g. the platform gateway). Listed so an edit shows the
                      whole tool set; each can be detached. */}
                  {gatewayArns.some((arn) => !knowledgeBases.some((kb) => kb.gateway_arn === arn)) && (
                    <Section
                      icon={Wrench}
                      title="Attached gateways"
                      hint="Knowledge Base 가 아닌 AgentCore 게이트웨이. 저장 시 그대로 유지됩니다."
                    >
                      <SelectRowGroup>
                        {gatewayArns
                          .filter((arn) => !knowledgeBases.some((kb) => kb.gateway_arn === arn))
                          .map((arn) => (
                            <div
                              key={arn}
                              className="flex items-center gap-2 px-3 py-2 text-xs"
                            >
                              <span className="min-w-0 flex-1 truncate font-mono text-xxs">
                                {arn.split("/").pop()}
                              </span>
                              <Button
                                variant="ghost"
                                size="icon-sm"
                                aria-label="게이트웨이 분리"
                                className="text-muted-foreground hover:text-destructive"
                                onClick={() =>
                                  setGatewayArns((prev) => prev.filter((a) => a !== arn))
                                }
                              >
                                <X className="size-3.5" />
                              </Button>
                            </div>
                          ))}
                      </SelectRowGroup>
                    </Section>
                  )}

                  <Section
                    icon={Terminal}
                    title="Built-in tools"
                    selectedCount={builtins.length}
                  >
                    <SelectList
                      items={catalog.builtin_tools.map((tool) => ({
                        key: tool,
                        title: BUILTIN_LABELS[tool]?.label || tool,
                        subtitle: BUILTIN_LABELS[tool]?.description,
                      }))}
                      selectedKeys={builtins}
                      onToggle={(key) => setBuiltins((prev) => toggle(prev, key))}
                    />
                  </Section>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Advanced</CardTitle>
                  <CardDescription>
                    비워 두면 서버 기본값을 씁니다. 모든 값은 나중에 수정할 수 있습니다
                    {editing ? "" : " (메모리 보존 기간만 생성 시에 정해집니다)"}.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="grid gap-3 sm:grid-cols-3">
                    <div className="grid gap-2">
                      <Label htmlFor="h-max-tokens">Max output tokens</Label>
                      <Input
                        id="h-max-tokens"
                        type="number"
                        min={1}
                        inputMode="numeric"
                        value={maxTokens}
                        onChange={(e) => setMaxTokens(e.target.value)}
                        placeholder="64000"
                      />
                      <p className="text-xxs leading-normal text-muted-foreground">
                        모델 호출 한 번의 출력 상한. Bedrock 기본 4096은 긴 답변을 오류로 끊습니다.
                      </p>
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="h-max-iter">Max iterations</Label>
                      <Input
                        id="h-max-iter"
                        type="number"
                        min={1}
                        inputMode="numeric"
                        value={maxIterations}
                        onChange={(e) => setMaxIterations(e.target.value)}
                        placeholder="75"
                      />
                      <p className="text-xxs leading-normal text-muted-foreground">
                        한 턴에서 모델↔툴 루프를 도는 최대 횟수.
                      </p>
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="h-timeout">Timeout (s)</Label>
                      <Input
                        id="h-timeout"
                        type="number"
                        min={1}
                        inputMode="numeric"
                        value={timeoutSeconds}
                        onChange={(e) => setTimeoutSeconds(e.target.value)}
                        placeholder="3600"
                      />
                      <p className="text-xxs leading-normal text-muted-foreground">
                        한 턴의 총 실행 시간 상한.
                      </p>
                    </div>
                  </div>

                  {teamOptions.length > 0 && (
                    <div className="grid gap-2">
                      <Label htmlFor="h-team">팀</Label>
                      <select
                        id="h-team"
                        className="h-9 rounded-md border bg-background px-2 text-sm"
                        value={team}
                        onChange={(e) => setTeam(e.target.value)}
                        disabled={!!editing}
                      >
                        {(isAdmin || myTeams.length === 0) && <option value="">shared (기본 역할)</option>}
                        {/* A member of several teams must pick one; without this the
                            browser shows the first team while the form sends "". */}
                        {!isAdmin && myTeams.length > 1 && (
                          <option value="" disabled>팀 선택</option>
                        )}
                        {teamOptions.map((t) => (
                          <option key={t} value={t}>{t}</option>
                        ))}
                      </select>
                      <p className="text-xxs leading-normal text-muted-foreground">
                        이 팀의 실행 역할로 동작합니다. 팀 설정에 허용 툴 목록이 있으면 그 목록이 적용되고(관리자만 덮어쓸 수 있음), 없으면 Allowed tools 를 씁니다. 여러 팀에 속해 있으면 하나를 골라야 합니다.
                      </p>
                    </div>
                  )}

                  <div className="grid gap-2">
                    <Label htmlFor="h-allowed">Allowed tools</Label>
                    <Input
                      id="h-allowed"
                      value={allowedTools}
                      onChange={(e) => setAllowedTools(e.target.value)}
                      placeholder="* (모두 허용)"
                    />
                    <p className="text-xxs leading-normal text-muted-foreground">
                      쉼표로 구분한 툴 이름 허용 목록. 비우면 붙인 툴을 모두 허용합니다.
                    </p>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="grid gap-2">
                      <Label htmlFor="h-trunc">Context truncation</Label>
                      <Select
                        value={truncationStrategy}
                        onValueChange={(v) =>
                          setTruncationStrategy(v as TruncationSettings["strategy"])
                        }
                      >
                        <SelectTrigger id="h-trunc">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {TRUNCATION_STRATEGIES.map((option) => (
                            <SelectItem key={option.value} value={option.value}>
                              {option.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <p className="text-xxs leading-normal text-muted-foreground">
                        {TRUNCATION_STRATEGIES.find((o) => o.value === truncationStrategy)?.hint}
                      </p>
                    </div>
                    {truncationStrategy === "sliding_window" && (
                      <div className="grid gap-2">
                        <Label htmlFor="h-trunc-count">Window (messages)</Label>
                        <Input
                          id="h-trunc-count"
                          type="number"
                          min={1}
                          inputMode="numeric"
                          value={truncationCount}
                          onChange={(e) => setTruncationCount(e.target.value)}
                          placeholder="150"
                        />
                      </div>
                    )}
                  </div>

                  <div className="grid gap-2 sm:max-w-xs">
                    <Label htmlFor="h-mem-expiry">Memory retention (days)</Label>
                    <Input
                      id="h-mem-expiry"
                      type="number"
                      min={1}
                      inputMode="numeric"
                      value={memoryExpiryDays}
                      onChange={(e) => setMemoryExpiryDays(e.target.value)}
                      placeholder="365"
                      disabled={!!editing}
                    />
                    <p className="text-xxs leading-normal text-muted-foreground">
                      {editing
                        ? "생성 시에 정해진 값입니다. 바꾸면 대화 메모리가 재생성될 수 있어 수정에서는 보내지 않습니다."
                        : "AgentCore Memory가 대화 이벤트를 보관하는 기간. AWS 기본은 30일이라 365로 고정해 둡니다."}
                    </p>
                  </div>
                </CardContent>
              </Card>

              <div className="flex gap-2">
                {editing && (
                  <Button
                    size="lg"
                    variant="outline"
                    disabled={saving}
                    onClick={resetForm}
                  >
                    취소
                  </Button>
                )}
                <Button
                  size="lg"
                  className="flex-1"
                  disabled={saving || !nameValid}
                  onClick={submit}
                >
                  {saving && <Loader2 className="size-4 animate-spin" />}
                  {saving
                    ? savingPhase ?? (editing ? "Harness를 수정하는 중…" : "Harness를 생성하는 중…")
                    : editing
                      ? "Save changes"
                      : "Create agent"}
                </Button>
              </div>
            </div>

            <div>
              {/* Sticks while the long composer form scrolls past it. */}
              <Card className="lg:sticky lg:top-0">
                <CardHeader className="flex-row items-center justify-between gap-2">
                  <CardTitle>Existing harnesses</CardTitle>
                  <Badge shape="count" variant="secondary">
                    {harnesses.length}
                  </Badge>
                </CardHeader>
                <CardContent className="space-y-2">
                  {harnesses.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      아직 생성된 harness가 없습니다.
                    </p>
                  ) : (
                    harnesses.map((harness) => {
                      const target = registryByArn[harness.harness_arn];
                      // Absent target means the registry couldn't be read at all;
                      // don't claim it is missing in that case.
                      const missing = target ? !target.registered : false;
                      const variant = statusVariant(harness.status);
                      return (
                        <div
                          key={harness.harness_id}
                          className="group rounded-md border border-border p-2.5 transition-colors hover:border-border-strong"
                        >
                          <div className="flex items-start justify-between gap-2">
                            <span className="min-w-0 flex-1 truncate text-xs font-semibold">
                              {harness.harness_name}
                            </span>
                            <span className="-mr-1 -mt-0.5 flex shrink-0 items-center">
                              {/* Editing is open to whoever may compose (same
                                  gate as create); only READY harnesses can be
                                  updated — AWS rejects a change mid-transition. */}
                              {/* Edit is admin-only, matching PUT /api/harnesses: a harness has no owner,
                                  so recomposing one changes an agent other people already use. */}
                              {isAdmin && (
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label={`${harness.harness_name} 수정`}
                                  disabled={
                                    harness.status !== "READY" ||
                                    saving ||
                                    loadingEdit !== null
                                  }
                                  className={cn(
                                    "text-muted-foreground transition-opacity hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100",
                                    editing?.harness_id === harness.harness_id
                                      ? "text-primary opacity-100"
                                      : "opacity-0"
                                  )}
                                  onClick={() => startEdit(harness)}
                                >
                                  {loadingEdit === harness.harness_id ? (
                                    <Loader2 className="size-3.5 animate-spin" />
                                  ) : (
                                    <Pencil className="size-3.5" />
                                  )}
                                </Button>
                              )}
                              {isAdmin && (
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label={`${harness.harness_name} 삭제`}
                                  className="text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
                                  onClick={() => removeHarness(harness.harness_id)}
                                >
                                  <Trash2 className="size-3.5" />
                                </Button>
                              )}
                            </span>
                          </div>
                          <div className="mt-1.5 flex flex-wrap gap-1">
                            {harness.status && (
                              <Badge
                                shape="chip"
                                variant={variant}
                                title={harness.status}
                              >
                                <StatusDot variant={variant} />
                                {humanizeStatus(harness.status)}
                              </Badge>
                            )}
                            {target?.registered && (
                              <Badge shape="tag" variant="success">
                                Registry
                                {target.record_status
                                  ? ` · ${humanizeStatus(target.record_status)}`
                                  : ""}
                              </Badge>
                            )}
                            {missing && (
                              <Badge
                                shape="tag"
                                variant={target?.retired ? "secondary" : "warning"}
                              >
                                {target?.retired
                                  ? "Registry deprecated"
                                  : "Registry 미등록"}
                              </Badge>
                            )}
                          </div>
                          {harness.model_id && (
                            <p className="mt-1.5 flex items-center gap-1 truncate font-mono text-xxs text-muted-foreground">
                              <Cpu className="size-3 shrink-0" />
                              {harness.model_id}
                            </p>
                          )}
                          {/* Manual registration goes through the account-wide
                              registry sync, which is admin-only. A non-admin
                              rarely needs it: their own create registers itself
                              server-side, and this button only covers the case
                              where that background step failed. */}
                          {missing && !target?.reason && isAdmin && (
                            <Button
                              size="sm"
                              variant="outline"
                              className="mt-2 w-full"
                              disabled={registering === harness.harness_arn}
                              onClick={() => registerHarness(harness)}
                            >
                              {registering === harness.harness_arn && (
                                <Loader2 className="size-3.5 animate-spin" />
                              )}
                              {target?.retired
                                ? "새 레코드로 재등록"
                                : "Registry에 등록"}
                            </Button>
                          )}
                          {harness.failure_reason && (
                            <p className="mt-1.5 text-xs leading-normal text-destructive">
                              {harness.failure_reason}
                            </p>
                          )}
                        </div>
                      );
                    })
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
        )}
      </PageBody>
    </>
  );
}
