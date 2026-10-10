"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Loader2,
  MessagesSquare,
  Pencil,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import {
  isChattable,
  triggerRecordSync,
  type RegistryRecordDetail,
  type StatusAction,
} from "@/lib/registry";
import type { HarnessSummary } from "@/lib/harness";
import { cn } from "@/lib/utils";
import { TypeMarker } from "./types";
import { RecordTryout } from "./RecordTryout";
import { McpToolTryout } from "./McpToolTryout";
import { SkillBundlePanel } from "./SkillBundlePanel";
import { RecordUsageTab } from "./RecordUsageTab";
import {
  StatusBadge,
  formatTime,
  Field,
  DescriptorDetail,
  HarnessDetail,
  McpSystemInfo,
  mcpEndpoint,
  mcpGatewayArn,
  STATUS_ACTIONS,
} from "./shared";

interface RecordDetailPanelProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  detail: RegistryRecordDetail | null;
  harness: HarnessSummary | null;
  isAdmin: boolean;
  onChat: (record: RegistryRecordDetail) => void;
  onStatus: (id: string, action: StatusAction) => void;
  // Reserved for Task 8: edit dialog.
  // When Task 8 adds the RecordEditDialog, this will be wired up.
  onEdit?: (detail: RegistryRecordDetail) => void;
  onDelete?: (detail: RegistryRecordDetail) => void;
  /** Re-read the record after something changed it out from under the panel. */
  onRefresh?: () => void;
  /**
   * Open another record in this panel. A skill's or MCP server's Usage names
   * the agents that reach it, and evaluation lives on those agents, so the tab
   * links to them rather than offering an evaluation this record cannot have.
   */
  onOpenRecord?: (recordId: string) => void;
}

/** "AWS::BedrockAgentCore::Runtime" → "Runtime": the kind of resource, without the CFN prefix. */
function humanSourceType(sourceType?: string | null): string {
  if (!sourceType) return "—";
  const last = sourceType.split("::").pop();
  return last || sourceType;
}

function credentialLabel(credential: "none" | "iam" | "oauth"): string {
  switch (credential) {
    case "none":
      return "None (public)";
    case "iam":
      return "IAM (platform sync role)";
    case "oauth":
      return "OAuth";
  }
}

/**
 * Metadata values are strings or booleans. A string that is an http(s) URL is
 * shown as a link; anything else is shown as typed. Only http(s) is linked, so a
 * stored value can never become a javascript: link.
 */
function renderMetaValue(value: string | boolean) {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (/^https?:\/\//i.test(value)) {
    return (
      <a
        href={value}
        target="_blank"
        rel="noopener noreferrer"
        className="break-all text-primary underline-offset-4 hover:underline"
      >
        {value}
      </a>
    );
  }
  return value;
}

export function RecordDetailPanel({
  open,
  onOpenChange,
  detail,
  harness,
  isAdmin,
  onChat,
  onStatus,
  onEdit,
  onDelete,
  onRefresh,
  onOpenRecord,
}: RecordDetailPanelProps) {
  const [usageOpen, setUsageOpen] = useState(false);
  const [syncing, setSyncing] = useState(false);

  // The sync is asynchronous: the record goes UPDATING, then DRAFT. The toast says
  // so, because a green "synced" with the record still unsubmitted would mislead.
  const resync = async () => {
    if (!detail) return;
    setSyncing(true);
    try {
      await triggerRecordSync(detail.record_id);
      toast.success(
        "동기화를 시작했습니다. 완료되면 DRAFT 로 돌아오며 다시 제출해야 합니다."
      );
      onRefresh?.();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  const metaEntries = Object.entries(detail?.custom_metadata ?? {});

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="flex w-full flex-col gap-0 p-0 sm:max-w-[640px]">
        {!detail ? (
          <div className="flex flex-1 items-center justify-center">
            <SheetTitle className="sr-only">Loading agent detail</SheetTitle>
            <Loader2 className="size-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <>
            <div className="border-b border-border px-5 pb-4 pt-5">
              <SheetHeader>
                <SheetTitle className="flex flex-wrap items-center gap-2 pr-8">
                  <TypeMarker type={detail.descriptor_type} />
                  {detail.name}
                  <StatusBadge status={detail.status} />
                </SheetTitle>
                {detail.description && (
                  <SheetDescription>{detail.description}</SheetDescription>
                )}
              </SheetHeader>
              <div className="mt-2.5 flex flex-wrap gap-1">
                {detail.descriptor_type && (
                  <Badge shape="code" variant="outline">
                    {detail.descriptor_type}
                  </Badge>
                )}
                {detail.version && (
                  <Badge shape="count" variant="secondary">
                    v{detail.version}
                  </Badge>
                )}
                {detail.harness_arn && (
                  <Badge shape="tag" variant="default">
                    Harness
                  </Badge>
                )}
                {detail.auto_detected && (
                  <Badge shape="tag" variant="secondary">
                    Auto-detected
                  </Badge>
                )}
                {detail.compliance_status === "NON_COMPLIANT" && (
                  <Badge
                    shape="tag"
                    variant="destructive"
                    title="메타데이터 스키마가 바뀌어 현재 값이 규칙에 맞지 않습니다. Edit에서 값을 고쳐주세요."
                  >
                    Metadata non-compliant
                  </Badge>
                )}
              </div>
            </div>

            <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4 text-sm">
              {detail.status_reason && (
                <p className="rounded-md border border-border bg-muted/40 p-2.5 text-xs leading-normal text-muted-foreground">
                  {detail.status_reason}
                </p>
              )}

              {/* A non-approved revision is the draft of something already live.
                  Say so, or an edited agent looks like it has been taken down. */}
              {detail.status !== "APPROVED" && detail.discoverable && (
                <p className="text-xs leading-normal text-muted-foreground">
                  승인본이 검색과 채팅에 계속 제공됩니다. 이 리비전은 승인 후 반영됩니다.
                </p>
              )}

              <DescriptorDetail record={detail} />

              {/* What is actually in S3 for this skill. The SKILL.md above is the
                  record's copy, indexed for search; this is the directory the
                  harness fetches, which is the thing that can be replaced. */}
              {detail.descriptor_type === "AGENT_SKILLS" && (
                <SkillBundlePanel
                  record={detail}
                  isAdmin={isAdmin}
                  onReplaced={onRefresh ?? (() => {})}
                />
              )}

              {harness && <HarnessDetail harness={harness} />}

              <details
                className="group rounded-md border border-border"
                onToggle={(e) => setUsageOpen(e.currentTarget.open)}
              >
                <summary className="cursor-pointer select-none px-3 py-2 caps-label-xs text-muted-foreground transition-colors hover:text-foreground">
                  Usage
                </summary>
                <div className="border-t border-border px-3 py-3">
                  {usageOpen && (
                    <RecordUsageTab
                      recordId={detail.record_id}
                      // Only a record that binds to a deployed agent owns
                      // threads, and a batch evaluation runs over threads — so
                      // evaluation is offered on these two types and nowhere else.
                      isAgent={
                        detail.descriptor_type === "A2A" ||
                        detail.descriptor_type === "CUSTOM"
                      }
                      onOpenRecord={onOpenRecord}
                    />
                  )}
                </div>
              </details>

              <div className="grid grid-cols-2 gap-3">
                <Field label="Created" value={formatTime(detail.created_at)} />
                <Field label="Updated" value={formatTime(detail.updated_at)} />
              </div>

              {metaEntries.length > 0 && (
                <div>
                  <p className="mb-1.5 caps-label-xs text-muted-foreground">
                    Custom metadata
                  </p>
                  <dl className="divide-y divide-border overflow-hidden rounded-md border border-border">
                    {metaEntries.map(([key, value]) => (
                      <div
                        key={key}
                        className="grid grid-cols-[8rem_1fr] gap-2 px-2.5 py-2 text-xs"
                      >
                        <dt className="break-all font-mono text-muted-foreground">
                          {key}
                        </dt>
                        <dd className="min-w-0 break-words">{renderMetaValue(value)}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}

              {detail.auto_detected && (
                <div className="space-y-3 rounded-md border border-border bg-muted/40 p-3">
                  <p className="caps-label-xs text-muted-foreground">Provenance</p>
                  <Field label="Source" value={humanSourceType(detail.source_type)} />
                  {detail.source_arn && (
                    <Field label="Source ARN" value={detail.source_arn} mono />
                  )}
                  <p className="text-xs leading-normal text-muted-foreground">
                    이 레코드의 디스크립터는 자동 감지기가 관리합니다.
                  </p>
                </div>
              )}

              {detail.sync_source && (
                <div className="space-y-3 rounded-md border border-border p-3">
                  <div className="flex items-center justify-between gap-2">
                    <p className="caps-label-xs text-muted-foreground">Synchronization</p>
                    {isAdmin && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={resync}
                        disabled={syncing}
                      >
                        <RefreshCw
                          className={cn("size-3.5", syncing && "animate-spin")}
                        />
                        Re-sync
                      </Button>
                    )}
                  </div>
                  <Field label="URL" value={detail.sync_source.url} mono />
                  <Field
                    label="Credential"
                    value={credentialLabel(detail.sync_source.credential)}
                  />
                </div>
              )}

              {isChattable(detail) && (
                <RecordTryout record={detail} onChat={onChat} />
              )}

              {/* The MCP counterpart of the agent try-out. Admin-only for the
                  same reason the route behind it is: calling a tool with
                  hand-written arguments is an operator's action, with no model
                  deciding what to pass. Needs a reachable endpoint — a gateway
                  record without one has nothing to connect to. */}
              {isAdmin &&
                detail.descriptor_type === "MCP" &&
                (mcpEndpoint(detail) || mcpGatewayArn(detail)) && (
                  <McpToolTryout record={detail} />
                )}

              {/* Record ids and ARNs identify the record to an operator, not the
                  agent to a user — so they live behind an admin-only fold, in the
                  same idiom as the raw descriptor. */}
              {isAdmin && (
                <details className="group rounded-md border border-border">
                  <summary className="cursor-pointer select-none px-3 py-2 caps-label-xs text-muted-foreground transition-colors hover:text-foreground">
                    System info
                  </summary>
                  <div className="space-y-3 border-t border-border px-3 py-3">
                    <Field label="Record id" value={detail.record_id} mono />
                    {detail.record_arn && (
                      <Field label="Record ARN" value={detail.record_arn} mono />
                    )}
                    {detail.harness_arn && (
                      <Field label="Harness ARN" value={detail.harness_arn} mono />
                    )}
                    {detail.agent_runtime_arn && (
                      <Field
                        label="Agent runtime ARN"
                        value={detail.agent_runtime_arn}
                        mono
                      />
                    )}
                    <McpSystemInfo record={detail} />
                    {detail.descriptor_content != null && (
                      <div>
                        <p className="mb-0.5 caps-label-xs text-muted-foreground">
                          Raw descriptor
                        </p>
                        <pre className="max-h-64 overflow-auto text-xs">
                          {typeof detail.descriptor_content === "string"
                            ? detail.descriptor_content
                            : JSON.stringify(detail.descriptor_content, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                </details>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-1.5 border-t border-border bg-muted/30 px-5 py-3">
              {isChattable(detail) && (
                <Button size="sm" onClick={() => onChat(detail)}>
                  <MessagesSquare className="size-3.5" />
                  Chat with this agent
                </Button>
              )}
              {isAdmin && detail.status !== "DEPRECATED" && onEdit && (
                <Button size="sm" variant="outline" onClick={() => onEdit(detail)}>
                  <Pencil className="size-3.5" />
                  Edit
                </Button>
              )}
              {isAdmin && detail.status === "DEPRECATED" && onDelete && (
                <Button
                  size="sm"
                  variant="outline"
                  className="text-destructive hover:bg-destructive/10"
                  onClick={() => onDelete(detail)}
                >
                  <Trash2 className="size-3.5" />
                  Delete
                </Button>
              )}
              {/* Lifecycle verbs are pushed right and rendered quietly: they
                  change a record's state, so they shouldn't sit at the same
                  weight as Chat. */}
              {isAdmin && (
                <div className="ml-auto flex flex-wrap items-center gap-0.5">
                  {STATUS_ACTIONS.map((action) => (
                    <Button
                      key={action}
                      size="sm"
                      variant="ghost"
                      className="capitalize"
                      onClick={() => onStatus(detail.record_id, action)}
                    >
                      {action}
                    </Button>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
