// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * GET /api/eval/runs/{id} — GetBatchEvaluation (결과·스코어, §9.6).
 *
 * 평가자별 평균 스코어(`evaluationResults.evaluatorSummaries`)와 insight 결과
 * (`executionSummaryResult`)를 화면이 그대로 보여줄 수 있게 전달한다. 업스트림 실패는 502.
 */

import { GetBatchEvaluationCommand } from '@aws-sdk/client-bedrock-agentcore';
import { handle } from '@/lib/api';
import { requireManager } from '@/lib/auth';
import { agentCoreDataClient } from '@/lib/aws-clients';
import { mapBatchEvaluation, upstreamError } from '@/lib/eval';

export const dynamic = 'force-dynamic';

export async function GET(request: Request, { params }: { params: { id: string } }) {
  return handle(async () => {
    await requireManager(request);

    try {
      const out = await agentCoreDataClient().send(
        new GetBatchEvaluationCommand({ batchEvaluationId: decodeURIComponent(params.id) })
      );
      return Response.json(
        {
          status: 'ok',
          run: mapBatchEvaluation(out),
          // 원본 구조도 함께 전달 — Preview 단계의 신규 필드를 화면이 필요 시 직접 읽는다.
          evaluation_results: out.evaluationResults ?? null,
          execution_summary_result: out.executionSummaryResult ?? null,
        },
        { status: 200 }
      );
    } catch (error) {
      return upstreamError(error, 'GetBatchEvaluation');
    }
  });
}
