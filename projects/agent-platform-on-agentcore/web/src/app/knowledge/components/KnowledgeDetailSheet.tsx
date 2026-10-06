"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check,
  Copy,
  FileText,
  FolderOpen,
  Loader2,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import {
  Badge,
  StatusDot,
  humanizeStatus,
  statusVariant,
  type BadgeVariant,
} from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { cn, copyText } from "@/lib/utils";
import {
  ACCEPT_ATTRIBUTE,
  MAX_UPLOAD_BYTES,
  canManageFiles,
  deleteKnowledgeDocument,
  getKnowledgeBase,
  isManagedSource,
  isSyncing,
  isUploadSource,
  sourceLabel,
  sourceTypeLabel,
  syncKnowledgeBase,
  uploadKnowledgeDocument,
  type KnowledgeBaseDetail,
  type KnowledgeBaseRecord,
  type SyncJob,
} from "@/lib/knowledge";

/**
 * Statuses AWS will not move a document off of. Everything else — including
 * transient values this build has never seen — counts as still working, so the
 * poll keeps running rather than freezing on an intermediate state. Bedrock adds
 * DocumentStatus values ahead of the SDK enum (TEXT_INDEXED is one), which is why
 * this is a list of endings and not a list of middles. The observed sequence for
 * a fresh upload is STARTING → TEXT_INDEXED → INDEXED.
 */
const TERMINAL = new Set([
  "INDEXED",
  "FAILED",
  "IGNORED",
  "NOT_FOUND",
  "METADATA_UPDATE_FAILED",
]);

const POLL_INTERVAL_MS = 5000;

/**
 * Treating unknown statuses as "still working" is the safe default for a stuck
 * badge, but it would poll forever if AWS ends a document on a status not listed
 * above. Ingestion of a 50 MB document takes well under a minute, so give up
 * after five, by which point the status on screen is the real outcome anyway.
 */
const MAX_POLLS = 60;

/**
 * Per-document status tone.
 *
 * `statusVariant` covers the shared lifecycle vocabulary; the two outcomes below
 * are specific to document ingestion and would otherwise fall through to "info".
 */
function documentStatusVariant(status?: string | null): BadgeVariant {
  switch ((status || "").toUpperCase()) {
    case "IGNORED":
    case "METADATA_UPDATE_FAILED":
      return "destructive";
    default:
      return statusVariant(status);
  }
}

export function KnowledgeDetailSheet({
  knowledgeBase,
  writable,
  onOpenChange,
}: {
  /** The knowledge base to show, or null when the sheet is closed. */
  knowledgeBase: KnowledgeBaseRecord | null;
  /** Owner or admin; read-only viewers of a shared KB cannot upload or delete. */
  writable: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [detail, setDetail] = useState<KnowledgeBaseDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const polls = useRef(0);
  const kbKey = knowledgeBase?.kb_key ?? null;

  const load = useCallback(async () => {
    if (!kbKey) return;
    setLoading(true);
    try {
      setDetail(await getKnowledgeBase(kbKey));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [kbKey]);

  useEffect(() => {
    setDetail(null);
    polls.current = 0;
    if (kbKey) void load();
  }, [kbKey, load]);

  useEffect(() => {
    return () => {
      if (copyTimer.current) clearTimeout(copyTimer.current);
    };
  }, []);

  // Ingestion is asynchronous, so poll while anything is still indexing. A
  // document with no status yet has not reached a terminal state either. A running
  // sync counts too: it is what makes those documents appear in the first place.
  const pending =
    isSyncing(detail?.sync) ||
    (detail?.documents ?? []).some(
      (d) => !TERMINAL.has((d.status || "").toUpperCase())
    );
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => {
      if (polls.current >= MAX_POLLS) {
        clearInterval(timer);
        return;
      }
      polls.current += 1;
      void load();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [pending, load]);

  const upload = async (file: File) => {
    if (!kbKey) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(
        `파일이 너무 큽니다 (${Math.round(file.size / 1024 / 1024)}MB). 최대 50MB까지 올릴 수 있습니다.`
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await uploadKnowledgeDocument(kbKey, file);
      // A new document deserves a full poll budget even in a long-open sheet.
      polls.current = 0;
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const removeDocument = async (docId: string) => {
    if (!kbKey) return;
    setBusy(true);
    setError(null);
    try {
      await deleteKnowledgeDocument(kbKey, docId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const sync = async () => {
    if (!kbKey) return;
    setBusy(true);
    setError(null);
    try {
      await syncKnowledgeBase(kbKey);
      // A sync scans the whole source, so it earns a fresh poll budget.
      polls.current = 0;
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const documents = detail?.documents ?? [];
  // Falls back to upload for a knowledge base created before source types.
  const uploadSource = knowledgeBase == null || isUploadSource(knowledgeBase);
  const managed = knowledgeBase != null && isManagedSource(knowledgeBase);
  // Upload sources and managed S3 sources both take files from the site;
  // external S3 sources are read-only.
  const filesManageable = knowledgeBase == null || canManageFiles(knowledgeBase);

  return (
    <Sheet open={knowledgeBase != null} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-4 sm:max-w-lg">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <span className="min-w-0 truncate">{knowledgeBase?.name}</span>
            {knowledgeBase && (
              <Badge shape="tag" variant="secondary">
                {uploadSource ? (
                  <Upload className="size-3" />
                ) : (
                  <FolderOpen className="size-3" />
                )}
                {sourceTypeLabel(knowledgeBase)}
              </Badge>
            )}
          </SheetTitle>
          <SheetDescription>
            {knowledgeBase?.description ||
              (uploadSource
                ? "업로드한 파일이 자동으로 인덱싱됩니다."
                : knowledgeBase
                  ? sourceLabel(knowledgeBase)
                  : "")}
          </SheetDescription>
        </SheetHeader>

        {error && (
          <p className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </p>
        )}

        {writable && filesManageable && (
          <div>
            <input
              ref={fileInput}
              type="file"
              className="hidden"
              accept={ACCEPT_ATTRIBUTE}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void upload(file);
              }}
            />
            <Button
              variant="outline"
              className="w-full"
              disabled={busy}
              onClick={() => fileInput.current?.click()}
            >
              {busy ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Upload className="size-3.5" />
              )}
              파일 업로드
            </Button>
            <p className="mt-2 text-xs text-muted-foreground">
              txt, md, html, csv, pdf, doc, docx · 최대 50MB
            </p>
          </div>
        )}

        {managed && knowledgeBase && (
          <button
            type="button"
            className="flex w-full items-center gap-2 rounded-md border border-border p-2 text-left text-xs text-muted-foreground transition-colors hover:bg-muted/50"
            title="경로 복사"
            onClick={() => {
              if (copyTimer.current) clearTimeout(copyTimer.current);
              void copyText(sourceLabel(knowledgeBase)).then((ok) => {
                setCopied(ok);
                if (ok) {
                  copyTimer.current = setTimeout(() => setCopied(false), 1500);
                }
              });
            }}
          >
            {copied ? <Check className="size-3.5 shrink-0" /> : <Copy className="size-3.5 shrink-0" />}
            <span className="min-w-0 flex-1 truncate font-mono">{sourceLabel(knowledgeBase)}</span>
          </button>
        )}

        {!uploadSource && (
          <SyncPanel
            job={detail?.sync ?? null}
            writable={writable}
            busy={busy}
            onSync={() => void sync()}
            managed={managed}
          />
        )}

        <div className="min-h-0 flex-1 overflow-auto">
          {loading && documents.length === 0 ? (
            <div className="py-8 text-center">
              <Loader2 className="mx-auto size-5 animate-spin text-muted-foreground" />
            </div>
          ) : documents.length === 0 ? (
            <p className="py-8 text-center text-xs text-muted-foreground">
              {uploadSource
                ? "아직 업로드한 파일이 없습니다."
                : managed
                  ? "아직 파일이 없습니다. 올리면 동기화 후 표시됩니다."
                  : "아직 동기화하지 않았습니다. 동기화하면 소스의 파일이 여기 표시됩니다."}
            </p>
          ) : (
            <div className="divide-y divide-border overflow-hidden rounded-md border border-border">
              {documents.map((doc) => {
                const variant = documentStatusVariant(doc.status);
                return (
                  <div
                    key={doc.doc_id}
                    className="group flex items-start gap-2 p-2.5 transition-colors hover:bg-muted/50"
                  >
                    <FileText className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-xs font-medium">
                        {doc.filename}
                      </p>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <Badge
                          shape="chip"
                          variant={variant}
                          title={doc.status || undefined}
                        >
                          <StatusDot variant={variant} />
                          {humanizeStatus(doc.status || "UNKNOWN")}
                        </Badge>
                        {doc.status_reason && (
                          <span className="text-xs text-destructive">
                            {doc.status_reason}
                          </span>
                        )}
                      </div>
                    </div>
                    {/* No delete for external pull source: it would only unindex, and the
                        next sync would bring the document straight back. */}
                    {writable && filesManageable && (
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`${doc.filename} 삭제`}
                        disabled={busy}
                        className="shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
                        onClick={() => void removeDocument(doc.doc_id)}
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

/**
 * Sync control and the last job's outcome, for a knowledge base that pulls from a
 * source. Read-only viewers see the state without the button: a sync changes what
 * everyone retrieves, so the server requires write access.
 */
function SyncPanel({
  job,
  writable,
  busy,
  onSync,
  managed,
}: {
  job: SyncJob | null;
  writable: boolean;
  busy: boolean;
  onSync: () => void;
  managed: boolean;
}) {
  const running = isSyncing(job);
  const variant = running ? "info" : statusVariant(job?.status);

  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <div className="flex items-center justify-between gap-2">
        {writable ? (
          <Button
            variant="outline"
            size="sm"
            disabled={busy || running}
            onClick={onSync}
          >
            <RefreshCw
              className={cn("size-3.5", running && "animate-spin")}
            />
            {running ? "동기화 중" : "동기화"}
          </Button>
        ) : (
          <span className="text-xs text-muted-foreground">
            동기화는 소유자만 실행할 수 있습니다.
          </span>
        )}
        {job && (
          <Badge shape="chip" variant={variant} title={job.status}>
            <StatusDot variant={variant} />
            {humanizeStatus(job.status)}
          </Badge>
        )}
      </div>

      {job && (
        <p className="text-xs text-muted-foreground">
          스캔 {job.documents_scanned} · 신규 {job.documents_indexed} · 수정{" "}
          {job.documents_modified} · 삭제 {job.documents_deleted}
          {job.documents_skipped > 0 && ` · 건너뜀 ${job.documents_skipped}`}
          {job.documents_failed > 0 && (
            <span className="text-destructive">
              {" "}
              · 실패 {job.documents_failed}
            </span>
          )}
        </p>
      )}

      {job?.failure_reasons?.map((reason, index) => (
        <p key={index} className="text-xs text-destructive">
          {reason}
        </p>
      ))}

      <p className="text-xs text-muted-foreground">
        {managed
          ? "S3에 직접 올린 파일은 동기화를 눌러 가져옵니다. 같은 이름으로 올리면 덮어씁니다."
          : "파일을 지우려면 소스에서 삭제한 뒤 동기화하세요."}
      </p>
    </div>
  );
}
