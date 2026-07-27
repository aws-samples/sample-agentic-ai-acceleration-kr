// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/eval/evaluators — ListEvaluators (builtin + custom).
 *
 * 배치 평가 실행 시 선택 가능한 평가자 목록이다. EX(Execution Accuracy) custom evaluator 는
 * evaluation 스택이 만들며, 화면은 `EXECUTION_EVALUATOR_ID` 를 기본 선택으로 표시한다.
 */

import { ListEvaluatorsCommand } from '@aws-sdk/client-bedrock-agentcore-control';
import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreControlClient } from '@/lib/aws-clients';
import { EXECUTION_EVALUATOR_ID } from '@/lib/env';
import { mapEvaluator, upstreamError } from '@/lib/eval';
import type { EvaluatorSummaryItem } from '@/lib/types';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  return handle(async () => {
    await requireManager(request);

    const client = agentCoreControlClient();
    const evaluators: EvaluatorSummaryItem[] = [];
    try {
      let nextToken: string | undefined;
      do {
        const out = await client.send(new ListEvaluatorsCommand({ nextToken, maxResults: 50 }));
        for (const summary of out.evaluators ?? []) evaluators.push(mapEvaluator(summary));
        nextToken = out.nextToken;
      } while (nextToken);
    } catch (error) {
      return upstreamError(error, 'ListEvaluators');
    }

    return Response.json(
      {
        status: 'ok',
        execution_evaluator_id: EXECUTION_EVALUATOR_ID || null,
        evaluators,
      },
      { status: 200 }
    );
  });
}
