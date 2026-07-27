// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { ToolCallStatus } from '@copilotkit/core';
import type { ReactToolCallRenderer } from '@copilotkit/react-core/v2';
import { ToolProgressChip } from './ToolProgressChip';

/**
 * v2 도구 렌더러 (orchestrator 확정 계약 기준).
 * `name: "*"` 와일드카드로 모든 TOOL_CALL_* 이벤트를 가로채 진행 상태 칩 + (run_sql 의)
 * SQL 코드블록을 인라인 렌더한다.
 *
 * 확정 계약:
 *  - 도구명은 정확히 `search_schema` / `run_sql` (정확 매칭).
 *  - **TOOL_CALL_RESULT 는 방출되지 않는다** — 결과 표는 도구 결과가 아니라 synthesis 단계의
 *    TEXT_MESSAGE(markdown 표)로 흐른다. CopilotChat 이 streamdown(remark-gfm)으로 자동 렌더.
 *  - run_sql 의 args.sql(누적 TOOL_CALL_ARGS delta → CopilotKit 이 파싱)만 SQL 코드블록으로 표시.
 */

/** ToolCallStatus enum → 칩 status 문자열로 정규화. */
function toChipStatus(status: ToolCallStatus): 'inProgress' | 'executing' | 'complete' {
  if (status === ToolCallStatus.Complete) return 'complete';
  if (status === ToolCallStatus.Executing) return 'executing';
  return 'inProgress';
}

/** run_sql 도구 인자에서 SQL 문자열 추출. */
function extractSql(args: unknown): string | null {
  if (!args || typeof args !== 'object') return null;
  const a = args as Record<string, unknown>;
  const candidate = a.sql ?? a.query ?? a.statement ?? a.sql_query;
  return typeof candidate === 'string' && candidate.trim().length > 0 ? candidate : null;
}

const wildcardRenderer: ReactToolCallRenderer<Record<string, unknown>> = {
  name: '*',
  render: ({ name, status, args }) => {
    // run_sql 인 경우에만 SQL 코드블록 렌더 (args.sql).
    const sql = name === 'run_sql' ? extractSql(args) : null;

    return (
      <div>
        <ToolProgressChip name={name} status={toChipStatus(status)} />
        {sql ? (
          <div className="t2s-sql-block">
            <div className="t2s-sql-label">생성된 SQL</div>
            <pre>
              <code>{sql}</code>
            </pre>
          </div>
        ) : null}
      </div>
    );
  },
};

export const toolCallRenderers: ReactToolCallRenderer<any>[] = [wildcardRenderer];
