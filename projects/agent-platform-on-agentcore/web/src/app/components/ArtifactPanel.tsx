"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Check, Copy, Download, Loader2, Share2, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ArtifactPreview } from "@/app/components/ArtifactPreview";
import {
  artifactFileName,
  getArtifactContent,
  shareArtifact,
  type ArtifactEvent,
} from "@/lib/artifacts";
import { cn, copyText } from "@/lib/utils";
import { fileFillsPanel } from "@/app/components/filePreviewRenderer.mjs";

interface ArtifactPanelProps {
  /** Every artifact version streamed in this thread. */
  artifacts: ArtifactEvent[];
  artifactId: string;
  version: number;
  onSelectVersion: (artifactId: string, version: number) => void;
  onClose: () => void;
}

export const ArtifactPanel = React.memo<ArtifactPanelProps>(
  ({ artifacts, artifactId, version, onSelectVersion, onClose }) => {
    const versions = useMemo(
      () =>
        artifacts
          .filter((a) => a.artifactId === artifactId)
          .sort((a, b) => a.version - b.version),
      [artifacts, artifactId]
    );

    // Never fall back to another version here: showing v3's body under a v1
    // request is worse than fetching, and the fallback also suppressed the fetch.
    const streamed = useMemo(
      () => versions.find((a) => a.version === version),
      [versions, version]
    );

    // A version selected from a reloaded thread has no inline content, so fetch it.
    const [fetched, setFetched] = useState<ArtifactEvent | null>(null);
    const [isFetching, setIsFetching] = useState(false);
    const [fetchError, setFetchError] = useState<string | null>(null);

    useEffect(() => {
      // Drop any body kept from the previously selected artifact, otherwise the
      // panel keeps rendering it while the new one loads.
      setFetched(null);
      setFetchError(null);
      if (streamed?.content !== undefined) {
        setIsFetching(false);
        return;
      }
      // Skip fetch for files: they have no inline content and would get 415 from
      // the content route.
      if (streamed?.kind === "file") {
        setIsFetching(false);
        return;
      }
      let cancelled = false;
      setIsFetching(true);
      getArtifactContent(artifactId, version)
        .then((data) => {
          if (cancelled) return;
          setFetched({
            artifactId: data.artifact_id,
            version: data.version,
            title: data.title,
            kind: data.kind,
            language: data.language ?? undefined,
            content: data.content,
          });
        })
        .catch((error: Error) => {
          if (!cancelled) setFetchError(error.message);
        })
        .finally(() => {
          if (!cancelled) setIsFetching(false);
        });
      return () => {
        cancelled = true;
      };
    }, [artifactId, version, streamed?.content, streamed?.kind]);

    const artifact =
      streamed?.content !== undefined
        ? streamed
        : fetched?.artifactId === artifactId && fetched.version === version
          ? fetched
          : null;

    // For files, use the metadata object directly since artifact will be null
    const metadata = streamed?.kind === "file" ? streamed : artifact;

    const [copied, setCopied] = useState(false);
    const handleCopy = useCallback(async () => {
      if (!artifact?.content) return;
      await copyText(artifact.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }, [artifact]);

    const handleDownload = useCallback(() => {
      if (!artifact?.content) return;
      const blob = new Blob([artifact.content], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = artifactFileName(artifact);
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, [artifact]);

    const [isSharing, setIsSharing] = useState(false);
    const handleShare = useCallback(async () => {
      if (!metadata) return;
      setIsSharing(true);
      try {
        const share = await shareArtifact(metadata.artifactId, metadata.version);
        await copyText(share.url);
        const days = Math.round(share.expires_in / 86400);
        toast.success(
          `공유 링크를 복사했습니다 (${days}일간 유효)`
        );
      } catch (error) {
        toast.error(`공유 링크 생성 실패: ${(error as Error).message}`);
      } finally {
        setIsSharing(false);
      }
    }, [metadata]);

    if (!artifact && !metadata) {
      return (
        <div className="flex h-full flex-col border-l border-border">
          <PanelHeader
            title={streamed?.title ?? "Artifact"}
            subtitle={
              streamed
                ? [streamed.kind, streamed.language].filter(Boolean).join(" · ")
                : undefined
            }
            onClose={onClose}
          />
          <div className="flex flex-1 items-center justify-center p-8 text-sm text-muted-foreground">
            {isFetching ? (
              <span className="inline-flex items-center gap-2">
                <Loader2 className="h-4 w-4 animate-spin" />
                불러오는 중...
              </span>
            ) : (
              fetchError ?? "Artifact를 찾을 수 없습니다."
            )}
          </div>
        </div>
      );
    }

    const unstored = streamed?.stored === false;
    const isFile = metadata?.kind === "file";
    const displayArtifact = artifact || metadata;
    // Kinds drawn in a sandboxed iframe: they fill the tab rather than flow.
    const fillsPanel =
      displayArtifact?.kind === "html" ||
      displayArtifact?.kind === "svg" ||
      (displayArtifact?.kind === "file" &&
        fileFillsPanel(displayArtifact.filename ?? undefined));

    if (!displayArtifact) {
      return (
        <div className="flex h-full flex-col border-l border-border">
          <PanelHeader
            title="Artifact"
            onClose={onClose}
          />
          <div className="flex flex-1 items-center justify-center p-8 text-sm text-muted-foreground">
            Artifact를 찾을 수 없습니다.
          </div>
        </div>
      );
    }

    return (
      <div className="flex h-full flex-col border-l border-border">
        <PanelHeader
          title={displayArtifact.title}
          subtitle={[displayArtifact.kind, displayArtifact.language].filter(Boolean).join(" · ")}
          onClose={onClose}
        />

        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2">
          {versions.length > 1 ? (
            <div className="flex items-center gap-1">
              {versions.map((v) => (
                <button
                  key={v.version}
                  type="button"
                  onClick={() => onSelectVersion(artifactId, v.version)}
                  className={cn(
                    "rounded-md border px-2 py-1 text-xs transition-colors",
                    v.version === version
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border bg-card text-muted-foreground hover:bg-accent"
                  )}
                >
                  v{v.version}
                </button>
              ))}
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">v{displayArtifact?.version}</span>
          )}

          <div className="flex items-center gap-1">
            {!isFile && (
              <Button variant="ghost" size="sm" onClick={handleCopy}>
                {copied ? (
                  <Check className="h-4 w-4" />
                ) : (
                  <Copy className="h-4 w-4" />
                )}
              </Button>
            )}
            {!isFile && (
              <Button variant="ghost" size="sm" onClick={handleDownload}>
                <Download className="h-4 w-4" />
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={handleShare}
              disabled={isSharing || unstored}
              title={
                unstored
                  ? "저장되지 않은 artifact는 공유할 수 없습니다"
                  : "공유 링크 복사"
              }
            >
              {isSharing ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Share2 className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>

        {unstored && (
          <p className="border-b border-border bg-muted px-4 py-2 text-xs text-muted-foreground">
            S3에 저장되지 않았습니다{streamed?.storeError ? `: ${streamed.storeError}` : " (ARTIFACTS_BUCKET 미설정)"}.
            지금 화면에서만 볼 수 있고 공유는 불가합니다.
          </p>
        )}

        {isFile ? (
          <div className="min-h-0 flex-1 overflow-hidden">
            {fillsPanel ? (
              // A swept .html is an iframe like the html kind below: outside the
              // ScrollArea so its percentage height has something to resolve
              // against (measured collapsed to 152px inside it).
              <div className="h-full p-4">
                <ArtifactPreview
                  key={`${displayArtifact.artifactId}-${displayArtifact.version}`}
                  kind={displayArtifact.kind}
                  language={displayArtifact.language}
                  title={displayArtifact.title}
                  content=""
                  artifactId={displayArtifact.artifactId}
                  version={displayArtifact.version}
                  filename={displayArtifact.filename}
                  sizeBytes={displayArtifact.sizeBytes}
                />
              </div>
            ) : (
              <ScrollArea className="h-full [&_[data-radix-scroll-area-viewport]>div]:!block">
                <div className="p-4">
                  <ArtifactPreview
                    key={`${displayArtifact.artifactId}-${displayArtifact.version}`}
                    kind={displayArtifact.kind}
                    language={displayArtifact.language}
                    title={displayArtifact.title}
                    content=""
                    artifactId={displayArtifact.artifactId}
                    version={displayArtifact.version}
                    filename={displayArtifact.filename}
                    sizeBytes={displayArtifact.sizeBytes}
                  />
                </div>
              </ScrollArea>
            )}
          </div>
        ) : (
          <Tabs defaultValue="preview" className="flex min-h-0 flex-1 flex-col gap-0">
            <TabsList className="mx-4 mt-3 w-fit">
              <TabsTrigger value="preview">Preview</TabsTrigger>
              <TabsTrigger value="source">Source</TabsTrigger>
            </TabsList>

            <TabsContent value="preview" className="min-h-0 flex-1">
              {fillsPanel ? (
                // The iframe scrolls its own document, so it takes the whole tab
                // instead of sitting inside ScrollArea. Radix wraps the viewport's
                // children in a `display: table` div whose height is its content,
                // so a percentage-height frame in there collapsed to its min-height
                // and left the rest of the panel blank.
                <div className="h-full p-4">
                  <ArtifactPreview
                    key={`${displayArtifact.artifactId}-${displayArtifact.version}`}
                    kind={displayArtifact.kind}
                    language={displayArtifact.language}
                    title={displayArtifact.title}
                    content={displayArtifact.content ?? ""}
                  />
                </div>
              ) : (
                // Radix wraps the viewport's children in a display:table div that
                // grows to its widest child, so a wide preview (a Word page with a
                // 16 cm figure) pushed past the panel edge and stopped wrapping.
                // Laid out as a block it takes the panel width and wraps instead.
                <ScrollArea className="h-full [&_[data-radix-scroll-area-viewport]>div]:!block">
                  <div className="p-4">
                    <ArtifactPreview
                      // Remount per version: the preview keeps rendered state
                      // (mermaid SVG, iframe document) that must not outlive a switch.
                      key={`${displayArtifact.artifactId}-${displayArtifact.version}`}
                      kind={displayArtifact.kind}
                      language={displayArtifact.language}
                      title={displayArtifact.title}
                      content={displayArtifact.content ?? ""}
                    />
                  </div>
                </ScrollArea>
              )}
            </TabsContent>

            <TabsContent value="source" className="min-h-0 flex-1">
              <ScrollArea className="h-full">
                <pre className="whitespace-pre-wrap break-words p-4 text-xs leading-relaxed">
                  {displayArtifact.content}
                </pre>
              </ScrollArea>
            </TabsContent>
          </Tabs>
        )}
      </div>
    );
  }
);

ArtifactPanel.displayName = "ArtifactPanel";

function PanelHeader({
  title,
  subtitle,
  onClose,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
}) {
  return (
    <div className="flex h-16 flex-shrink-0 items-center justify-between gap-2 border-b border-border px-4">
      <div className="min-w-0">
        <h2 className="truncate text-sm font-semibold">{title}</h2>
        {subtitle && (
          <p className="truncate text-xs text-muted-foreground">{subtitle}</p>
        )}
      </div>
      <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close artifact">
        <X className="h-4 w-4" />
      </Button>
    </div>
  );
}
