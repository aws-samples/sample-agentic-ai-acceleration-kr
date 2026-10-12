"use client";

import React, { useCallback, useMemo, useRef, useState, useEffect } from "react";
import { Download, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { JsonTree } from "@/app/components/JsonTree";
import { CodeBlock } from "@/app/components/CodeBlock";
import { MarkdownContent } from "@/app/components/MarkdownContent";
import { MermaidDiagram } from "@/app/components/MermaidDiagram";
import { fileFillsPanel, fileRendererFor } from "@/app/components/filePreviewRenderer.mjs";
import { renderPptxDeck } from "@/app/components/pptxDeckRender.mjs";
import {
  fetchArtifactFile,
  fetchArtifactPreview,
  type ArtifactKind,
  type ArtifactPreview as ArtifactPreviewType,
} from "@/lib/artifacts";

/** Minimal RFC 4180 parse: enough for agent-generated CSV with quoted fields. */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (quoted) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          quoted = false;
        }
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else if (char !== "\r") {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => r.some((c) => c.trim() !== ""));
}

function CsvTable({ content }: { content: string }) {
  const rows = useMemo(() => parseCsv(content), [content]);
  if (!rows.length) return null;
  const [header, ...body] = rows;

  return (
    <div className="overflow-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr>
            {header.map((cell, i) => (
              <th
                key={i}
                className="border border-border bg-muted px-3 py-2 text-left font-medium"
              >
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i}>
              {r.map((cell, j) => (
                <td key={j} className="border border-border px-3 py-2">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JsonPreview({ content }: { content: string }) {
  const parsed = useMemo(() => {
    try {
      return { value: JSON.parse(content) as object, error: null };
    } catch (e) {
      return { value: null, error: (e as Error).message };
    }
  }, [content]);

  if (parsed.error || parsed.value === null || typeof parsed.value !== "object") {
    return <CodeBlock content={content} language="json" />;
  }
  return <JsonTree value={parsed.value} collapsed={2} />;
}

interface Sheet {
  name: string;
  rows: string[][];
  /** The extractor stopped early — absent on previews written before the flag. */
  truncated?: boolean;
}

function SheetTable({ sheet }: { sheet: Sheet }) {
  const [header, ...body] = sheet.rows;
  if (!header) return null;

  return (
    <div className="mb-6">
      <div className="overflow-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              {header.map((cell, i) => (
                <th
                  key={i}
                  className="border border-border bg-muted px-3 py-2 text-left font-medium"
                >
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((r, i) => (
              <tr key={i}>
                {r.map((cell, j) => (
                  <td key={j} className="border border-border px-3 py-2">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {sheet.truncated && (
        <p className="mt-2 text-xs text-muted-foreground">
          앞부분 {Math.max(sheet.rows.length - 1, 0)}행만 표시합니다. 전체는
          다운로드해서 확인하세요.
        </p>
      )}
    </div>
  );
}

/** One workbook: a tab per sheet, because a stack of tables hides the later ones. */
function SheetsView({ sheets }: { sheets: Sheet[] }) {
  const [active, setActive] = useState(0);
  const sheet = sheets[Math.min(active, sheets.length - 1)];

  return (
    <div>
      {sheets.length > 1 && (
        <div className="mb-3 flex flex-wrap items-center gap-1">
          {sheets.map((candidate, index) => (
            <button
              key={`${candidate.name}-${index}`}
              type="button"
              onClick={() => setActive(index)}
              className={cn(
                "rounded-md border px-2 py-1 text-xs transition-colors",
                index === active
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border bg-card text-muted-foreground hover:bg-accent"
              )}
            >
              {candidate.name}
            </button>
          ))}
        </div>
      )}
      {sheets.length === 1 && (
        <h3 className="mb-3 text-sm font-semibold">{sheet.name}</h3>
      )}
      <SheetTable sheet={sheet} />
    </div>
  );
}

function PreviewContent({ preview }: { preview: ArtifactPreviewType }) {
  switch (preview.media) {
    case "markdown":
      return <MarkdownContent content={preview.text} />;
    case "csv":
      return <CsvTable content={preview.text} />;
    case "json":
      return <JsonPreview content={preview.text} />;
    case "sheets": {
      let data: { sheets?: Sheet[] } | null = null;
      try {
        data = JSON.parse(preview.text);
      } catch {
        // The body comes from S3 and a preview is best-effort; a render throw
        // would take the whole panel down instead of one card.
        data = null;
      }
      if (!data?.sheets?.length) {
        return (
          <pre className="whitespace-pre-wrap break-words text-sm leading-relaxed">
            {preview.text}
          </pre>
        );
      }
      return <SheetsView sheets={data.sheets} />;
    }
    case "html":
      return (
        <SandboxedFrame
          content={preview.text}
          title="Preview"
        />
      );
    case "text":
    default:
      return (
        <pre className="whitespace-pre-wrap break-words text-sm leading-relaxed">
          {preview.text}
        </pre>
      );
  }
}

function FileDownloadCard({
  artifactId,
  version,
  filename,
  sizeBytes,
}: {
  artifactId: string;
  version: number;
  filename?: string;
  sizeBytes?: number;
}) {
  const [isDownloading, setIsDownloading] = useState(false);

  const handleDownload = async () => {
    setIsDownloading(true);
    try {
      const { blob, filename: downloadFilename } = await fetchArtifactFile(
        artifactId,
        version
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = downloadFilename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (error) {
      toast.error(`다운로드 실패: ${(error as Error).message}`);
    } finally {
      setIsDownloading(false);
    }
  };

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{filename || "File"}</p>
          {sizeBytes && (
            <p className="text-xs text-muted-foreground">
              {(sizeBytes / 1024).toFixed(1)} KB
            </p>
          )}
        </div>
        <Button
          onClick={handleDownload}
          disabled={isDownloading}
          size="sm"
        >
          {isDownloading ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <>
              <Download className="mr-2 h-4 w-4" />
              다운로드
            </>
          )}
        </Button>
      </div>
    </div>
  );
}

function PreviewSpinner() {
  return (
    <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" />
      미리보기 불러오는 중...
    </div>
  );
}

/** The text the server extracted from the file, if it extracted any. */
function ServerPreview({
  artifactId,
  version,
  fill = false,
}: {
  artifactId: string;
  version: number;
  /** The preview is an iframe that must take the whole tab, not flow. */
  fill?: boolean;
}) {
  const [preview, setPreview] = useState<ArtifactPreviewType | null | undefined>(undefined);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    fetchArtifactPreview(artifactId, version)
      .then((data) => {
        if (!cancelled) {
          setPreview(data);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError((err as Error).message);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [artifactId, version]);

  if (isLoading) return <PreviewSpinner />;
  if (error) {
    return (
      <p className="text-sm text-muted-foreground">
        미리보기를 불러올 수 없습니다.
      </p>
    );
  }
  if (!preview) return null;
  return (
    <div className={fill ? "h-full" : "overflow-auto"}>
      <PreviewContent preview={preview} />
    </div>
  );
}

/**
 * A .docx rendered from its own bytes: headings, emphasis, lists, tables and
 * embedded images all come from the document, not from an extraction of it.
 *
 * Kept on a white surface with dark text on purpose. Word styling assumes paper,
 * so in dark mode the document's own near-black text would land on a near-black
 * background.
 */
function DocxView({ blob, onFailure }: { blob: Blob; onFailure: () => void }) {
  const container = useRef<HTMLDivElement>(null);
  const [isRendering, setIsRendering] = useState(true);

  useEffect(() => {
    const target = container.current;
    if (!target) return;
    let cancelled = false;
    // Dynamic, like MermaidDiagram: the renderer carries a zip reader and is
    // only needed once a .docx is actually opened.
    import("docx-preview")
      .then(({ renderAsync }) =>
        renderAsync(blob, target, undefined, {
          // The panel is a narrow column, so the document flows to its width
          // instead of holding a 21cm page and forcing sideways scrolling.
          inWrapper: false,
          ignoreWidth: true,
          ignoreHeight: true,
          breakPages: false,
          // Data URLs, not blob URLs: nothing to revoke when the panel closes.
          useBase64URL: true,
        })
      )
      .then(() => {
        if (!cancelled) setIsRendering(false);
      })
      .catch((error) => {
        console.warn("docx render failed, falling back to the text preview", error);
        if (!cancelled) onFailure();
      });
    return () => {
      cancelled = true;
      // The renderer appends to the container and injects its stylesheet there;
      // leaving that behind would stack documents across version switches.
      target.replaceChildren();
    };
  }, [blob, onFailure]);

  return (
    <>
      {isRendering && <PreviewSpinner />}
      <div
        ref={container}
        className={cn(
          "overflow-auto rounded-lg border border-border bg-white p-6 text-black",
          // The panel is narrower than an A4 page. Word sizes pictures and tables
          // in centimetres, so a 16 cm figure plus the page margins pushed the
          // whole document past the panel edge and the text stopped wrapping
          // (the ScrollArea wraps content in display:table, which grows to the
          // widest child). Cap those and drop the page margins the page no
          // longer has.
          "[&_img]:!h-auto [&_img]:!max-w-full [&_table]:!max-w-full",
          // docx-preview wraps each picture in an inline-block div with the
          // picture's width in pt, so the img cap alone left the wrapper wide.
          "[&_section.docx_div]:!max-w-full",
          "[&_section.docx]:!px-2 [&_section.docx]:!py-4",
          isRendering && "hidden"
        )}
      />
    </>
  );
}

/** Pages past this are download-only: rendering is per-page work on the main thread. */
const PDF_MAX_PAGES = 20;
/** Canvas width in CSS pixels. Fixed, so the render is independent of panel width. */
const PDF_PAGE_WIDTH = 900;

/**
 * A .pdf drawn page by page with pdf.js.
 *
 * Not an `<iframe src={blobUrl}>`: that hands the file to the browser's own
 * viewer, which is not everywhere. iOS Safari renders only the first page of a
 * framed PDF, and a Chromium build without the viewer extension shows a blank
 * white frame — measured on this box, headless and headful alike.
 */
function PdfView({ blob, onFailure }: { blob: Blob; onFailure: () => void }) {
  const container = useRef<HTMLDivElement>(null);
  const [pages, setPages] = useState<number | null>(null);

  useEffect(() => {
    const target = container.current;
    if (!target) return;
    let cancelled = false;

    (async () => {
      const pdfjs = await import("pdfjs-dist");
      // The worker ships beside the library; bundling it by URL keeps it out of
      // the main chunk and off any CDN.
      pdfjs.GlobalWorkerOptions.workerSrc = new URL(
        "pdfjs-dist/build/pdf.worker.min.mjs",
        import.meta.url
      ).toString();

      const data = new Uint8Array(await blob.arrayBuffer());
      const pdf = await pdfjs.getDocument({ data }).promise;
      if (cancelled) return;
      setPages(pdf.numPages);

      for (let number = 1; number <= Math.min(pdf.numPages, PDF_MAX_PAGES); number += 1) {
        const page = await pdf.getPage(number);
        if (cancelled) return;
        const unscaled = page.getViewport({ scale: 1 });
        // Device pixels for a sharp render, CSS pixels for the layout width.
        const scale = (PDF_PAGE_WIDTH / unscaled.width) * (window.devicePixelRatio || 1);
        const viewport = page.getViewport({ scale });
        const canvas = createPageCanvas(viewport.width, viewport.height);
        const context = canvas.getContext("2d");
        if (!context) throw new Error("2d canvas context unavailable");
        await page.render({ canvasContext: context, viewport }).promise;
        if (cancelled) return;
        target.appendChild(canvas);
      }
    })().catch((error) => {
      console.warn("pdf render failed, falling back to the text preview", error);
      if (!cancelled) onFailure();
    });

    return () => {
      cancelled = true;
      target.replaceChildren();
    };
  }, [blob, onFailure]);

  return (
    <div className="space-y-2">
      {pages === null && <PreviewSpinner />}
      <div ref={container} className="flex flex-col items-center gap-4" />
      {pages !== null && pages > PDF_MAX_PAGES && (
        <p className="text-xs text-muted-foreground">
          전체 {pages}페이지 중 {PDF_MAX_PAGES}페이지까지 표시합니다. 나머지는
          다운로드해서 확인하세요.
        </p>
      )}
    </div>
  );
}

/** A page-sized canvas, styled to sit on the panel like a sheet of paper. */
function createPageCanvas(width: number, height: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(width);
  canvas.height = Math.floor(height);
  canvas.style.width = "100%";
  canvas.style.height = "auto";
  canvas.className = "rounded-lg border border-border bg-white";
  return canvas;
}

/** Slides are laid out at this width in CSS pixels, then scaled to the panel. */
const PPTX_RENDER_WIDTH = 960;

/**
 * A .pptx as slides, drawn from the file by pptx-preview.
 *
 * Fidelity is not Word-level: PowerPoint's text autofit is not implemented, so a
 * title that wraps can collide with the paragraph under it, and speaker notes are
 * not rendered at all. It still beats the extracted outline by a wide margin on
 * any deck with a theme, images or coloured shapes — and the outline stays as the
 * fallback when a render fails.
 */
function PptxView({ blob, onFailure }: { blob: Blob; onFailure: () => void }) {
  const outer = useRef<HTMLDivElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const [isRendering, setIsRendering] = useState(true);

  useEffect(() => {
    const target = container.current;
    const frame = outer.current;
    if (!target || !frame) return;
    let observer: ResizeObserver | null = null;

    // Rendered at a fixed size and CSS-scaled to the panel: the library lays
    // shapes out in absolute pixels, so a small viewport clips them instead of
    // shrinking them. The deck goes in a node of its own so that cancelling this
    // render cannot clear a render that outlives it — see pptxDeckRender.mjs.
    const render = renderPptxDeck({
      container: target,
      load: () => import("pptx-preview").then((module) => module.init),
      source: blob,
      width: PPTX_RENDER_WIDTH,
      height: Math.round((PPTX_RENDER_WIDTH * 9) / 16),
    });

    render.done
      .then((host: HTMLElement | null) => {
        if (!host) return;
        // Measured continuously, not once: the frame's width changes when the
        // panel is resized, and a single measurement pins the frame to the first
        // slide's height while `overflow-hidden` swallows the other 18.
        const fit = () => {
          // The library builds a fixed-height box that scrolls its slide list
          // internally. Flattening it to the full list height moves scrolling
          // back to the panel, so the deck reads as one column of slides.
          const wrapper = host.querySelector<HTMLElement>(".pptx-preview-wrapper");
          if (wrapper) wrapper.style.height = `${wrapper.scrollHeight}px`;
          // clientWidth of the frame, not the target: the target is the 960px-wide
          // canvas being scaled down.
          const scale = frame.clientWidth / PPTX_RENDER_WIDTH;
          target.style.transformOrigin = "top left";
          target.style.transform = `scale(${scale})`;
          frame.style.height = `${target.scrollHeight * scale}px`;
        };
        fit();
        observer = new ResizeObserver(fit);
        observer.observe(target);
        observer.observe(frame);
        setIsRendering(false);
      })
      .catch((error: unknown) => {
        console.warn("pptx render failed, falling back to the text preview", error);
        onFailure();
      });

    return () => {
      observer?.disconnect();
      render.cancel();
    };
  }, [blob, onFailure]);

  return (
    <>
      {isRendering && <PreviewSpinner />}
      {/* The frame stays in layout while rendering so its width can be measured;
          the 960px canvas is taken out of flow so it cannot stretch the panel. */}
      <div ref={outer} className="relative w-full overflow-hidden">
        <div
          ref={container}
          className="absolute left-0 top-0"
          style={{ width: PPTX_RENDER_WIDTH, opacity: isRendering ? 0 : 1 }}
        />
      </div>
    </>
  );
}

/**
 * Downloads the file so the browser can render it, and hands over to the text
 * preview if either the download or the render fails.
 */
function BrowserRender({
  renderer,
  artifactId,
  version,
  onFailure,
}: {
  renderer: "docx" | "pdf" | "pptx" | "image";
  artifactId: string;
  version: number;
  onFailure: () => void;
}) {
  const [blob, setBlob] = useState<Blob | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchArtifactFile(artifactId, version)
      .then((file) => {
        if (!cancelled) setBlob(file.blob);
      })
      .catch((error) => {
        console.warn("artifact download failed, falling back to the text preview", error);
        if (!cancelled) onFailure();
      });
    return () => {
      cancelled = true;
    };
  }, [artifactId, version, onFailure]);

  if (!blob) return <PreviewSpinner />;
  if (renderer === "pdf") return <PdfView blob={blob} onFailure={onFailure} />;
  if (renderer === "pptx") return <PptxView blob={blob} onFailure={onFailure} />;
  if (renderer === "image") return <ImageView blob={blob} onFailure={onFailure} />;
  return <DocxView blob={blob} onFailure={onFailure} />;
}

/**
 * A raster image drawn from its own bytes. The object URL is revoked when the
 * blob changes or the panel closes; a decode failure (truncated screenshot,
 * mislabelled extension) hands over to the text preview like the other renderers.
 */
function ImageView({ blob, onFailure }: { blob: Blob; onFailure: () => void }) {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    const objectUrl = URL.createObjectURL(blob);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [blob]);

  if (!url) return <PreviewSpinner />;
  return (
    <div className="rounded-lg border border-border bg-white p-2">
      <img
        src={url}
        alt=""
        onError={onFailure}
        className="mx-auto h-auto max-w-full"
      />
    </div>
  );
}

function FilePreview({
  artifactId,
  version,
  filename,
  sizeBytes,
}: {
  artifactId: string;
  version: number;
  filename?: string;
  sizeBytes?: number;
}) {
  const renderer = fileRendererFor(filename);
  const [fellBack, setFellBack] = useState(false);
  // Stable, or BrowserRender's effect would refetch on every parent render.
  const handleFailure = useCallback(() => setFellBack(true), []);

  // A swept .html is drawn in an iframe (via the server preview), and an iframe
  // has no intrinsic height: laid out as a flex column, the card keeps its size
  // and the frame takes the rest of the tab instead of collapsing to min-height.
  const fills = fileFillsPanel(filename);

  return (
    <div className={fills ? "flex h-full flex-col gap-4" : "space-y-4"}>
      <div className={fills ? "shrink-0" : undefined}>
        <FileDownloadCard
          artifactId={artifactId}
          version={version}
          filename={filename}
          sizeBytes={sizeBytes}
        />
      </div>
      {renderer && !fellBack ? (
        <BrowserRender
          renderer={renderer}
          artifactId={artifactId}
          version={version}
          onFailure={handleFailure}
        />
      ) : (
        <div className={fills ? "min-h-0 flex-1" : undefined}>
          <ServerPreview artifactId={artifactId} version={version} fill={fills} />
        </div>
      )}
    </div>
  );
}

/**
 * Agent-authored HTML/SVG runs in a sandboxed iframe: it is model output, so it
 * must not reach the app's origin, cookies or localStorage.
 */
function SandboxedFrame({
  content,
  title,
}: {
  content: string;
  title: string;
}) {
  return (
    <iframe
      title={title}
      srcDoc={content}
      sandbox="allow-scripts"
      className="h-full w-full rounded-lg border border-border bg-white"
    />
  );
}

export const ArtifactPreview = React.memo<{
  kind: ArtifactKind;
  language?: string | null;
  title: string;
  content: string;
  artifactId?: string;
  version?: number;
  filename?: string;
  sizeBytes?: number;
}>(({
  kind,
  language,
  title,
  content,
  artifactId,
  version,
  filename,
  sizeBytes,
}) => {
  if (kind === "file") {
    return (
      <FilePreview
        artifactId={artifactId || ""}
        version={version || 0}
        filename={filename}
        sizeBytes={sizeBytes}
      />
    );
  }

  switch (kind) {
    case "markdown":
      return <MarkdownContent content={content} />;
    case "code":
      return <CodeBlock content={content} language={language ?? undefined} />;
    case "html":
      return <SandboxedFrame content={content} title={title} />;
    case "svg":
      return (
        <SandboxedFrame
          content={`<!DOCTYPE html><html><body style="margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh">${content}</body></html>`}
          title={title}
        />
      );
    case "csv":
      return <CsvTable content={content} />;
    case "json":
      return <JsonPreview content={content} />;
    case "mermaid":
      return <MermaidDiagram source={content} />;
    default:
      return (
        <pre className="whitespace-pre-wrap break-words text-sm leading-relaxed">
          {content}
        </pre>
      );
  }
});

ArtifactPreview.displayName = "ArtifactPreview";
