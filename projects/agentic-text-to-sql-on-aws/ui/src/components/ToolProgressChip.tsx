// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * TOOL_CALL_* 이벤트의 status 를 한국어 상태 칩으로 렌더링.
 * 도구명은 orchestrator 확정 계약에 따라 정확히 `search_schema` / `run_sql` 두 가지.
 */

type ToolStatus = 'inProgress' | 'executing' | 'complete';

/** 확정 도구명 → (진행중, 완료) 라벨. 정확 매칭. */
const TOOL_LABELS: Record<string, { running: string; done: string }> = {
  search_schema: { running: '스키마 검색 중…', done: '스키마 검색 완료' },
  run_sql: { running: 'SQL 실행 중…', done: 'SQL 실행 완료' },
};

function labelFor(name: string, done: boolean): string {
  const entry = TOOL_LABELS[name];
  if (entry) return done ? entry.done : entry.running;
  // 미등록 도구명은 이름 그대로 노출 (관측 가능성 우선).
  return done ? `${name} 완료` : `${name} 실행 중…`;
}

export function ToolProgressChip({ name, status }: { name: string; status: ToolStatus }) {
  const done = status === 'complete';
  return (
    <div className={`t2s-tool-chip${done ? ' done' : ''}`}>
      {done ? (
        <span className="t2s-check" aria-hidden>
          ✓
        </span>
      ) : (
        <span className="t2s-spinner" aria-hidden />
      )}
      <span>{labelFor(name, done)}</span>
    </div>
  );
}
