"use client";

/**
 * One attachment in a message's strip: an image thumbnail or a file chip.
 *
 * Neither puts the server route in `src`/`href`. That route is gated on the
 * session and an `<img>` or a plain link carries no Authorization header, so
 * with AUTH_ENFORCED on every thumbnail broke and every link opened a 401
 * (found in review). The bytes come through fetchAttachment — the session's
 * token, then an object URL — the way browser screenshots already do.
 *
 * Images are fetched when the strip renders, since the thumbnail is the point.
 * Files are fetched on click and saved with the original name: a new tab opened
 * after an awaited fetch is what popup blockers exist to stop, while a download
 * started from the click is reliable in every current browser.
 */

import { useCallback, useEffect, useState } from "react";
import { FileText, Loader2 } from "lucide-react";
import { fetchAttachment, isImage, type AttachmentRef } from "@/lib/attachments";
import { cn } from "@/lib/utils";

/*
 * Firefox needs the object URL to outlive the synthetic click until the
 * download has actually begun; revoking in the same tick cancels it there.
 */
const REVOKE_AFTER_DOWNLOAD_MS = 60_000;

const chipClass =
  "flex items-center gap-2 rounded-md border border-border bg-card px-2 py-1.5 text-xs";

interface Props {
  threadId: string;
  attachment: AttachmentRef;
}

export function MessageAttachment({ threadId, attachment }: Props) {
  return isImage(attachment.filename) ? (
    <ImageThumbnail threadId={threadId} attachment={attachment} />
  ) : (
    <FileChip threadId={threadId} attachment={attachment} />
  );
}

function ImageThumbnail({ threadId, attachment }: Props) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  /* Revoked on the way out: a long thread would otherwise keep one blob per
     thumbnail per render. `cancelled` covers a fetch that lands after unmount. */
  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    setFailed(false);
    fetchAttachment(threadId, attachment.attachment_id).then((fetched) => {
      if (cancelled) {
        if (fetched) URL.revokeObjectURL(fetched);
        return;
      }
      if (!fetched) {
        setFailed(true);
        return;
      }
      url = fetched;
      setObjectUrl(fetched);
    });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
      setObjectUrl(null);
    };
  }, [threadId, attachment.attachment_id]);

  if (failed) {
    return (
      <span className={cn(chipClass, "text-muted-foreground")} title="이미지를 불러올 수 없습니다">
        <FileText className="h-4 w-4 flex-shrink-0" />
        <span className="max-w-[200px] truncate">{attachment.filename}</span>
      </span>
    );
  }

  if (!objectUrl) {
    return (
      <span
        className="flex h-40 w-40 items-center justify-center rounded-lg border border-border bg-muted/50"
        aria-label={`${attachment.filename} 불러오는 중`}
      >
        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
      </span>
    );
  }

  return (
    <a href={objectUrl} target="_blank" rel="noreferrer" className="block">
      <img
        src={objectUrl}
        alt={attachment.filename}
        className="max-h-40 rounded-lg border border-border object-cover"
      />
    </a>
  );
}

function FileChip({ threadId, attachment }: Props) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  const download = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setFailed(false);
    const url = await fetchAttachment(threadId, attachment.attachment_id);
    setBusy(false);
    if (!url) {
      setFailed(true);
      return;
    }
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = attachment.filename;
    anchor.rel = "noreferrer";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_DOWNLOAD_MS);
  }, [busy, threadId, attachment.attachment_id, attachment.filename]);

  return (
    <button
      type="button"
      onClick={download}
      disabled={busy}
      className={cn(chipClass, "hover:bg-accent disabled:opacity-60")}
      title={failed ? "파일을 불러올 수 없습니다" : `${attachment.filename} 다운로드`}
    >
      {busy ? (
        <Loader2 className="h-4 w-4 flex-shrink-0 animate-spin" />
      ) : (
        <FileText className="h-4 w-4 flex-shrink-0" />
      )}
      <span className="max-w-[200px] truncate">{attachment.filename}</span>
      {failed && <span className="text-destructive">불러올 수 없음</span>}
    </button>
  );
}
