"use client";

import React from "react";
import { FileText, FileDown } from "lucide-react";
import type { ArtifactEvent } from "@/lib/artifacts";

export const ArtifactCard = React.memo<{
  artifact: ArtifactEvent;
  isActive: boolean;
  onOpen: (artifactId: string, version: number) => void;
}>(({ artifact, isActive, onOpen }) => {
  // Versions loaded from storage have no inline body, so fall back to its size.
  const size = artifact.content
    ? `${artifact.content.split("\n").length} lines`
    : artifact.sizeBytes
      ? `${artifact.sizeBytes.toLocaleString()} B`
      : null;

  const isFile = artifact.kind === "file";
  const Icon = isFile ? FileDown : FileText;
  // A file's own extension says more than the kind, which is always "file".
  const label = isFile
    ? (artifact.filename || artifact.title).split(".").pop()?.toUpperCase()
    : artifact.language || artifact.kind;

  return (
    <button
      type="button"
      onClick={() => onOpen(artifact.artifactId, artifact.version)}
      className={`flex w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors ${
        isActive
          ? "border-primary/50 bg-primary-tint"
          : "border-border bg-card hover:bg-accent"
      }`}
    >
      <Icon className="h-5 w-5 flex-shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{artifact.title}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {[label, `v${artifact.version}`, size]
            .filter(Boolean)
            .join(" · ")}
        </span>
      </span>
    </button>
  );
});

ArtifactCard.displayName = "ArtifactCard";
