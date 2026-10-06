"use client";

import React, { useState, useMemo, useCallback, useEffect } from "react";
import {
  ChevronDown,
  ChevronUp,
  Terminal,
  AlertCircle,
  Loader2,
  CircleCheckBigIcon,
  StopCircle,
  Braces,
  FileJson,
  Copy,
  Check,
  FileText,
  Type,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ToolCall } from "@/app/types/types";
import { cn, copyText } from "@/lib/utils";
import { parseBrowserScreenshot } from "@/hooks/browserScreenshot.mjs";
import { fetchBrowserScreenshot } from "@/lib/browserScreenshots";
import { JsonTree } from "@/app/components/JsonTree";
import { MarkdownContent } from "@/app/components/MarkdownContent";
import { looksLikeMarkdown, resultView } from "@/lib/mcp-tools";

/** Too big to sit inline on one row, so it gets a toggle instead. */
const isLongText = (text: string) =>
  text.length > 120 || text.includes("\n");

/*
 * MarkdownContent is sized for an answer bubble — `text-sm`, `mt-8` headings,
 * five-unit paragraph gaps. Inside a tool box those margins are most of the
 * height, so the scale is pulled down to the box's own `text-xs`. Descendant
 * selectors, which outrank the component's own utility classes.
 */
const MARKDOWN_IN_BOX =
  "text-xs [&_h1]:mt-3 [&_h1]:mb-1.5 [&_h1]:text-sm [&_h2]:mt-3 [&_h2]:mb-1.5 [&_h2]:text-sm [&_h3]:mt-2.5 [&_h3]:mb-1 [&_h3]:text-xs [&_h4]:mt-2.5 [&_h4]:mb-1 [&_h4]:text-xs [&_p]:my-1.5 [&_p]:leading-5 [&_ul]:my-1.5 [&_ol]:my-1.5 [&_li]:mt-0.5 [&_hr]:my-2 [&_table]:my-2 [&_th]:px-2 [&_th]:py-1 [&_td]:px-2 [&_td]:py-1 [&_blockquote]:my-1.5";

interface ToolCallBoxProps {
  toolCall: ToolCall;
  uiComponent?: any;
  stream?: any;
  isInterrupted?: boolean;
  /** Needed to re-fetch a screenshot whose presigned URL has expired. */
  threadId?: string;
}

export const ToolCallBox = React.memo<ToolCallBoxProps>(
  ({ toolCall, uiComponent, stream, isInterrupted, threadId }) => {
    const [isExpanded, setIsExpanded] = useState(false);
    const [expandedArgs, setExpandedArgs] = useState<Record<string, boolean>>(
      {}
    );
    const [isResultExpanded, setIsResultExpanded] = useState(false);
    /* A JSON result shows its tree; this switches to the bytes behind it. */
    const [showRaw, setShowRaw] = useState(false);
    const [copied, setCopied] = useState(false);
    /* The presigned URL is tried first and this switches to the fetched copy
       when it fails — see the img below. */
    const [screenshotFellBack, setScreenshotFellBack] = useState(false);
    /* Object URL for a screenshot re-fetched through the platform's own route. */
    const [refetched, setRefetched] = useState<string | null>(null);

    const { name, args, result, status } = useMemo(() => {
      const toolName = toolCall.name || "Unknown Tool";
      const toolArgs = toolCall.args || "{}";
      let parsedArgs = {};
      try {
        parsedArgs =
          typeof toolArgs === "string" ? JSON.parse(toolArgs) : toolArgs;
      } catch {
        parsedArgs = { raw: toolArgs };
      }
      const toolResult = toolCall.result || null;
      const toolStatus = isInterrupted
        ? "interrupted"
        : toolCall.status || "completed";

      return {
        name: toolName,
        args: parsedArgs,
        result: toolResult,
        status: toolStatus,
      };
    }, [toolCall, isInterrupted]);

    /*
     * Arguments render as a flat key/value grid rather than one accordion per
     * key: most of them are short (`a: 2`), and wrapping those in a bordered
     * row plus a collapsed pane cost three nested boxes and a click to read two
     * characters. Only values too long for a line keep a toggle.
     */
    const argEntries = useMemo(
      () =>
        Object.entries(args).map(([key, value]) => {
          const text =
            typeof value === "string" ? value : JSON.stringify(value, null, 2);
          const oneLine = text.trim().replace(/\s+/g, " ");
          return {
            key,
            text,
            isLong: isLongText(text),
            preview: oneLine.length > 60 ? `${oneLine.slice(0, 60)}…` : oneLine,
          };
        }),
      [args]
    );

    /*
     * The key column is sized to the longest key actually present rather than a
     * fixed width: at `w-24` a one-character key like `a` left ~100px of dead
     * space before its value. `ch` is exact here because the keys are mono.
     */
    const keyColumnWidth = useMemo(() => {
      const longest = argEntries.reduce(
        (max, { key }) => Math.max(max, key.length),
        0
      );
      return `${Math.min(longest, 24)}ch`;
    }, [argEntries]);

    /*
     * Nearly every tool answers in JSON, and the harness path delivers it as one
     * long string — so printing the result verbatim meant a wall of one-line JSON
     * whose collapsed preview read `{ "results": [ { …`, saying nothing about what
     * the tool found. `resultView` is the same decision the registry's tool probe
     * makes, and it keeps genuine prose (most tool errors) as prose.
     */
    const view = useMemo(() => resultView(result), [result]);

    /* The text path below is unchanged; only a text result reaches it. */
    const resultText = view.kind === "text" ? view.text : null;

    /*
     * A tool that answers in Markdown — a report, a summary, a table — was
     * printed as monospace source: `## 요약` and pipe-drawn table rows, which is
     * the same complaint the JSON tree fixed one shape over. It renders instead,
     * with the source still a click away, since `**` around the wrong words is
     * only visible in the source.
     */
    const isMarkdown = useMemo(
      () => !!resultText && looksLikeMarkdown(resultText),
      [resultText]
    );

    const copyable =
      view.kind === "json" ? view.raw : view.kind === "text" ? view.text : "";

    const copy = useCallback(async () => {
      if (!copyable) return;
      if (await copyText(copyable)) {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }
    }, [copyable]);

    /*
     * A browser screenshot is the one result worth looking at rather than
     * reading: the tool returns JSON pointing at a PNG, so as text it is a byte
     * count and a 1.5 KB presigned URL, and the page the agent actually saw was
     * invisible. The JSON stays below — it says which session and how large —
     * but the image goes on top.
     */
    const screenshot = useMemo(
      () => parseBrowserScreenshot(result),
      [result]
    );

    /*
     * The presigned URL first: it works the instant the call lands, including
     * mid-turn, before the message exists anywhere the server could look it up,
     * and it costs no request of our own. It expires after an hour though, so a
     * reopened thread falls back to the platform's route.
     *
     * That fallback cannot be an `<img src>`. The route is gated on the session
     * and an `<img>` sends no Authorization header, so it 401s — see
     * fetchBrowserScreenshot. The bytes are fetched instead and shown as an
     * object URL.
     */
    const canRefetch = !!(screenshot?.s3Key && threadId);
    const screenshotSrc = screenshotFellBack
      ? refetched
      : screenshot?.url ?? refetched;

    /* A new capture in the same box gets a fresh chance at its own URL. */
    useEffect(() => {
      setScreenshotFellBack(false);
    }, [screenshot?.url, screenshot?.s3Key]);

    /*
     * Fetched only once the presigned URL has actually failed, or when there was
     * none to begin with — a live capture must not cost a second download of the
     * same PNG. The object URL is revoked on the way out; without that a long
     * thread leaks one screenshot per render.
     */
    const needsRefetch =
      canRefetch && !refetched && (screenshotFellBack || !screenshot?.url);
    useEffect(() => {
      if (!needsRefetch || !threadId) return;
      let objectUrl: string | null = null;
      let cancelled = false;

      fetchBrowserScreenshot(threadId, toolCall.id).then((url) => {
        if (!url) return;
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setRefetched(url);
      });

      return () => {
        cancelled = true;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
      };
    }, [needsRefetch, threadId, toolCall.id]);

    /* A one-line result like `1048576` is shown outright; anything longer
       collapses behind a row previewing its first line. */
    const isResultShort = !!resultText && !isLongText(resultText);

    const resultPreview = useMemo(() => {
      if (!resultText) return null;
      /* A pretty-printed JSON result opens on a bare `{`, which previews
         nothing — collapse the whole thing onto one line instead. */
      const oneLine = resultText.trim().replace(/\s+/g, " ");
      return oneLine.length > 80 ? `${oneLine.slice(0, 80)}…` : oneLine;
    }, [resultText]);

    const statusIcon = useMemo(() => {
      switch (status) {
        case "completed":
          return (
            <CircleCheckBigIcon
              size={13}
              className="text-muted-foreground"
            />
          );
        case "error":
          return (
            <AlertCircle
              size={13}
              className="text-destructive"
            />
          );
        case "pending":
          return (
            <Loader2
              size={13}
              className="animate-spin"
            />
          );
        case "interrupted":
          return (
            <StopCircle
              size={13}
              className="text-warning"
            />
          );
        default:
          return (
            <Terminal
              size={13}
              className="text-muted-foreground"
            />
          );
      }
    }, [status]);

    const toggleExpanded = useCallback(() => {
      setIsExpanded((prev) => !prev);
    }, []);

    const toggleArgExpanded = useCallback((argKey: string) => {
      setExpandedArgs((prev) => ({
        ...prev,
        [argKey]: !prev[argKey],
      }));
    }, []);

    const toggleResultExpanded = useCallback(() => {
      setIsResultExpanded((prev) => !prev);
    }, []);

    /* `view.kind` rather than `result` itself: a tool that returned only
       whitespace has nothing to open, and the box used to offer a toggle onto an
       empty pane. */
    const hasContent = view.kind !== "empty" || argEntries.length > 0;

    // Auto-expand when status is interrupted
    useEffect(() => {
      if (status === "interrupted" && hasContent) {
        setIsExpanded(true);
      }
    }, [status, hasContent]);

    /* A screenshot behind a collapsed toggle is no more visible than the JSON
       was. Opening the box is the whole point of rendering it.
       Keyed on the result, not the resolved src: a re-fetched image arrives a
       moment later, and waiting for it would make the box open with a jump. */
    useEffect(() => {
      if (screenshot) {
        setIsExpanded(true);
      }
    }, [screenshot]);

    return (
      <div
        className={cn(
          /* Full width and `bg-muted/50`, matching the reasoning and sub-agent
             blocks it sits between — a chip sized to its own label made the
             width look arbitrary and jump when expanded. A tool call is
             machinery rather than part of the answer, so the container carries
             the separation and the label itself stays quiet. */
          "w-full overflow-hidden rounded-md border border-border bg-muted/50 shadow-none outline-none transition-colors duration-200"
        )}
      >
        <Button
          variant="ghost"
          size="sm"
          onClick={toggleExpanded}
          className={cn(
            "flex h-8 w-full items-center justify-between gap-2 rounded-none border-none bg-transparent px-2.5 py-0 text-left shadow-none outline-none focus-visible:ring-0 focus-visible:ring-offset-0 disabled:cursor-default"
          )}
          disabled={!hasContent}
        >
          <span className="flex min-w-0 items-center gap-1.5">
            {statusIcon}
            <span className="truncate text-xs font-normal text-muted-foreground">
              {name}
            </span>
          </span>
          {hasContent &&
            (isExpanded ? (
              <ChevronUp
                size={12}
                className="shrink-0 text-muted-foreground"
              />
            ) : (
              <ChevronDown
                size={12}
                className="shrink-0 text-muted-foreground"
              />
            ))}
        </Button>

        {isExpanded && hasContent && (
          <div className="space-y-3 border-t border-border p-2.5">
            {screenshotSrc && (
              <div>
                <p className="mb-1.5 caps-label-xs text-muted-foreground">
                  Screenshot
                </p>
                {/* A plain img, as ChartRenderer and AttachmentBar already use:
                    the source is an S3 presigned URL or an authenticated route on
                    the API host, and next/image cannot be pointed at either
                    without allowlisting a signature-bearing domain. Bordered
                    because a screenshot of a white page would otherwise dissolve
                    into the bubble. */}
                <img
                  src={screenshotSrc}
                  alt="What the browser session showed"
                  className="w-full rounded-sm border border-border"
                  onError={() => setScreenshotFellBack(true)}
                />
              </div>
            )}
            {argEntries.length > 0 && (
              <div>
                <p className="mb-1.5 caps-label-xs text-muted-foreground">
                  Arguments
                </p>
                {/* Dividers only — no fill and no outline. `bg-card` is pure
                    white, so an inner box read brighter than the `bg-muted/50`
                    container around it and highlighted the machinery instead of
                    letting it recede. */}
                <ul className="m-0 divide-y divide-border/60">
                  {argEntries.map(({ key, text, isLong, preview }) => (
                    <li
                      key={key}
                      className="px-0.5 py-1"
                    >
                      {isLong ? (
                        <>
                          <button
                            onClick={() => toggleArgExpanded(key)}
                            className="flex w-full items-baseline gap-3 text-left"
                          >
                            <span
                              className="shrink-0 truncate font-mono text-xs text-muted-foreground"
                              style={{ width: keyColumnWidth }}
                            >
                              {key}
                            </span>
                            <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
                              {expandedArgs[key]
                                ? `${text.length.toLocaleString()} chars`
                                : preview}
                            </span>
                            {expandedArgs[key] ? (
                              <ChevronUp
                                size={12}
                                className="shrink-0 text-muted-foreground"
                              />
                            ) : (
                              <ChevronDown
                                size={12}
                                className="shrink-0 text-muted-foreground"
                              />
                            )}
                          </button>
                          {expandedArgs[key] && (
                            <pre className="m-0 mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-sm bg-muted px-2 py-1.5 font-mono text-xs leading-5 text-foreground">
                              {text}
                            </pre>
                          )}
                        </>
                      ) : (
                        /* Key and value share a baseline on one line, both left
                           aligned — right-aligning the values made short ones
                           float away from their keys. */
                        <div className="flex items-baseline gap-3">
                          <span
                            className="shrink-0 truncate font-mono text-xs text-muted-foreground"
                            style={{ width: keyColumnWidth }}
                          >
                            {key}
                          </span>
                          <span className="min-w-0 flex-1 whitespace-pre-wrap break-all font-mono text-xs text-foreground">
                            {text}
                          </span>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {view.kind !== "empty" && (
              <div>
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <p className="caps-label-xs text-muted-foreground">Result</p>
                  <div className="flex items-center gap-0.5">
                    {/* Not redundant with the tree: a re-serialised tree loses key
                        order, whitespace and the text-block boundaries, and those
                        are what someone checks when a tool's answer looks wrong. */}
                    {view.kind === "json" && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-5 px-1.5 text-xxs text-muted-foreground"
                        onClick={() => setShowRaw((prev) => !prev)}
                      >
                        {showRaw ? (
                          <Braces className="size-2.5" />
                        ) : (
                          <FileJson className="size-2.5" />
                        )}
                        {showRaw ? "트리" : "원본"}
                      </Button>
                    )}
                    {/* Same toggle for the same reason as JSON's: the rendered
                        document hides the syntax, and the syntax is what someone
                        checks when the formatting came out wrong. */}
                    {isMarkdown && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-5 px-1.5 text-xxs text-muted-foreground"
                        onClick={() => setShowRaw((prev) => !prev)}
                      >
                        {showRaw ? (
                          <Type className="size-2.5" />
                        ) : (
                          <FileText className="size-2.5" />
                        )}
                        {showRaw ? "본문" : "원본"}
                      </Button>
                    )}
                    {copyable && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-5 px-1.5 text-xxs text-muted-foreground"
                        onClick={() => void copy()}
                      >
                        {copied ? (
                          <Check className="size-2.5" />
                        ) : (
                          <Copy className="size-2.5" />
                        )}
                        {copied ? "복사됨" : "복사"}
                      </Button>
                    )}
                  </div>
                </div>
                {view.kind === "json" ? (
                  showRaw ? (
                    <pre className="m-0 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-sm bg-muted px-2 py-1.5 font-mono text-xs leading-5 text-foreground">
                      {view.raw}
                    </pre>
                  ) : (
                    /* Capped like the raw pane: a search result carrying a page of
                       scraped text would otherwise push the answer below it off
                       the screen. Two levels open, not the probe panel's three —
                       the chat is a place to glance at a result, not audit it. */
                    <div className="max-h-64 overflow-auto">
                      <JsonTree
                        value={view.value}
                        collapsed={2}
                        size="xs"
                      />
                    </div>
                  )
                ) : isMarkdown && resultText ? (
                  /* Shown outright rather than behind a second toggle, as the
                     JSON tree is: opening the box was already the request to
                     read the result. Capped the same way so a long report
                     cannot push the answer below it off the screen. */
                  <div className="max-h-64 overflow-auto">
                    {showRaw ? (
                      <pre className="m-0 whitespace-pre-wrap break-all rounded-sm bg-muted px-2 py-1.5 font-mono text-xs leading-5 text-foreground">
                        {resultText}
                      </pre>
                    ) : (
                      <MarkdownContent
                        content={resultText}
                        className={MARKDOWN_IN_BOX}
                      />
                    )}
                  </div>
                ) : /* A short result needs no toggle at all — the value is the
                      row. Only multi-line or long output collapses. */
                isResultShort ? (
                  <div className="whitespace-pre-wrap break-all px-0.5 py-1 font-mono text-xs text-foreground">
                    {resultText}
                  </div>
                ) : (
                  <>
                    <button
                      onClick={toggleResultExpanded}
                      className="flex w-full items-baseline gap-3 px-0.5 py-1 text-left"
                    >
                      <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
                        {resultPreview}
                      </span>
                      <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
                        {view.text.length.toLocaleString()} chars
                        {isResultExpanded ? (
                          <ChevronUp size={12} />
                        ) : (
                          <ChevronDown size={12} />
                        )}
                      </span>
                    </button>
                    {isResultExpanded && (
                      <pre className="m-0 mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-sm bg-muted px-2 py-1.5 font-mono text-xs leading-5 text-foreground">
                        {resultText}
                      </pre>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }
);

ToolCallBox.displayName = "ToolCallBox";
