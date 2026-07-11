'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useState } from 'react';
import JsonView from '@uiw/react-json-view';
import { ChevronDown, ChevronUp, Play, Loader2, FormInput, Code2 } from 'lucide-react';
import { SchemaForm, isFullyRenderable } from './SchemaForm';

interface McpTool {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
}

interface ToolListResponse {
  tools: McpTool[];
  status: 'Complete' | 'Unavailable' | 'Disabled';
}

// Build a starter input object from a tool's JSON schema so the operator
// doesn't author it from scratch: prefills `default` values, leaves the rest.
function objectFromSchema(schema: Record<string, unknown> | undefined): Record<string, unknown> {
  const props = (schema?.properties ?? {}) as Record<string, { default?: unknown }>;
  const obj: Record<string, unknown> = {};
  for (const key of Object.keys(props)) {
    if (props[key]?.default !== undefined) obj[key] = props[key].default;
  }
  return obj;
}

export function ToolList() {
  const [data, setData] = useState<ToolListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Tool-test (playground) state.
  const [selected, setSelected] = useState<McpTool | null>(null);
  const [inputMode, setInputMode] = useState<'form' | 'json'>('form');
  const [formValues, setFormValues] = useState<Record<string, unknown>>({});
  const [toolInput, setToolInput] = useState('{}');
  const [result, setResult] = useState<unknown>(null);
  const [executing, setExecuting] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    const fetchTools = async () => {
      try {
        setLoading(true);
        const response = await fetch('/api/tools/list');
        if (!response.ok) throw new Error(`API error: ${response.status}`);
        setData((await response.json()) as ToolListResponse);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    };
    fetchTools();
  }, []);

  const selectTool = (tool: McpTool) => {
    const schema = tool.inputSchema;
    const seed = objectFromSchema(schema);
    setSelected(tool);
    setFormValues(seed);
    setToolInput(JSON.stringify(seed, null, 2));
    setInputMode(isFullyRenderable(schema as never) ? 'form' : 'json');
    setResult(null);
    setExpanded(tool.name);
  };

  // Keep form and JSON views in sync when editing the form.
  const handleFormChange = (next: Record<string, unknown>) => {
    setFormValues(next);
    setToolInput(JSON.stringify(next, null, 2));
  };

  // When switching modes, carry edits across.
  const switchMode = (mode: 'form' | 'json') => {
    if (mode === inputMode) return;
    if (mode === 'form') {
      try {
        const parsed = JSON.parse(toolInput);
        if (parsed && typeof parsed === 'object') setFormValues(parsed);
      } catch {
        // keep last good form values if JSON is currently invalid
      }
    } else {
      setToolInput(JSON.stringify(formValues, null, 2));
    }
    setInputMode(mode);
  };

  const executeTool = async () => {
    if (!selected) return;
    setExecuting(true);
    try {
      const input = inputMode === 'form' ? formValues : JSON.parse(toolInput);
      const response = await fetch('/api/tools/call', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool_name: selected.name, input }),
      });
      setResult(await response.json());
    } catch (err) {
      setResult({ error: err instanceof Error ? err.message : String(err) });
    } finally {
      setExecuting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        불러오는 중…
      </div>
    );
  }
  if (data?.status === 'Disabled') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        Tool Gateway가 비활성화되어 있습니다.
      </div>
    );
  }
  if (data?.status === 'Unavailable') {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        Tool Gateway에 연결할 수 없습니다.
      </div>
    );
  }
  if (error) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-destructive">오류: {error}</div>
    );
  }
  if (!data || data.tools.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        등록된 tool이 없습니다.
      </div>
    );
  }

  const modeBtn = (mode: 'form' | 'json', label: string, Icon: typeof FormInput) => (
    <button
      type="button"
      onClick={() => switchMode(mode)}
      className={`inline-flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium transition-colors ${
        inputMode === mode
          ? 'bg-primary text-primary-foreground'
          : 'text-muted-foreground hover:text-foreground'
      }`}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Left: tool catalogue */}
      <div className="lg:col-span-1 space-y-2 max-h-[48rem] overflow-y-auto">
        {data.tools.map((tool) => {
          const isSel = selected?.name === tool.name;
          return (
            <button
              key={tool.name}
              onClick={() => selectTool(tool)}
              className={`w-full text-left p-3 rounded-lg border transition-colors ${
                isSel ? 'border-primary bg-primary/5' : 'border-input hover:bg-muted/50'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-sm font-semibold truncate">{tool.name}</span>
                {expanded === tool.name ? (
                  <ChevronUp className="h-4 w-4 shrink-0" />
                ) : (
                  <ChevronDown className="h-4 w-4 shrink-0" />
                )}
              </div>
              {tool.description && (
                <p className="mt-1 text-xs text-muted-foreground line-clamp-2">{tool.description}</p>
              )}
            </button>
          );
        })}
      </div>

      {/* Right: tool tester */}
      <div className="lg:col-span-2 space-y-4">
        {!selected ? (
          <div className="flex items-center justify-center h-48 rounded-lg border border-dashed text-sm text-muted-foreground">
            왼쪽에서 도구를 선택해 호출을 테스트하세요.
          </div>
        ) : (
          <>
            <div className="border rounded-lg p-4 space-y-4 bg-card">
              <h3 className="font-mono text-sm font-semibold break-all">도구 테스트: {selected.name}</h3>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">입력</span>
                  <div className="inline-flex rounded-md border border-input p-0.5">
                    {modeBtn('form', '폼', FormInput)}
                    {modeBtn('json', 'JSON', Code2)}
                  </div>
                </div>

                {inputMode === 'form' ? (
                  <div className="rounded-md border border-input p-4">
                    <SchemaForm
                      schema={selected.inputSchema as never}
                      value={formValues}
                      onChange={handleFormChange}
                    />
                  </div>
                ) : (
                  <textarea
                    value={toolInput}
                    onChange={(e) => setToolInput(e.target.value)}
                    className="w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    rows={8}
                  />
                )}
              </div>

              {selected.inputSchema && (
                <details className="group">
                  <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
                    입력 스키마 보기
                  </summary>
                  <div className="mt-2 bg-muted/50 rounded p-3 overflow-auto max-h-48">
                    <JsonView value={selected.inputSchema} className="text-sm !bg-transparent" collapsed={2} />
                  </div>
                </details>
              )}

              <button
                onClick={executeTool}
                disabled={executing}
                className="w-full inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
              >
                {executing ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    실행 중…
                  </>
                ) : (
                  <>
                    <Play className="h-4 w-4" />
                    실행
                  </>
                )}
              </button>
            </div>

            {result !== null && (
              <div className="border rounded-lg p-4 space-y-2 bg-card">
                <h3 className="text-sm font-semibold">결과</h3>
                <div className="overflow-auto max-h-96 bg-muted/50 rounded p-3">
                  <JsonView value={result as object} className="text-sm !bg-transparent" collapsed={2} />
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
