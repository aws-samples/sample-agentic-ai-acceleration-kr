"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  FolderOpen,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
  Users,
} from "lucide-react";
import { Badge, StatusDot, statusVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  EmptyState,
  LoadingState,
  Notice,
  PageBody,
  PageHeader,
} from "@/app/components/PageHeader";
import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/AuthProvider";
import {
  KnowledgeApiError,
  deleteKnowledgeBase,
  isProvisioning,
  isUploadSource,
  listKnowledgeBases,
  sourceLabel,
  sourceTypeLabel,
  statusLabel,
  type KnowledgeBaseRecord,
} from "@/lib/knowledge";
import { CreateKnowledgeDialog } from "./components/CreateKnowledgeDialog";
import { KnowledgeDetailSheet } from "./components/KnowledgeDetailSheet";

export default function KnowledgePage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [records, setRecords] = useState<KnowledgeBaseRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<KnowledgeBaseRecord | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRecords(await listKnowledgeBases());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Provisioning runs server-side in a background task; polling both shows
  // progress and is what restarts a driver that died mid-chain.
  const provisioning = records.some(isProvisioning);
  useEffect(() => {
    if (!provisioning) return;
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [provisioning, load]);

  /**
   * Whether the caller may upload to and delete this knowledge base.
   *
   * A private knowledge base in this list is necessarily the caller's own — the
   * server answers 404 for anyone else's — so it is writable. A shared one is
   * writable by admins, and since only an admin can create a shared knowledge
   * base in the first place, that also covers its owner. The server enforces all
   * of this; this only decides which buttons to render.
   */
  const writable = (kb: KnowledgeBaseRecord) => !kb.shared || isAdmin;

  const remove = async (kb: KnowledgeBaseRecord) => {
    setDeleting(kb.kb_key);
    setError(null);
    try {
      await deleteKnowledgeBase(kb.kb_key);
      await load();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      // 409: a harness still uses it. Deleting anyway breaks that agent's tool,
      // so the choice is the user's rather than automatic.
      if (e instanceof KnowledgeApiError && e.status === 409) {
        if (window.confirm(`${message}\n\n그래도 삭제하시겠습니까?`)) {
          try {
            await deleteKnowledgeBase(kb.kb_key, true);
            await load();
          } catch (forced) {
            setError(forced instanceof Error ? forced.message : String(forced));
          }
        }
      } else {
        setError(message);
      }
    } finally {
      setDeleting(null);
    }
  };

  return (
    <>
      <PageHeader
        icon={BookOpen}
        title="Knowledge"
        hint="파일을 올리면 에이전트가 검색할 수 있는 Knowledge Base가 만들어집니다"
        actions={
          <>
            <Button variant="outline" size="sm" onClick={() => void load()}>
              <RefreshCw className="size-3.5" />
              새로고침
            </Button>
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus className="size-3.5" />
              새 Knowledge Base
            </Button>
          </>
        }
      />

      <PageBody>
        {error && (
          <Notice tone="error" icon={AlertTriangle}>
            {error}
          </Notice>
        )}

        {loading ? (
          <LoadingState label="Knowledge Base를 불러오는 중…" />
        ) : records.length === 0 ? (
          <EmptyState
            icon={BookOpen}
            title="아직 Knowledge Base가 없습니다"
            description="만들고 파일을 올리면 Agent Harness에서 검색 도구로 붙일 수 있습니다."
            action={
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus className="size-3.5" />
                새 Knowledge Base
              </Button>
            }
          />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
            {records.map((kb, i) => {
              const variant = statusVariant(kb.status);
              return (
                <Card
                  key={kb.kb_key}
                  style={{ "--i": i } as React.CSSProperties}
                  onClick={() => setSelected(kb)}
                  className={cn(
                    "group animate-rise cursor-pointer p-4",
                    "transition-[border-color,background-color] duration-150 ease-snap",
                    "hover:border-border-strong hover:bg-muted/40"
                  )}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-semibold tracking-tight">
                        {kb.name}
                      </p>
                      <p className="mt-0.5 truncate text-xs text-muted-foreground">
                        {kb.description || kb.kb_key}
                      </p>
                    </div>
                    {writable(kb) && (
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`${kb.name} 삭제`}
                        disabled={deleting === kb.kb_key}
                        // Only revealed on hover/focus: a delete button on every
                        // card is the loudest thing in the grid otherwise.
                        className="-mr-1 -mt-1 shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
                        onClick={(e) => {
                          e.stopPropagation();
                          void remove(kb);
                        }}
                      >
                        {deleting === kb.kb_key ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <Trash2 className="size-3.5" />
                        )}
                      </Button>
                    )}
                  </div>

                  <div className="mt-3 flex flex-wrap items-center gap-1">
                    <Badge shape="chip" variant={variant} title={kb.status}>
                      {isProvisioning(kb) ? (
                        <Loader2 className="size-3 animate-spin" />
                      ) : (
                        <StatusDot variant={variant} />
                      )}
                      {statusLabel(kb)}
                    </Badge>
                    {/* The s3:// path in the tooltip tells managed apart from
                        external at a glance without widening the badge row. */}
                    <Badge shape="tag" variant="secondary" title={sourceLabel(kb)}>
                      {isUploadSource(kb) ? (
                        <Upload className="size-3" />
                      ) : (
                        <FolderOpen className="size-3" />
                      )}
                      {sourceTypeLabel(kb)}
                    </Badge>
                    {kb.shared && (
                      <Badge shape="tag" variant="info">
                        <Users className="size-3" />
                        공용
                      </Badge>
                    )}
                  </div>

                  {kb.failure_reason && (
                    <p className="mt-2 text-xs leading-normal text-destructive">
                      {kb.failure_reason}
                    </p>
                  )}
                  {kb.shared && (
                    <p className="mt-2 truncate text-xs text-muted-foreground">
                      소유자: {kb.owner_name || kb.owner_id}
                    </p>
                  )}
                </Card>
              );
            })}
          </div>
        )}
      </PageBody>

      <CreateKnowledgeDialog
        open={creating}
        onOpenChange={setCreating}
        isAdmin={isAdmin}
        onCreated={() => void load()}
      />
      <KnowledgeDetailSheet
        knowledgeBase={selected}
        writable={selected ? writable(selected) : false}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      />
    </>
  );
}
