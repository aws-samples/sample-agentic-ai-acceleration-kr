"use client";

import { X, FileText, Loader2, AlertCircle } from "lucide-react";
import type { PendingAttachment } from "@/lib/attachments";
import { formatSize, isImage } from "@/lib/attachments";
import { cn } from "@/lib/utils";

interface AttachmentBarProps {
  items: PendingAttachment[];
  onRemove: (localId: string) => void;
}

/**
 * Chips for the files staged on the current message.
 *
 * Each chip owns its own state, so one failed upload is visible and removable
 * without blocking the rest of the message.
 */
export function AttachmentBar({ items, onRemove }: AttachmentBarProps) {
  if (items.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 border-b border-border px-[18px] py-2">
      {items.map((item) => (
        <div
          key={item.localId}
          className={cn(
            "group flex items-center gap-2 rounded-md border border-border bg-card px-2 py-1.5",
            item.state === "error" && "border-destructive/50"
          )}
          title={item.error || item.file.name}
        >
          {item.previewUrl && isImage(item.file.name) ? (
            <img
              src={item.previewUrl}
              alt=""
              className="h-8 w-8 flex-shrink-0 rounded object-cover"
            />
          ) : (
            <FileText className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
          )}

          <span className="max-w-[160px] truncate text-xs">{item.file.name}</span>

          {item.state === "uploading" && (
            <Loader2 className="h-3 w-3 flex-shrink-0 animate-spin text-muted-foreground" />
          )}
          {item.state === "error" && (
            <AlertCircle className="h-3 w-3 flex-shrink-0 text-destructive" />
          )}
          {item.state === "ready" && (
            <span className="flex-shrink-0 text-[10px] text-muted-foreground">
              {formatSize(item.file.size)}
            </span>
          )}

          <button
            type="button"
            onClick={() => onRemove(item.localId)}
            aria-label={`Remove ${item.file.name}`}
            className="flex-shrink-0 rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}
    </div>
  );
}
