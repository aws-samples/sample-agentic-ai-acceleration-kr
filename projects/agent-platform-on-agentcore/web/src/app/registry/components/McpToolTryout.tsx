"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertTriangle,
  Braces,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  FileJson,
  Loader2,
  Play,
  Plug,
  Sparkles,
} from "lucide-react";
import { JsonTree } from "@/app/components/JsonTree";
import { useClient } from "@/providers/ClientProvider";
import { cn, copyText } from "@/lib/utils";
import {
  buildArgumentFields,
  buildArguments,
  countArguments,
  resultIsError,
  resultView,
  type ArgumentField,
  type McpToolInfo,
} from "@/lib/mcp-tools";
import type { RegistryRecordDetail } from "@/lib/registry";

interface McpToolTryoutProps {
  record: RegistryRecordDetail;
}

interface ToolState {
  fields: ArgumentField[];
  result: { success: boolean; result?: unknown; error?: string } | null;
  running: boolean;
}

/**
 * Connect to a registered MCP server, list its tools, and call one.
 *
 * The registry equivalent of `RecordTryout`: a probe, not the MCP Inspector. The
 * Inspector page exists to reach *any* server from a hand-written config; this
 * only ever talks to the record that is open, so it takes no connection fields
 * and the endpoint is resolved server-side from the descriptor.
 *
 * Tools are fetched on demand rather than when the panel opens. A tools/list is
 * a live MCP session — a TCP connect plus an initialize round trip — and most
 * panel opens are someone reading the description, not testing a call.
 */
export function McpToolTryout({ record }: McpToolTryoutProps) {
  const apiClient = useClient();
  const [tools, setTools] = useState<McpToolInfo[] | null>(null);
  const [endpoint, setEndpoint] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [states, setStates] = useState<Record<string, ToolState>>({});
  // Set once the panel moves to another record, so a slow tools/list from the
  // previous one cannot land in this one's state.
  const liveRef = useRef(true);

  useEffect(() => {
    liveRef.current = true;
    // A new record means new tools; drop everything the last one accumulated.
    setTools(null);
    setEndpoint(null);
    setError(null);
    setExpanded(null);
    setStates({});
    return () => {
      liveRef.current = false;
    };
  }, [record.record_id]);

  const connect = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await apiClient.listRecordMcpTools(record.record_id);
      if (!liveRef.current) return;
      setTools(body.tools);
      setEndpoint(body.endpoint);
    } catch (e) {
      if (!liveRef.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setTools(null);
    } finally {
      if (liveRef.current) setLoading(false);
    }
  }, [apiClient, record.record_id]);

  const toggle = (tool: McpToolInfo) => {
    setExpanded((prev) => (prev === tool.name ? null : tool.name));
    setStates((prev) =>
      prev[tool.name]
        ? prev
        : {
            ...prev,
            [tool.name]: {
              fields: buildArgumentFields(tool.input_schema),
              result: null,
              running: false,
            },
          }
    );
  };

  const setField = (toolName: string, index: number, value: unknown) => {
    setStates((prev) => {
      const state = prev[toolName];
      if (!state) return prev;
      const fields = state.fields.map((f, i) =>
        i === index ? { ...f, value } : f
      );
      return { ...prev, [toolName]: { ...state, fields } };
    });
  };

  const execute = useCallback(
    async (toolName: string) => {
      const state = states[toolName];
      if (!state || state.running) return;

      setStates((prev) => ({
        ...prev,
        [toolName]: { ...prev[toolName], running: true, result: null },
      }));

      let result: ToolState["result"];
      try {
        result = await apiClient.callRecordMcpTool(
          record.record_id,
          toolName,
          buildArguments(state.fields)
        );
      } catch (e) {
        // A transport failure, as opposed to a tool that ran and failed — the
        // route reports the latter as success=false with a 200.
        result = {
          success: false,
          error: e instanceof Error ? e.message : String(e),
        };
      }
      if (!liveRef.current) return;
      setStates((prev) => ({
        ...prev,
        [toolName]: { ...prev[toolName], running: false, result },
      }));
    },
    [apiClient, record.record_id, states]
  );

  return (
    <div className="space-y-2 rounded-md border border-border bg-muted/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="caps-label-xs text-muted-foreground">도구 살펴보기</p>
        {tools !== null && (
          <Button
            size="sm"
            variant="ghost"
            className="h-6 text-xs"
            onClick={() => void connect()}
            disabled={loading}
          >
            새로 고침
          </Button>
        )}
      </div>

      {tools === null ? (
        <>
          <Button
            size="sm"
            variant="outline"
            className="w-full"
            onClick={() => void connect()}
            disabled={loading}
          >
            {loading ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Plug className="size-3.5" />
            )}
            {loading ? "연결 중…" : "연결하고 도구 목록 보기"}
          </Button>
          {error && <ConnectError error={error} />}
        </>
      ) : (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            {tools.length}개 도구
            {endpoint && (
              // A separate line: an AgentCore endpoint is a URL-encoded ARN long
              // enough to wrap twice, and inline it read as part of the count.
              <span className="mt-0.5 block break-all font-mono text-xxs opacity-70">
                {endpoint}
              </span>
            )}
          </p>
          {error && (
            <p className="text-xs text-destructive">{error}</p>
          )}

          {tools.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              이 서버가 노출하는 도구가 없습니다.
            </p>
          ) : (
            <ul className="divide-y divide-border overflow-hidden rounded-md border border-border bg-background">
              {tools.map((tool) => {
                const isOpen = expanded === tool.name;
                const state = states[tool.name];
                const argCount = countArguments(tool.input_schema);
                // The MCP Apps `ui` block; worth flagging because such a tool
                // renders an interface in chat rather than returning text.
                const hasApp = Boolean(tool.meta?.ui);

                return (
                  <li key={tool.name}>
                    <button
                      type="button"
                      onClick={() => toggle(tool)}
                      className="flex w-full items-start gap-2 px-2.5 py-2 text-left transition-colors hover:bg-accent/60"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-mono text-xs">{tool.name}</span>
                          {argCount > 0 && (
                            <span className="text-xxs text-muted-foreground">
                              {argCount} {argCount === 1 ? "arg" : "args"}
                            </span>
                          )}
                          {hasApp && (
                            <span className="flex items-center gap-0.5 text-xxs text-muted-foreground">
                              <Sparkles className="size-2.5" />
                              app
                            </span>
                          )}
                        </div>
                        {tool.description && (
                          <span className="mt-0.5 block text-xs leading-normal text-muted-foreground">
                            {tool.description}
                          </span>
                        )}
                      </div>
                      {isOpen ? (
                        <ChevronUp className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                      ) : (
                        <ChevronDown className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                      )}
                    </button>

                    {isOpen && state && (
                      <div className="space-y-2 border-t border-border bg-muted/30 px-2.5 py-2">
                        {state.fields.length === 0 ? (
                          <p className="text-xs text-muted-foreground">
                            인자가 없습니다.
                          </p>
                        ) : (
                          state.fields.map((field, index) => (
                            <ArgumentInput
                              key={field.key}
                              id={`${record.record_id}-${tool.name}-${field.key}`}
                              field={field}
                              onChange={(value) =>
                                setField(tool.name, index, value)
                              }
                            />
                          ))
                        )}

                        <Button
                          size="sm"
                          className="w-full"
                          onClick={() => void execute(tool.name)}
                          disabled={state.running}
                        >
                          {state.running ? (
                            <Loader2 className="size-3.5 animate-spin" />
                          ) : (
                            <Play className="size-3.5" />
                          )}
                          {state.running ? "실행 중…" : "실행"}
                        </Button>

                        {state.result && <ToolResult result={state.result} />}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * A failed connection.
 *
 * Split on the blank line the server puts between its explanation and the raw
 * cause: the explanation is the part that helps, and the cause below it is a
 * URL-encoded ARN plus an MDN link. Run together in one paragraph the sentence
 * that matters was unreadable.
 */
function ConnectError({ error }: { error: string }) {
  const [head, ...rest] = error.split("\n\n");
  const detail = rest.join("\n\n").trim();

  return (
    <div className="space-y-1 text-xs leading-normal text-destructive">
      <p className="flex items-start gap-1.5">
        <AlertTriangle className="mt-0.5 size-3 shrink-0" />
        {head}
      </p>
      {detail && (
        <p className="break-all pl-[1.125rem] text-xxs opacity-80">{detail}</p>
      )}
    </div>
  );
}

/** One argument, rendered by its schema type. */
function ArgumentInput({
  id,
  field,
  onChange,
}: {
  id: string;
  field: ArgumentField;
  onChange: (value: unknown) => void;
}) {
  const { key, schema, value } = field;
  const type = schema.type;

  return (
    <div className="space-y-1">
      <Label htmlFor={id} className="font-mono text-xxs">
        {key}
        {schema.required === true && (
          <span className="ml-0.5 text-destructive">*</span>
        )}
        {type && (
          <span className="ml-1 font-sans font-normal text-muted-foreground">
            {type}
          </span>
        )}
      </Label>
      {schema.description && (
        <p className="text-xxs leading-normal text-muted-foreground">
          {schema.description}
        </p>
      )}
      {type === "boolean" ? (
        <div className="flex items-center gap-2">
          <Switch
            id={id}
            checked={Boolean(value)}
            onCheckedChange={onChange}
          />
          <span className="text-xs text-muted-foreground">
            {value ? "true" : "false"}
          </span>
        </div>
      ) : type === "array" || type === "object" ? (
        <Textarea
          id={id}
          rows={2}
          value={typeof value === "string" ? value : JSON.stringify(value)}
          onChange={(e) => onChange(e.target.value)}
          placeholder={type === "array" ? '["a", "b"]' : '{"k": "v"}'}
          className="min-h-0 resize-none font-mono text-xs"
        />
      ) : (
        <Input
          id={id}
          type={type === "number" || type === "integer" ? "number" : "text"}
          value={value === undefined ? "" : String(value)}
          onChange={(e) => onChange(e.target.value)}
          placeholder={
            schema.enum
              ? schema.enum.join(" | ")
              : schema.default !== undefined
                ? String(schema.default)
                : ""
          }
          className="h-8 text-xs"
        />
      )}
    </div>
  );
}

/**
 * A tool's return value.
 *
 * The shape MCP tools actually return is JSON serialised into a text block, so
 * the old rendering — text blocks as prose, JSON only as a last resort — printed
 * a wall of single-line JSON for nearly every call. `resultView` unwraps that,
 * and JSON gets a collapsible tree while genuine prose stays prose.
 *
 * The tree can be switched to the raw bytes, which is not redundant: a probe
 * exists to see what a server sent, and a re-serialised tree hides key order,
 * whitespace and the text-block boundaries.
 */
function ToolResult({
  result,
}: {
  result: { success: boolean; result?: unknown; error?: string };
}) {
  const [showRaw, setShowRaw] = useState(false);
  const [copied, setCopied] = useState(false);

  // A tool that ran and reported failure is shown in the error style too — the
  // route only reports transport failures as success=false, so `isError` on the
  // payload is the other half of "this call did not work".
  const failed = !result.success || resultIsError(result.result);
  const view = result.success
    ? resultView(result.result)
    : ({ kind: "text", text: result.error ?? "알 수 없는 오류" } as const);

  const copyable =
    view.kind === "json" ? view.raw : view.kind === "text" ? view.text : "";

  const copy = async () => {
    if (!copyable) return;
    if (await copyText(copyable)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    }
  };

  return (
    <div
      className={cn(
        "overflow-hidden rounded-md border",
        failed
          ? "border-destructive/25 bg-destructive/[0.07]"
          : "border-border bg-background"
      )}
    >
      <div className="flex items-center justify-between gap-2 px-2 pt-1.5">
        <span
          className={cn(
            "caps-label-xs",
            failed ? "text-destructive" : "text-muted-foreground"
          )}
        >
          {failed ? "오류" : "결과"}
        </span>
        <div className="flex items-center gap-0.5">
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

      <div className="max-h-64 overflow-auto px-2 pb-2 pt-1">
        {view.kind === "empty" ? (
          <p className="text-xxs text-muted-foreground">
            반환값이 없습니다.
          </p>
        ) : view.kind === "json" && !showRaw ? (
          // Three levels: a result is usually `{results: [ … ]}`, and at two the
          // array opened but every item in it stayed a `{...}`, which is the one
          // level that actually holds what someone ran the tool to see.
          <JsonTree value={view.value} collapsed={3} size="xs" />
        ) : (
          // `bg-transparent border-0 p-0` undoes the global `pre` rule in
          // globals.css, which paints its own muted fill and hairline — a second
          // box inside this one.
          <pre
            className={cn(
              "whitespace-pre-wrap break-all border-0 bg-transparent p-0 font-mono text-xxs leading-normal",
              failed && "text-destructive"
            )}
          >
            {view.kind === "json" ? view.raw : view.text}
          </pre>
        )}
      </div>
    </div>
  );
}
