// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useState } from 'react';
import { ChevronDown, ChevronRight, Database, Code, BarChart3, Eye, Wand2, Play, Pencil, Copy, Loader2 } from 'lucide-react';
import { ChartRenderer } from './ChartRenderer';
import type { ToolCall } from './types';

const TOOL_META: Record<string, { label: string; icon: React.ReactNode }> = {
  ask_sql_specialist: { label: 'SQL Specialist 호출', icon: <Database size={14} /> },
  ask_code_specialist: { label: 'Code Specialist 호출', icon: <Code size={14} /> },
  ask_validator: { label: 'SQL Validator 검증', icon: <Eye size={14} /> },
  ask_viz_specialist: { label: 'Viz Specialist', icon: <BarChart3 size={14} /> },
  query_db: { label: 'SQL 실행', icon: <Database size={14} /> },
  get_schema: { label: 'Schema 조회', icon: <Database size={14} /> },
  render_chart: { label: 'Chart spec 생성', icon: <BarChart3 size={14} /> },
  execute_python: { label: 'Python 실행', icon: <Code size={14} /> },
};

interface Props {
  toolCall: ToolCall;
  /** 인라인 SQL 재실행(§57 — deep-insight 차용). 세션ID 있으면 실행/편집 활성. */
  sessionId?: string | null;
}

/**
 * 인라인 SQL 에디터 — 생성된 SQL 을 보고/수정해 **LLM 미경유** 재실행(ms~s,
 * 토큰 0). POST /admin/chat/sql/execute 는 query_db Lambda 검증 스택(sqlglot
 * AST+화이트리스트+SELECT-only+LIMIT+read-only role)을 그대로 통과하므로 안전.
 * 결과는 블록 안에 인라인 테이블 — 대화 이력을 오염시키지 않는다.
 */
function SqlEditor({ sql, sessionId }: { sql: string; sessionId: string }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(sql);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{
    ok?: boolean;
    rows?: Array<Record<string, unknown>>;
    row_count?: number;
    error?: string;
  } | null>(null);

  const run = async (q: string) => {
    setRunning(true);
    setResult(null);
    try {
      const r = await fetch('/api/chat-proxy/admin/chat/sql/execute', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql: q, session_id: sessionId }),
      });
      setResult(await r.json());
    } catch (e) {
      setResult({ ok: false, error: String(e) });
    } finally {
      setRunning(false);
    }
  };

  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className="text-muted-foreground">생성된 SQL</span>
        <button
          type="button"
          onClick={() => run(draft)}
          disabled={running}
          className="ml-auto flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-accent disabled:opacity-50"
        >
          {running ? <Loader2 size={10} className="animate-spin" /> : <Play size={10} />}
          실행
        </button>
        <button
          type="button"
          onClick={() => setEditing((e) => !e)}
          className="flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-accent"
        >
          <Pencil size={10} />
          편집
        </button>
        <button
          type="button"
          onClick={() => navigator.clipboard?.writeText(draft)}
          className="flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-accent"
        >
          <Copy size={10} />
          복사
        </button>
      </div>
      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') run(draft);
          }}
          rows={Math.min(12, draft.split('\n').length + 2)}
          className="w-full rounded bg-background/80 border border-border p-2 font-mono text-[11px] resize-y"
          placeholder="SQL 수정 후 ⌘/Ctrl+Enter 로 실행"
        />
      ) : (
        <pre className="font-mono text-[11px] whitespace-pre-wrap break-all max-h-72 overflow-auto rounded bg-background/60 p-2">
          {draft}
        </pre>
      )}
      {result && (
        <div className="mt-2">
          {result.ok === false ? (
            // Lambda 검증/실행 에러를 그대로 노출 — 분석가가 보고 수정(에디터 UX 핵심)
            <div className="rounded border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive whitespace-pre-wrap">
              {result.error || '실행 실패'}
            </div>
          ) : (
            <div>
              <div className="mb-1 text-muted-foreground">
                재실행 결과 {result.row_count != null && `(${result.row_count}행)`}
              </div>
              {result.rows && result.rows.length > 0 ? (
                <div className="max-h-64 overflow-auto">
                  <ChartRenderer
                    spec={{ kind: 'table', data: result.rows, encoding: { x: '', y: '' } }}
                  />
                </div>
              ) : (
                <div className="text-[11px] text-muted-foreground">0행</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ToolCallBlock({ toolCall, sessionId }: Props) {
  const [open, setOpen] = useState(false);
  const meta = TOOL_META[toolCall.tool] || { label: toolCall.tool, icon: <Wand2 size={14} /> };

  return (
    <div className="glass rounded-apple-md overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs hover:bg-accent/50 transition-colors"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="text-muted-foreground">{meta.icon}</span>
        <span className="font-medium">{meta.label}</span>
        {toolCall.status === 'running' && (
          <span className="ml-auto text-muted-foreground animate-pulse">실행 중...</span>
        )}
        {toolCall.status === 'failed' && (
          <span className="ml-auto text-destructive">실패</span>
        )}
      </button>

      {open && (
        <div className="border-t border-border bg-muted/30 px-3 py-2 text-xs space-y-2">
          {toolCall.args && (
            <div>
              <div className="text-muted-foreground mb-1">Args</div>
              <pre className="font-mono text-[11px] whitespace-pre-wrap break-all">
                {JSON.stringify(toolCall.args, null, 2)}
              </pre>
            </div>
          )}
          {/* SQL Specialist 가 생성한 SQL — "이 숫자는 이 쿼리로 나왔다" 투명성.
              세션이 있으면 인라인 에디터(실행/편집/복사 — LLM 미경유 재실행, §57),
              없으면(히스토리 복원 직후 등) 읽기 전용 코드 블록. */}
          {toolCall.result && typeof (toolCall.result as { sql?: unknown }).sql === 'string' && (
            sessionId ? (
              <SqlEditor
                sql={String((toolCall.result as { sql?: string }).sql)}
                sessionId={sessionId}
              />
            ) : (
              <div>
                <div className="text-muted-foreground mb-1">생성된 SQL</div>
                <pre className="font-mono text-[11px] whitespace-pre-wrap break-all max-h-72 overflow-auto rounded bg-background/60 p-2">
                  {String((toolCall.result as { sql?: string }).sql)}
                </pre>
              </div>
            )
          )}
          {/* Code Specialist 가 실행한 Python 을 별도 코드 블록으로 — "이 숫자는
              이 코드로 나왔다" 투명성. result.code 가 있으면 우선 노출. */}
          {toolCall.result && typeof (toolCall.result as { code?: unknown }).code === 'string' && (
            <div>
              <div className="text-muted-foreground mb-1">실행된 Python</div>
              <pre className="font-mono text-[11px] whitespace-pre-wrap break-all max-h-72 overflow-auto rounded bg-background/60 p-2">
                {String((toolCall.result as { code?: string }).code)}
              </pre>
            </div>
          )}
          {toolCall.result && (
            <div>
              <div className="text-muted-foreground mb-1">Result</div>
              <pre className="font-mono text-[11px] whitespace-pre-wrap break-all max-h-64 overflow-auto">
                {JSON.stringify(toolCall.result, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
